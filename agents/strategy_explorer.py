"""
strategy_explorer.py  ──  參數化隨機策略探索引擎（連續執行版）
=================================================================
由排程啟動 daemon thread 後持續探索，跑完一個立刻跑下一個。

儲存門檻：
  加權複合分數 ≥ 0.65（各指標正規化後加權平均）：
    CAGR   × 1.0  Sharpe × 1.0  MDD × 1.5（加重）  勝率 × 0.5
  正規化基準：地板值（MIN_FLOOR）為 0 分，現有最佳（S_BENCHMARKS + DB）為 1 分

最低地板（低於此直接淘汰，不進入複合分數計算）：
  CAGR   > 6%  Sharpe > 0.35  MDD > -60%

因子庫包含：FCF、ROE、PE、月營收、MA趨勢、創新高、動能、
           鯨魚籌碼、分點balance、外資投信、董監增持、
           營業利益成長、低波動等，閾值全部可隨機化。
"""
import os, sys, json, hashlib, random, re, textwrap, traceback, math
from html import escape as _esc
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))

import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data, dataframe as fldf
from finlab.backtest import sim
import pandas as pd
from loguru import logger

from core.database import (save_discovered_strategy, is_strategy_tried,
                            get_passed_strategy_count, get_all_discovered_strategies,
                            get_passed_by_condition_group, supersede_strategy,
                            init_db)
from core.notifier import send_message

# ── 品質判斷 ──────────────────────────────────────────────────
STRATEGIES_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / 'strategies'

# ── 回測期間設定 ──────────────────────────────────────────────────────
# 訓練期：~ BACKTEST_END（2020-12-31）
# 熊市驗證（獨立）：2021-01-01 ~ 2022-12-31（不與訓練期重疊）
# OOS 驗證（最終）：OOS_START ~ 今（嚴格獨立）
BACKTEST_END = '2020-12-31'   # 訓練期截至 2020，確保 2021~2022 為獨立驗證集
OOS_START    = '2023-01-01'

# ── 熊市段驗證（2021~2022，完全獨立於訓練期）───────────────────────
# 訓練期結束（2020）後、OOS 前（2023），作為獨立空頭驗證集
# 台股大盤 2021~2022 最大跌幅約 -25%，是近年最顯著的空頭段
BEAR_START   = '2021-01-01'
BEAR_END     = '2022-12-31'
BEAR_MAX_MDD = -0.50

# ── OOS 通過門檻 ─────────────────────────────────────────────────────
OOS_MIN_CAGR   = 0.15   # 動態基準（0050 OOS CAGR + 5%）不可用時的 fallback
OOS_MIN_SHARPE = 0.80   # 提高至 0.80（原 0.60），要求更穩定的風險調整收益

# ── 訓練期統計顯著性門檻（Harvey, Liu & Zhu 2016）───────────────────
TSTAT_MIN = 3.0   # t-statistic = monthly_sharpe × sqrt(n_months) 須 > 3.0

# ── 交易成本設定 ─────────────────────────────────────────────────────
FEE_RATIO = 1.425 / 1000   # 0.1425%（標準手續費，單邊）
TAX_RATIO = 3.0   / 1000   # 0.300%（交易稅，賣出時）

# 最低地板：低於此直接淘汰
MIN_FLOOR = dict(cagr=0.06, sharpe=0.35, mdd=-0.60)

# s01~s10 硬編碼最佳績效（作為各指標天花板下限，確保新系統有高標準起點）
_S_BENCH_FLOOR = dict(
    max_cagr   = 0.416,   # s07 處置股策略
    max_sharpe = 1.04,    # s07
    max_mdd    = -0.290,  # s09 集保鯨魚（最低MDD）
    max_win    = 0.518,   # s03 PEG
)


def _get_benchmarks() -> dict:
    """動態讀取 DB 中已通過策略的最佳績效，以硬編碼下限為底。
    DB 策略越優秀，自動標竿越高，確保只有真正超越現有最佳才能入選。
    """
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
        'max_cagr':   max(_S_BENCH_FLOOR['max_cagr'],   db_cagr   or 0),
        'max_sharpe': max(_S_BENCH_FLOOR['max_sharpe'],  db_sharpe or 0),
        'max_mdd':    min(_S_BENCH_FLOOR['max_mdd'],     db_mdd    or -1),
        'max_win':    max(_S_BENCH_FLOOR['max_win'],     db_win    or 0),
    }


# 向後相容別名（部分外部程式碼可能直接引用 S_BENCHMARKS）
S_BENCHMARKS = _S_BENCH_FLOOR


# 複合分數門檻（各指標正規化後加權平均，≥ 此值才入選）
COMPOSITE_THRESHOLD = 0.75   # 提高門檻以緩解多重假設問題（原 0.65）

# 資料快取（每日重新載入一次）
_data_cache: dict = {'data': None, 'date': None}

# ═══════════════════════════════════════════════════════════════
# 因子庫定義
# 每個因子是一個 dict：
#   key        : 因子名稱（英文，用於 hash）
#   label      : 人類可讀標籤（中文）
#   params     : 可隨機化的參數 {param_name: [候選值, ...]}
#   build      : callable(d, params) → boolean DataFrame
#   requires   : 此因子需要哪些 data key（用於 namespace 組裝）
# ═══════════════════════════════════════════════════════════════
FACTOR_POOL = [

    # ─── 流動性（必選，幾乎所有策略都需要） ───
    {'key': 'vol_ok', 'label': '均量>{vol_k}張',
     'params': {'vol_k': [300, 500, 1000]},   # 移除 100/200：避免小型股換倉衝擊成本過高
     'build': lambda d, p: d['volume'].average(20) > p['vol_k'] * 1000,
     'requires': ['volume']},

    # ─── FCF ───
    {'key': 'fcf_pos', 'label': 'FCF>0',
     'params': {},
     'build': lambda d, p: d['fcf'] > 0,
     'requires': ['fcf']},

    {'key': 'fcf_rank', 'label': 'FCF前{pct}%',
     'params': {'pct': [30, 40, 50]},
     'build': lambda d, p: d['fcf'].rank(axis=1, pct=True) > (1 - p['pct']/100),
     'requires': ['fcf']},

    # ─── ROE ───
    {'key': 'roe_min', 'label': 'ROE>{roe}%',
     'params': {'roe': [8, 10, 12, 15, 18, 20]},
     'build': lambda d, p: d['roe'] > p['roe'],
     'requires': ['roe']},

    # ─── PE ───
    {'key': 'pe_max', 'label': 'PE<{pe}',
     'params': {'pe': [12, 15, 18, 20, 25]},
     'build': lambda d, p: (d['pe'] > 0) & (d['pe'] < p['pe']),
     'requires': ['pe']},

    # ─── 月營收 ───
    {'key': 'rev_grow', 'label': '月營收年增>0',
     'params': {},
     'build': lambda d, p: d['rev'] > d['rev'].shift(12),
     'requires': ['rev']},

    {'key': 'rev_accel', 'label': '月營收加速>{accel}',
     'params': {'accel': [1.05, 1.1, 1.15, 1.2]},
     'build': lambda d, p: d['rev'].average(3) / d['rev'].average(12) > p['accel'],
     'requires': ['rev']},

    {'key': 'rev_mom', 'label': '月營收月增>0(連{n}月)',
     'params': {'n': [2, 3]},
     'build': lambda d, p: (d['rev'] > d['rev'].shift(1)).rolling(p['n']).sum() >= p['n'],
     'requires': ['rev']},

    # ─── MA 趨勢 ───
    {'key': 'ma_bull', 'label': '股價>MA{ma}',
     'params': {'ma': [20, 60, 120]},
     'build': lambda d, p: d['close'] > d['close'].average(p['ma']),
     'requires': ['close']},

    {'key': 'ma_cross', 'label': 'MA20>MA{long}',
     'params': {'long': [60, 120]},
     'build': lambda d, p: d['close'].average(20) > d['close'].average(p['long']),
     'requires': ['close']},

    # ─── 創新高 ───
    {'key': 'high_n', 'label': '創{n}日新高',
     'params': {'n': [20, 40, 60, 90, 120]},
     'build': lambda d, p: d['close'] >= d['close'].rolling(p['n']).max().shift(1),
     'requires': ['close']},

    # ─── 動能 ───
    {'key': 'momentum', 'label': '{n}日漲幅>0',
     'params': {'n': [20, 40, 60, 90]},
     'build': lambda d, p: d['close'] > d['close'].shift(p['n']),
     'requires': ['close']},

    # ─── 集保鯨魚 ───
    {'key': 'whale_conc', 'label': '大股東佔比>{pct}%',
     'params': {'pct': [15, 20, 25, 30]},
     'build': lambda d, p: d['big'] > p['pct'],
     'requires': ['big']},

    {'key': 'whale_rise', 'label': '大股東連{n}期上升',
     'params': {'n': [2, 3]},
     'build': lambda d, p: fldf.FinlabDataFrame(d['big']).rise().sustain(p['n']),
     'requires': ['big']},

    # ─── 分點籌碼 ───
    {'key': 'bi_high', 'label': 'BalanceIndex>{bi}',
     'params': {'bi': [0.50, 0.52, 0.55]},
     'build': lambda d, p: d['bi'] > p['bi'],
     'requires': ['bi']},

    {'key': 'bi_rise', 'label': 'BI連升({n}日内{k}次)',
     'params': {'n': [3, 5], 'k': [2, 3]},
     'build': lambda d, p: (d['bi'] > d['bi'].shift(1)).rolling(p['n']).sum() >= p['k'],
     'requires': ['bi']},

    {'key': 'bsr_high', 'label': 'BSR>{bsr}',
     'params': {'bsr': [1.02, 1.05, 1.10]},
     'build': lambda d, p: d['bsr'] > p['bsr'],
     'requires': ['bsr']},

    # ─── 法人 ───
    {'key': 'foreign_buy', 'label': '外資{w}日淨買入',
     'params': {'w': [3, 5, 10]},
     'build': lambda d, p: d['外資'].rolling(p['w']).sum() > 0,
     'requires': ['外資']},

    {'key': 'trust_buy', 'label': '投信{w}日淨買入',
     'params': {'w': [3, 5, 10]},
     'build': lambda d, p: d['投信'].rolling(p['w']).sum() > 0,
     'requires': ['投信']},

    {'key': 'inst_both', 'label': '外資投信{w}日雙買',
     'params': {'w': [3, 5, 10]},
     'build': lambda d, p: (
         (d['外資'].rolling(p['w']).sum() > 0) &
         (d['投信'].rolling(p['w']).sum() > 0)
     ),
     'requires': ['外資', '投信']},

    # ─── 董監 ───
    {'key': 'insider_buy', 'label': '董監增持>0',
     'params': {},
     'build': lambda d, p: d['insider_add'] > 0,
     'requires': ['insider_add']},

    {'key': 'insider_skin', 'label': '董監持股>{pct}%',
     'params': {'pct': [5, 10, 15]},
     'build': lambda d, p: d['insider_pct'] > p['pct'],
     'requires': ['insider_pct']},

    # ─── 營業利益 ───
    {'key': 'op_grow', 'label': '營業利益成長率>{op}%',
     'params': {'op': [0, 10, 20]},
     'build': lambda d, p: d['op'] > p['op'],
     'requires': ['op']},

    # ─── 低波動 ───
    {'key': 'low_vol', 'label': '{n}日波動率後{pct}%',
     'params': {'n': [20, 60], 'pct': [30, 40]},
     'build': lambda d, p: (
         (d['close'].pct_change().rolling(p['n']).std())
         .rank(axis=1, pct=True) < (p['pct']/100)
     ),
     'requires': ['close']},

    # ─── 殖利率（Dividend Yield） ───
    {'key': 'div_yield', 'label': '殖利率>{y}%',
     'params': {'y': [3, 4, 5, 6, 8]},
     'build': lambda d, p: d['dy'] > p['y'],
     'requires': ['dy']},

    # ─── PB 股價淨值比（Price-to-Book） ───
    {'key': 'pb_low', 'label': 'PB<{pb}',
     'params': {'pb': [1.0, 1.5, 2.0, 2.5, 3.0]},
     'build': lambda d, p: (d['pb'] > 0) & (d['pb'] < p['pb']),
     'requires': ['pb']},
]

# ── 因子語義家族（每個策略每家族最多取一個因子）────────────────────
# ROE/FCF/op_grow 同屬「品質/獲利能力」家族（Fama-French RMW 因子）
# PE 單獨為「估值」家族（Fama-French HML 因子）
# low_vol 獨立為「波動率」家族（與動能/趨勢家族分開）
FACTOR_FAMILIES = {
    'valuation':     ['pe_max'],
    'quality':       ['roe_min', 'fcf_pos', 'fcf_rank', 'op_grow'],
    'revenue':       ['rev_grow', 'rev_accel', 'rev_mom'],
    'technical':     ['ma_bull', 'ma_cross', 'high_n', 'momentum'],
    'risk':          ['low_vol'],
    'whale':         ['whale_conc', 'whale_rise'],
    'broker':        ['bi_high', 'bi_rise', 'bsr_high'],
    'institutional': ['foreign_buy', 'trust_buy', 'inst_both'],
    'insider':       ['insider_buy', 'insider_skin'],
    'income':        ['div_yield'],   # 股息殖利率（Fama-French HML 股利維度）
    'book_value':    ['pb_low'],      # 帳面淨值估值（Fama-French HML PB 維度）
}
_KEY_TO_FAMILY = {k: fam for fam, keys in FACTOR_FAMILIES.items() for k in keys}

# ── 排名依據選項 ──────────────────────────────────────────────
RANK_OPTIONS = [
    {'key': 'fcf',         'label': 'FCF最高',        'requires': ['fcf']},
    {'key': 'roe',         'label': 'ROE最高',        'requires': ['roe']},
    {'key': 'rev',         'label': '月營收最高',      'requires': ['rev']},
    {'key': 'big',         'label': '大股東佔比最高',  'requires': ['big']},
    {'key': 'bi',          'label': 'BalanceIndex最高', 'requires': ['bi']},
    {'key': '外資',        'label': '外資買超最多',    'requires': ['外資']},
    {'key': 'peg',         'label': 'PEG最低(PE/op)', 'requires': ['pe', 'op']},
    {'key': 'momentum60',  'label': '60日動能最強',   'requires': ['close']},
    {'key': 'op',          'label': '營業利益成長最高', 'requires': ['op']},
    {'key': 'dy',          'label': '殖利率最高',      'requires': ['dy']},
    {'key': 'pb',          'label': 'PB最低',          'requires': ['pb']},
]

TOP_N_OPTIONS    = [10, 15, 20, 25, 30]   # 最少 10 檔，單股上限降至 10%（原含 5 檔）
RESAMPLE_OPTIONS = ['M', 'M', 'M', 'M']   # 全部月頻（季頻樣本量不足，統計顯著性低）


def _sample_strategy(seed: int) -> dict:
    """根據隨機種子，從因子庫抽取一組策略規格"""
    rng = random.Random(seed)

    # 1. vol_ok 固定必選
    vol_factor = FACTOR_POOL[0]
    vol_params = {k: rng.choice(v) for k, v in vol_factor['params'].items()}

    # 2. 從其餘因子中隨機選 2~4 個，每個語義家族最多 1 個
    # 偏向 3 個因子（避免 2 個過於稀疏、4 個過度過濾）
    candidates = FACTOR_POOL[1:]
    n_extra = rng.choices([2, 3, 4], weights=[2, 5, 3], k=1)[0]
    shuffled = rng.sample(candidates, len(candidates))   # shuffle（種子確定性）
    chosen = []
    used_families: set = set()
    for f in shuffled:
        family = _KEY_TO_FAMILY.get(f['key'], f['key'])
        if family not in used_families:
            chosen.append(f)
            used_families.add(family)
            if len(chosen) >= n_extra:
                break

    # 3. 為每個因子隨機化參數
    factors = [{'factor': vol_factor, 'params': vol_params}]
    for f in chosen:
        params = {k: rng.choice(v) for k, v in f['params'].items()}
        factors.append({'factor': f, 'params': params})

    # 4. 排名依據：從 RANK_OPTIONS 隨機選一個
    rank = rng.choice(RANK_OPTIONS)
    top_n = rng.choice(TOP_N_OPTIONS)
    resample = rng.choice(RESAMPLE_OPTIONS)

    # 5. 產生人類可讀標籤
    labels = []
    for item in factors:
        label = item['factor']['label']
        for k, v in item['params'].items():
            label = label.replace(f'{{{k}}}', str(v))
        labels.append(label)

    name = '+'.join(labels)[:30]
    rank_label = rank['label']

    return {
        'seed': seed,
        'factors': factors,
        'rank': rank,
        'top_n': top_n,
        'resample': resample,
        'name': name,
        'description': f"{name}，排名{rank_label}取前{top_n}檔",
    }


def _combo_hash(spec: dict) -> str:
    """產生策略規格的唯一 hash，用於去重"""
    canonical = json.dumps({
        'factors': [
            {'key': item['factor']['key'], 'params': item['params']}
            for item in spec['factors']
        ],
        'rank': spec['rank']['key'],
        'top_n': spec['top_n'],
        'resample': spec['resample'],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _condition_group_hash(spec: dict) -> str:
    """只用 factor keys + rank key 產生 hash（忽略參數值），用於同條件族去重"""
    canonical = json.dumps({
        'factor_keys': sorted(item['factor']['key'] for item in spec['factors']),
        'rank': spec['rank']['key'],
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _compute_composite(cagr: float, sharpe: float, mdd: float,
                        win_ratio: float, db_passed: list) -> float:
    """計算複合分數（同 _is_best_ever 的正規化公式，用於比較兩個策略）"""
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


def _get_data() -> dict:
    """含每日快取的資料載入（避免連續跑時重複下載）"""
    from datetime import date
    today = str(date.today())
    if _data_cache['date'] != today or _data_cache['data'] is None:
        _data_cache['data'] = _load_data()
        _data_cache['date'] = today
    return _data_cache['data']


def _load_data() -> dict:
    """一次載入所有 FinLab 資料，單個資料源失敗不影響其他（對可選因子容錯）"""
    logger.info("[explorer] 載入 FinLab 資料...")
    _KEYS = {
        'close':       'price:收盤價',
        'volume':      'price:成交股數',
        'pe':          'price_earning_ratio:本益比',
        'roe':         'fundamental_features:ROE稅後',
        'fcf':         'fundamental_features:自由現金流量',
        'rev':         'monthly_revenue:當月營收',
        'op':          'fundamental_features:營業利益成長率',
        'big':         'etl:inventory:大於四百張佔比',
        'bi':          'etl:broker_transactions:balance_index',
        'bsr':         'etl:broker_transactions:buy_sell_ratio',
        '外資':        'institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)',
        '投信':        'institutional_investors_trading_summary:投信買賣超股數',
        'insider_add': 'internal_equity_changes:董監增加股數',
        'insider_pct': 'internal_equity_changes:董監持有股數占比',
        'dy':          'price_earning_ratio:殖利率(%)',    # 殖利率（%）- 注意有(%)
        'pb':          'price_earning_ratio:股價淨值比',   # PB 股價淨值比
    }
    result = {}
    for var_key, finlab_key in _KEYS.items():
        try:
            result[var_key] = data.get(finlab_key)
        except Exception as e:
            logger.warning(f"[explorer] 資料源 {finlab_key!r} 不可用：{e}，相關因子將略過")
            result[var_key] = None
    return result


def _build_position(d: dict, spec: dict):
    """根據策略規格建立 position DataFrame"""
    combined = None
    for item in spec['factors']:
        cond = item['factor']['build'](d, item['params'])
        combined = cond if combined is None else (combined & cond)

    rank_key = spec['rank']['key']
    if rank_key == 'peg':
        # 只取正成長（op > 0）且 PE 有意義（pe > 0）的股票計算 PEG
        op_safe = d['op'].where(d['op'] > 0.001)
        rank_df = d['pe'].where(d['pe'] > 0) / op_safe
        return rank_df[combined].is_smallest(spec['top_n'])   # 低PEG優先
    elif rank_key == 'momentum60':
        rank_df = d['close'].pct_change(60)
        return rank_df[combined].is_largest(spec['top_n'])
    elif rank_key == 'pb':
        rank_df = d['pb'].where(d['pb'] > 0)
        return rank_df[combined].is_smallest(spec['top_n'])   # 低PB優先
    else:
        rank_df = d[rank_key]
        return rank_df[combined].is_largest(spec['top_n'])


def _dynamic_floor(db_passed: list) -> dict:
    """以現有通過策略的 P10 為動態地板（不低於靜態地板）。

    DB 策略品質提升 → 地板自動升高。
    P10 = 新策略需超過最差的 10%。需 ≥5 個通過策略才啟用。
    """
    if len(db_passed) < 5:
        return MIN_FLOOR
    n = len(db_passed)
    p10_idx = max(0, n // 10)
    cagrs  = sorted(r['cagr']   for r in db_passed)
    sharps = sorted(r['sharpe'] for r in db_passed)
    mdds   = sorted(r['mdd']    for r in db_passed)   # 負值，升序 = 最差（最低）在前
    return {
        'cagr':   max(MIN_FLOOR['cagr'],   cagrs[p10_idx]),
        'sharpe': max(MIN_FLOOR['sharpe'], sharps[p10_idx]),
        'mdd':    min(MIN_FLOOR['mdd'],    mdds[-(p10_idx + 1)]),  # 取 MDD 最差的 P10
    }


def _passes_floor(s: dict, floor: dict = MIN_FLOOR) -> bool:
    """最低地板：低於此絕對不存"""
    return (s['cagr']          > floor['cagr']
            and s['monthly_sharpe'] > floor['sharpe']
            and s['max_drawdown']   > floor['mdd'])


def _is_best_ever(s: dict, db_passed: list) -> bool:
    """加權複合分數 ≥ COMPOSITE_THRESHOLD。
    各指標以地板值為 0、現有最佳（DB動態 + 硬編碼下限）為 1 正規化後加權平均：
      CAGR × 1.0  Sharpe × 1.0  MDD × 1.5（加重）  勝率 × 0.5
    """
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
    ) / 4.0  # total weight = 1.0 + 1.0 + 1.5 + 0.5

    logger.debug(f"[explorer] 複合分數 {score:.3f} (門檻 {COMPOSITE_THRESHOLD})")
    return score >= COMPOSITE_THRESHOLD


def _should_save(spec: dict, s: dict, db_passed: list) -> tuple[bool, str]:
    """
    回傳 (should_save, reason)
    reason: 'best_ever' | None
    """
    floor = _dynamic_floor(db_passed)
    if not _passes_floor(s, floor):
        logger.debug(
            f"[explorer] 未過動態地板 CAGR>{floor['cagr']:.1%} "
            f"Sharpe>{floor['sharpe']:.2f} MDD>{floor['mdd']:.1%}"
        )
        return False, None
    if _is_best_ever(s, db_passed):
        return True, 'best_ever'
    return False, None


def _is_diverse_enough(spec: dict, db_passed: list) -> bool:
    """確保新策略因子家族組合與現有策略有足夠差異。
    計算 Jaccard 相似度時排除 vol_ok（所有策略共有，稀釋差異性）。
    若超過 25% 的現有策略與新策略的有效因子家族重疊度 > 50%，視為過度相似，跳過。
    策略數 < 10 時不限制（鼓勵早期探索）。
    """
    if len(db_passed) < 10:
        return True

    # 排除 vol_ok 家族（'vol_ok'），只比較有語義意義的因子家族
    new_families = frozenset(
        _KEY_TO_FAMILY.get(item['factor']['key'], item['factor']['key'])
        for item in spec['factors']
        if item['factor']['key'] != 'vol_ok'
    )

    high_overlap = 0
    valid = 0
    for r in db_passed:
        h = r.get('hypothesis', '')
        if not h or 'factors=' not in h:
            continue
        m = re.search(r"factors=\[([^\]]+)\]", h)
        if not m:
            continue
        valid += 1
        ex_keys = [k.strip().strip("'\"") for k in m.group(1).split(',')]
        # 同樣排除 vol_ok
        ex_families = frozenset(
            _KEY_TO_FAMILY.get(k, k) for k in ex_keys
            if k and k != 'vol_ok'
        )
        if not ex_families or not new_families:
            continue
        overlap = len(new_families & ex_families) / max(len(new_families | ex_families), 1)
        if overlap > 0.50:   # 更嚴格：排除 vol_ok 後閾值從 0.60 降至 0.50
            high_overlap += 1

    if valid == 0:
        return True
    return (high_overlap / valid) < 0.25


def _gen_code(spec: dict, stats: dict) -> str:
    """產生獨立可執行的策略 .py 檔（逐行拼接，避免 textwrap.dedent 縮排問題）"""
    # 條件程式碼（無前置縮排，模組層級）
    cond_lines = []
    cond_vars = []
    for i, item in enumerate(spec['factors']):
        f = item['factor']
        p = item['params']
        var = f'cond_{i}'
        cond_vars.append(var)
        line = _factor_to_code(f['key'], p)
        if line:
            cond_lines.append(f"{var} = {line}")
        else:
            cond_lines.append(f"# {f['key']} (params={p})")

    combine_str = ' & '.join(cond_vars) if cond_vars else 'pd.DataFrame(True, index=close.index, columns=close.columns)'

    # 動態 position_limit（等權分配，上限 20%）
    pos_limit = round(min(0.20, 1.0 / spec['top_n']), 4)

    rank_key = spec['rank']['key']
    if rank_key == 'peg':
        rank_lines = [
            "op_safe = op.where(op > 0.001)",
            "rank_df = pe.where(pe > 0) / op_safe",
        ]
        pos_line = f"position = rank_df[condition].is_smallest({spec['top_n']})"
    elif rank_key == 'momentum60':
        rank_lines = ["rank_df = close.pct_change(60)"]
        pos_line   = f"position = rank_df[condition].is_largest({spec['top_n']})"
    elif rank_key == 'pb':
        rank_lines = ["rank_df = pb.where(pb > 0)"]
        pos_line   = f"position = rank_df[condition].is_smallest({spec['top_n']})"
    else:
        rank_lines = [f"rank_df = {rank_key}"]
        pos_line   = f"position = rank_df[condition].is_largest({spec['top_n']})"

    # 月頻資料需用 index_str_to_date() 將字串索引（如 "2024-M1"）轉為公告日（2024-01-10）
    # 避免回測系統誤用月底日期而產生 look-ahead bias（FinLab 官方建議）
    _monthly_cond_keys = {'rev_grow', 'rev_accel', 'rev_mom', 'op_grow',
                          'whale_conc', 'whale_rise'}
    _monthly_rank_keys = {'rev', 'op', 'big', 'peg'}
    uses_monthly = (
        any(item['factor']['key'] in _monthly_cond_keys for item in spec['factors'])
        or rank_key in _monthly_rank_keys
    )
    reindex_line = (
        "position = position.reindex(rev.index_str_to_date().index, method='ffill')"
        if uses_monthly else ""
    )

    cond_block   = '\n'.join(cond_lines)
    rank_block   = '\n'.join(rank_lines)
    resample_str = spec['resample']
    top_n        = spec['top_n']
    name_str     = spec['name']
    desc_str     = spec['description']
    date_str     = datetime.now().strftime('%Y-%m-%d')
    cagr_str     = f'{stats["cagr"]:.2%}'
    sharpe_str   = f'{stats["monthly_sharpe"]:.2f}'
    mdd_str      = f'{stats["max_drawdown"]:.2%}'
    win_str      = f'{stats["win_ratio"]:.2%}'

    return (
        f'"""\n'
        f'{name_str}  (auto-discovered {date_str})\n'
        f'============================================================\n'
        f'{desc_str}\n'
        f'\n'
        f'回測績效（~2020，訓練期）：\n'
        f'  CAGR   : {cagr_str}\n'
        f'  Sharpe : {sharpe_str}\n'
        f'  MDD    : {mdd_str}\n'
        f'  勝率   : {win_str}\n'
        f'\n'
        f'注意：策略已通過 t-stat>3.0 + 2021~2022 熊市驗證 + 2023~今 OOS 驗證\n'
        f'"""\n'
        f'import os, sys\n'
        f"sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        f'from dotenv import load_dotenv\n'
        f"load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env'))\n"
        f'import finlab, pandas as pd\n'
        f"finlab.login(os.getenv('FINLAB_API_TOKEN', ''))\n"
        f'from finlab import data, dataframe as fldf\n'
        f'from finlab.backtest import sim\n'
        f'\n'
        f"close       = data.get('price:收盤價')\n"
        f"volume      = data.get('price:成交股數')\n"
        f"pe          = data.get('price_earning_ratio:本益比')\n"
        f"roe         = data.get('fundamental_features:ROE稅後')\n"
        f"fcf         = data.get('fundamental_features:自由現金流量')\n"
        f"rev         = data.get('monthly_revenue:當月營收')\n"
        f"op          = data.get('fundamental_features:營業利益成長率')\n"
        f"big         = data.get('etl:inventory:大於四百張佔比')\n"
        f"bi          = data.get('etl:broker_transactions:balance_index')\n"
        f"bsr         = data.get('etl:broker_transactions:buy_sell_ratio')\n"
        f"外資        = data.get('institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)')\n"
        f"投信        = data.get('institutional_investors_trading_summary:投信買賣超股數')\n"
        f"insider_add = data.get('internal_equity_changes:董監增加股數')\n"
        f"insider_pct = data.get('internal_equity_changes:董監持有股數占比')\n"
        f"dy          = data.get('price_earning_ratio:殖利率(%)')\n"
        f"pb          = data.get('price_earning_ratio:股價淨值比')\n"
        f'\n'
        f'# ── 策略條件 ──\n'
        f'{cond_block}\n'
        f'condition = {combine_str}\n'
        f'\n'
        f'# ── 選股排名 ──\n'
        f'{rank_block}\n'
        f'{pos_line}\n'
        + (f'{reindex_line}\n' if reindex_line else '')
        + f'\n'
        f"report = sim(position, resample='{resample_str}',\n"
        f'             fee_ratio=1.425/1000, tax_ratio=3/1000,\n'
        f'             position_limit={pos_limit},\n'
        f"             trade_at_price='open', upload=False)\n"
        f'\n'
        f"if __name__ == '__main__':\n"
        f'    s = report.get_stats()\n'
        f'    print(f\'CAGR={{s["cagr"]:.2%}}  Sharpe={{s["monthly_sharpe"]:.2f}}  \'\n'
        f'          f\'MDD={{s["max_drawdown"]:.2%}}  勝率={{s["win_ratio"]:.2%}}\')\n'
    )


def _factor_to_code(key: str, params: dict) -> str:
    """將因子鍵與參數轉為 Python 程式碼字串"""
    p = params
    table = {
        'vol_ok':      f"volume.average(20) > {p.get('vol_k', 300) * 1000}",
        'fcf_pos':     "fcf > 0",
        'fcf_rank':    f"fcf.rank(axis=1, pct=True) > {1 - p.get('pct',40)/100:.2f}",
        'roe_min':     f"roe > {p.get('roe', 10)}",
        'pe_max':      f"(pe > 0) & (pe < {p.get('pe', 20)})",
        'rev_grow':    "rev > rev.shift(12)",
        'rev_accel':   f"rev.average(3) / rev.average(12) > {p.get('accel', 1.1)}",
        'rev_mom':     f"(rev > rev.shift(1)).rolling({p.get('n',3)}).sum() >= {p.get('n',3)}",
        'ma_bull':     f"close > close.average({p.get('ma', 20)})",
        'ma_cross':    f"close.average(20) > close.average({p.get('long', 60)})",
        'high_n':      f"close >= close.rolling({p.get('n', 60)}).max().shift(1)",
        'momentum':    f"close > close.shift({p.get('n', 60)})",
        'whale_conc':  f"big > {p.get('pct', 20)}",
        'whale_rise':  f"fldf.FinlabDataFrame(big).rise().sustain({p.get('n', 2)})",
        'bi_high':     f"bi > {p.get('bi', 0.52)}",
        'bi_rise':     f"(bi > bi.shift(1)).rolling({p.get('n',3)}).sum() >= {p.get('k',2)}",
        'bsr_high':    f"bsr > {p.get('bsr', 1.05)}",
        'foreign_buy': f"外資.rolling({p.get('w', 5)}).sum() > 0",
        'trust_buy':   f"投信.rolling({p.get('w', 5)}).sum() > 0",
        'inst_both':   f"(外資.rolling({p.get('w',5)}).sum() > 0) & (投信.rolling({p.get('w',5)}).sum() > 0)",
        'insider_buy': "insider_add > 0",
        'insider_skin':f"insider_pct > {p.get('pct', 10)}",
        'op_grow':     f"op > {p.get('op', 0)}",
        'low_vol':     (f"(close.pct_change().rolling({p.get('n',20)}).std())"
                        f".rank(axis=1, pct=True) < {p.get('pct',30)/100:.2f}"),
        'div_yield':   f"dy > {p.get('y', 4)}",
        'pb_low':      f"(pb > 0) & (pb < {p.get('pb', 1.5)})",
    }
    return table.get(key, f"# unknown factor: {key}")


def run_exploration():
    """執行一次策略探索（連續跑時每次呼叫此函式）"""
    init_db()

    # 取所有 DB 中已通過的策略（動態更新最佳標竿）
    all_records = get_all_discovered_strategies()
    db_passed   = [r for r in all_records if r['passed']]

    # 載入資料（有快取，每日只載一次）
    try:
        d = _get_data()
    except Exception as e:
        logger.error(f"[explorer] FinLab 資料載入失敗：{e}")
        return False

    # 找一個未嘗試過且因子組合夠多樣的組合（最多試 50 次 seed）
    base_seed = int(datetime.now().timestamp())
    spec = tid = None
    for attempt in range(50):
        candidate     = _sample_strategy(base_seed + attempt)
        candidate_tid = _combo_hash(candidate)
        if is_strategy_tried(candidate_tid):
            continue
        if not _is_diverse_enough(candidate, db_passed):
            logger.debug(f"[explorer] 種子 {attempt} 因子重疊度過高，跳過")
            continue
        # 確認所有因子與排名所需資料均可用（防止 None 資料造成回測失敗）
        missing = [
            req for item in candidate['factors']
            for req in item['factor'].get('requires', [])
            if d.get(req) is None
        ] + [
            req for req in candidate['rank'].get('requires', [])
            if d.get(req) is None
        ]
        if missing:
            logger.debug(f"[explorer] 種子 {attempt} 缺少資料 {missing}，跳過")
            continue
        spec = candidate
        tid  = candidate_tid
        break

    if spec is None:
        logger.info("[explorer] 50 次種子均已試過或多樣性不足，略過本輪")
        return False

    logger.info(f"[explorer] 測試：{spec['name']} | {spec['description']}")

    # 回測（僅使用訓練期 2018~BACKTEST_END，嚴格分離 OOS）
    try:
        position = _build_position(d, spec)
        position_train = position[position.index <= BACKTEST_END]
        if len(position_train) < 6:
            logger.warning(f"[explorer] 訓練期資料不足（{len(position_train)} 期），略過")
            return False
        # 動態 position_limit：等權分配，上限 20%（避免 top_n 小時資金閒置）
        pos_limit = round(min(0.20, 1.0 / spec['top_n']), 4)
        report = sim(position_train, resample=spec['resample'],
                     fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                     position_limit=pos_limit,
                     trade_at_price='open', upload=False)
        stats = report.get_stats()
    except Exception as e:
        logger.error(f"[explorer] 回測失敗：{e}")
        save_discovered_strategy(tid, spec['name'], spec['description'],
                                 0, 0, 0, 0, False,
                                 hypothesis=f"ERROR: {e}")
        return False

    should, reason = _should_save(spec, stats, db_passed)

    # ── Step 1: 訓練期 t-statistic 檢查（Harvey, Liu & Zhu 2016：t > 3.0）──
    if should:
        n_months = max(1, round(
            (pd.Timestamp(BACKTEST_END) - position_train.index[0]).days / 30.44
        ))
        t_stat = stats['monthly_sharpe'] * math.sqrt(n_months)
        if t_stat >= TSTAT_MIN:
            logger.info(f"[explorer] ✅ t-stat={t_stat:.2f}（n={n_months}月，門檻>{TSTAT_MIN}）")
        else:
            logger.info(
                f"[explorer] ⚠️ t-stat={t_stat:.2f} < {TSTAT_MIN}（n={n_months}月），"
                f"統計不顯著，跳過"
            )
            should = False

    # ── Step 2: 熊市段驗證（2021~2022，獨立驗證集，不與訓練期重疊）──
    if should:
        try:
            bear_mask     = (position.index >= BEAR_START) & (position.index <= BEAR_END)
            position_bear = position[bear_mask]
            if len(position_bear) >= 3:
                report_bear = sim(
                    position_bear, resample=spec['resample'],
                    fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                    position_limit=pos_limit,
                    trade_at_price='open', upload=False
                )
                bear_mdd = report_bear.get_stats()['max_drawdown']
                if bear_mdd > BEAR_MAX_MDD:
                    logger.info(
                        f"[explorer] ✅ 熊市驗證通過 | {BEAR_START}~{BEAR_END} "
                        f"MDD={bear_mdd:.2%}（門檻>{BEAR_MAX_MDD:.0%}）"
                    )
                else:
                    logger.info(
                        f"[explorer] ⚠️ 熊市驗證失敗 MDD={bear_mdd:.2%}"
                        f"（需>{BEAR_MAX_MDD:.0%}），降為不儲存"
                    )
                    should = False
            else:
                logger.debug(f"[explorer] 熊市段期數不足（{len(position_bear)}<3），略過熊市驗證")
        except Exception as _bear_e:
            logger.warning(f"[explorer] 熊市驗證失敗（忽略）：{_bear_e}")

    # ── Step 3: OOS 驗證（2023~今，嚴格獨立）──
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
                            f"[explorer] 0050 OOS CAGR={bm_cagr:.2%}，"
                            f"策略門檻={oos_cagr_required:.2%}"
                        )
            except Exception:
                pass  # benchmark 取得失敗，使用固定門檻

            position_oos = position[position.index >= OOS_START]
            min_periods  = 12   # 月頻至少 12 個月
            if len(position_oos) >= min_periods:
                report_oos = sim(position_oos, resample=spec['resample'],
                                 fee_ratio=FEE_RATIO, tax_ratio=TAX_RATIO,
                                 position_limit=pos_limit,
                                 trade_at_price='open', upload=False)
                s_oos     = report_oos.get_stats()
                train_mdd = stats['max_drawdown']
                oos_mdd   = s_oos['max_drawdown']
                mdd_ok    = oos_mdd > train_mdd * 1.5
                oos_ok    = (s_oos['cagr']           > oos_cagr_required
                             and s_oos['monthly_sharpe'] > OOS_MIN_SHARPE
                             and mdd_ok)
                if oos_ok:
                    logger.info(
                        f"[explorer] ✅ OOS 通過 | {OOS_START}~今 "
                        f"CAGR={s_oos['cagr']:.2%}（需>{oos_cagr_required:.2%}） "
                        f"Sharpe={s_oos['monthly_sharpe']:.2f} MDD={oos_mdd:.2%}"
                    )
                else:
                    fail_reasons = []
                    if s_oos['cagr'] <= oos_cagr_required:
                        fail_reasons.append(
                            f"CAGR={s_oos['cagr']:.2%}（需>{oos_cagr_required:.2%}）"
                        )
                    if s_oos['monthly_sharpe'] <= OOS_MIN_SHARPE:
                        fail_reasons.append(
                            f"Sharpe={s_oos['monthly_sharpe']:.2f}（需>{OOS_MIN_SHARPE}）"
                        )
                    if not mdd_ok:
                        fail_reasons.append(f"MDD惡化 訓練{train_mdd:.2%}→OOS{oos_mdd:.2%}")
                    logger.info(
                        f"[explorer] ⚠️ OOS 未過（{', '.join(fail_reasons)}），降為不儲存"
                    )
                    should = False
            else:
                logger.debug(
                    f"[explorer] OOS 期數不足（{len(position_oos)} < {min_periods}），略過 OOS 檢查"
                )
        except Exception as _oos_e:
            logger.warning(f"[explorer] OOS 驗證失敗（忽略）：{_oos_e}")

    verdict = f"✅ {reason}" if should else "❌ 不符合"
    logger.info(
        f"[explorer] {verdict} | "
        f"CAGR={stats['cagr']:.2%} Sharpe={stats['monthly_sharpe']:.2f} "
        f"MDD={stats['max_drawdown']:.2%} 勝率={stats['win_ratio']:.2%}"
    )

    code_to_save = fpath = None
    group_hash = _condition_group_hash(spec)
    is_superseding = False

    if should:
        # ── 同條件族比對：factor keys + rank 相同算同族 ──────────────
        existing = get_passed_by_condition_group(group_hash)
        if existing:
            new_score = _compute_composite(
                stats['cagr'], stats['monthly_sharpe'], stats['max_drawdown'], stats['win_ratio'],
                db_passed)
            old_score = _compute_composite(
                existing['cagr'], existing['sharpe'], existing['mdd'], existing['win_ratio'],
                db_passed)
            if new_score <= old_score:
                logger.info(
                    f"[explorer] 同條件族已有更優策略 [{existing['name'][:20]}]"
                    f"（{old_score:.3f} ≥ {new_score:.3f}），略過儲存"
                )
                should = False
            else:
                logger.info(
                    f"[explorer] 取代同條件族舊策略 [{existing['name'][:20]}]"
                    f"（{old_score:.3f} → {new_score:.3f}）"
                )
                is_superseding = True
                supersede_strategy(existing['template_id'])
                fpath = existing.get('file_path')   # 沿用原檔案路徑

    if should:
        if not fpath:
            n    = get_passed_strategy_count() + 11
            slug = spec['name'].replace(' ', '_').replace('+', '_')[:18]
            fpath = str(STRATEGIES_DIR / f"s{n:02d}_{slug}.py")
        fname = Path(fpath).name
        code_to_save = _gen_code(spec, stats)
        STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
        Path(fpath).write_text(code_to_save, encoding='utf-8')
        logger.info(f"[explorer] 已存為 {fname}" + ("（取代舊版）" if is_superseding else ""))

        if is_superseding:
            msg = (
                f"🔄 <b>策略更新！</b>（同條件族取代）\n"
                f"<b>{_esc(spec['name'])}</b>\n"
                f"{_esc(spec['description'])}\n\n"
                f"📊 訓練期績效（2018~2022）：\n"
                f"  CAGR   : <b>{stats['cagr']:.2%}</b>\n"
                f"  Sharpe : <b>{stats['monthly_sharpe']:.2f}</b>\n"
                f"  MDD    : {stats['max_drawdown']:.2%}\n"
                f"  勝率   : {stats['win_ratio']:.2%}\n\n"
                f"取代：{_esc(existing['name'][:30])}\n已存為 {_esc(fname)}"
            )
        else:
            msg = (
                f"🔬 <b>新策略發現！</b>（打敗所有現有策略）\n"
                f"<b>{_esc(spec['name'])}</b>\n"
                f"{_esc(spec['description'])}\n\n"
                f"📊 訓練期績效（2018~2022）：\n"
                f"  CAGR   : <b>{stats['cagr']:.2%}</b>\n"
                f"  Sharpe : <b>{stats['monthly_sharpe']:.2f}</b>\n"
                f"  MDD    : {stats['max_drawdown']:.2%}\n"
                f"  勝率   : {stats['win_ratio']:.2%}\n\n"
                f"已存為 {_esc(fname)}"
            )
        send_message(msg)

    factor_keys  = [f['factor']['key'] for f in spec['factors']]
    mdd_val      = stats['max_drawdown']
    calmar       = round(stats['cagr'] / abs(mdd_val), 3) if mdd_val and mdd_val != 0 else None
    vol_val      = stats.get('annual_volatility') or stats.get('volatility')
    pos_limit_val = round(min(0.20, 1.0 / spec['top_n']), 4)

    save_discovered_strategy(
        tid, spec['name'], spec['description'],
        stats['cagr'], stats['monthly_sharpe'], mdd_val, stats['win_ratio'],
        should, code_to_save, fpath,
        hypothesis=f"seed={spec['seed']}, factors={factor_keys}, reason={reason}",
        condition_group=group_hash,
        calmar_ratio=calmar,
        volatility=round(vol_val, 4) if vol_val else None,
        factor_list=json.dumps(factor_keys, ensure_ascii=False),
        ranking_factor=spec['rank']['key'],
        rebalance_freq=spec['resample'],
        position_limit=pos_limit_val,
    )
    return should


def run_loop():
    """連續探索迴圈（由排程 daemon thread 呼叫）"""
    import time
    logger.info("[explorer] 連續探索模式啟動")
    tried = passed = 0
    session_start = time.time()
    while True:
        try:
            result = run_exploration()
            tried += 1
            if result:
                passed += 1
            if tried % 50 == 0:
                elapsed = (time.time() - session_start) / 3600
                logger.info(
                    f"[explorer] 統計 tried={tried} passed={passed} "
                    f"rate={passed/tried:.1%} elapsed={elapsed:.1f}h"
                )
        except Exception as e:
            logger.error(f"[explorer] 非預期錯誤：{e}\n{traceback.format_exc()}")
            time.sleep(30)   # 發生錯誤才等 30s，正常情況立刻繼續


if __name__ == '__main__':
    run_exploration()
