#!/usr/bin/env python3
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from loguru import logger

from config.settings import ANTHROPIC_API_KEY, BOT_CLAUDE_MODEL
from core.database import (
    get_watchlist, get_recent_prices,
    get_positions,
    get_all_discovered_strategies,
)

_client    = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
MODEL      = BOT_CLAUDE_MODEL
MAX_TOKENS = 1024

TOOLS = [
    {
        "name": "get_platform_summary",
        "description": (
            "取得量化平台整體摘要：追蹤股票總數、台股/美股分佈。"
            "當使用者問整體狀況、有幾檔股票時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_watchlist",
        "description": (
            "取得觀察清單中的股票列表。"
            "可指定市場篩選（TW=台股、US=美股、不傳=全部）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "enum": ["TW", "US"],
                    "description": "市場代碼：TW=台股，US=美股。不填則回傳全部。",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_positions",
        "description": (
            "查詢目前持倉部位彙總（FIFO計算後的剩餘張數/股數與加權平均成本）。"
            "當使用者問我的部位、持有哪些股票、現在有多少張時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "enum": ["TW", "US"],
                    "description": "市場篩選，不填則回傳全部",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_stock_price",
        "description": (
            "取得特定股票的最新收盤價與趨勢判斷（多頭/空頭/盤整）、"
            "距60日高位置、量能狀態。"
            "當使用者問某一檔股票的價格、現在能不能買、走勢如何時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "股票代碼，例如 '2330' 或 'AAPL'",
                },
                "market": {
                    "type": "string",
                    "enum": ["TW", "US"],
                    "description": "市場：TW=台股，US=美股",
                },
                "days": {
                    "type": "integer",
                    "description": "查詢天數，預設20",
                    "default": 20,
                },
            },
            "required": ["symbol", "market"],
        },
    },
    {
        "name": "get_position_risk",
        "description": (
            "分析目前持倉的風險指標：集中度、市場分布（台股/美股比例）、"
            "最大單一部位佔比。當使用者問持倉風險、部位集中度、資金配置時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_strategy_recommendation",
        "description": (
            "根據策略庫中已驗證的策略，依指定目標（高報酬/低風險/高勝率）"
            "推薦前3名策略及其績效指標。"
            "當使用者問哪個策略最好、推薦策略、策略績效時使用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "enum": ["cagr", "mdd", "win_ratio", "sharpe"],
                    "description": "排序目標：cagr=高報酬, mdd=低最大回撤, win_ratio=高勝率, sharpe=高夏普",
                }
            },
            "required": [],
        },
    },
]

SYSTEM_PROMPT = """你是一個量化交易平台的智能助理，協助使用者查詢股票觀察清單、價格資料，以及分析持倉部位。

可用工具：
- get_platform_summary：平台整體摘要
- get_watchlist：觀察清單
- get_stock_price：個股價格與技術指標
- get_positions：查詢持倉部位（FIFO計算後的剩餘量與加權均價）
- get_position_risk：持倉風險分析（集中度、台美比例）
- get_strategy_recommendation：推薦策略（依高報酬/低風險/高勝率排序）

回答規則：
1. 永遠使用繁體中文回答
2. 回答要簡潔，避免不必要的廢話
3. 台股持倉單位為「張」，美股為「股」
4. 如果問題與量化平台無關，禮貌說明你的職責範圍
5. 不捏造資料，只根據工具回傳的資料回答
6. 買賣操作請使用 Bot 指令 /pos buy 與 /pos sell，AI 不執行交易"""


def _execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name == "get_platform_summary":
            wl = get_watchlist()
            lines = [
                "量化平台摘要：",
                f"  追蹤清單：{len(wl)} 檔（台股 {sum(1 for i in wl if i['market']=='TW')} / 美股 {sum(1 for i in wl if i['market']=='US')}）",
            ]
            return "\n".join(lines)

        elif name == "get_watchlist":
            market = tool_input.get("market")
            items  = get_watchlist(market=market)
            if not items:
                return "觀察清單為空"
            lines = [f"共 {len(items)} 檔："]
            for i in items:
                flag = "🇹🇼" if i["market"] == "TW" else "🇺🇸"
                lines.append(f"  {flag} [{i['market']}] {i['symbol']}")
            return "\n".join(lines)

        elif name == "get_stock_price":
            symbol = tool_input["symbol"].upper()
            market = tool_input["market"].upper()
            days   = tool_input.get("days", 20)
            rows   = get_recent_prices(symbol, market, days=max(days, 30))
            if not rows:
                return f"找不到 {symbol}（{market}）的資料，請確認代碼正確且已加入觀察清單"
            latest = rows[-1]
            close  = latest.get('close_price')
            ma5    = latest.get('ma5')
            ma20   = latest.get('ma20')
            vol    = latest.get('volume')
            vol_ma = latest.get('vol_ma20')
            n_high = latest.get('n_day_high')

            # 趨勢判斷
            if ma5 and ma20:
                if ma5 > ma20 and close and close > ma5:
                    trend = "多頭 📈"
                elif ma5 < ma20 and close and close < ma5:
                    trend = "空頭 📉"
                else:
                    trend = "盤整 ➡️"
            else:
                trend = "資料不足"

            # 距60日高
            dist_str = ""
            if n_high and close and n_high > 0:
                dist = (close - n_high) / n_high * 100
                dist_str = "創60日新高 🔝" if dist >= 0 else f"距60日高 {dist:.1f}%"

            # 量能
            vol_str = ""
            if vol and vol_ma and vol_ma > 0:
                ratio = vol / vol_ma
                if ratio >= 1.5:
                    vol_str = f"放量 ({ratio:.1f}x) 🔊"
                elif ratio <= 0.5:
                    vol_str = f"縮量 ({ratio:.1f}x) 🔇"
                else:
                    vol_str = f"量能正常 ({ratio:.1f}x)"

            lines = [
                f"{symbol}（{market}）{latest.get('trade_date', '')}",
                f"  收盤：{close}",
                f"  趨勢：{trend}",
            ]
            if dist_str:
                lines.append(f"  位置：{dist_str}")
            if vol_str:
                lines.append(f"  量能：{vol_str}")
            return "\n".join(lines)

        elif name == "get_positions":
            market = tool_input.get("market")
            positions = get_positions(market=market)
            if not positions:
                return "目前無持倉記錄"
            lines = ["目前持倉："]
            for p in positions:
                flag = "🇹🇼" if p["market"] == "TW" else "🇺🇸"
                unit = "股"
                lines.append(f"  {flag} {p['symbol']} | {p['quantity']:.2f}{unit} | 均價 {p['avg_cost']:.2f}")
            return "\n".join(lines)

        elif name == "get_position_risk":
            positions = get_positions()
            if not positions:
                return "目前無持倉記錄"
            total_val = sum(p['quantity'] * p['avg_cost'] for p in positions)
            if total_val <= 0:
                return "持倉成本資料不足，無法計算風險"
            lines = [f"持倉風險分析（總成本基礎 {total_val:,.0f}）："]
            tw_val = sum(p['quantity'] * p['avg_cost'] for p in positions if p['market'] == 'TW')
            us_val = sum(p['quantity'] * p['avg_cost'] for p in positions if p['market'] == 'US')
            lines.append(f"  台股比例：{tw_val/total_val:.1%}　美股比例：{us_val/total_val:.1%}")
            lines.append(f"  持倉總數：{len(positions)} 檔")
            sorted_pos = sorted(positions, key=lambda x: x['quantity'] * x['avg_cost'], reverse=True)
            for p in sorted_pos:
                val = p['quantity'] * p['avg_cost']
                pct = val / total_val
                flag = "🇹🇼" if p['market'] == 'TW' else "🇺🇸"
                warn = "⚠️" if pct > 0.3 else ("⚡" if pct > 0.2 else "✓")
                lines.append(f"  {flag} {p['symbol']}：{pct:.1%} {warn}（均價 {p['avg_cost']:.2f}）")
            max_p = sorted_pos[0]
            max_pct = max_p['quantity'] * max_p['avg_cost'] / total_val
            risk_level = "⚠️ 集中度高" if max_pct > 0.3 else ("尚可" if max_pct > 0.2 else "✓ 分散良好")
            lines.append(f"  最大單一部位：{max_p['symbol']} {max_pct:.1%}　{risk_level}")
            return "\n".join(lines)

        elif name == "get_strategy_recommendation":
            goal   = tool_input.get("goal", "sharpe")
            strats = get_all_discovered_strategies()
            passed = [s for s in strats if s.get('passed')]
            if not passed:
                return "策略庫尚無通過的策略"
            sort_map = {
                'cagr':      (lambda s: s.get('cagr', 0),      '高年化報酬'),
                'mdd':       (lambda s: -abs(s.get('mdd', -1)), '低最大回撤'),
                'win_ratio': (lambda s: s.get('win_ratio', 0),  '高勝率'),
                'sharpe':    (lambda s: s.get('sharpe', 0),     '高夏普比'),
            }
            sort_fn, goal_label = sort_map.get(goal, sort_map['sharpe'])
            top3 = sorted(passed, key=sort_fn, reverse=True)[:3]
            lines = [f"策略推薦（依{goal_label}）："]
            for i, s in enumerate(top3, 1):
                lines.append(
                    f"  {i}. {s['name'][:25]}\n"
                    f"     CAGR={s['cagr']:.1%}  Sharpe={s['sharpe']:.2f}  "
                    f"MDD={s['mdd']:.1%}  勝率={s['win_ratio']:.1%}"
                )
            return "\n".join(lines)

        else:
            return f"未知工具：{name}"

    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return f"查詢失敗：{e}"


async def handle_claude_message(user_text: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY 未設定，無法使用 AI 功能。"

    messages = [{"role": "user", "content": user_text}]

    for _ in range(5):
        response = await _client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_blocks) if text_blocks else "（無回應）"

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tu in tool_uses:
            result_str = _execute_tool(tu.name, tu.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    return "查詢逾時，請稍後再試。"
