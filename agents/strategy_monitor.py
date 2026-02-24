#!/usr/bin/env python3
"""
策略監控模組
============
每日 20:00 執行，根據「處置股策略」與「PEG」兩個策略：
1. 推播今日新增的策略選股（建議進場）
2. 若持倉中有對應標的已達出場條件，推播提醒
"""
import sys
import os
import datetime
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", ".env"))

from loguru import logger
import finlab
finlab.login(os.getenv("FINLAB_API_TOKEN", ""))

from finlab import data
from core.database import get_conn
from core.notifier import send_message


# ── 工具函式 ────────────────────────────────────────────────

def _get_my_positions() -> dict:
    """回傳 {symbol: qty} 目前有餘量的台股持倉"""
    rows = get_conn().execute("""
        SELECT symbol, SUM(remaining) AS qty
        FROM position_lots
        WHERE remaining > 0 AND market = 'TW'
        GROUP BY symbol
    """).fetchall()
    return {r["symbol"]: r["qty"] for r in rows}


def _get_name_map(extra_ids: list = None) -> dict:
    """從 watchlist 取股票名稱；若傳入 extra_ids 則補查 FinLab security_categories"""
    rows = get_conn().execute(
        "SELECT symbol, name FROM watchlist WHERE market='TW' AND name IS NOT NULL"
    ).fetchall()
    nm = {r["symbol"]: r["name"] for r in rows}

    # 補查 watchlist 沒有的 id（如 PEG 選出的非持倉股）
    if extra_ids:
        missing = [sid for sid in extra_ids if sid not in nm]
        if missing:
            try:
                info = data.get("security_categories")[["stock_id", "name"]].drop_duplicates("stock_id")
                fl_map = dict(zip(info["stock_id"], info["name"]))
                for sid in missing:
                    if sid in fl_map and fl_map[sid] != sid:
                        nm[sid] = fl_map[sid]
            except Exception:
                pass
    return nm


# ── 處置股策略監控 ──────────────────────────────────────────────

def run_disposal_stock_monitor() -> dict:
    """
    回傳:
      entries: [(stock_id, 處置開始, 處置結束, stock_name)]  今日新進入處置
      exits:   [(stock_id, 處置結束, stock_name)]            今日結束處置且我有持倉
    """
    today = datetime.date.today().strftime("%Y-%m-%d")

    disposal = data.get('disposal_information').sort_index()
    disposal = disposal[~disposal["分時交易"].isna()].dropna(how='all')
    disposal = disposal.reset_index()[["stock_id", "date", "處置結束時間"]]
    disposal.columns = ["stock_id", "處置開始時間", "處置結束時間"]

    # 只看4碼普通股
    disposal = disposal[disposal["stock_id"].str.len() == 4]

    my_positions = _get_my_positions()
    disposal_ids = disposal["stock_id"].tolist()
    name_map = _get_name_map(extra_ids=disposal_ids)

    def get_name(sid):
        return name_map.get(sid, sid)

    entries, exits = [], []

    for _, row in disposal.iterrows():
        sid   = row["stock_id"]
        start = str(row["處置開始時間"])[:10]
        end   = str(row["處置結束時間"])[:10]

        # 今日新進入處置（進場訊號）
        if start == today:
            entries.append((sid, start, end, get_name(sid)))

        # 今日結束處置 且 我有持倉（出場提醒）
        if end == today and sid in my_positions:
            exits.append((sid, end, get_name(sid)))

    return {"entries": entries, "exits": exits}


# ── PEG 監控 ────────────────────────────────────────────────

def run_peg_monitor() -> dict:
    """
    跑 PEG 策略，取當前入選的前10檔
    回傳:
      selected:    [(stock_id, peg_val, stock_name)]  目前選中
      exit_remind: [(stock_id, peg_val, stock_name)]  持倉中但已不在前10（排除處置期持倉）
    """
    pe           = data.get("price_earning_ratio:本益比")
    rev          = data.get("monthly_revenue:當月營收")
    op_growth    = data.get("fundamental_features:營業利益成長率")

    rev_ma3  = rev.average(3)
    rev_ma12 = rev.average(12)
    peg      = pe / op_growth

    cond1 = (rev_ma3 / rev_ma12 > 1.1).iloc[-1]
    cond2 = (rev / rev.shift(1) > 0.9).iloc[-1]
    peg_latest = peg.iloc[-1]

    # 對齊 columns 再做布林篩選
    common = peg_latest.index.intersection(cond1.index).intersection(cond2.index)
    mask = cond1.reindex(common) & cond2.reindex(common)
    valid = peg_latest.reindex(common)[mask].dropna()
    valid = valid[valid > 0].nsmallest(10)
    selected_ids = list(valid.index)

    my_positions = _get_my_positions()
    name_map = _get_name_map(extra_ids=selected_ids)

    def get_name(sid):
        return name_map.get(sid, sid)

    selected = [(sid, float(valid[sid]), get_name(sid)) for sid in selected_ids]

    # 取得目前仍在處置期的股票（這些是處置股策略/事件策略持倉，排除 PEG 出場提醒）
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    disposal_active: set = set()
    try:
        disposal_df = data.get('disposal_information').sort_index().reset_index()
        disposal_active = set(
            disposal_df[
                (disposal_df['date'].astype(str) <= today_str) &
                (disposal_df['處置結束時間'].astype(str) >= today_str)
            ]['stock_id'].astype(str).tolist()
        )
    except Exception:
        pass

    # 出場提醒：持倉中「有正PEG值」但已不在前10，且不在處置期的股票
    peg_latest_all = peg.iloc[-1]
    exit_remind = []
    for sid in my_positions:
        if sid in selected_ids:
            continue
        if sid in disposal_active:
            continue  # 處置期持倉不推播 PEG 出場提醒（策略進場理由不同）
        if sid not in peg_latest_all.index:
            continue
        peg_val = peg_latest_all[sid]
        if peg_val > 0:
            exit_remind.append((sid, float(peg_val), get_name(sid)))

    return {"selected": selected, "exit_remind": exit_remind}


# ── Regime 偵測 / Kelly 估算 ─────────────────────────────────

def _detect_regime(close) -> str:
    """從全市場收盤價偵測 Regime（使用已載入的 FinLab 快取，無額外下載）"""
    if len(close) < 61:
        return 'unknown'
    ret_60 = (close.iloc[-1] / close.iloc[-61] - 1).dropna()
    if len(ret_60) < 10:
        return 'unknown'
    median_r = float(ret_60.median())
    if median_r > 0.03:
        return 'bull'
    elif median_r < -0.03:
        return 'bear'
    return 'sideways'


def _score_to_kelly(score: int) -> str:
    """基於健診評分估算簡化 Half-Kelly 建議倉位比例（上限 12%）"""
    fraction = min(0.12, score / 9.0 * 0.12)
    return f"{fraction:.0%}" if fraction > 0 else "—"


# ── 持倉健診 ─────────────────────────────────────────────────

def run_position_health_check() -> dict:
    """
    對持倉台股個股進行多策略訊號交叉分析。
    回傳:
      add:    [(sid, name, score, signals)]  建議加碼（評分 ≥4）
      hold:   [(sid, name, score, signals)]  持有觀察（評分 2~3）
      reduce: [(sid, name, warns)]           考慮減碼（評分 <2 且均線偏空）
      skipped: True 表示無台股個股持倉，略過此節
    """
    rows = get_conn().execute('''
        SELECT p.symbol, SUM(p.remaining) AS qty, w.name
        FROM position_lots p
        LEFT JOIN watchlist w ON p.symbol=w.symbol AND p.market=w.market
        WHERE p.remaining > 0 AND p.market = 'TW'
        GROUP BY p.symbol
    ''').fetchall()

    tw_stocks = [r['symbol'] for r in rows if not r['symbol'].startswith('00')]
    name_map  = {r['symbol']: (r['name'] or r['symbol']) for r in rows}

    if not tw_stocks:
        return {'add': [], 'hold': [], 'reduce': [], 'skipped': True}

    # 載入 FinLab 資料（與 run_peg_monitor 共用快取，不重複下載）
    close  = data.get('price:收盤價')
    regime = _detect_regime(close)
    pe     = data.get('price_earning_ratio:本益比')
    op     = data.get('fundamental_features:營業利益成長率')
    rev    = data.get('monthly_revenue:當月營收')
    roe    = data.get('fundamental_features:ROE稅後')
    bi     = data.get('etl:broker_transactions:balance_index')
    bsr    = data.get('etl:broker_transactions:buy_sell_ratio')
    外資   = data.get('institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)')
    投信   = data.get('institutional_investors_trading_summary:投信買賣超股數')

    # PEG 前10（同 run_peg_monitor 邏輯）
    peg      = pe / op
    rev_ma3  = rev.average(3)
    rev_ma12 = rev.average(12)
    cond1    = (rev_ma3 / rev_ma12 > 1.1).iloc[-1]
    cond2    = (rev / rev.shift(1) > 0.9).iloc[-1]
    peg_latest = peg.iloc[-1]
    common   = peg_latest.index.intersection(cond1.index).intersection(cond2.index)
    mask     = cond1.reindex(common) & cond2.reindex(common)
    valid    = peg_latest.reindex(common)[mask].dropna()
    peg_top10 = set(valid[valid > 0].nsmallest(10).index.tolist())

    # 技術/籌碼訊號
    ma5       = close.average(5)
    ma20      = close.average(20)
    趨勢多    = (close > ma5) & (ma5 > ma20)
    chip_cond = (bi > 0.52).iloc[-1] & (bsr > 1.05).iloc[-1] \
                & ((bi > bi.shift(1)).rolling(3).sum() >= 2).iloc[-1] \
                & 趨勢多.iloc[-1]
    ma_bull   = 趨勢多.iloc[-1]
    new_high  = (close >= close.rolling(60).max().shift(1)).iloc[-1]
    外資_buy  = (外資.rolling(5).sum() > 0).iloc[-1]
    投信_buy  = (投信.rolling(5).sum() > 0).iloc[-1]
    roe_latest = roe.iloc[-1]

    results = []
    for sid in tw_stocks:
        if sid not in close.columns:
            continue

        score, signals, warns = 0, [], []

        if sid in peg_top10:
            signals.append('PEG⭐'); score += 3

        if sid in chip_cond.index and chip_cond.get(sid, False):
            signals.append('籌碼✅'); score += 2

        is_bull = sid in ma_bull.index and bool(ma_bull.get(sid, False))
        if is_bull:
            signals.append('MA多📈'); score += 1

        if sid in new_high.index and bool(new_high.get(sid, False)):
            signals.append('新高🔝'); score += 1

        f_ok = sid in 外資_buy.index and bool(外資_buy.get(sid, False))
        t_ok = sid in 投信_buy.index and bool(投信_buy.get(sid, False))
        if f_ok and t_ok:
            signals.append('法人💰'); score += 2
        elif f_ok:
            signals.append('外資買'); score += 1
        elif t_ok:
            signals.append('投信買'); score += 1

        if not is_bull:
            warns.append('均線空頭⚠️')
        if sid in roe_latest.index:
            rv = roe_latest[sid]
            if not pd.isna(rv) and float(rv) < 5:
                warns.append(f'ROE低({float(rv):.1f}%)')

        results.append({
            'sid': sid, 'name': name_map.get(sid, sid),
            'score': score, 'signals': signals, 'warns': warns,
            'is_bull': is_bull, 'kelly': _score_to_kelly(score),
        })

    results.sort(key=lambda x: x['score'], reverse=True)
    return {
        'add':    [r for r in results if r['score'] >= 4],
        # 2~3 分，或分數不足但均線仍多頭（不達減碼標準）
        'hold':   [r for r in results if 2 <= r['score'] < 4 or (r['score'] < 2 and r['is_bull'])],
        'reduce': [r for r in results if r['score'] < 2 and not r['is_bull']],
        'skipped': False, 'regime': regime,
    }


# ── 事件警示監控（減資重啟 / 庫藏股買回 / 注意股票）────────────

def run_event_alert_monitor() -> dict:
    """
    掃描今日新發生的事件（減資重啟、庫藏股買回期開始、注意股票新增）
    回傳:
      capital_reduction: [(stock_id, name)]  今日恢復買賣（減資重啟）
      treasury_new:      [(stock_id, name)]  今日開始庫藏股買回
      attention_new:     [(stock_id, name)]  今日新增注意股票（若資料可用）
    """
    today = datetime.date.today().strftime("%Y-%m-%d")

    # 第一輪：先收集所有事件股票 ID，之後一次查名稱
    cap_ids: list = []
    trs_ids: list = []
    att_ids: list = []

    # 減資後恢復買賣
    try:
        df_tse = data.get('capital_reduction_tse').sort_index().reset_index()
        df_otc = data.get('capital_reduction_otc').sort_index().reset_index()
        seen: set = set()
        for df in (df_tse, df_otc):
            if '恢復買賣日期' not in df.columns:
                continue
            for _, row in df.iterrows():
                sid = str(row.get('stock_id', ''))
                if len(sid) != 4 or sid in seen:
                    continue
                if str(row['恢復買賣日期'])[:10] == today:
                    cap_ids.append(sid)
                    seen.add(sid)
    except Exception as e:
        logger.debug(f"減資資料不可用：{e}")

    # 庫藏股買回期開始
    try:
        df = data.get('treasury_stock').sort_index().reset_index()
        if '預定買回期間-起' in df.columns:
            seen_t: set = set()
            for _, row in df.iterrows():
                sid = str(row.get('stock_id', ''))
                if len(sid) != 4 or sid in seen_t:
                    continue
                if str(row['預定買回期間-起'])[:10] == today:
                    trs_ids.append(sid)
                    seen_t.add(sid)
    except Exception as e:
        logger.debug(f"庫藏股資料不可用：{e}")

    # 注意股票新增（可選，資料不存在則略過）
    try:
        attn = data.get('trading_attention').sort_index().reset_index()
        date_col = next((c for c in ['date', 'Date'] if c in attn.columns), None)
        if date_col:
            seen_a: set = set()
            for _, row in attn.iterrows():
                sid = str(row.get('stock_id', ''))
                if len(sid) != 4 or sid in seen_a:
                    continue
                if str(row[date_col])[:10] == today:
                    att_ids.append(sid)
                    seen_a.add(sid)
    except Exception:
        pass  # 注意股票資料為可選

    # 第二輪：一次查名稱（含非觀察清單股票，從 FinLab security_categories 補查）
    all_ids = cap_ids + trs_ids + att_ids
    name_map = _get_name_map(extra_ids=all_ids)

    def get_name(sid):
        return name_map.get(str(sid), str(sid))

    return {
        'capital_reduction': [(sid, get_name(sid)) for sid in cap_ids],
        'treasury_new':      [(sid, get_name(sid)) for sid in trs_ids],
        'attention_new':     [(sid, get_name(sid)) for sid in att_ids],
    }


# ── Telegram 推播 ────────────────────────────────────────────

def notify_strategy_report(rabbit: dict, peg: dict, health: dict = None,
                            events: dict = None):
    lines = ["📊 <b>每日策略監控報告</b>", f"日期：{datetime.date.today()}"]

    # ── 事件警示（減資重啟 / 庫藏股 / 注意股票）──
    if events:
        has_event = any(events.get(k) for k in ('capital_reduction', 'treasury_new', 'attention_new'))
        if has_event:
            lines.append("\n📅 <b>今日事件警示</b>")
            if events.get('capital_reduction'):
                lines.append("✂️ 減資後恢復買賣（今日重啟）：")
                for sid, name in events['capital_reduction']:
                    lines.append(f"  • {sid} {name}")
            if events.get('treasury_new'):
                lines.append("🏦 庫藏股買回期開始（今日）：")
                for sid, name in events['treasury_new']:
                    lines.append(f"  • {sid} {name}")
            if events.get('attention_new'):
                lines.append("⚠️ 今日新增注意股票：")
                for sid, name in events['attention_new']:
                    lines.append(f"  • {sid} {name}")

    # ── 處置股策略 ──
    lines.append("\n⚠️ <b>處置股策略</b>")
    if rabbit["entries"]:
        lines.append("▶ 今日新進入處置（可考慮進場）：")
        for sid, start, end, name in rabbit["entries"]:
            lines.append(f"  • {sid} {name}　處置至 {end}")
    else:
        lines.append("  今日無新增處置股")

    if rabbit["exits"]:
        lines.append("⚠️ 處置期結束（持倉提醒）：")
        for sid, end, name in rabbit["exits"]:
            lines.append(f"  • {sid} {name}　今日處置結束，可考慮出場")

    # ── PEG ──
    lines.append("\n⭐ <b>PEG 策略（月頻）</b>")
    if peg["selected"]:
        lines.append("▶ 當前選股（低PEG前10）：")
        for sid, peg_val, name in peg["selected"]:
            lines.append(f"  • {sid} {name}　PEG={peg_val:.2f}")
    else:
        lines.append("  本期無符合條件標的")

    if peg["exit_remind"]:
        lines.append("⚠️ 持倉已不在PEG前10（可考慮出場）：")
        for sid, peg_val, name in peg["exit_remind"]:
            lines.append(f"  • {sid} {name}　PEG={peg_val:.2f}（已落榜）")

    # ── 持倉健診 ──
    if health and not health.get('skipped'):
        _regime       = health.get('regime', 'unknown')
        _regime_emoji = {'bull': '🟢', 'bear': '🔴', 'sideways': '🟡', 'unknown': '⚪'}.get(_regime, '⚪')
        _regime_label = {'bull': '多頭', 'bear': '空頭', 'sideways': '盤整', 'unknown': '不明'}.get(_regime, '不明')
        lines.append(f"\n💼 <b>持倉健診</b>  {_regime_emoji} 市場：{_regime_label}")
        if health['add']:
            lines.append("🟢 建議加碼（多訊號共振）：")
            for r in health['add']:
                kelly_str = f"  Kelly≈{r['kelly']}" if r.get('kelly', '—') != '—' else ""
                warn_str  = f"  ⚠️ {'  '.join(r['warns'])}" if r['warns'] else ""
                lines.append(f"  • {r['sid']} {r['name']}　{'  '.join(r['signals'])}  ({r['score']}分){kelly_str}{warn_str}")
        if health['hold']:
            lines.append("🟡 持有觀察：")
            for r in health['hold']:
                sig_str   = ('  '.join(r['signals']) + '  ') if r['signals'] else ''
                kelly_str = f"  Kelly≈{r['kelly']}" if r.get('kelly', '—') != '—' else ""
                warn_str  = f"⚠️ {'  '.join(r['warns'])}" if r['warns'] else ""
                lines.append(f"  • {r['sid']} {r['name']}　{sig_str}{warn_str}({r['score']}分){kelly_str}")
        if health['reduce']:
            lines.append("🔴 考慮減碼：")
            for r in health['reduce']:
                lines.append(f"  • {r['sid']} {r['name']}　{'  '.join(r['warns'])}")
        if not any([health['add'], health['hold'], health['reduce']]):
            lines.append("  持倉訊號中性，無明確建議")

    msg = "\n".join(lines)
    send_message(msg)
    h_add    = len(health['add'])    if health and not health.get('skipped') else 0
    h_reduce = len(health['reduce']) if health and not health.get('skipped') else 0
    logger.info(
        f"策略監控推播完成：處置股策略進場{len(rabbit['entries'])}檔 / "
        f"PEG選股{len(peg['selected'])}檔 / 健診加碼{h_add}檔 減碼{h_reduce}檔"
    )


# ── 主入口 ───────────────────────────────────────────────────

def run():
    logger.info("=== 策略監控啟動 ===")
    try:
        rabbit = run_disposal_stock_monitor()
        logger.info(f"處置股策略：進場{len(rabbit['entries'])}檔，出場提醒{len(rabbit['exits'])}檔")
    except Exception as e:
        logger.error(f"處置股策略監控失敗：{e}")
        rabbit = {"entries": [], "exits": []}

    try:
        peg = run_peg_monitor()
        logger.info(f"PEG：選股{len(peg['selected'])}檔，出場提醒{len(peg['exit_remind'])}檔")
    except Exception as e:
        logger.error(f"PEG 監控失敗：{e}")
        peg = {"selected": [], "exit_remind": []}

    try:
        health = run_position_health_check()
        if not health['skipped']:
            logger.info(f"持倉健診：加碼{len(health['add'])}檔，觀察{len(health['hold'])}檔，減碼{len(health['reduce'])}檔")
        else:
            logger.info("持倉健診：無台股個股持倉，略過")
    except Exception as e:
        logger.error(f"持倉健診失敗：{e}")
        health = {"add": [], "hold": [], "reduce": [], "skipped": True}

    try:
        events = run_event_alert_monitor()
        cap_n = len(events.get('capital_reduction', []))
        trs_n = len(events.get('treasury_new', []))
        att_n = len(events.get('attention_new', []))
        logger.info(f"事件警示：減資重啟{cap_n}檔 / 庫藏股{trs_n}檔 / 注意股{att_n}檔")
    except Exception as e:
        logger.error(f"事件警示失敗：{e}")
        events = {}

    notify_strategy_report(rabbit, peg, health, events)


if __name__ == "__main__":
    run()
