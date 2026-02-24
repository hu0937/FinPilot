"""
run_all_backtests.py
====================
對所有策略 (s01~sXX) 執行回測，以最新動態門檻重新驗證並回寫 DB。

執行：
  python agents/run_all_backtests.py
"""
import os, sys, time, traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))

import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data, dataframe as fldf
from finlab.backtest import sim
import pandas as pd

from core.database import init_db, get_conn, transaction, _migrate_discovered_strategies

STRATEGIES_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / 'strategies'

# ── 篩選規則（與 strategy_explorer.py 相同）─────────────────────
MIN_FLOOR          = dict(cagr=0.06, sharpe=0.35, mdd=-0.60)
COMPOSITE_THRESHOLD = 0.75   # 已提高（原 0.65），對應多重假設修正

# ── 熊市段驗證（與 strategy_explorer.py 相同）────────────────────
BEAR_START   = '2021-08-01'
BEAR_END     = '2022-12-31'
BEAR_MAX_MDD = -0.50

# 動態標竿底線（與 _S_BENCH_FLOOR 相同）
_BENCH_FLOOR = dict(max_cagr=0.416, max_sharpe=1.04, max_mdd=-0.290, max_win=0.518)


def _get_benchmarks() -> dict:
    """從 DB 讀取最佳通過策略績效，以硬編碼下限為底。"""
    try:
        row = get_conn().execute(
            "SELECT MAX(cagr), MAX(sharpe), MIN(mdd), MAX(win_ratio) "
            "FROM discovered_strategies WHERE passed=1"
        ).fetchone()
        db_cagr, db_sharpe, db_mdd, db_win = row if row else (None,)*4
    except Exception:
        db_cagr = db_sharpe = db_mdd = db_win = None
    return {
        'max_cagr':   max(_BENCH_FLOOR['max_cagr'],   db_cagr   or 0),
        'max_sharpe': max(_BENCH_FLOOR['max_sharpe'],  db_sharpe or 0),
        'max_mdd':    min(_BENCH_FLOOR['max_mdd'],     db_mdd    or -1),
        'max_win':    max(_BENCH_FLOOR['max_win'],     db_win    or 0),
    }


def run_strategy_file(py_file: Path) -> dict:
    """執行策略檔案，回傳 get_stats() 結果。
    使用 exec() + finlab 內建快取，避免重複下載資料。
    """
    code = py_file.read_text(encoding='utf-8')

    # 先嘗試直接編譯；若有舊版縮排 bug 才嘗試 compile 後 exec
    try:
        compile(code, py_file.name, 'exec')
    except IndentationError:
        # 舊版 _gen_code 縮排 bug 補救：重新縮排
        lines = code.split('\n')
        fixed = []
        in_main = False
        for line in lines:
            ws = len(line) - len(line.lstrip()) if line.strip() else -1
            if 'if __name__' in line:
                in_main = True
            if ws == -1:
                fixed.append(line)
            elif not in_main and ws == 0:
                fixed.append('    ' + line)
            elif not in_main and ws == 8:
                fixed.append(line[4:])
            else:
                fixed.append(line)
        import textwrap
        code = textwrap.dedent('\n'.join(fixed))

    ns = {
        '__name__': '__run_all__',   # 跳過 if __name__ == '__main__' 區塊
        'os': os, 'sys': sys, 'pd': pd,
        'finlab': finlab, 'data': data, 'fldf': fldf, 'sim': sim,
    }
    exec(compile(code, py_file.name, 'exec'), ns)

    report = ns.get('report')
    if report is None:
        raise RuntimeError("report 未定義")
    position = ns.get('position')   # 同時回傳 position 供熊市驗證使用
    return report.get_stats(), position


# ── 動態地板（與 strategy_explorer._dynamic_floor 相同邏輯）──────
def dynamic_floor(results: list) -> dict:
    passed = [r for r in results if r.get('above_static')]
    if len(passed) < 5:
        return MIN_FLOOR
    n = len(passed)
    p10 = max(0, n // 10)
    cagrs  = sorted(r['cagr']   for r in passed)
    sharps = sorted(r['sharpe'] for r in passed)
    mdds   = sorted(r['mdd']    for r in passed)
    return {
        'cagr':   max(MIN_FLOOR['cagr'],   cagrs[p10]),
        'sharpe': max(MIN_FLOOR['sharpe'], sharps[p10]),
        'mdd':    min(MIN_FLOOR['mdd'],    mdds[-(p10 + 1)]),
    }


def composite_score(cagr, sharpe, mdd, win, results: list) -> float:
    bench = _get_benchmarks()
    passed_static = [r for r in results if r.get('above_static')]
    best_cagr   = max(bench['max_cagr'],   max((r['cagr']   for r in passed_static), default=0))
    best_sharpe = max(bench['max_sharpe'],  max((r['sharpe'] for r in passed_static), default=0))
    best_mdd    = max(bench['max_mdd'],     max((r['mdd']    for r in passed_static), default=-1))
    best_win    = max(bench['max_win'],     max((r['win']    for r in passed_static), default=0))

    def _n(val, floor, best):
        span = best - floor
        return max(0.0, (val - floor) / span) if span != 0 else 0.0

    return (
        1.0 * _n(cagr,   MIN_FLOOR['cagr'],   best_cagr)   +
        1.0 * _n(sharpe, MIN_FLOOR['sharpe'], best_sharpe) +
        1.5 * _n(mdd,    MIN_FLOOR['mdd'],    best_mdd)    +
        0.5 * _n(win,    0.0,                 best_win)
    ) / 4.0


def update_db(result: dict, passed_final: bool):
    """依 file_path 找到 DB 記錄並更新績效 + passed 狀態。
    找不到對應記錄時靜默跳過（s01~s10 手動策略未在 discovered_strategies 表）。
    """
    fpath = str(result['file'])
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM discovered_strategies WHERE file_path=?", (fpath,)
    ).fetchone()
    if not row:
        return False   # 不在 DB，跳過

    cagr   = result['cagr']
    sharpe = result['sharpe']
    mdd    = result['mdd']
    win    = result['win']
    calmar = round(cagr / abs(mdd), 3) if mdd and mdd != 0 else None

    with transaction() as c:
        c.execute("""
            UPDATE discovered_strategies
               SET cagr=?, sharpe=?, mdd=?, win_ratio=?,
                   calmar_ratio=?, passed=?
             WHERE file_path=?
        """, (cagr, sharpe, mdd, win, calmar, 1 if passed_final else 0, fpath))
    return True


def main():
    init_db()
    _migrate_discovered_strategies()   # 確保新欄位存在

    strategy_files = sorted(STRATEGIES_DIR.glob('s*.py'))
    print(f"共找到 {len(strategy_files)} 個策略檔案\n")

    results = []

    HDR = f"{'策略':<6} {'名稱':<32} {'CAGR':>8} {'Sharpe':>7} {'MDD':>8} {'勝率':>7}  狀態"
    SEP = '─' * 92
    print(SEP)
    print(HDR)
    print(SEP)

    for py_file in strategy_files:
        sid  = py_file.name[:3]
        name = py_file.stem
        short_name = name[:32]
        t0 = time.time()

        try:
            s, position = run_strategy_file(py_file)
            elapsed = time.time() - t0

            cagr   = s['cagr']
            sharpe = s['monthly_sharpe']
            mdd    = s['max_drawdown']
            win    = s['win_ratio']

            above = (cagr > MIN_FLOOR['cagr']
                     and sharpe > MIN_FLOOR['sharpe']
                     and mdd > MIN_FLOOR['mdd'])

            # ── 熊市段驗證 ──────────────────────────────────────
            bear_ok = True
            bear_mdd_val = None
            if position is not None and above:
                try:
                    bear_mask = (
                        (position.index >= BEAR_START) &
                        (position.index <= BEAR_END)
                    )
                    pos_bear = position[bear_mask]
                    if len(pos_bear) >= 3:
                        resample = 'M'   # 所有自動策略均月頻
                        r_bear = sim(pos_bear, resample=resample,
                                     fee_ratio=1.425/1000, tax_ratio=3/1000,
                                     trade_at_price='open', upload=False)
                        bear_mdd_val = r_bear.get_stats()['max_drawdown']
                        bear_ok = bear_mdd_val > BEAR_MAX_MDD
                except Exception as _be:
                    pass   # 熊市驗證失敗則略過（不強制淘汰）

            r = dict(sid=sid, name=name, file=py_file,
                     cagr=cagr, sharpe=sharpe, mdd=mdd, win=win,
                     above_static=above, elapsed=elapsed,
                     bear_ok=bear_ok, bear_mdd=bear_mdd_val)
            results.append(r)

            dyn   = dynamic_floor(results)
            dyn_ok = (cagr > dyn['cagr']
                      and sharpe > dyn['sharpe']
                      and mdd > dyn['mdd'])
            score = composite_score(cagr, sharpe, mdd, win, results)
            r.update(dyn_ok=dyn_ok, score=score)

            if dyn_ok and score >= COMPOSITE_THRESHOLD:
                status = '✅ 通過'
            elif not above:
                status = '❌ 靜態地板未過'
            elif not dyn_ok:
                fails = []
                if cagr   <= dyn['cagr']:   fails.append(f"CAGR≤{dyn['cagr']:.1%}")
                if sharpe <= dyn['sharpe']: fails.append(f"Sharpe≤{dyn['sharpe']:.2f}")
                if mdd    <= dyn['mdd']:    fails.append(f"MDD≤{dyn['mdd']:.1%}")
                status = f"⚠️  動態地板未過 ({', '.join(fails)})"
            else:
                status = f"⚠️  分數不足 ({score:.3f})"

            print(f"{sid:<6} {short_name:<32} {cagr:>7.1%} {sharpe:>7.2f} {mdd:>7.1%} {win:>6.1%}  {status}  ({elapsed:.0f}s)")

        except Exception as e:
            elapsed = time.time() - t0
            print(f"{sid:<6} {short_name:<32} {'ERROR':<47}  {str(e)[:60]}  ({elapsed:.0f}s)")
            results.append(dict(sid=sid, name=name, file=py_file, error=str(e),
                                cagr=0, sharpe=0, mdd=0, win=0,
                                above_static=False, dyn_ok=False, score=0))

    # ── 最終動態地板 ──────────────────────────────────────────────
    final_floor = dynamic_floor(results)
    passed_static = [r for r in results if r.get('above_static')]
    print(f"\n{SEP}")
    print(f"最終動態地板（{len(passed_static)} 個策略通過靜態地板，取 P10）：")
    print(f"  CAGR > {final_floor['cagr']:.1%}  "
          f"Sharpe > {final_floor['sharpe']:.2f}  "
          f"MDD > {final_floor['mdd']:.1%}")

    # ── 最終 Pass/Fail + DB 回寫 ────────────────────────────────
    db_updated = db_skipped = 0
    for r in results:
        if r.get('error'):
            continue
        dyn_ok = (r['cagr'] > final_floor['cagr']
                  and r['sharpe'] > final_floor['sharpe']
                  and r['mdd'] > final_floor['mdd'])
        r['passed_final'] = (dyn_ok
                             and r.get('score', 0) >= COMPOSITE_THRESHOLD
                             and r.get('bear_ok', True))
        updated = update_db(r, r['passed_final'])
        if updated:
            db_updated += 1
        else:
            db_skipped += 1

    print(f"\nDB 回寫：已更新 {db_updated} 筆 / 跳過（無DB記錄）{db_skipped} 筆")

    # ── 彙總 ─────────────────────────────────────────────────────
    passing = [r for r in results if r.get('passed_final')]
    failing = [r for r in results if not r.get('passed_final') and not r.get('error')]
    errors  = [r for r in results if r.get('error')]

    print(f"\n{'─'*60}")
    print(f"✅ 通過最新篩選規則 ({len(passing)} 個，依 CAGR 排序)：")
    for r in sorted(passing, key=lambda x: -x['cagr']):
        print(f"  {r['sid']}  CAGR={r['cagr']:.1%}  Sharpe={r['sharpe']:.2f}  "
              f"MDD={r['mdd']:.1%}  勝率={r['win']:.1%}  分數={r['score']:.3f}")

    if failing:
        print(f"\n{'─'*60}")
        print(f"❌ 不符合最新篩選規則 ({len(failing)} 個)：")
        for r in sorted(failing, key=lambda x: x['sid']):
            reasons = []
            if r['cagr']   <= final_floor['cagr']:   reasons.append(f"CAGR {r['cagr']:.1%}≤{final_floor['cagr']:.1%}")
            if r['sharpe'] <= final_floor['sharpe']: reasons.append(f"Sharpe {r['sharpe']:.2f}≤{final_floor['sharpe']:.2f}")
            if r['mdd']    <= final_floor['mdd']:    reasons.append(f"MDD {r['mdd']:.1%}≤{final_floor['mdd']:.1%}")
            if not reasons and r.get('score', 0) < COMPOSITE_THRESHOLD:
                reasons.append(f"分數 {r['score']:.3f}<{COMPOSITE_THRESHOLD}")
            if not r.get('bear_ok', True) and r.get('bear_mdd') is not None:
                reasons.append(f"熊市MDD {r['bear_mdd']:.1%}<{BEAR_MAX_MDD:.0%}")
            print(f"  {r['sid']}  {r['name'][:40]:<40}  → {', '.join(reasons)}")

    if errors:
        print(f"\n⚠️  執行錯誤 ({len(errors)} 個)：")
        for r in errors:
            print(f"  {r['sid']}  {str(r.get('error','?'))[:80]}")

    print(f"\n{'═'*60}")
    print(f"合計：{len(strategy_files)} 個策略  |  通過 {len(passing)}  |  淘汰 {len(failing)}  |  錯誤 {len(errors)}")


if __name__ == '__main__':
    main()
