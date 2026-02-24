"""
策略03｜PEG（低本益比/成長率 + 月營收確認）⭐
=====================================================
條件：低PEG（PE/營業利益成長率）
      + 近3月均月營收 > 近12月均月營收 × 1.1（月營收加速成長）
      + 月營收未明顯衰退
停損：10%
頻率：月（依月營收發布日）
回測：CAGR +24.3%  Sharpe 0.88  MDD -37.8%  勝率 51.8%
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))

from finlab import data
from finlab.backtest import sim

pe            = data.get("price_earning_ratio:本益比")
rev           = data.get("monthly_revenue:當月營收")
op_growth     = data.get("fundamental_features:營業利益成長率")

rev_ma3  = rev.average(3)
rev_ma12 = rev.average(12)
peg      = pe / op_growth

cond1 = rev_ma3 / rev_ma12 > 1.1       # 月營收加速成長
cond2 = rev / rev.shift(1) > 0.9       # 當月不明顯衰退
result = peg * (cond1 & cond2)
position = result[result > 0].is_smallest(10).reindex(rev.index_str_to_date().index, method="ffill")

report = sim(position, fee_ratio=1.425/1000, tax_ratio=3/1000, stop_loss=0.1, upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
