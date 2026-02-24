"""
策略06｜創250日新高 + ROE > 15%
==================================
條件：收盤價創近250日新高
      + ROE稅後 > 15%（高獲利能力）
取前20大（按收盤價排序）
頻率：月頻再平衡
回測：CAGR +12.2%  Sharpe 0.40  MDD -56.1%  勝率 46.8%
注意：ROE 為季報，FinLab & 運算子自動對齊
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

close = data.get("price:收盤價")
roe   = data.get("fundamental_features:ROE稅後")

new_high = (close == close.rolling(250).max())
roe_ok   = roe > 15             # FinLab & 自動對齊季報 index

position = close[new_high & roe_ok].is_largest(20)
report = sim(position, resample="M", fee_ratio=1.425/1000, tax_ratio=3/1000, upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
