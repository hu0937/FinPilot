"""
策略02｜營收動能（月營收創歷史新高）
=======================================
條件：近3個月合計營收 = 近24個月最大值 + 10日均量 > 300張 + 月營收年增率 > 0
停損：30%  單檔上限：10%
頻率：月（依月營收發布日）
回測：CAGR +8.8%  Sharpe 0.34  MDD -59.8%  勝率 45.2%
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

rev    = data.get("monthly_revenue:當月營收")
rev_rf = data.get("monthly_revenue:去年同月增減(%)")
vol    = data.get("price:成交股數") / 1000

rev_recent_3 = rev.rolling(3).sum()
vol_avg      = vol.average(10)

cond1 = (rev_recent_3 / rev_recent_3.rolling(24, min_periods=12).max()) == 1
cond2 = vol_avg > 300
result = rev_rf * (cond1 & cond2)
position = result[result > 0].is_largest(10).reindex(rev.index_str_to_date().index, method="ffill")

report = sim(position, stop_loss=0.3, position_limit=0.1, fee_ratio=1.425/1000, tax_ratio=3/1000, upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
