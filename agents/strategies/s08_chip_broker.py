"""
策略08｜籌碼分點集中策略
===========================================
概念：
  利用 FinLab ETL 資料偵測主力分點積極買進、籌碼持續集中的股票，
  結合均線多頭趨勢進場。

核心指標：
  - balance_index   ：籌碼平衡指數（>0.5 偏買方，越高越集中）
  - buy_sell_ratio  ：主力分點買賣比（>1 表示買方主導）

回測：CAGR +11.2%  Sharpe 0.51  MDD -35.5%  勝率 45.9%

⚠️  此策略依賴 FinLab ETL 私有資料集：
      etl:broker_transactions:balance_index
      etl:broker_transactions:buy_sell_ratio
    需要 FinLab VIP 訂閱才能存取，本 repo 不提供實作程式碼。
    請參考 FinLab 文件：https://doc.finlab.tw/
"""

raise NotImplementedError(
    "此策略需要 FinLab ETL VIP 資料（etl:broker_transactions），本 repo 不提供實作。\n"
    "請參考 https://doc.finlab.tw/ 了解 ETL 資料存取方式。"
)
