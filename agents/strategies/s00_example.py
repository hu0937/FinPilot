"""
策略範本｜請依此格式撰寫自訂策略
=====================================================
說明：本平台的策略以 FinLab 回測引擎為基礎，每個策略檔應包含：
  1. 用 data.get() 取得所需資料
  2. 組合篩選條件（布林矩陣）
  3. 排名並建立 position（FinlabDataFrame）
  4. 用 sim() 執行回測

執行方式：
    python agents/strategies/s00_example.py

參考文件：
    FinLab API  https://doc.finlab.tw/
    FinMind API https://finmindtrade.com/
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))

import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

# ── 1. 取得資料 ────────────────────────────────────────────────────────────
close  = data.get('price:收盤價')
volume = data.get('price:成交股數')
rev    = data.get('monthly_revenue:當月營收')
# 更多資料集請參考 FinLab 文件

# ── 2. 篩選條件 ────────────────────────────────────────────────────────────
cond_liquidity = volume.average(20) > 300_000          # 均量 > 300 張
cond_revenue   = rev.average(3) / rev.average(12) > 1.05  # 月營收加速成長
condition      = cond_liquidity & cond_revenue

# ── 3. 排名選股 ────────────────────────────────────────────────────────────
rank_df  = close.pct_change(60)                        # 60 日動能排名
position = rank_df[condition].is_largest(10)           # 取最強 10 檔

# ── 4. 執行回測 ────────────────────────────────────────────────────────────
report = sim(
    position,
    resample='M',                    # 月頻再平衡
    fee_ratio=1.425 / 1000,          # 標準手續費 0.1425%
    tax_ratio=3 / 1000,              # 股票交易稅 0.3%
    position_limit=0.1,              # 單檔上限 10%
    trade_at_price='open',
    upload=False,
)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
