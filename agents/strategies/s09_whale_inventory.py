"""
策略09｜集保鯨魚策略
==========================================
概念：
  追蹤集保庫存資料中「大股東（>400張）」的持股佔比。
  當大戶持續吸籌、股價仍維持多頭，代表聰明錢正在布局。

核心邏輯：
  - 大股東佔比 > 25%（已高度集中）
  - 大股東佔比連2期上升（持續吸籌中）
  - 股價站上 MA20（趨勢多頭確認）
  - 日均量 > 300張（基本流動性）

回測：CAGR +8.2%  Sharpe 0.51  MDD -29.0%  勝率 48.2%

⚠️  此策略依賴 FinLab ETL 私有資料集：
      etl:inventory:大於四百張佔比
    需要 FinLab VIP 訂閱才能存取，本 repo 不提供實作程式碼。
    請參考 FinLab 文件：https://doc.finlab.tw/
"""

raise NotImplementedError(
    "此策略需要 FinLab ETL VIP 資料（etl:inventory），本 repo 不提供實作。\n"
    "請參考 https://doc.finlab.tw/ 了解 ETL 資料存取方式。"
)
