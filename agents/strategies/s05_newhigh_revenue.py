"""
策略05｜創250日新高 + 月營收年增率 > 0
=========================================
條件：收盤價創近250日新高
      + 月營收年增率 > 0（月營收同比成長）
      + 20日均量 > 300萬股（流動性）
取前20大（按收盤價市值排序）
頻率：月頻再平衡
回測：CAGR +14.1%  Sharpe 0.55  MDD -53.1%  勝率 47.0%
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

close  = data.get("price:收盤價")
rev_rf = data.get("monthly_revenue:去年同月增減(%)")
volume = data.get("price:成交股數")

new_high = (close == close.rolling(250).max())
rev_pos  = rev_rf > 0           # FinLab 自動對齊月報 index
liquid   = volume.average(20) > 3_000_000

position = close[new_high & rev_pos & liquid].is_largest(20)
report = sim(position, resample="M", fee_ratio=1.425/1000, tax_ratio=3/1000, upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
