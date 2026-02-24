#!/usr/bin/env python3
import sys, threading, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config.settings import DB_PATH, DATA_DIR
from core.database import init_db, get_watchlist
from core.data_fetcher import update_all_prices
from core.notifier import notify_heartbeat

CST = pytz.timezone("Asia/Taipei")
scheduler = BlockingScheduler(timezone=CST)


@scheduler.scheduled_job(CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone=CST))
def job_tw():
    logger.info("=== 台股排程啟動 ===")
    n = update_all_prices(market="TW")
    logger.info(f"台股完成：更新{n}筆")


@scheduler.scheduled_job(CronTrigger(hour=6, minute=0, day_of_week="tue-sat", timezone=CST))
def job_us():
    logger.info("=== 美股排程啟動 ===")
    n = update_all_prices(market="US")
    logger.info(f"美股完成：更新{n}筆")


@scheduler.scheduled_job(CronTrigger(hour=20, minute=0, timezone=CST))
def job_strategy_monitor():
    """每日 20:00 執行處置股策略 + PEG 策略監控，推播選股與出場提醒"""
    try:
        from agents.strategy_monitor import run
        run()
    except Exception as e:
        logger.error(f"策略監控排程失敗：{e}")


def _start_explorer_daemon():
    """啟動月頻因子策略探索 daemon thread，跑完一個立刻跑下一個"""
    def _loop():
        from agents.strategy_explorer import run_loop
        run_loop()
    t = threading.Thread(target=_loop, daemon=True, name="strategy-explorer")
    t.start()
    logger.info("月頻策略探索 daemon 已啟動（連續模式）")


def _start_event_explorer_daemon():
    """啟動事件驅動策略探索 daemon thread，跑完一個立刻跑下一個"""
    def _loop():
        from agents.event_strategy_explorer import run_loop
        run_loop()
    t = threading.Thread(target=_loop, daemon=True, name="event-explorer")
    t.start()
    logger.info("事件策略探索 daemon 已啟動（連續模式）")


@scheduler.scheduled_job(CronTrigger(hour=2, minute=0, timezone=CST))
def job_backup():
    """每日 02:00 備份 SQLite DB，保留最近 7 份"""
    try:
        dest = DATA_DIR / f"quant_{datetime.now().strftime('%Y%m%d')}.db"
        shutil.copy2(DB_PATH, dest)
        backups = sorted(DATA_DIR.glob("quant_????????.db"))
        for old in backups[:-7]:
            old.unlink()
        logger.info(f"[backup] DB 已備份 → {dest.name}（共保留 {min(len(backups),7)} 份）")
    except Exception as e:
        logger.error(f"[backup] 備份失敗：{e}")


@scheduler.scheduled_job(CronTrigger(hour=8, minute=0, timezone=CST))
def job_heartbeat():
    wl = get_watchlist()
    tw = sum(1 for i in wl if i["market"] == "TW")
    us = sum(1 for i in wl if i["market"] == "US")
    from core.database import get_conn
    conn = get_conn()
    pos_count = conn.execute(
        "SELECT COUNT(DISTINCT symbol) FROM position_lots WHERE remaining > 0"
    ).fetchone()[0]
    strat_count = conn.execute(
        "SELECT COUNT(*) FROM discovered_strategies WHERE passed=1"
    ).fetchone()[0]
    notify_heartbeat(len(wl), tw, us, pos_count, strat_count)
    logger.info("心跳推播完成")


def run_once(market: str = "ALL"):
    """手動觸發一次價格更新（測試用）"""
    logger.info(f"手動執行 market={market}")
    n = update_all_prices(market=market)
    logger.info(f"完成：更新{n}筆")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="手動執行一次後結束")
    parser.add_argument("--market", default="ALL", choices=["ALL","TW","US"])
    args = parser.parse_args()

    init_db()
    if args.once:
        run_once(args.market)
    else:
        _start_explorer_daemon()
        _start_event_explorer_daemon()
        logger.info("APScheduler 啟動，等待排程...")
        scheduler.start()
