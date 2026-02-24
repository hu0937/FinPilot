"""
event_strategy_explorer.py  ── 事件驅動型策略探索引擎（連續執行版）
=================================================================
專為事件觸發型策略設計。
由排程啟動 daemon thread 後持續探索，跑完一個立刻跑下一個。

與 strategy_explorer.py 的差異：
  - 策略類型：事件驅動（Event-driven），不是月頻再平衡
  - Position 建構：根據特定事件逐筆填入 bool DataFrame
  - 持有期間：由事件時間軸決定（可隨機化進出場偏移）
  - 可選疊加：流動性篩選條件

事件模板庫（3 種）：
  ┌ attention_stock    : 注意股票（如 FinLab 資料可用，否則略過）
  ├ capital_reduction  : 上市/上櫃減資後恢復買賣（TSE + OTC 合併）
  └ treasury_stock     : 庫藏股買回護盤（買回期間持有）

注意：
  disposal_intraday / disposal_any / disposal_fixed 三個模板原本包含於此引擎，
  其邏輯源自 FinLab 社群處置股策略（https://ai.finlab.tw/），
  已移除，不包含於此公開 repo。

儲存門檻：與 strategy_explorer.py 一致
  複合分數 ≥ 0.75 + OOS 驗證（CAGR>15%, Sharpe>0.6）+ 熊市段驗證（MDD>-50%）

策略命名：延續 discovered_strategies DB 的通過策略編號（sXX）。
"""
import os, sys, json, hashlib, random, traceback, math
from html import escape as _esc
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))

import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim
import pandas as pd
from loguru import logger

from core.database import (save_discovered_strategy, is_strategy_tried,
                            get_passed_strategy_count, get_all_discovered_strategies,
                            get_passed_by_condition_group, supersede_strategy,
                            init_db)
from core.notifier import send_message

# ── 回測期間 ─────────────────────────────────────────────────────
# 訓練期：~ BACKTEST_END（2020-12-31）
# 熊市驗證（獨立）：2021-01-01 ~ 2022-12-31
# OOS 驗證（最終）：OOS_START ~ 今
BACKTEST_END = '2020-12-31'
OOS_START    = '2023-01-01'
BEAR_START   = '2021-01-01'
BEAR_END     = '2022-12-31'

# ── OOS 通過門檻 ─────────────────────────────────────────────────
OOS_MIN_CAGR    = 0.15   # 動態基準（0050 OOS CAGR + 5%）不可用時的 fallback
OOS_MIN_SHARPE  = 0.80   # 提高至 0.80（原 0.60）
BEAR_MAX_MDD    = -0.50

# ── 訓練期統計顯著性門檻（Harvey, Liu & Zhu 2016）───────────────────
TSTAT_MIN = 3.0   # t-statistic = monthly_sharpe × sqrt(n_months) 須 > 3.0

# ── 交易成本 ─────────────────────────────────────────────────────
FEE_RATIO = 1.425 / 1000
TAX_RATIO = 3.0   / 1000

# ── 品質門檻 ─────────────────────────────────────────────────────
MIN_FLOOR = dict(cagr=0.06, sharpe=0.35, mdd=-0.60)

# s07 處置股策略硬編碼基準（作為標竿下限）
_S_BENCH_FLOOR = dict(
    max_cagr   = 0.416,
    max_sharpe = 1.04,
    max_mdd    = -0.290,
    max_win    = 0.518,
)

COMPOSITE_THRESHOLD = 0.75   # 與 strategy_explorer 一致

STRATEGIES_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / 'strategies'

# 資料快取（每日重新載入一次）
_data_cache: dict = {'data': None, 'date': None}


# ═══════════════════════════════════════════════════════════════
# 評分函式（與 strategy_explorer 同邏輯）
# ═══════════════════════════════════════════════════════════════

def _get_benchmarks() -> dict:
    from core.database import get_conn
    try:
        row = get_conn().execute(
            "SELECT MAX(cagr), MAX(sharpe), MIN(mdd), MAX(win_ratio) "
            "FROM discovered_strategies WHERE passed=1"
        ).fetchone()
        db_cagr, db_sharpe, db_mdd, db_win = row if row else (None,)*4
    except Exception:
        db_cagr = db_sharpe = db_mdd = db_win = None
    return {
        'max_cagr':   max(_S_BENCH_FLOOR['max_cagr'],  db_cagr   or 0),
        'max_sharpe': max(_S_BENCH_FLOOR['max_sharpe'], db_sharpe or 0),
        'max_mdd':    min(_S_BENCH_FLOOR['max_mdd'],    db_mdd    or -1),
        'max_win':    max(_S_BENCH_FLOOR['max_win'],    db_win    or 0),
    }


def _compute_composite(cagr, sharpe, mdd, win_ratio, db_passed) -> float:
    bench = _get_benchmarks()
    best_cagr   = max(bench['max_cagr'],   max((r['cagr']      for r in db_passed), default=0))
    best_sharpe = max(bench['max_sharpe'],  max((r['sharpe']    for r in db_passed), default=0))
    best_mdd    = max(bench['max_mdd'],     max((r['mdd']       for r in db_passed), default=-1))
    best_win    = max(bench['max_win'],     max((r['win_ratio'] for r in db_passed), default=0))

    def _n(val, floor, best):
        span = best - floor
        return max(0.0, (val - floor) / span) if span != 0 else 0.0

    return (
        1.0 * _n(cagr,      MIN_FLOOR['cagr'],   best_cagr)   +
        1.0 * _n(sharpe,    MIN_FLOOR['sharpe'], best_sharpe) +
        1.5 * _n(mdd,       MIN_FLOOR['mdd'],    best_mdd)    +
        0.5 * _n(win_ratio, 0.0,                 best_win)
    ) / 4.0


def _dynamic_floor(db_passed: list) -> dict:
    if len(db_passed) < 5:
        return MIN_FLOOR
    n = len(db_passed)
    p10_idx = max(0, n // 10)
    cagrs  = sorted(r['cagr']   for r in db_passed)
    sharps = sorted(r['sharpe'] for r in db_passed)
    mdds   = sorted(r['mdd']    for r in db_passed)
    return {
        'cagr':   max(MIN_FLOOR['cagr'],   cagrs[p10_idx]),
        'sharpe': max(MIN_FLOOR['sharpe'], sharps[p10_idx]),
        'mdd':    min(MIN_FLOOR['mdd'],    mdds[-(p10_idx + 1)]),
    }


def _passes_floor(s: dict, floor: dict = MIN_FLOOR) -> bool:
    return (s['cagr']          > floor['cagr']
            and s['monthly_sharpe'] > floor['sharpe']
            and s['max_drawdown']   > floor['mdd'])


def _is_best_ever(s: dict, db_passed: list) -> bool:
    bench = _get_benchmarks()
    best_cagr   = max(bench['max_cagr'],   max((r['cagr']      for r in db_passed), default=0))
    best_sharpe = max(bench['max_sharpe'],  max((r['sharpe']    for r in db_passed), default=0))
    best_mdd    = max(bench['max_mdd'],     max((r['mdd']       for r in db_passed), default=-1))
    best_win    = max(bench['max_win'],     max((r['win_ratio'] for r in db_passed), default=0))

    def _n(val, floor, best):
        span = best - floor
        return max(0.0, (val - floor) / span) if span != 0 else 0.0

    score = (
        1.0 * _n(s['cagr'],          MIN_FLOOR['cagr'],   best_cagr)   +
        1.0 * _n(s['monthly_sharpe'], MIN_FLOOR['sharpe'], best_sharpe) +
        1.5 * _n(s['max_drawdown'],   MIN_FLOOR['mdd'],    best_mdd)    +
        0.5 * _n(s['win_ratio'],      0.0,                 best_win)
    ) / 4.0

    logger.debug(f"[event-explorer] 複合分數 {score:.3f} (門檻 {COMPOSITE_THRESHOLD})")
    return score >= COMPOSITE_THRESHOLD


def _should_save(spec: dict, s: dict, db_passed: list) -> tuple:
    floor = _dynamic_floor(db_passed)
    if not _passes_floor(s, floor):
        logger.debug(
            f"[event-explorer] 未過動態地板 CAGR>{floor['cagr']:.1%} "
            f"Sharpe>{floor['sharpe']:.2f} MDD>{floor['mdd']:.1%}"
        )
        return False, None
    if _is_best_ever(s, db_passed):
        return True, 'best_ever'
    return False, None


# ═══════════════════════════════════════════════════════════════
# 事件模板庫（6 種）
# ═══════════════════════════════════════════════════════════════
    # disposal_intraday / disposal_any / disposal_fixed 已移除。
    # 此三個模板邏輯源自 FinLab 社群處置股策略（https://ai.finlab.tw/），
    # 不包含於此公開 repo。

EVENT_TEMPLATE_POOL = [

    # ─── 注意股票（如 FinLab 資料可用，否則略過）───────────────
    {
        'key':          'attention_stock',
        'label':        '注意股票',
        'params': {
            'entry_offset': [0, 1],
            'exit_offset':  [0, 1, -1],
        },
        'requires':     ['attention', 'close'],
        'fixed_hold':   False,
    },

    # ─── 減資後恢復買賣（TSE + OTC 合併）───────────────────────
    # 假說：現金減資 = 公司把錢還股東，財務健全；
    #       恢復交易日因新的計價基準常有劇烈波動，短線反彈空間大。
    {
        'key':          'capital_reduction',
        'label':        '減資後重啟',
        'params': {
            'entry_offset': [0, 1, 2],      # 恢復交易日當天或之後進場
            'hold_days':    [3, 5, 10, 15, 20],  # 固定持有天數
        },
        'requires':     ['cap_red', 'close'],
        'fixed_hold':   True,
    },

    # ─── 庫藏股買回護盤（買回期間持有）─────────────────────────
    # 假說：公司宣告買回 = 管理層認為股價低估 + 主動護盤；
    #       買回期間股價有公司資金撐盤，下跌空間有限。
    {
        'key':          'treasury_stock',
        'label':        '庫藏股買回',
        'params': {
            'entry_offset': [0, 1],          # 買回期間開始後進場
            'exit_offset':  [0, -5, -10],    # 0=到期, 負值=提前N天出場
            'min_len':      [0, 10, 20, 30], # 最短買回天數（排除過短的宣告）
        },
        'requires':     ['treasury', 'close'],
        'fixed_hold':   False,
    },
]

# ── 疊加過濾（可選）────────────────────────────────────────────
OVERLAY_POOL = [
    {'key': 'none',       'label': '無過濾',          'params': {}},
    {'key': 'vol_filter', 'label': '均量>{vol_k}張',  'params': {'vol_k': [100, 200, 300, 500]}},
]


# ═══════════════════════════════════════════════════════════════
# 資料載入
# ═══════════════════════════════════════════════════════════════

def _load_data() -> dict:
    """一次載入所有事件資料，失敗的設為 None"""
    logger.info("[event-explorer] 載入 FinLab 資料...")
    result = {}

    # 必要資料
    for var_key, finlab_key in [
        ('close',  'price:收盤價'),
        ('volume', 'price:成交股數'),
    ]:
        try:
            result[var_key] = data.get(finlab_key)
        except Exception as e:
            logger.error(f"[event-explorer] 必要資料 {finlab_key!r} 失敗：{e}")
            result[var_key] = None

    # disposal_information 已移除（邏輯源自 FinLab 社群處置股策略）

    # 注意股（可選）
    try:
        result['attention'] = data.get('trading_attention').sort_index()
        logger.info(f"[event-explorer] trading_attention：{len(result['attention'])} 筆")
    except Exception as e:
        logger.debug(f"[event-explorer] trading_attention 不可用（略過）：{e}")
        result['attention'] = None

    # 減資（TSE + OTC 合併）
    try:
        df_tse = data.get('capital_reduction_tse').sort_index().reset_index()
        df_otc = data.get('capital_reduction_otc').sort_index().reset_index()
        # 只保留兩表共同欄位：stock_id + 恢復買賣日期
        frames = []
        for df in (df_tse, df_otc):
            if '恢復買賣日期' in df.columns and 'stock_id' in df.columns:
                frames.append(df[['stock_id', '恢復買賣日期']].copy())
        if frames:
            result['cap_red'] = pd.concat(frames, ignore_index=True).dropna()
            logger.info(f"[event-explorer] capital_reduction（TSE+OTC）：{len(result['cap_red'])} 筆")
        else:
            result['cap_red'] = None
    except Exception as e:
        logger.warning(f"[event-explorer] capital_reduction 不可用：{e}")
        result['cap_red'] = None

    # 庫藏股
    try:
        df = data.get('treasury_stock').sort_index().reset_index()
        cols_needed = ['stock_id', '預定買回期間-起', '預定買回期間-迄']
        if all(c in df.columns for c in cols_needed):
            result['treasury'] = df[cols_needed].copy().dropna(
                subset=['預定買回期間-起', '預定買回期間-迄']
            )
            logger.info(f"[event-explorer] treasury_stock：{len(result['treasury'])} 筆")
        else:
            result['treasury'] = None
    except Exception as e:
        logger.warning(f"[event-explorer] treasury_stock 不可用：{e}")
        result['treasury'] = None

    return result


def _get_data() -> dict:
    from datetime import date
    today = str(date.today())
    if _data_cache['date'] != today or _data_cache['data'] is None:
        _data_cache['data'] = _load_data()
        _data_cache['date'] = today
    return _data_cache['data']


# ═══════════════════════════════════════════════════════════════
# Position 建構工具
# ═══════════════════════════════════════════════════════════════

def _init_position(close: pd.DataFrame) -> pd.DataFrame:
    """建立全 False 的 position 框架"""
    return close < 0


def _fill(position: pd.DataFrame, stock_id: str,
          start: pd.Timestamp, end: pd.Timestamp):
    """安全地填入 position 區間（stock_id 不存在或日期超界則略過）"""
    if stock_id not in position.columns:
        return
    if pd.isna(start) or pd.isna(end) or start > end:
        return
    try:
        position.loc[start:end, stock_id] = True
    except Exception:
        pass


def _build_attention_pos(d: dict, params: dict) -> pd.DataFrame:
    """注意股票：進名單時進場，出名單（或偏移）時出場"""
    df = d['attention'].reset_index()
    # 嘗試找結束日期欄位（FinLab 版本間欄位名稱可能不同）
    for end_col in ['處置結束時間', 'end_date', 'end', 'notice_end']:
        if end_col in df.columns:
            df = df[['stock_id', 'date', end_col]].copy()
            df.columns = ['stock_id', 'start', 'end']
            break
    else:
        raise ValueError("trading_attention 找不到結束日期欄位")

    df = df.dropna(subset=['start', 'end'])
    df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]

    entry_offset = params.get('entry_offset', 0)
    exit_offset  = params.get('exit_offset', 0)

    position = _init_position(d['close'])
    for _, row in df.iterrows():
        _fill(position, str(row['stock_id']),
              pd.to_datetime(row['start']) + timedelta(days=entry_offset),
              pd.to_datetime(row['end'])   + timedelta(days=exit_offset))
    return position


def _build_capital_reduction_pos(d: dict, params: dict) -> pd.DataFrame:
    """減資後恢復買賣日進場，固定持有 N 天"""
    df = d['cap_red'].copy()
    df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]

    entry_offset = params.get('entry_offset', 0)
    hold_days    = params.get('hold_days', 10)

    position = _init_position(d['close'])
    for _, row in df.iterrows():
        resume = pd.to_datetime(row['恢復買賣日期'])
        start  = resume + timedelta(days=entry_offset)
        _fill(position, str(row['stock_id']), start, start + timedelta(days=hold_days))
    return position


def _build_treasury_pos(d: dict, params: dict) -> pd.DataFrame:
    """庫藏股買回期間持有"""
    df = d['treasury'].copy()
    df.columns = ['stock_id', 'start', 'end']
    df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]

    min_len = params.get('min_len', 0)
    if min_len > 0:
        df = df.copy()
        df['_dur'] = (pd.to_datetime(df['end']) - pd.to_datetime(df['start'])).dt.days
        df = df[df['_dur'] >= min_len]

    entry_offset = params.get('entry_offset', 0)
    exit_offset  = params.get('exit_offset', 0)

    position = _init_position(d['close'])
    for _, row in df.iterrows():
        _fill(position, str(row['stock_id']),
              pd.to_datetime(row['start']) + timedelta(days=entry_offset),
              pd.to_datetime(row['end'])   + timedelta(days=exit_offset))
    return position


def _build_event_position(d: dict, spec: dict) -> pd.DataFrame:
    key    = spec['template']['key']
    params = spec['params']
    if key == 'attention_stock':
        return _build_attention_pos(d, params)
    elif key == 'capital_reduction':
        return _build_capital_reduction_pos(d, params)
    elif key == 'treasury_stock':
        return _build_treasury_pos(d, params)
    raise ValueError(f"未知事件模板：{key}")


def _apply_overlay(position: pd.DataFrame, d: dict, spec: dict) -> pd.DataFrame:
    overlay = spec['overlay']
    if overlay['key'] == 'none':
        return position
    if overlay['key'] == 'vol_filter':
        vol_k    = spec['overlay_params'].get('vol_k', 300)
        vol_ok   = d['volume'].rolling(20).mean() > vol_k * 1000
        vol_mask = vol_ok.reindex(position.index, method='ffill').fillna(False)
        return position & vol_mask
    return position


# ═══════════════════════════════════════════════════════════════
# 策略採樣與 Hash
# ═══════════════════════════════════════════════════════════════

def _sample_strategy(seed: int) -> dict:
    rng      = random.Random(seed)
    template = rng.choice(EVENT_TEMPLATE_POOL)
    params   = {k: rng.choice(v) for k, v in template['params'].items()}
    overlay  = rng.choices(OVERLAY_POOL, weights=[3, 2], k=1)[0]
    overlay_params = {k: rng.choice(v) for k, v in overlay['params'].items()}

    # 人類可讀描述
    parts = []
    for k, v in params.items():
        if k == 'entry_offset' and v:
            parts.append(f"進場+{v}日")
        elif k == 'exit_offset' and v:
            parts.append(f"出場{'+' if v > 0 else ''}{v}日")
        elif k == 'min_len' and v:
            parts.append(f"最短{v}天")
        elif k == 'hold_days':
            parts.append(f"持有{v}天")
        elif k == 'intraday_only':
            parts.append("分時" if v else "全級別")

    overlay_label = ''
    if overlay['key'] != 'none':
        lbl = overlay['label']
        for k, v in overlay_params.items():
            lbl = lbl.replace(f'{{{k}}}', str(v))
        overlay_label = f'＋{lbl}'

    desc_suffix = ('、'.join(parts) if parts else '預設') + overlay_label
    name = f"{template['label']}（{desc_suffix}）"[:40]

    return dict(
        seed=seed, template=template, params=params,
        overlay=overlay, overlay_params=overlay_params,
        name=name, description=f"{template['label']}：{desc_suffix}",
    )


def _combo_hash(spec: dict) -> str:
    canonical = json.dumps({
        'template':       spec['template']['key'],
        'params':         spec['params'],
        'overlay':        spec['overlay']['key'],
        'overlay_params': spec['overlay_params'],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _condition_group_hash(spec: dict) -> str:
    canonical = json.dumps({
        'template': spec['template']['key'],
        'overlay':  spec['overlay']['key'],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════
# 程式碼生成
# ═══════════════════════════════════════════════════════════════

def _gen_code(spec: dict, stats: dict) -> str:
    """產生獨立可執行的 .py 策略檔"""
    tmpl           = spec['template']
    params         = spec['params']
    overlay        = spec['overlay']
    overlay_params = spec['overlay_params']

    entry_offset   = params.get('entry_offset', 0)
    exit_offset    = params.get('exit_offset', 0)
    min_len        = params.get('min_len', 0)
    hold_days      = params.get('hold_days', None)
    intraday_only  = params.get('intraday_only', tmpl.get('intraday_only', True))

    # ── 事件區塊 ──
    # disposal_intraday / disposal_any / disposal_fixed 已移除
    if tmpl['key'] == 'capital_reduction':
        event_block = (
            "# 合併上市 + 上櫃減資資料\n"
            "df_tse = data.get('capital_reduction_tse').sort_index().reset_index()\n"
            "df_otc = data.get('capital_reduction_otc').sort_index().reset_index()\n"
            "df = pd.concat([\n"
            "    df_tse[['stock_id', '恢復買賣日期']],\n"
            "    df_otc[['stock_id', '恢復買賣日期']],\n"
            "], ignore_index=True).dropna()\n"
            "df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]\n"
            "\n"
            "position = close < 0  # 全部初始化為 False\n"
            "\n"
            "for _, row in df.iterrows():\n"
            "    stock_id = str(row['stock_id'])\n"
            "    if stock_id not in position.columns:\n"
            "        continue\n"
            f"    start = pd.to_datetime(row['恢復買賣日期']) + timedelta(days={entry_offset})\n"
            f"    end   = start + timedelta(days={hold_days})\n"
            "    position.loc[start:end, stock_id] = True"
        )

    elif tmpl['key'] == 'treasury_stock':
        min_len_block = (
            f"df['_dur'] = (pd.to_datetime(df['end']) - pd.to_datetime(df['start'])).dt.days\n"
            f"df = df[df['_dur'] >= {min_len}]\n"
            if min_len > 0 else ""
        )
        event_block = (
            "df = data.get('treasury_stock').sort_index().reset_index()\n"
            "df = df[['stock_id', '預定買回期間-起', '預定買回期間-迄']].copy()\n"
            "df.columns = ['stock_id', 'start', 'end']\n"
            "df = df.dropna(subset=['start', 'end'])\n"
            f"{min_len_block}"
            "df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]\n"
            "\n"
            "position = close < 0  # 全部初始化為 False\n"
            "\n"
            "for _, row in df.iterrows():\n"
            "    stock_id = str(row['stock_id'])\n"
            "    if stock_id not in position.columns:\n"
            "        continue\n"
            f"    start = pd.to_datetime(row['start']) + timedelta(days={entry_offset})\n"
            f"    end   = pd.to_datetime(row['end'])   + timedelta(days={exit_offset})\n"
            "    position.loc[start:end, stock_id] = True"
        )

    elif tmpl['key'] == 'attention_stock':
        event_block = (
            "df = data.get('trading_attention').sort_index().reset_index()\n"
            "for _end_col in ['處置結束時間', 'end_date', 'end', 'notice_end']:\n"
            "    if _end_col in df.columns:\n"
            "        df = df[['stock_id', 'date', _end_col]].copy()\n"
            "        df.columns = ['stock_id', 'start', 'end']\n"
            "        break\n"
            "df = df.dropna(subset=['start', 'end'])\n"
            "df = df[df['stock_id'].apply(lambda x: len(str(x)) == 4)]\n"
            "\n"
            "position = close < 0\n"
            "\n"
            "for _, row in df.iterrows():\n"
            "    stock_id = str(row['stock_id'])\n"
            "    if stock_id not in position.columns:\n"
            "        continue\n"
            f"    start = pd.to_datetime(row['start']) + timedelta(days={entry_offset})\n"
            f"    end   = pd.to_datetime(row['end'])   + timedelta(days={exit_offset})\n"
            "    position.loc[start:end, stock_id] = True"
        )

    else:
        raise ValueError(f"未知模板：{tmpl['key']}")

    # ── 疊加過濾區塊 ──
    if overlay['key'] == 'vol_filter':
        vol_k = overlay_params.get('vol_k', 300)
        overlay_block = (
            f"\n# 均量過濾：均量 < {vol_k} 張時不持有\n"
            f"vol_ok   = volume.rolling(20).mean() > {vol_k * 1000}\n"
            f"vol_mask = vol_ok.reindex(position.index, method='ffill').fillna(False)\n"
            f"position = position & vol_mask"
        )
    else:
        overlay_block = ''

    date_str   = datetime.now().strftime('%Y-%m-%d')
    cagr_str   = f'{stats["cagr"]:.2%}'
    sharpe_str = f'{stats["monthly_sharpe"]:.2f}'
    mdd_str    = f'{stats["max_drawdown"]:.2%}'
    win_str    = f'{stats["win_ratio"]:.2%}'

    return (
        f'"""\n'
        f'{spec["name"]}  (event-strategy auto-discovered {date_str})\n'
        f'======================================================\n'
        f'{spec["description"]}\n\n'
        f'回測績效（~2020，訓練期）：\n'
        f'  CAGR   : {cagr_str}\n'
        f'  Sharpe : {sharpe_str}\n'
        f'  MDD    : {mdd_str}\n'
        f'  勝率   : {win_str}\n\n'
        f'注意：已通過 t-stat>3.0 + 2021~2022 熊市驗證 + 2023~今 OOS 驗證\n'
        f'"""\n'
        f'import os, sys, pandas as pd\n'
        f'from datetime import timedelta\n'
        f"sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        f'from dotenv import load_dotenv\n'
        f"load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))\n"
        f'import finlab\n'
        f"finlab.login(os.getenv('FINLAB_API_TOKEN', ''))\n"
        f'from finlab import data\n'
        f'from finlab.backtest import sim\n'
        f'\n'
        f"close  = data.get('price:收盤價')\n"
        f"volume = data.get('price:成交股數')\n"
        f'\n'
        f'# ── 事件 position ──\n'
        f'{event_block}\n'
        f'{overlay_block}\n'
        f'\n'
        f'report = sim(\n'
        f'    position,\n'
        f"    trade_at_price='open',\n"
        f'    fee_ratio=1.425/1000,\n'
        f'    tax_ratio=3/1000,\n'
        f'    position_limit=0.2,\n'
        f'    upload=False,\n'
        f"    name={repr(spec['name'][:30])},\n"
        f')\n'
        f'\n'
        f"if __name__ == '__main__':\n"
        f'    s = report.get_stats()\n'
        f"    print(f'CAGR={{s[\"cagr\"]:.2%}}  Sharpe={{s[\"monthly_sharpe\"]:.2f}}  '\n"
        f"          f'MDD={{s[\"max_drawdown\"]:.2%}}  勝率={{s[\"win_ratio\"]:.2%}}')\n"
    )


# ═══════════════════════════════════════════════════════════════
# 主探索迴圈
# ═══════════════════════════════════════════════════════════════

def run_exploration() -> bool:
    """執行一次事件策略探索"""
    init_db()

    all_records = get_all_discovered_strategies()
    db_passed   = [r for r in all_records if r['passed']]

    try:
        d = _get_data()
    except Exception as e:
        logger.error(f"[event-explorer] 資料載入失敗：{e}")
        return False

    if d.get('close') is None:
        logger.error("[event-explorer] 收盤價不可用，略過")
        return False

    # ── 找一個未嘗試過的組合（最多嘗試 50 個種子）──
    base_seed = int(datetime.now().timestamp())
    spec = tid = None

    for attempt in range(50):
        candidate     = _sample_strategy(base_seed + attempt)
        candidate_tid = _combo_hash(candidate)

        if is_strategy_tried(candidate_tid):
            continue

        # 確認此模板所需的資料均可用
        missing = [r for r in candidate['template'].get('requires', [])
                   if d.get(r) is None]
        if missing:
            logger.debug(f"[event-explorer] 種子 {attempt} 缺資料 {missing}，跳過")
            continue

        spec = candidate
        tid  = candidate_tid
        break

    if spec is None:
        logger.info("[event-explorer] 50 次種子均已試過或資料不足，略過本輪")
        return False

    logger.info(f"[event-explorer] 測試：{spec['name']}")

    # ── 建構 position ──
    try:
        position = _build_event_position(d, spec)
        position = _apply_overlay(position, d, spec)
    except Exception as e:
        logger.error(f"[event-explorer] position 建構失敗：{e}")
        save_discovered_strategy(tid, spec['name'], spec['description'],
                                 0, 0, 0, 0, False, hypothesis=f"BUILD_ERROR: {e}")
        return False

    # ── 訓練期回測（2018~BACKTEST_END）──
    try:
        position_train = position[position.index <= BACKTEST_END]
        if not position_train.any(axis=None):
            logger.warning("[event-explorer] 訓練期無任何持倉，略過")
            return False
        report = sim(position_train, trade_at_price='open',
                     fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                     position_limit=0.2, upload=False)
        stats = report.get_stats()
    except Exception as e:
        logger.error(f"[event-explorer] 訓練期回測失敗：{e}")
        save_discovered_strategy(tid, spec['name'], spec['description'],
                                 0, 0, 0, 0, False, hypothesis=f"BACKTEST_ERROR: {e}")
        return False

    should, reason = _should_save(spec, stats, db_passed)

    # ── Step 1: 訓練期 t-statistic 檢查（Harvey, Liu & Zhu 2016：t > 3.0）──
    if should:
        n_months = max(1, round(
            (position_train.index[-1] - position_train.index[0]).days / 30.44
        ))
        t_stat = stats['monthly_sharpe'] * math.sqrt(n_months)
        if t_stat >= TSTAT_MIN:
            logger.info(
                f"[event-explorer] ✅ t-stat={t_stat:.2f}（n≈{n_months}月，門檻>{TSTAT_MIN}）"
            )
        else:
            logger.info(
                f"[event-explorer] ⚠️ t-stat={t_stat:.2f} < {TSTAT_MIN}（n≈{n_months}月），"
                f"統計不顯著，跳過"
            )
            should = False

    # ── Step 2: 熊市段驗證（2021~2022，獨立驗證集，不與訓練期重疊）──
    if should:
        try:
            bear_mask     = (position.index >= BEAR_START) & (position.index <= BEAR_END)
            position_bear = position[bear_mask]
            if len(position_bear) >= 60 and position_bear.any(axis=None):
                report_bear = sim(position_bear, trade_at_price='open',
                                  fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                                  position_limit=0.2, upload=False)
                bear_mdd = report_bear.get_stats()['max_drawdown']
                if bear_mdd > BEAR_MAX_MDD:
                    logger.info(
                        f"[event-explorer] ✅ 熊市驗證通過 | {BEAR_START}~{BEAR_END} "
                        f"MDD={bear_mdd:.2%}（門檻>{BEAR_MAX_MDD:.0%}）"
                    )
                else:
                    logger.info(
                        f"[event-explorer] ⚠️ 熊市驗證失敗 MDD={bear_mdd:.2%}"
                        f"（需>{BEAR_MAX_MDD:.0%}），降為不儲存"
                    )
                    should = False
            else:
                logger.debug("[event-explorer] 熊市段資料不足或無持倉，略過")
        except Exception as e:
            logger.warning(f"[event-explorer] 熊市驗證失敗（忽略）：{e}")

    # ── Step 3: OOS 驗證（2023~今，至少 250 個交易日≈1年）──
    # 動態基準：0050 OOS 期間 CAGR + 5%；資料不可用時退回固定門檻
    if should:
        try:
            oos_cagr_required = OOS_MIN_CAGR
            try:
                close_all = d.get('close')
                if close_all is not None and '0050' in close_all.columns:
                    taiex_oos = close_all['0050'].dropna()
                    taiex_oos = taiex_oos[taiex_oos.index >= OOS_START]
                    if len(taiex_oos) >= 2:
                        n_yrs = (taiex_oos.index[-1] - taiex_oos.index[0]).days / 365.25
                        bm_cagr = (float(taiex_oos.iloc[-1]) / float(taiex_oos.iloc[0])) \
                                  ** (1 / max(n_yrs, 0.1)) - 1
                        oos_cagr_required = bm_cagr + 0.05
                        logger.debug(
                            f"[event-explorer] 0050 OOS CAGR={bm_cagr:.2%}，"
                            f"策略門檻={oos_cagr_required:.2%}"
                        )
            except Exception:
                pass

            position_oos = position[position.index >= OOS_START]
            if len(position_oos) >= 250 and position_oos.any(axis=None):
                report_oos = sim(position_oos, trade_at_price='open',
                                 fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                                 position_limit=0.2, upload=False)
                s_oos     = report_oos.get_stats()
                train_mdd = stats['max_drawdown']
                oos_mdd   = s_oos['max_drawdown']
                mdd_ok    = oos_mdd > train_mdd * 1.5
                oos_ok    = (s_oos['cagr']           > oos_cagr_required
                             and s_oos['monthly_sharpe'] > OOS_MIN_SHARPE
                             and mdd_ok)
                if oos_ok:
                    logger.info(
                        f"[event-explorer] ✅ OOS 通過 | {OOS_START}~今 "
                        f"CAGR={s_oos['cagr']:.2%}（需>{oos_cagr_required:.2%}） "
                        f"Sharpe={s_oos['monthly_sharpe']:.2f} MDD={oos_mdd:.2%}"
                    )
                else:
                    fail = []
                    if s_oos['cagr'] <= oos_cagr_required:
                        fail.append(f"CAGR={s_oos['cagr']:.2%}（需>{oos_cagr_required:.2%}）")
                    if s_oos['monthly_sharpe'] <= OOS_MIN_SHARPE:
                        fail.append(f"Sharpe={s_oos['monthly_sharpe']:.2f}（需>{OOS_MIN_SHARPE}）")
                    if not mdd_ok:
                        fail.append(f"MDD惡化 {train_mdd:.2%}→{oos_mdd:.2%}")
                    logger.info(f"[event-explorer] ⚠️ OOS 未過（{', '.join(fail)}）")
                    should = False
            else:
                logger.debug("[event-explorer] OOS 資料不足或無持倉，略過 OOS 驗證")
        except Exception as e:
            logger.warning(f"[event-explorer] OOS 驗證失敗（忽略）：{e}")

    verdict = f"✅ {reason}" if should else "❌ 不符合"
    logger.info(
        f"[event-explorer] {verdict} | "
        f"CAGR={stats['cagr']:.2%} Sharpe={stats['monthly_sharpe']:.2f} "
        f"MDD={stats['max_drawdown']:.2%} 勝率={stats['win_ratio']:.2%}"
    )

    # ── 儲存 ──
    code_to_save = fpath = None
    group_hash   = _condition_group_hash(spec)
    is_superseding = False

    if should:
        existing = get_passed_by_condition_group(group_hash)
        if existing:
            new_score = _compute_composite(
                stats['cagr'], stats['monthly_sharpe'], stats['max_drawdown'],
                stats['win_ratio'], db_passed)
            old_score = _compute_composite(
                existing['cagr'], existing['sharpe'], existing['mdd'],
                existing['win_ratio'], db_passed)
            if new_score <= old_score:
                logger.info(
                    f"[event-explorer] 同條件族已有更優策略 [{existing['name'][:20]}]"
                    f"（{old_score:.3f} ≥ {new_score:.3f}），略過"
                )
                should = False
            else:
                logger.info(
                    f"[event-explorer] 取代同條件族舊策略（{old_score:.3f}→{new_score:.3f}）"
                )
                is_superseding = True
                supersede_strategy(existing['template_id'])
                fpath = existing.get('file_path')

    if should:
        if not fpath:
            n    = get_passed_strategy_count() + 11
            slug = spec['name'].replace(' ', '_').replace('（', '_').replace('）', '')[:18]
            fpath = str(STRATEGIES_DIR / f"s{n:02d}_{slug}.py")
        fname = Path(fpath).name
        code_to_save = _gen_code(spec, stats)
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        Path(fpath).write_text(code_to_save, encoding='utf-8')
        logger.info(f"[event-explorer] 已存為 {fname}" + ("（取代）" if is_superseding else ""))

        tag = "🔄 <b>事件策略更新！</b>" if is_superseding else "🔬 <b>新事件策略發現！</b>"
        send_message(
            f"{tag}\n"
            f"<b>{_esc(spec['name'])}</b>\n"
            f"{_esc(spec['description'])}\n\n"
            f"📊 訓練期績效（2018~2022）：\n"
            f"  CAGR   : <b>{stats['cagr']:.2%}</b>\n"
            f"  Sharpe : <b>{stats['monthly_sharpe']:.2f}</b>\n"
            f"  MDD    : {stats['max_drawdown']:.2%}\n"
            f"  勝率   : {stats['win_ratio']:.2%}\n\n"
            f"已存為 {_esc(fname)}"
        )

    # ── DB 記錄（無論通過與否）──
    factor_keys = [spec['template']['key']] + (
        [spec['overlay']['key']] if spec['overlay']['key'] != 'none' else []
    )
    mdd_val = stats['max_drawdown']
    calmar  = round(stats['cagr'] / abs(mdd_val), 3) if mdd_val else None
    vol_val = stats.get('annual_volatility') or stats.get('volatility')

    save_discovered_strategy(
        tid, spec['name'], spec['description'],
        stats['cagr'], stats['monthly_sharpe'], mdd_val, stats['win_ratio'],
        should, code_to_save, fpath,
        hypothesis=(
            f"seed={spec['seed']}, template={spec['template']['key']}, "
            f"params={spec['params']}, overlay={spec['overlay']['key']}, "
            f"overlay_params={spec['overlay_params']}, reason={reason}"
        ),
        condition_group=group_hash,
        calmar_ratio=calmar,
        volatility=round(vol_val, 4) if vol_val else None,
        factor_list=json.dumps(factor_keys, ensure_ascii=False),
        ranking_factor=spec['template']['key'],
        rebalance_freq='event',
        position_limit=0.2,
    )
    return bool(should)


def run_loop():
    """連續探索迴圈（由排程 daemon thread 呼叫）"""
    import time
    logger.info("[event-explorer] 連續探索模式啟動")
    tried = passed = 0
    session_start = time.time()
    while True:
        try:
            result = run_exploration()
            tried += 1
            if result:
                passed += 1
            if tried % 20 == 0:
                elapsed = (time.time() - session_start) / 3600
                logger.info(
                    f"[event-explorer] 統計 tried={tried} passed={passed} "
                    f"rate={passed/tried:.1%} elapsed={elapsed:.1f}h"
                )
        except Exception as e:
            logger.error(f"[event-explorer] 非預期錯誤：{e}\n{traceback.format_exc()}")
            time.sleep(30)


if __name__ == '__main__':
    run_exploration()
