"""
策略04｜60日動能 + ROE正值
============================
條件：近60日漲幅前30名 + ROE > 0（獲利中的公司）
頻率：月頻再平衡
回測：CAGR +17.6%  Sharpe 0.59  MDD -47.6%  勝率 44.7%
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

position = ((close / close.shift(60)).is_largest(30) & (roe > 0))
report = sim(position, resample="M", fee_ratio=1.425/1000, tax_ratio=3/1000, upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f"CAGR={s['cagr']:.2%}  Sharpe={s['monthly_sharpe']:.2f}  "
          f"MDD={s['max_drawdown']:.2%}  勝率={s['win_ratio']:.2%}")
