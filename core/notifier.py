import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from loguru import logger
from telegram import Bot
from telegram.error import TelegramError

from config import settings


async def _send_async(text: str, chat_id: str):
    bot = Bot(token=settings.TELEGRAM_TOKEN)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


def send_message(text: str, chat_id: str = None):
    target = chat_id or settings.TELEGRAM_CHAT_ID
    if not settings.TELEGRAM_TOKEN or not target:
        logger.warning("Telegram 未設定，跳過推播")
        return
    try:
        asyncio.run(_send_async(text, target))
    except TelegramError as e:
        logger.error(f"Telegram 推播失敗: {e}")


def notify_heartbeat(watchlist_count: int, tw: int, us: int,
                     position_count: int = 0, strategy_count: int = 0):
    parts = [
        f"💓 <b>系統心跳</b>",
        f"追蹤清單：{watchlist_count} 檔（台股 {tw} / 美股 {us}）",
        f"持倉部位：{position_count} 檔",
        f"通過策略：{strategy_count} 個",
    ]
    send_message("\n".join(parts))
