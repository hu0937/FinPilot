import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
import pandas as pd
from loguru import logger

from config.settings import FINMIND_TOKEN, HISTORY_DAYS
from core.database import get_watchlist, upsert_prices


def _start_date() -> str:
    return (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()


def fetch_tw_stocks(symbols: list[str] = None) -> pd.DataFrame:
    """FinMind 抓台股日線資料"""
    from FinMind.data import DataLoader
    dl = DataLoader()
    if FINMIND_TOKEN:
        dl.login_by_token(api_token=FINMIND_TOKEN)

    if symbols is None:
        symbols = [w["symbol"] for w in get_watchlist(market="TW")]
    if not symbols:
        return pd.DataFrame()

    today = date.today().isoformat()
    start = _start_date()
    results = []

    for sym in symbols:
        try:
            df = dl.taiwan_stock_daily(stock_id=sym, start_date=start, end_date=today)
            if df.empty:
                logger.warning(f"[TW] {sym} 無資料")
                continue
            df = df.rename(columns={
                "date": "trade_date",
                "max":  "high_price",
                "min":  "low_price",
                "close": "close_price",
                "open":  "open_price",
                "Trading_Volume": "volume",
            })
            df["symbol"] = sym
            df["market"] = "TW"
            df = df.sort_values("trade_date")
            df["ma5"]        = df["close_price"].rolling(5).mean().round(2)
            df["ma20"]       = df["close_price"].rolling(20).mean().round(2)
            df["vol_ma20"]   = df["volume"].rolling(20).mean().round(0)
            df["n_day_high"] = df["high_price"].rolling(60).max()
            results.append(df[["symbol","market","trade_date","open_price",
                                "high_price","low_price","close_price","volume",
                                "ma5","ma20","vol_ma20","n_day_high"]])
            logger.info(f"[TW] {sym} 取得 {len(df)} 筆")
        except Exception as e:
            logger.error(f"[TW] {sym} 抓取失敗: {e}")

    return pd.concat(results) if results else pd.DataFrame()


def fetch_us_stocks(symbols: list[str] = None) -> pd.DataFrame:
    """yfinance 抓美股日線資料"""
    import yfinance as yf

    if symbols is None:
        symbols = [w["symbol"] for w in get_watchlist(market="US")]
    if not symbols:
        return pd.DataFrame()

    start = _start_date()
    results = []

    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(start=start)
            if df.empty:
                logger.warning(f"[US] {sym} 無資料")
                continue
            df = df.reset_index()
            df["trade_date"] = df["Date"].dt.strftime("%Y-%m-%d")
            df = df.rename(columns={
                "Open":   "open_price",
                "High":   "high_price",
                "Low":    "low_price",
                "Close":  "close_price",
                "Volume": "volume",
            })
            df["symbol"]     = sym.upper()
            df["market"]     = "US"
            df = df.sort_values("trade_date")
            df["ma5"]        = df["close_price"].rolling(5).mean().round(2)
            df["ma20"]       = df["close_price"].rolling(20).mean().round(2)
            df["vol_ma20"]   = df["volume"].rolling(20).mean().round(0)
            df["n_day_high"] = df["high_price"].rolling(60).max()
            results.append(df[["symbol","market","trade_date","open_price",
                                "high_price","low_price","close_price","volume",
                                "ma5","ma20","vol_ma20","n_day_high"]])
            logger.info(f"[US] {sym} 取得 {len(df)} 筆")
        except Exception as e:
            logger.error(f"[US] {sym} 抓取失敗: {e}")

    return pd.concat(results) if results else pd.DataFrame()


def update_all_prices(market: str = "ALL"):
    """抓取並批次寫入所有觀察清單股票的價格"""
    dfs = []
    if market in ("ALL", "TW"):
        dfs.append(fetch_tw_stocks())
    if market in ("ALL", "US"):
        dfs.append(fetch_us_stocks())

    df = pd.concat([d for d in dfs if not d.empty]) if dfs else pd.DataFrame()
    if df.empty:
        logger.info("無新資料可寫入")
        return 0

    records = df.where(pd.notna(df), None).to_dict("records")
    upsert_prices(records)
    logger.info(f"寫入 {len(records)} 筆價格資料")
    return len(records)
