"""
s10_fcf_quality.py  ──  自由現金流品質策略
============================================
概念：
  自由現金流（FCF）充裕 + ROE 高 + 月營收成長
  = 高品質企業，長期複利效果顯著。

  FCF 策略不加「趨勢多頭」過濾，反而效果更好，
  因為具備強 FCF 的公司在下跌時仍值得持有，
  等待均值回歸的時間通常較短。

核心邏輯：
  - 自由現金流 > 0（不燒錢）
  - FCF 橫截面前40%（相對高FCF）
  - ROE > 10%（資本效率優良）
  - 月營收年增率 > 0（仍在成長）
  - 日均量 > 300張（基本流動性）
  取 FCF 最高前10檔，月頻再平衡

回測績效（2018~2026，8年）：
  CAGR   : +17.11%
  Sharpe : 0.68
  MDD    : -38.48%
  勝率   : 50.67%  ← 所有基本面策略中唯一超過50%

特色：
  勝率突破50%，Sharpe 0.68 僅次於處置股策略（1.04）和PEG（0.88）。
  集中持有前10檔，換手率低，適合長期持有型投資人。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

# ── 資料載入 ─────────────────────────────────────────────────
close  = data.get('price:收盤價')
volume = data.get('price:成交股數')
fcf    = data.get('fundamental_features:自由現金流量')  # 單位：百萬
roe    = data.get('fundamental_features:ROE稅後')
rev    = data.get('monthly_revenue:當月營收')

# ── 因子計算 ─────────────────────────────────────────────────
vol_ok   = volume.average(20) > 300_000           # 流動性：均量 > 300張
fcf_pos  = fcf > 0                                # FCF 為正
fcf_rank = fcf.rank(axis=1, pct=True) > 0.6      # FCF 橫截面前40%
roe_good = roe > 10                               # ROE > 10%
rev_grow = rev > rev.shift(12)                    # 月營收 > 去年同月（年增率 > 0）

# ── 選股 ─────────────────────────────────────────────────────
condition = vol_ok & fcf_pos & fcf_rank & roe_good & rev_grow

# 取 FCF 最高前10檔
position = fcf[condition].is_largest(10)

# ── 回測 ─────────────────────────────────────────────────────
report = sim(
    position,
    resample='M',
    fee_ratio=1.425/1000, tax_ratio=3/1000,
    position_limit=0.15,
    trade_at_price='open',
    upload=False
)

if __name__ == '__main__':
    stats = report.get_stats()
    print('=== 自由現金流品質策略 回測結果 ===')
    print(f'  CAGR        : {stats["cagr"]:.2%}')
    print(f'  Sharpe      : {stats["monthly_sharpe"]:.2f}')
    print(f'  Max Drawdown: {stats["max_drawdown"]:.2%}')
    print(f'  勝率        : {stats["win_ratio"]:.2%}')
