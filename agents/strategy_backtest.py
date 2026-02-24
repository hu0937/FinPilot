#!/usr/bin/env python3
"""
Strategy Agent + Backtest Agent（使用 Claude Max 訂閱，不消耗 API key）
========================================================================
用法（在 VS Code 終端直接執行）：
    python agents/strategy_backtest.py

需要：
    - config/.env 中設定 FINLAB_API_TOKEN
    - 已登入 Claude Code（Max 訂閱）
"""

import sys
import os
import subprocess
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

FINLAB_TOKEN = os.getenv("FINLAB_API_TOKEN", "")

# ── 找 claude 執行檔 ──────────────────────────────────────────

_CLAUDE_CANDIDATES = [
    "claude",  # 若已在 PATH

]

def _find_claude() -> str:
    for c in _CLAUDE_CANDIDATES:
        if shutil.which(c) or os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "找不到 claude 執行檔。\n"
        "請確認已安裝 Claude Code VS Code 擴充功能並登入 Max 帳號。"
    )

CLAUDE_BIN = _find_claude()

# ── System Prompts ────────────────────────────────────────────

STRATEGY_PROMPT_TEMPLATE = """\
你是一個 FinLab 台股量化策略生成器。根據以下選股條件，產生完整可執行的 Python 程式碼。

## 規則
1. 只輸出 Python 程式碼，不要任何說明文字，不要 markdown ``` 包裝
2. 必須包含 from finlab import data 和 from finlab.backtest import sim
3. 結尾必須是以下格式（使用標準散戶費率，不可修改 fee_ratio 和 tax_ratio）：
   report = sim(position, resample="M", fee_ratio=1.425/1000, tax_ratio=3/1000, position_limit=0.1, trade_at_price="open", upload=False)
4. 不要在最後一行單獨寫 report（會觸發 HTML 輸出）
5. 避免 lookahead bias：季報/月報 FinLab 已自動對齊
6. 不要使用 hold_until（該版本有 numpy read-only bug）

## 常用資料路徑
close    = data.get("price:收盤價")
volume   = data.get("price:成交股數")
momentum = close.pct_change(60)       # 60日動能
sma20    = close.average(20)
sma60    = close.average(60)
vol_ma20 = volume.average(20)

## 常用操作
cond = (volume > vol_ma20 * 1.5).sustain(3)      # 連續3日量能放大
position = momentum[cond].is_largest(20)          # 取最強20檔
position = pe[cond].is_smallest(10)               # 取最低PE10檔

## 選股條件
{criteria}
"""

FIX_PROMPT_TEMPLATE = """\
以下 FinLab 策略程式碼執行失敗，請輸出修正後的完整程式碼（只有程式碼，無說明，無 markdown ``` 包裝）。

程式碼：
{code}

錯誤訊息：
{error}
"""

# ── Agents ───────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    """呼叫 claude --print，使用 Max 訂閱，不消耗 API token"""
    result = subprocess.run(
        [CLAUDE_BIN, "--print", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude 呼叫失敗：{result.stderr.strip()}")
    return result.stdout.strip()


def _strip_code(text: str) -> str:
    """移除 Claude 有時加上的 markdown code fence"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return text.strip()


def strategy_agent(criteria: str) -> str:
    """Strategy Agent：呼叫 claude --print 生成 FinLab 策略程式碼"""
    print("\n🤖 [Strategy Agent] 生成策略中...")
    prompt = STRATEGY_PROMPT_TEMPLATE.format(criteria=criteria)
    raw = _call_claude(prompt)
    return _strip_code(raw)


def fix_agent(code: str, error: str) -> str:
    """Fix Agent：讓 claude --print 修正錯誤程式碼"""
    print("🔧 [Fix Agent] 修正程式碼中...")
    prompt = FIX_PROMPT_TEMPLATE.format(code=code, error=error)
    raw = _call_claude(prompt)
    return _strip_code(raw)


def backtest_agent(code: str):
    """Backtest Agent：exec 策略程式碼，回傳 (stats_dict, report)"""
    namespace: dict = {"__builtins__": __builtins__}
    exec(compile(code, "<strategy>", "exec"), namespace)  # noqa: S102
    report = namespace.get("report")
    if report is None:
        raise RuntimeError("程式碼未產生 `report`，請確認最後一行有呼叫 sim()")
    return report.get_stats(), report


# ── Helpers ──────────────────────────────────────────────────

def _print_stats(stats: dict):
    print("\n" + "=" * 52)
    print("📊  回測績效")
    print("=" * 52)
    rows = [
        ("年化報酬率 (CAGR)",   "cagr",           "{:.2%}"),
        ("夏普比率（月）",      "monthly_sharpe",  "{:.2f}"),
        ("最大回撤",            "max_drawdown",    "{:.2%}"),
        ("勝率",                "win_ratio",       "{:.2%}"),
        ("Beta",               "beta",            "{:.2f}"),
        ("Alpha（年化）",      "alpha",           "{:.2%}"),
    ]
    for label, key, fmt in rows:
        val = stats.get(key)
        if val is not None:
            try:
                print(f"  {label:<22} {fmt.format(val)}")
            except (ValueError, TypeError):
                print(f"  {label:<22} {val}")
    print("=" * 52)


# ── Main Loop ─────────────────────────────────────────────────

def main():
    if not FINLAB_TOKEN:
        print("❌ 請在 config/.env 設定 FINLAB_API_TOKEN")
        print("   取得：https://www.finlab.finance/payment")
        return

    import finlab
    finlab.login(FINLAB_TOKEN)

    print(f"\n🚀 Strategy + Backtest Agent（claude: {CLAUDE_BIN}）")
    print("輸入選股條件，Agent 自動生成策略並回測。輸入 q 離開\n")
    print("範例：")
    print("  月營收年增率連3個月 > 20%，取最強10檔")
    print("  只看股價與量能，60日動能 + 量能擴張，月頻再平衡\n")

    while True:
        try:
            criteria = input("條件> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n結束")
            break

        if criteria.lower() in ("q", "quit", "exit", ""):
            break

        # Strategy Agent
        code = strategy_agent(criteria)
        print("\n📝 生成策略程式碼：")
        print("─" * 52)
        print(code)
        print("─" * 52)

        # Backtest Agent（失敗最多 Fix 2 次）
        for attempt in range(3):
            try:
                label = "執行中" if attempt == 0 else f"重試 {attempt}"
                print(f"\n⚙️  [Backtest Agent] {label}...")
                stats, _ = backtest_agent(code)
                _print_stats(stats)
                break
            except Exception as exc:
                err = str(exc)
                print(f"❌ 執行失敗：{err}")
                if attempt < 2:
                    code = fix_agent(code, err)
                    print("\n修正後程式碼：")
                    print("─" * 52)
                    print(code)
                    print("─" * 52)
                else:
                    print("❌ 三次均失敗，請調整條件描述")

        print()


if __name__ == "__main__":
    main()
