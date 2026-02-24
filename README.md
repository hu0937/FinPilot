# FinPilot — 台股量化分析平台

台股 + 美股日線級量化分析平台，整合 Telegram Bot 推播、Claude AI 自然語言查詢，以及策略研究 Agent。

> **免責聲明**：本專案僅供個人學習與研究使用，所有策略回測結果均為歷史模擬，不代表未來績效。
> 本專案不構成任何投資建議，使用者須自行承擔所有投資決策與交易損益之責任。
> 資料來源（FinLab、FinMind、yfinance）之準確性及時效性由各資料提供方負責，本專案不對資料錯誤或延遲負責。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)

---


## 目錄

- [系統架構](#系統架構)
- [目錄結構](#目錄結構)
- [服務說明](#服務說明)
- [安裝與設定](#安裝與設定)
- [啟動與管理](#啟動與管理)
- [Telegram Bot 指令](#telegram-bot-指令)
- [Claude AI 自然語言查詢](#claude-ai-自然語言查詢)
- [策略研究 Agent](#策略研究-agent)
- [持倉掃描器](#持倉掃描器手動執行)
- [排程時間表](#排程時間表)
- [資料庫 Schema](#資料庫-schema)
- [常用操作](#常用操作)
- [故障排除](#故障排除)


---

## 系統架構

```
                    ┌─────────────────────────────────────────┐
                    │               FinPilot/                 │
                    │                                         │
  FinMind (台股) ──▶│  data_fetcher.py  ──▶  SQLite DB        │
  yfinance (美股) ──▶│                        quant.db         │
                    │         ▼                    ▼           │
                    │  APScheduler          price_history      │
                    │  (台股15:35 / 美股06:00) watchlist       │
                    │                        position_lots     │
                    │                               ▼          │
                    │  Telegram Bot ◀──── notifier.py         │
                    │  (指令 + Claude AI)                      │
                    └─────────────────────────────────────────┘

  終端執行：
  agents/strategy_backtest.py
  ├── Strategy Agent (claude --print)  ← 需 Claude Code（Pro/Max 訂閱）
  ├── Backtest Agent (finlab.sim)      ← 本地執行
  └── Fix Agent (claude --print)      ← 自動修正錯誤
```

### 資料流

1. **排程觸發** → `data_fetcher.py` 向 FinMind/yfinance 抓當日收盤資料
2. **寫入** `price_history`，計算 MA5/MA20/均量/N日高
3. **查詢** Bot 指令或 Claude AI 自然語言可隨時查詢

---

## 目錄結構

```
FinPilot/
├── README.md                          ← 本文件
├── requirements.txt                   ← Python 套件清單
├── LICENSE                            ← MIT License
├── config/
│   ├── settings.py                    ← 全域設定（讀取 .env）
│   ├── .env.example                   ← 金鑰範本（複製為 .env 並填入）
│   └── .env                           ← API 金鑰（需自行填入，已加入 .gitignore）
├── data/
│   ├── quant.db                       ← SQLite 主資料庫
│   └── logs/                          ← 各服務 log
│       ├── bot.log
│       └── scheduler.log
├── core/
│   ├── database.py                    ← SQLite CRUD（thread-local）
│   ├── data_fetcher.py                ← FinMind + yfinance 資料抓取
│   └── notifier.py                    ← Telegram 推播
├── scheduler/
│   └── job_runner.py                  ← APScheduler 排程主程式
├── bot/
│   ├── telegram_bot.py                ← Bot 指令處理 + NLP 路由
│   └── claude_handler.py              ← Claude AI 自然語言處理
├── agents/
│   ├── strategy_backtest.py           ← 策略研究 Agent（終端執行）
│   ├── strategy_explorer.py           ← 自動策略探索引擎（月頻因子，daemon，連續執行）
│   ├── event_strategy_explorer.py     ← 事件驅動型策略探索引擎（daemon，連續執行）
│   ├── strategy_monitor.py            ← 每日 20:00 事件警示+處置股+PEG+持倉健診+Regime/Kelly推播
│   ├── position_scanner.py            ← 持倉掃描器（手動執行，依策略命中數排名）
│   ├── run_all_backtests.py           ← 全策略回測驗證工具（一鍵跑 s01~s118 含動態地板）
│   ├── strategies/                    ← 策略檔
│   │   ├── s00_example.py             策略撰寫範本
│   │   ├── s01~s06、s10               手動設計策略（含完整實作）
│   │   ├── s07_disposal_stock.py      處置股策略（⚠️ FinLab 社群作品，僅含說明）
│   │   ├── s08_chip_broker.py         籌碼分點策略（⚠️ 需 ETL VIP，僅含說明）
│   │   ├── s09_whale_inventory.py     集保鯨魚策略（⚠️ 需 ETL VIP，僅含說明）
│   │   ├── s62/s102/s111/s116/s118 🤖 自動探索策略（嚴格重新驗證後保留 5 檔）
│   │   │   ├── s111 CAGR +45.7%  Sharpe 1.31  MDD -28.8%  勝率 52.9%  ✅
│   │   │   ├── s116 CAGR +30.2%  Sharpe 1.32  MDD -21.8%  勝率 50.8%  ✅
│   │   │   ├── s118 CAGR +25.1%  Sharpe 1.67  MDD -15.0%  勝率 59.8%  ✅
│   │   │   ├── s62  CAGR +24.3%  Sharpe 1.46  MDD -20.0%  勝率 57.5%  ✅
│   │   │   └── s102 CAGR +20.5%  Sharpe 1.34  MDD -25.8%  勝率 54.8%  ✅
│   │   └── （exp_* 實驗性策略未包含於此 repo）
│   └── reports/                       ← 回測報告（Markdown）
└── scripts/
    ├── start.sh                       ← 一鍵啟動所有服務
    ├── stop.sh                        ← 停止所有服務
    └── pids/                          ← 各服務 PID 檔（自動生成）
```

---

## 服務說明

| 服務 | 腳本 | 說明 |
|------|------|------|
| Telegram Bot | `bot/telegram_bot.py` | 指令 + Claude AI 自然語言查詢 |
| APScheduler | `scheduler/job_runner.py` | 排程抓資料更新 |

### Python 套件（主要）

```
finlab
FinMind（https://github.com/FinMind/FinMind）
yfinance
python-telegram-bot
apscheduler
fastapi + uvicorn
anthropic
pandas, numpy, ta, loguru ...
```

詳細版本需求請見 `requirements.txt`。

---

## 安裝與設定

### 1. Clone 並安裝套件

```bash
git clone https://github.com/hu0937/FinPilot.git
cd FinPilot
pip install -r requirements.txt
```

### 2. 填入 API 金鑰

```bash
cp config/.env.example config/.env
```

編輯 `config/.env`：

```ini
# FinMind Token（免費申請：https://finmindtrade.com/）
FINMIND_TOKEN=your_token_here

# Telegram Bot Token（向 @BotFather 申請）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

# 你的 Telegram Chat ID（向 @userinfobot 查詢）
TELEGRAM_CHAT_ID=987654321

# Anthropic API Key（Telegram Claude AI 查詢功能需要）
ANTHROPIC_API_KEY=sk-ant-...

# FinLab Token（策略研究 Agent 需要）
FINLAB_API_TOKEN=your_finlab_token
```

### 3. 取得必要金鑰

**FinMind Token：**
1. 前往 https://finmindtrade.com/ 註冊
2. 登入後在個人頁面取得 API Token

**Telegram Bot Token：**
1. 在 Telegram 搜尋 `@BotFather`，傳送 `/newbot`

**Telegram Chat ID：**
1. 在 Telegram 搜尋 `@userinfobot`，傳送任意訊息

**FinLab Token：**
1. 前往 https://www.finlab.finance/ 取得（Free 每日 500 MB）

### 4. 設定 Claude AI 模型（選填）

`config/.env` 中可調整：

```ini
# Claude AI 模型
CLAUDE_MODEL=claude-sonnet-4-6   # 可切換為 claude-opus-4-6 等
```

---

## 啟動與管理

### 一鍵啟動

```bash
bash scripts/start.sh
```

### 停止所有服務

```bash
bash scripts/stop.sh
```

### 查看 Log

```bash
tail -f data/logs/scheduler.log
tail -f data/logs/bot.log
```

---

## Telegram Bot 指令

### 管理觀察清單

| 指令 | 說明 | 範例 |
|------|------|------|
| `/add <代碼> <市場>` | 新增追蹤 | `/add 2330 TW` |
| `/del <代碼> <市場>` | 移除追蹤 | `/del AAPL US` |
| `/list` | 顯示全部清單 | `/list` |
| `/list TW` | 只顯示台股 | `/list TW` |

### 查詢狀態

| 指令 | 說明 |
|------|------|
| `/status` | 系統狀態（排程器、清單數） |

### 持倉管理

| 指令 | 說明 | 範例 |
|------|------|------|
| `/pos` | 查詢全部持倉部位（含損益%） | `/pos` |
| `/pos buy <代碼> <市> <量> <均價> [日期]` | 買入（FIFO新增lot） | `/pos buy 2330 TW 1 500.5` |
| `/pos sell <代碼> <市> <量>` | 賣出（FIFO消耗） | `/pos sell 2330 TW 0.5` |
| `/pos lots <代碼> <市>` | 查看 lot 明細 | `/pos lots 2330 TW` |

- 數量單位統一為**股**
- 成本計算採 **先進先出（FIFO）**

### 分析工具

| 指令 | 說明 | 範例 |
|------|------|------|
| `/price <代碼> <TW\|US>` | 個股收盤價、趨勢（多頭/空頭/盤整）、距60日高、量能 | `/price 2330 TW` |
| `/risk` | 持倉集中度分析（台美比例、各部位佔比、最大集中度） | `/risk` |

---

## Claude AI 自然語言查詢

Telegram Bot 已整合 Claude AI，直接輸入自然語言查詢，無需記指令。

### 設定

在 `config/.env` 中填入 `ANTHROPIC_API_KEY`。

### 問法範例

```
幫我看一下 NVDA 最近的技術面
我剛買了 2330 100股，均價 950
目前持倉有多少？
我的持倉風險如何？集中度有沒有問題？
幫我推薦低風險的策略
```

### 可用工具

| 工具 | 說明 |
|------|------|
| `get_platform_summary` | 追蹤數統計 |
| `get_watchlist` | 觀察清單 |
| `get_stock_price` | 個股收盤價、趨勢判斷（多頭/空頭/盤整）、距60日高、量能狀態 |
| `get_positions` | 查詢持倉（FIFO彙總） |
| `get_position_risk` | 持倉風險分析（集中度、台美比例） |
| `get_strategy_recommendation` | 依目標推薦前3策略（高報酬/低風險/高勝率/高夏普） |

> 買賣操作請使用 Bot 指令 `/pos buy` 與 `/pos sell`，AI 不執行交易。

---

## 策略研究 Agent

使用 FinLab 回測引擎，透過 Claude Code 自動生成策略並回測。

### 執行方式

**在終端機**（不是在 Claude Code session 內）：

```bash
python agents/strategy_backtest.py
```

### 需要

- `config/.env` 中設定 `FINLAB_API_TOKEN`
- 已安裝並登入 Claude Code（可自由選擇方案，支援 Pro 或 Max 訂閱）

### 運作流程

```
用戶輸入條件
    ↓
Strategy Agent（claude --print）
  → 根據 FinLab API 知識生成策略程式碼
    ↓
Backtest Agent（本地執行 finlab.sim()）
  → 執行回測，顯示 CAGR / Sharpe / MDD / 勝率
    ↓
Fix Agent（claude --print，失敗才觸發）
  → 自動修正錯誤，最多重試 2 次
```

### 輸入範例

```
條件> 月營收年增率連3個月 > 20%，取最強10檔
條件> 只看股價與量能，60日動能 + 量能擴張，月頻再平衡
條件> ROE > 15% 且 本益比最低20檔，含停損8%
```

### 已驗證策略（`agents/strategies/`）

#### 手動策略（s01~s10）

| 檔案 | 策略 | CAGR | Sharpe | MDD | 狀態 |
|------|------|------|--------|-----|------|
| s07_disposal_stock.py | 處置股策略 | **+41.6%** | **1.04** | -56.3% | ⚠️ FinLab 社群作品，僅含說明 |
| s03_peg.py ⭐ | PEG 低本益比/成長率 | +24.3% | 0.88 | -37.8% | ✅ 含完整實作 |
| s04_momentum_roe.py | 60日動能 + ROE > 0 | +17.6% | 0.59 | -47.6% | ✅ 含完整實作 |
| s10_fcf_quality.py ✨ | 自由現金流品質（FCF+ROE+營收，前10） | +17.1% | 0.68 | -38.5% | ✅ 含完整實作 |
| s01_new_high.py | 創250日新高 | +16.8% | 0.55 | -58.2% | ✅ 含完整實作 |
| s05_newhigh_revenue.py | 創新高 + 月營收年增率 > 0 | +14.1% | 0.55 | -53.1% | ✅ 含完整實作 |
| s06_newhigh_roe.py | 創新高 + ROE > 15% | +12.2% | 0.40 | -56.1% | ✅ 含完整實作 |
| s08_chip_broker.py | 籌碼分點集中 | +11.2% | 0.51 | -35.5% | ⚠️ 需 FinLab ETL VIP，僅含說明 |
| s02_revenue_momentum.py | 月營收創歷史新高 | +8.8% | 0.34 | -59.8% | ✅ 含完整實作 |
| s09_whale_inventory.py | 集保鯨魚（大股東>400張佔比上升） | +8.2% | 0.51 | -29.0% | ⚠️ 需 FinLab ETL VIP，僅含說明 |

#### 自動探索策略（🤖 嚴格重新驗證後保留 5 檔）

> **2026-02-23 嚴格重新驗證結果**：150 個策略以新標準（複合分數 ≥ 0.75、熊市 MDD > -50%、動態地板 CAGR > 17.5% / Sharpe > 0.90）重新評估，原 107 檔中僅 5 檔通過。

| 檔案 | 主要條件 | CAGR | Sharpe | MDD | 勝率 | 分數 |
|------|---------|------|--------|-----|------|------|
| s111 | 均量>100張＋營業利益成長率>0%＋創40日新高＋月營收加速＋BI>0.52 | **+45.7%** | 1.31 | -28.8% | 52.9% | 0.789 |
| s116 | 均量>100張＋大股東連2期上升＋殖利率>5%＋月營收加速>1.1 | +30.2% | 1.32 | **-21.8%** | 50.8% | 0.759 |
| s118 | 均量>100張＋殖利率>5%＋營業利益成長率>20%＋月營收加速＋低波動 | +25.1% | **1.67** | **-15.0%** | 59.8% | — |
| s62  | 均量>200張＋60日低波動＋月營收加速>1.1 | +24.3% | 1.46 | -20.0% | 57.5% | 0.756 |
| s102 | 均量>100張＋董監持股>5%＋月營收加速>1.1＋殖利率排名前15 | +20.5% | 1.34 | -25.8% | 54.8% | 0.770 |

> 🤖 = 自動探索引擎發現（複合分數 ≥ 0.75 + OOS CAGR > 15% / Sharpe > 0.6 + 熊市 MDD > -50%）
> s118 為重新驗證後由新標準發現，分數欄為 n/a（待下次 run_all_backtests.py 計算）

#### 實驗性策略（exp_*）

> 實驗性策略為手動設計，Sharpe 不足或虧損，不包含於此公開 repo。

單獨執行策略（以 s03 為例）：
```bash
python agents/strategies/s03_peg.py
```

### 注意事項

- VIP Tier 每日 5000 MB 額度
- `hold_until` 在目前版本有 numpy read-only 限制，請改用月頻再平衡
- ROE / 財務報表為季報，FinLab `&` 運算子自動對齊，勿手動 `.reindex()`
- 生成的策略程式碼會顯示在終端，可直接複製修改
- 回測報告存於 `agents/reports/`

---

## 自動策略探索引擎（連續執行）

`agents/strategy_explorer.py` 隨 Scheduler 啟動後以 **daemon thread** 持續運行，不間斷地測試新因子組合，發現優秀策略時自動存檔並推播。

> **注意事項（費率標準化）**：所有策略檔（s01~s10 手動 + s62/s102/s111/s116/s118 自動 + exp_*）均採用統一標準費率 `fee_ratio=1.425/1000, tax_ratio=3/1000`（0.1425% 買賣手續費 + 0.3% 股票交易稅）。

### 運作方式

```
Scheduler 啟動
    ↓
daemon thread（strategy-explorer）
  → 從 26 種因子庫隨機抽 3~5 個因子（加權：3因子偏好），並隨機化閾值參數
  → 因子多樣性檢查（Jaccard 相似度 > 50% 且 > 25% 現有策略重疊 → 跳過）
  → 以時間戳為種子產生 hash，確認未曾測試過
  → FinLab 回測（2018~2022 訓練期，嚴格分離 OOS）
  → 判斷是否達到入選門檻（靜態地板 + 動態地板 + 複合分數 ≥ 0.75）
  → 達標後：Walk-forward OOS 驗證（2023~今）
      OOS 通過條件（升級）：CAGR > 8%、Sharpe > 0.4、MDD 不惡化 > 50%
  → 兩段皆通過：存 .py 檔 + DB + Telegram 推播
  → 跑完立刻跑下一個（失敗則等待 30 秒後繼續）
```

### 入選門檻

**加權複合分數 ≥ 0.75**（各指標正規化後加權平均）：

| 指標 | 權重 | 說明 |
|------|------|------|
| CAGR | 1.0 | 地板 6%，基準現有最佳 |
| Sharpe | 1.0 | 地板 0.35，基準現有最佳 |
| MDD | **1.5** | 地板 -60%，基準現有最佳（加重，強調風險控制） |
| 勝率 | 0.5 | 基準現有最佳 |

**最低地板**（低於此直接淘汰，不進入複合分數計算）：CAGR > 6%，Sharpe > 0.35，MDD > -60%

**動態地板**（隨已通過策略數量自動提升，取現有通過策略的 P10 值）：
- 激活條件：通過靜態地板的策略 ≥ 5 個
- 現行動態地板（重新驗證後 148 個策略通過靜態地板 P10）：CAGR > 17.5%，Sharpe > 0.90

**因子庫（26 種）**：

| 家族 | 因子 |
|------|------|
| 流動性 | `vol_ok`（均量過濾，300~1000張）|
| 估值 | `pe_max`（PE上限）|
| 品質/獲利 | `roe_min`, `fcf_pos`, `fcf_rank`, `op_grow` |
| 月營收 | `rev_grow`, `rev_accel`, `rev_mom` |
| 技術面 | `ma_bull`, `ma_cross`, `high_n`, `momentum` |
| 低波動 | `low_vol` |
| 集保鯨魚 | `whale_conc`, `whale_rise` |
| 分點籌碼 | `bi_high`, `bi_rise`, `bsr_high` |
| 法人 | `foreign_buy`, `trust_buy`, `inst_both` |
| 董監 | `insider_buy`, `insider_skin` |
| 股息收益 🆕 | `div_yield`（殖利率 %） |
| 帳面淨值 🆕 | `pb_low`（PB 股價淨值比）|

**Walk-forward OOS 驗證**（通過複合分數後的第二關）：
- 訓練期：2018~2022-12-31（嚴格 OOS 分離）
- 驗證期：2023-01-01~今（out-of-sample，至少 12 個月）
- 通過條件：OOS 段 **CAGR > 15%**、**Sharpe > 0.6**、MDD 不惡化超過 50%
- 若 OOS 驗證失敗 → 不存檔（防止過擬合）

**熊市段驗證**（通過 OOS 後的第三關）：
- 驗證期：2021-08-01~2022-12-31（台股最大跌幅約 -25% 的空頭段）
- 通過條件：熊市段 **MDD > -50%**（期數不足 3 期則略過）
- 目的：確認策略在空頭市場不會崩潰，而非只在多頭市場有效

**月營收資料時間對齊**：
- 月營收索引格式為 `"2024-M1"`，使用 `.index_str_to_date()` 轉換為公告日（如 `2024-01-10`）
- 確保換倉時使用「已公告」的資料，符合 FinLab 官方建議，避免 look-ahead bias

**因子多樣性控制**：
- 每次抽樣最多嘗試 50 個種子
- Jaccard 相似度計算**排除 vol_ok**（所有策略共有，會稀釋差異性）
- 若 > 25% 的現有策略因子家族重疊度 > 50% → 跳過

**標竿動態化**：
- 複合分數正規化的「最佳值」（ceiling）改為從 DB 動態讀取
- 底線（floor）保留硬編碼 s07 值，確保空 DB 時仍有高標準
- 隨著更優策略入庫，標竿自動提高，入選門檻持續升級

### 通過後

- 自動存為 `agents/strategies/sXX_*.py`
- 記錄至 DB `discovered_strategies` 表（含 `hypothesis` 欄位）
- 透過 Telegram 推播通知

### 每日資料快取

FinLab 資料每日只下載一次，快取於記憶體中（`_data_cache`），連續探索時不重複下載。

### 手動觸發

```bash
python agents/strategy_explorer.py
```

---

## 事件驅動型策略探索引擎（連續執行）

`agents/event_strategy_explorer.py` 為事件驅動型策略的自動探索引擎，與月頻因子型的 `strategy_explorer.py` 並行運作。

> **注意**：`disposal_intraday` / `disposal_any` / `disposal_fixed` 三個處置股模板原包含於此引擎，
> 其邏輯源自 FinLab 社群處置股策略（https://ai.finlab.tw/），已從公開 repo 移除。
> 由此生成的自動策略（s16、s17）同樣不包含於此 repo。

**與月頻因子型引擎的差異**：

| 項目 | strategy_explorer | event_strategy_explorer |
|------|-------------------|------------------------|
| 策略類型 | 月頻再平衡因子組合 | 事件觸發（持有特定期間） |
| Position 建構 | 向量化布林篩選 | 逐筆事件 `position.loc[start:end]` |
| 持有期間 | 固定每月換倉 | 由事件時間軸決定 |
| 入選門檻 | 複合分數 ≥ 0.75 + OOS + 熊市段 | 與月頻型相同 |

### 事件模板庫（3 種）

| 模板 | 說明 | 假說 | 可調參數 |
|------|------|------|---------|
| `attention_stock` | 注意股票 | 監管注意具短期賣壓解除效應 | entry_offset, exit_offset |
| `capital_reduction` | 減資後恢復買賣（TSE+OTC 合併） | 現金減資 = 公司還錢股東，恢復交易日計價基準重設有反彈空間 | entry_offset, hold_days |
| `treasury_stock` | 庫藏股買回期間持有 | 公司宣告買回 = 管理層認為股價低估，買回期間有資金護盤 | entry_offset, exit_offset, min_len |

**疊加過濾（可選）**：`無過濾` 或 `均量>{vol_k}張`（vol_k：100/200/300/500）

### 三段式驗證（與月頻型相同）

1. **訓練期回測**（2018~2022）：複合分數 ≥ 0.75 + 動態地板
2. **Walk-forward OOS**（2023~今，至少 250 個交易日）：CAGR > 15%、Sharpe > 0.6、MDD 不惡化
3. **熊市段驗證**（2021-08~2022-12，至少 60 個交易日）：MDD > -50%

### 手動觸發

```bash
python agents/event_strategy_explorer.py
```

### 通過後

- 自動存為 `agents/strategies/sXX_*.py`
- 記錄至 DB `discovered_strategies` 表（`rebalance_freq='event'`）
- 透過 Telegram 推播通知（🔬 新事件策略發現！ / 🔄 事件策略更新！）

---

## 每日策略監控（20:00 自動推播）

`agents/strategy_monitor.py` 每晚 20:00 透過 APScheduler 自動執行，推播四節內容。

### 事件警示（新增）

掃描今日新發生的公司事件，有事件時才顯示此節：

| 類型 | 觸發條件 | 用途 |
|------|---------|------|
| ✂️ 減資後恢復買賣 | `capital_reduction_tse/otc` 的 `恢復買賣日期 == 今日` | 減資重啟日可能有計價基準重設效應 |
| 🏦 庫藏股買回開始 | `treasury_stock` 的 `預定買回期間-起 == 今日` | 公司主動護盤，買回期間有支撐 |
| ⚠️ 新增注意股票 | `trading_attention` 的 `date == 今日`（資料可用時）| 監管注意後的賣壓解除效應 |

### 處置股策略

- **進場**：今日新被列入「分時交易」處置的 4 碼普通股 → 推播進場建議
- **出場**：今日處置期結束 且 我有持倉 → 推播出場提醒

### PEG 策略（月頻）

- **選股**：即時計算月營收成長 + 營業利益成長率過濾後，取 PEG 最低前 10 檔
- **出場**：持倉中有正 PEG 值但已不在前 10 → 推播「已落榜」提醒（**自動排除處置期持倉**，避免假訊號）

### 持倉健診（加減碼建議）

對持倉台股個股進行多策略訊號交叉評分，輸出三層建議：

| 評分 | 建議 | 條件 |
|------|------|------|
| ≥ 4 | 🟢 建議加碼 | 多訊號共振（PEG⭐ / 籌碼✅ / 法人💰 / MA多📈 等） |
| 2~3 | 🟡 持有觀察 | 部分訊號，維持現有部位 |
| < 2 且均線多頭 | 🟡 持有觀察 | 訊號不足但趨勢未轉弱 |
| < 2 且均線偏空 | 🔴 考慮減碼 | 無正向訊號且技術面轉弱 |

評分因子：PEG前10（+3）、籌碼分點集中（+2）、法人同買（+2）、MA多頭（+1）、60日新高（+1）、外資/投信各（+1）

> ⚠️ 若持倉同時有 ROE 偏低等負面指標，加碼/觀察行尾會附加 warns 標記。

### 推播範例

```
📊 每日策略監控報告
日期：2026-02-23

📅 今日事件警示
✂️ 減資後恢復買賣（今日重啟）：
  • 1234 某某股
🏦 庫藏股買回期開始（今日）：
  • 5678 某某股

⚠️ 處置股策略
▶ 今日新進入處置（可考慮進場）：
  • 1122 某某股　處置至 2026-03-05
⚠️ 處置期結束（持倉提醒）：
  • 3344 某某股　今日處置結束，可考慮出場

⭐ PEG 策略（月頻）
▶ 當前選股（低PEG前10）：
  • 3017 奇鋐　PEG=0.26
  • 2330 台積電　PEG=0.81
⚠️ 持倉已不在PEG前10（可考慮出場）：
  • 2313 華通　PEG=3.03（已落榜）

💼 持倉健診  🟢 市場：多頭
🟢 建議加碼（多訊號共振）：
  • 2344 華邦電　籌碼✅  MA多📈  法人💰  (5分)  Kelly≈7%  ⚠️ ROE低(3.2%)
🟡 持有觀察：
  • 2454 聯發科　外資買  (2分)  Kelly≈3%
  • 9876 某某股　MA多📈  (1分)  Kelly≈1%
🔴 考慮減碼：
  • 9999 某某股　均線空頭⚠️  ROE低(1.5%)
```

### 手動觸發

```bash
python agents/strategy_monitor.py
```

---

## 持倉掃描器（手動執行）

`agents/position_scanner.py` 一次性指令，掃描 DB 中**所有通過**的自動探索策略，依命中率排名後推播 Telegram。

### 特性

- 不跑 `sim()`，僅重建 `position`，**不消耗額外 FinLab token**
- FinLab 資料一次性載入（約 3 秒），所有策略共用
- **市場 Regime 偵測**：自動判斷牛/熊/盤整市場環境，動態調整策略排序偏好
- **Sharpe 加權命中分數**：依各策略 Sharpe ratio 加權，而非等權命中計數
- **Half-Kelly 倉位建議**：對每檔持股計算建議倉位比例（上限 15%）
- 自動分類台股個股 / ETF / 美股，只對個股排名
- 包含股票名稱（從 watchlist 查詢）

### Regime 偵測邏輯

| Regime | 判斷條件 | 策略排序偏好 |
|--------|---------|------------|
| 🟢 牛市 (bull) | 觀察清單中位數 60日漲幅 > 3% | 優先高 CAGR 動能策略 |
| 🔴 熊市 (bear) | 中位數 60日漲幅 < -3% | 優先低 MDD 防禦策略 |
| 🟡 盤整 (sideways) | 介於 -3%~3% | 優先高勝率均值回歸策略 |

### 執行

```bash
python agents/position_scanner.py
```

### 輸出格式

```
📊 持倉策略掃描報告
🗓 2026-02-20 · 全部 23 個策略（成功執行 23 個）
🟢 市場 Regime：BULL　牛市：優先動能/高CAGR

🇹🇼 台股個股排名（Sharpe加權命中）
🥇 2344 華邦電   3/23 (0.48w) Kelly≈8%  ███░░░░
🥈 2454 聯發科   2/23 (0.31w) Kelly≈6%  ██░░░░░
⬜  2330 台積電   0/23  ░░░░░░░
...

ℹ️ ETF 10 檔：0050 006208 ...
🇺🇸 美股（6 檔，策略不含）：AAPL CRDO ...

━━━━━━━━━━━━━━━━━━━━
📋 策略明細
1. 均量>300張+大股東佔比>25%+PE｜35% 79分｜✅ 2344 華邦電
2. ...
```

### 備註

- 只掃描自動探索策略（s62/s102/s111/s116/s118），手動策略（s01~s10）及實驗性策略（exp_*）尚未納入
- Sharpe 加權命中 = 各策略命中時以 Sharpe/總Sharpe 加權，比等權命中更能反映策略品質
- Kelly 建議 = Half-Kelly × Sharpe 調整，上限 15%，為多個命中策略的平均值

---

## 排程時間表

| 任務 | 時間（CST，台北時間）| 執行日 |
|------|---------------------|--------|
| 台股價格更新 | 15:35 | 週一至週五 |
| 美股價格更新 | 06:00 | 週二至週六 |
| 每日心跳推播（追蹤清單 / 持倉數 / 策略數） | 08:00 | 每天 |
| 策略監控推播（事件警示 + PEG + 持倉健診） | 20:00 | 每天 |
| 自動策略探索—月頻因子（daemon，連續） | 啟動即執行 | 常駐 |
| 自動策略探索—事件驅動（daemon，連續） | 啟動即執行 | 常駐 |

---

## 資料庫 Schema

資料庫位置：`data/quant.db`

### watchlist（觀察清單）

| 欄位 | 類型 | 說明 |
|------|------|------|
| symbol | TEXT | 股票代碼（2330、AAPL） |
| market | TEXT | TW 或 US |
| name | TEXT | 股票名稱（中文） |
| added_by | INTEGER | Telegram user_id |
| is_active | INTEGER | 1=追蹤中，0=已停用 |

### price_history（價格歷史）

| 欄位 | 類型 | 說明 |
|------|------|------|
| symbol, market | TEXT | 識別鍵 |
| trade_date | DATE | 交易日期 |
| open/high/low/close_price | REAL | OHLC |
| volume | INTEGER | 成交量 |
| ma5, ma20 | REAL | 均線 |
| vol_ma20 | REAL | 成交量均量 |
| n_day_high | REAL | N日滾動最高價 |

### position_lots（持倉 Lot）

| 欄位 | 類型 | 說明 |
|------|------|------|
| symbol, market | TEXT | 識別鍵 |
| trade_date | DATE | 買入日期 |
| quantity | REAL | 原始買入量（股） |
| remaining | REAL | 剩餘量（FIFO 消耗後更新） |
| unit_cost | REAL | 每股成本 |

成本計算採 **FIFO（先進先出）**。

### discovered_strategies（自動探索策略）

| 欄位 | 類型 | 說明 |
|------|------|------|
| template_id | TEXT | 策略組合 hash（去重鍵，UNIQUE） |
| name | TEXT | 策略名稱 |
| description | TEXT | 策略描述 |
| cagr / sharpe / mdd / win_ratio | REAL | 回測績效 |
| calmar_ratio | REAL | Calmar 比率（CAGR / \|MDD\|） |
| volatility | REAL | 年化波動率 |
| factor_list | TEXT | JSON 因子鍵列表（如 `["vol_ok","roe_min","rev_grow"]`） |
| ranking_factor | TEXT | 排名依據（如 `"roe"`, `"peg"`, `"dy"`） |
| rebalance_freq | TEXT | 再平衡頻率（`"M"` 月頻） |
| position_limit | REAL | 動態持股上限（`min(0.20, 1/top_n)`） |
| passed | INTEGER | 1=達標儲存，0=淘汰 |
| code | TEXT | 策略程式碼（通過時才儲存） |
| file_path | TEXT | 儲存路徑 |
| hypothesis | TEXT | 探索種子與因子組合記錄 |
| condition_group | TEXT | 條件族 hash（factor keys + rank，用於同族去重） |
| tried_at | DATETIME | 測試時間 |
| notified | INTEGER | 1=已推播 |

---

## 常用操作

### 手動觸發資料更新

```bash
# 台股
python scheduler/job_runner.py --once --market TW

# 美股
python scheduler/job_runner.py --once --market US
```

### 查詢 SQLite 資料庫

```bash
sqlite3 data/quant.db

SELECT * FROM watchlist WHERE is_active=1;

-- 查詢持倉（FIFO 彙總）
SELECT symbol, market,
       SUM(remaining) AS qty,
       SUM(remaining*unit_cost)/SUM(remaining) AS avg_cost
FROM position_lots WHERE remaining>0
GROUP BY symbol, market;
```

---

## 故障排除

### Bot 無回應

```bash
tail -50 data/logs/bot.log
bash scripts/start.sh
```

### 策略 Agent 無法執行

```bash
# 確認 FinLab token
grep FINLAB_API_TOKEN config/.env

# 確認 claude binary
which claude

# 注意：必須在終端機執行，不能在 Claude Code session 內執行
```
