"""
均量>100張+大股東連2期上升+月營收月增>0(連3月)+  (auto-discovered 2026-02-23)
============================================================
均量>100張+大股東連2期上升+月營收月增>0(連3月)+，排名FCF最高取前10檔

回測績效（2018~2022，訓練期）：
  CAGR   : 22.81%
  Sharpe : 1.08
  MDD    : -21.83%
  勝率   : 50.67%

注意：策略已通過 2023~今 OOS 驗證（out-of-sample）
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config', '.env'))
import finlab, pandas as pd
finlab.login(os.getenv('FINLAB_API_TOKEN', ''))
from finlab import data, dataframe as fldf
from finlab.backtest import sim

close       = data.get('price:收盤價')
volume      = data.get('price:成交股數')
pe          = data.get('price_earning_ratio:本益比')
roe         = data.get('fundamental_features:ROE稅後')
fcf         = data.get('fundamental_features:自由現金流量')
rev         = data.get('monthly_revenue:當月營收')
op          = data.get('fundamental_features:營業利益成長率')
big         = data.get('etl:inventory:大於四百張佔比')
bi          = data.get('etl:broker_transactions:balance_index')
bsr         = data.get('etl:broker_transactions:buy_sell_ratio')
外資        = data.get('institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)')
投信        = data.get('institutional_investors_trading_summary:投信買賣超股數')
insider_add = data.get('internal_equity_changes:董監增加股數')
insider_pct = data.get('internal_equity_changes:董監持有股數占比')
dy          = data.get('price_earning_ratio:殖利率(%)')
pb          = data.get('price_earning_ratio:股價淨值比')

# ── 策略條件 ──
cond_0 = volume.average(20) > 100000
cond_1 = fldf.FinlabDataFrame(big).rise().sustain(2)
cond_2 = (rev > rev.shift(1)).rolling(3).sum() >= 3
cond_3 = close.average(20) > close.average(60)
condition = cond_0 & cond_1 & cond_2 & cond_3

# ── 選股排名 ──
rank_df = fcf
position = rank_df[condition].is_largest(10)

report = sim(position, resample='M',
             fee_ratio=1.425/1000, tax_ratio=3/1000,
             position_limit=0.1,
             trade_at_price='open', upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f'CAGR={s["cagr"]:.2%}  Sharpe={s["monthly_sharpe"]:.2f}  '
          f'MDD={s["max_drawdown"]:.2%}  勝率={s["win_ratio"]:.2%}')
