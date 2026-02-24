#!/usr/bin/env python3
"""
position_scanner.py  ──  用策略掃描現有持倉，通知 Telegram
========================================================================
- 掃描所有通過的自動探索策略的當前選股名單
- 以 Sharpe 加權命中分數排名持倉
- 偵測市場 Regime（牛/熊/盤整），推薦對應策略排序
- 提供 Kelly 建議倉位比例
- 不跑 sim()，不額外消耗 FinLab token

用法：
    python agents/position_scanner.py
    python agents/position_scanner.py --top 5
"""

import os, sys, re, argparse
from datetime import date
from html import escape as _esc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))

import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from loguru import logger
from core.database import get_conn, get_watchlist, get_recent_prices
from core.notifier import send_message
from agents.strategy_explorer import (
    _get_data, _sample_strategy, _build_position,
    MIN_FLOOR, S_BENCHMARKS,
)

DEFAULT_TOP_N = 10

# ── Regime → 策略排序偏好 ────────────────────────────────────────────
REGIME_SORT = {
    'bull':     (lambda r: r['cagr'],                '牛市：優先動能/高CAGR'),
    'bear':     (lambda r: -abs(r.get('mdd', -0.5)), '熊市：優先低MDD'),
    'sideways': (lambda r: r.get('win_ratio', 0),    '盤整：優先高勝率'),
    'unknown':  (lambda r: r.get('score', 0),        '複合分數'),
}
REGIME_EMOJI = {'bull': '🟢', 'bear': '🔴', 'sideways': '🟡', 'unknown': '⚪'}


# ── 市場 Regime 偵測 ─────────────────────────────────────────────────
def detect_regime() -> str:
    """偵測台股市場 Regime（牛/熊/盤整）

    優先使用 FinLab 全市場收盤價資料（已快取），計算所有股票中位數 60 日漲幅，
    樣本量遠大於只看觀察清單，判斷更可靠。
    若 FinLab 資料不可用則 fallback 至 DB 觀察清單（去除 [:15] 限制）。
    """
    # ── 優先：FinLab 全市場資料 ──────────────────────────────────────
    try:
        d = _get_data()
        close = d['close']
        if len(close) >= 61:
            ret_60 = (close.iloc[-1] / close.iloc[-61] - 1).dropna()
            if len(ret_60) >= 30:   # 至少 30 支股票才有意義
                median_r = float(ret_60.median())
                logger.info(
                    f"[scanner] Regime 偵測（全市場 {len(ret_60)} 支）"
                    f" 60日中位漲幅={median_r:.2%}"
                )
                if median_r > 0.03:
                    return 'bull'
                elif median_r < -0.03:
                    return 'bear'
                return 'sideways'
    except Exception as e:
        logger.warning(f"[scanner] FinLab Regime 偵測失敗，切換至 DB fallback：{e}")

    # ── Fallback：DB 觀察清單（不限數量）───────────────────────────
    wl = get_watchlist(market='TW')
    if not wl:
        return 'unknown'

    returns = []
    for item in wl:   # 取全部，不再限制 [:15]
        rows = get_recent_prices(item['symbol'], 'TW', days=65)
        prices = [r['close_price'] for r in rows if r.get('close_price')]
        if len(prices) >= 60:
            returns.append((prices[-1] - prices[-60]) / prices[-60])

    if not returns:
        return 'unknown'

    median_r = sorted(returns)[len(returns) // 2]
    if median_r > 0.03:
        return 'bull'
    elif median_r < -0.03:
        return 'bear'
    return 'sideways'


# ── Kelly 建議倉位 ───────────────────────────────────────────────────
def _kelly_fraction(win_ratio: float, sharpe: float) -> float:
    """簡化 Half-Kelly，以 Sharpe 調整，上限 15%"""
    full_kelly = max(0.0, 2 * win_ratio - 1)
    half_kelly = full_kelly * 0.5
    return min(0.15, half_kelly * max(0.5, sharpe / 2.0))


# ── ETF 判斷 ────────────────────────────────────────────────────────
def _is_etf(symbol: str) -> bool:
    if symbol.startswith('00'):
        return True
    if any(c.isalpha() for c in symbol) and len(symbol) <= 4:
        return False
    if len(symbol) > 4 and symbol.isdigit():
        return True
    return False


# ── 複合分數 ─────────────────────────────────────────────────────────
def _composite_score(row: dict, all_passed: list) -> float:
    best_cagr   = max(S_BENCHMARKS['max_cagr'],   max((r['cagr']      for r in all_passed), default=0))
    best_sharpe = max(S_BENCHMARKS['max_sharpe'],  max((r['sharpe']    for r in all_passed), default=0))
    best_mdd    = max(S_BENCHMARKS['max_mdd'],     max((r['mdd']       for r in all_passed), default=-1))
    best_win    = max(S_BENCHMARKS['max_win'],     max((r['win_ratio'] for r in all_passed), default=0))

    def _n(val, floor, best):
        span = best - floor
        return max(0.0, (val - floor) / span) if span != 0 else 0.0

    return (
        1.0 * _n(row['cagr'],      MIN_FLOOR['cagr'],   best_cagr)   +
        1.0 * _n(row['sharpe'],    MIN_FLOOR['sharpe'], best_sharpe) +
        1.5 * _n(row['mdd'],       MIN_FLOOR['mdd'],    best_mdd)    +
        0.5 * _n(row['win_ratio'], 0.0,                 best_win)
    ) / 4.0


# ── 股名查詢 ─────────────────────────────────────────────────────────
def get_name_map() -> dict:
    rows = get_conn().execute(
        "SELECT symbol, name FROM watchlist WHERE market='TW' AND name IS NOT NULL"
    ).fetchall()
    return {r['symbol']: r['name'].strip() for r in rows if r['name']}


def _fmt(symbol: str, name_map: dict) -> str:
    name = name_map.get(symbol, '')
    return f"{symbol} {name}" if name else symbol


# ── DB 查詢 ──────────────────────────────────────────────────────────
def get_all_strategies(regime: str = 'unknown') -> list:
    """取所有通過的策略，依 Regime 偏好排序"""
    rows = get_conn().execute(
        "SELECT name, cagr, sharpe, mdd, win_ratio, hypothesis "
        "FROM discovered_strategies WHERE passed=1"
    ).fetchall()
    all_passed = [dict(r) for r in rows]
    scored = [{**r, 'score': _composite_score(r, all_passed)} for r in all_passed]
    sort_fn, _ = REGIME_SORT.get(regime, REGIME_SORT['unknown'])
    scored.sort(key=sort_fn, reverse=True)
    return scored


def get_holdings() -> list:
    rows = get_conn().execute(
        "SELECT DISTINCT symbol, market FROM position_lots WHERE remaining > 0"
    ).fetchall()
    return [dict(r) for r in rows]


# ── 策略重建 ─────────────────────────────────────────────────────────
def extract_seed(hypothesis: str):
    if not hypothesis:
        return None
    m = re.search(r'seed=(\d+)', hypothesis)
    return int(m.group(1)) if m else None


def get_current_picks(seed: int) -> set:
    d        = _get_data()
    spec     = _sample_strategy(seed)
    position = _build_position(d, spec)
    last_row = position.iloc[-1]
    return set(last_row[last_row].index.tolist())


# ── 掃描主邏輯 ───────────────────────────────────────────────────────
def scan(strategies: list, tw_stocks: set, tw_etfs: set) -> list:
    results = []
    for i, s in enumerate(strategies):
        seed = extract_seed(s.get('hypothesis', ''))
        if seed is None:
            logger.warning(f"[scanner] 策略 [{s['name'][:20]}] 無 seed，跳過")
            results.append({**s, 'picks': set(), 'matched': set(),
                             'error': '無 seed 資訊'})
            continue

        logger.info(f"[scanner] ({i+1}/{len(strategies)}) {s['name'][:25]}")
        try:
            picks   = get_current_picks(seed)
            matched = picks & tw_stocks
            results.append({**s, 'picks': picks, 'matched': matched})
            logger.info(f"[scanner]   選股 {len(picks)} 檔，持倉符合 {len(matched)} 檔：{sorted(matched)}")
        except Exception as e:
            logger.error(f"[scanner] 策略 [{s['name'][:20]}] 失敗：{e}")
            results.append({**s, 'picks': set(), 'matched': set(), 'error': str(e)})

    return results


# ── 格式化 Telegram 訊息 ─────────────────────────────────────────────
_RANK_ICONS = ['🥇', '🥈', '🥉'] + ['🔹'] * 20


def format_message(results: list, tw_stocks: set, tw_etfs: set,
                   us_holdings: set, name_map: dict,
                   regime: str = 'unknown') -> str:
    today   = str(date.today())
    n_ok    = sum(1 for r in results if 'error' not in r)
    n_total = len(results)

    regime_emoji = REGIME_EMOJI.get(regime, '⚪')
    _, regime_desc = REGIME_SORT.get(regime, REGIME_SORT['unknown'])

    # ── Sharpe 加權命中分數 ───────────────────────────────────────────
    total_sharpe  = sum(max(0, r.get('sharpe', 0)) for r in results if 'error' not in r) or 1.0
    hit_count:    dict[str, int]   = {s: 0   for s in tw_stocks}
    weighted_hit: dict[str, float] = {s: 0.0 for s in tw_stocks}

    for r in results:
        if 'error' in r:
            continue
        w = max(0, r.get('sharpe', 0)) / total_sharpe
        for sym in r.get('matched', set()):
            hit_count[sym]    = hit_count.get(sym, 0) + 1
            weighted_hit[sym] = weighted_hit.get(sym, 0.0) + w

    # ── Kelly 建議倉位 ────────────────────────────────────────────────
    kelly_sum:   dict[str, float] = {s: 0.0 for s in tw_stocks}
    kelly_count: dict[str, int]   = {s: 0   for s in tw_stocks}
    for r in results:
        if 'error' in r:
            continue
        for sym in r.get('matched', set()):
            kelly_count[sym] += 1
            kelly_sum[sym]   += _kelly_fraction(r.get('win_ratio', 0.5), r.get('sharpe', 0.8))
    kelly_map = {
        sym: f"{kelly_sum[sym] / kelly_count[sym]:.0%}"
        for sym in tw_stocks if kelly_count[sym] > 0
    }

    # 依 Sharpe 加權分數 → 等權命中數 → 排序
    ranked = sorted(tw_stocks,
                    key=lambda s: (weighted_hit.get(s, 0), hit_count.get(s, 0)),
                    reverse=True)

    lines = [
        "📊 <b>持倉策略掃描報告</b>",
        f"🗓 {today}  ·  {n_total} 個策略（成功 {n_ok} 個）",
        f"{regime_emoji} 市場 Regime：<b>{regime.upper()}</b>　{regime_desc}",
        "",
        "🇹🇼 <b>台股個股排名</b>（Sharpe加權命中）",
    ]

    prev_hits = -1
    rank_idx  = 0
    for sym in ranked:
        hits = hit_count.get(sym, 0)
        if hits != prev_hits:
            rank_idx += 1
            prev_hits = hits
        icon  = _RANK_ICONS[rank_idx - 1] if rank_idx <= len(_RANK_ICONS) else '🔸'
        label = _esc(_fmt(sym, name_map))
        bar   = '█' * hits + '░' * (n_ok - hits)
        pct   = f"{hits}/{n_ok}"
        wt    = f"({weighted_hit.get(sym, 0):.2f}w)"
        kelly = f" Kelly≈{kelly_map[sym]}" if sym in kelly_map else ""

        if hits > 0:
            lines.append(f"{icon} <b>{label}</b>  {pct} {wt}{kelly}  {bar}")
        else:
            lines.append(f"⬜ {label}  {pct}  {bar}")

    if tw_etfs:
        lines.append("")
        lines.append(f"ℹ️ ETF（{len(tw_etfs)} 檔）：{'  '.join(sorted(tw_etfs))}")

    if us_holdings:
        lines.append("")
        lines.append(f"🇺🇸 美股（{len(us_holdings)} 檔，策略不含）")
        lines.append("  " + "  ".join(sorted(us_holdings)))

    lines += ["", "━━━━━━━━━━━━━━━━━━━━", "📋 <b>策略明細</b>"]
    for i, r in enumerate(results):
        cagr_pct  = r['cagr'] * 100
        score_pct = r['score'] * 100
        matched   = r.get('matched', set())
        status = ('⚠️ ' + r['error'][:30]) if 'error' in r else (
                  ('✅ ' + ' '.join(_esc(_fmt(s, name_map)) for s in sorted(matched)))
                  if matched else '⬜ 無')
        lines.append(f"{i+1}. {_esc(r['name'][:20])}｜{cagr_pct:.0f}% {score_pct:.0f}分｜{status}")

    return "\n".join(lines)


# ── 主程式 ───────────────────────────────────────────────────────────
def main(top_n: int = DEFAULT_TOP_N):
    regime = detect_regime()
    logger.info(f"[scanner] 市場 Regime：{regime.upper()}")

    strategies = get_all_strategies(regime=regime)
    logger.info(f"[scanner] 開始掃描（{len(strategies)} 個策略，Regime={regime}）")

    holdings    = get_holdings()
    tw_all      = {h['symbol'] for h in holdings if h['market'] == 'TW'}
    tw_etfs     = {s for s in tw_all if _is_etf(s)}
    tw_stocks   = tw_all - tw_etfs
    us_holdings = {h['symbol'] for h in holdings if h['market'] == 'US'}

    logger.info(f"[scanner] 台股個股 {len(tw_stocks)} 檔：{sorted(tw_stocks)}")
    logger.info(f"[scanner] 台股ETF  {len(tw_etfs)} 檔")
    logger.info(f"[scanner] 美股     {len(us_holdings)} 檔：{sorted(us_holdings)}")

    name_map = get_name_map()
    results  = scan(strategies, tw_stocks, tw_etfs)
    msg      = format_message(results, tw_stocks, tw_etfs, us_holdings,
                               name_map, regime=regime)

    print("\n" + msg)
    send_message(msg)
    logger.info("[scanner] 掃描完成，已發送 Telegram 通知")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='持倉策略掃描器')
    parser.add_argument('--top', type=int, default=DEFAULT_TOP_N,
                        help=f'使用前幾名策略（預設 {DEFAULT_TOP_N}）')
    args = parser.parse_args()
    main(top_n=args.top)
