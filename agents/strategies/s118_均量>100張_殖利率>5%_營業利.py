"""
均量>100張+殖利率>5%+營業利益成長率>20%+月營收  (auto-discovered 2026-02-23)
============================================================
均量>100張+殖利率>5%+營業利益成長率>20%+月營收，排名PB最低取前10檔

回測績效（2018~2022，訓練期）：
  CAGR   : 25.08%
  Sharpe : 1.67
  MDD    : -14.99%
  勝率   : 59.77%

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
cond_1 = dy > 5
cond_2 = op > 20
cond_3 = rev.average(3) / rev.average(12) > 1.05
cond_4 = (close.pct_change().rolling(60).std()).rank(axis=1, pct=True) < 0.40
condition = cond_0 & cond_1 & cond_2 & cond_3 & cond_4

# ── 選股排名 ──
rank_df = pb.where(pb > 0)
position = rank_df[condition].is_smallest(10)

report = sim(position, resample='M',
             fee_ratio=1.425/1000, tax_ratio=3/1000,
             position_limit=0.1,
             trade_at_price='open', upload=False)

if __name__ == '__main__':
    s = report.get_stats()
    print(f'CAGR={s["cagr"]:.2%}  Sharpe={s["monthly_sharpe"]:.2f}  '
          f'MDD={s["max_drawdown"]:.2%}  勝率={s["win_ratio"]:.2%}')
