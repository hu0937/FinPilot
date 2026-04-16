#!/usr/bin/env python3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functools
from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from config.settings import TELEGRAM_TOKEN, ALLOWED_USER_IDS


def auth_required(func):
    """只允許 ALLOWED_USER_IDS 中的使用者呼叫此 handler。"""
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if not ALLOWED_USER_IDS:
            logger.warning("TELEGRAM_ALLOWED_USER_IDS 未設定，拒絕所有請求（user_id={}）", user_id)
            await update.message.reply_text("⛔ Bot 尚未開放授權，請聯繫管理員。")
            return
        if user_id not in ALLOWED_USER_IDS:
            logger.warning("未授權存取（user_id={}）", user_id)
            await update.message.reply_text("⛔ 您沒有使用此 Bot 的權限。")
            return
        return await func(update, ctx)
    return wrapper


from core.database import (
    add_to_watchlist, remove_from_watchlist, get_watchlist,
    buy_position, sell_position, get_positions, get_position_lots,
    get_recent_prices,
)
from bot.claude_handler import handle_claude_message


@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📈 量化分析平台 Bot\n\n"
        "【觀察清單】\n"
        "/add <代碼> <市場>  新增追蹤（市場: TW 或 US）\n"
        "/del <代碼> <市場>  移除追蹤\n"
        "/list [TW/US]       顯示觀察清單\n\n"
        "【持倉管理】\n"
        "/pos                      查詢全部部位（含損益）\n"
        "/pos buy <代碼> <市> <量> <均價> [日期]  買入\n"
        "/pos sell <代碼> <市> <量>              賣出(FIFO)\n"
        "/pos lots <代碼> <市>                  查看 lot 明細\n\n"
        "【分析工具】\n"
        "/price <代碼> <TW|US>   個股價格與趨勢\n"
        "/risk                   持倉集中度分析\n\n"
        "【系統】\n"
        "/status             系統狀態\n\n"
        "或直接輸入自然語言問問題（策略推薦、持倉分析等）💬"
    )


@auth_required
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("用法：/add <代碼> <TW|US>\n範例：/add 2330 TW")
        return
    symbol, market = args[0].upper(), args[1].upper()
    if market not in ("TW", "US"):
        await update.message.reply_text("市場請填 TW 或 US")
        return
    ok = add_to_watchlist(symbol, market, update.effective_user.id)
    msg = f"✅ 已加入：{symbol} ({market})" if ok else f"⚠️ {symbol} ({market}) 已在清單中"
    await update.message.reply_text(msg)


@auth_required
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("用法：/del <代碼> <TW|US>")
        return
    symbol, market = args[0].upper(), args[1].upper()
    ok = remove_from_watchlist(symbol, market)
    msg = f"🗑️ 已移除：{symbol} ({market})" if ok else f"⚠️ 找不到 {symbol} ({market})"
    await update.message.reply_text(msg)


@auth_required
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    market = ctx.args[0].upper() if ctx.args else None
    wl = get_watchlist(market=market)
    if not wl:
        await update.message.reply_text("觀察清單為空，用 /add 新增股票")
        return
    tw = [f"  {i['symbol']}" for i in wl if i["market"] == "TW"]
    us = [f"  {i['symbol']}" for i in wl if i["market"] == "US"]
    lines = ["📋 <b>觀察清單</b>"]
    if tw:
        lines += ["", "🇹🇼 台股："] + tw
    if us:
        lines += ["", "🇺🇸 美股："] + us
    lines.append(f"\n共 {len(wl)} 檔")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import os
    from datetime import datetime
    from config.settings import PID_DIR

    wl = get_watchlist()

    services = {}
    for svc in ["scheduler"]:
        pid_file = PID_DIR / f"{svc}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                services[svc] = "✅ 運行中" if os.path.exists(f"/proc/{pid}") else "❌ 已停止"
            except Exception:
                services[svc] = "⚠️ 未知"
        else:
            services[svc] = "⚫ 未啟動"

    msg = (
        f"📊 <b>系統狀態</b>\n\n"
        f"追蹤清單：{len(wl)} 檔\n"
        f"  🇹🇼 台股：{sum(1 for i in wl if i['market']=='TW')} 檔\n"
        f"  🇺🇸 美股：{sum(1 for i in wl if i['market']=='US')} 檔\n\n"
        f"服務狀態：\n"
        f"  排程器：{services.get('scheduler','未知')}\n\n"
        f"更新時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


@auth_required
async def cmd_pos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args

    # /pos → 查詢全部部位
    if not args:
        positions = get_positions()
        if not positions:
            await update.message.reply_text("目前無持倉記錄\n用 /pos buy 2330 TW 1 500.5 新增")
            return
        lines = ["📦 <b>目前持倉</b>\n"]
        for p in positions:
            flag = "🇹🇼" if p["market"] == "TW" else "🇺🇸"
            unit = "股"
            pnl_str = ""
            try:
                price_rows = get_recent_prices(p['symbol'], p['market'], days=5)
                if price_rows:
                    cur = price_rows[-1].get('close_price')
                    if cur and p['avg_cost'] and p['avg_cost'] > 0:
                        pnl_pct = (cur - p['avg_cost']) / p['avg_cost'] * 100
                        arrow = "▲" if pnl_pct >= 0 else "▼"
                        pnl_str = f" | {arrow}{abs(pnl_pct):.1f}%"
            except Exception:
                pass
            lines.append(f"{flag} <b>{p['symbol']}</b> | {p['quantity']:.2f}{unit} | 均價 {p['avg_cost']:.2f}{pnl_str}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    sub = args[0].lower()

    # /pos buy SYMBOL MARKET QTY COST [DATE]
    if sub == "buy":
        if len(args) < 5:
            await update.message.reply_text("用法：/pos buy <代碼> <TW|US> <數量> <均價> [日期 YYYY-MM-DD]")
            return
        symbol, market = args[1].upper(), args[2].upper()
        try:
            quantity, unit_cost = float(args[3]), float(args[4])
        except ValueError:
            await update.message.reply_text("數量與均價請填數字")
            return
        trade_date = args[5] if len(args) >= 6 else None
        buy_position(symbol, market, quantity, unit_cost, trade_date)
        unit = "股"
        await update.message.reply_text(f"✅ 已記錄買入：{symbol}({market}) {quantity}{unit} @ {unit_cost}")
        return

    # /pos sell SYMBOL MARKET QTY
    if sub == "sell":
        if len(args) < 4:
            await update.message.reply_text("用法：/pos sell <代碼> <TW|US> <數量>")
            return
        symbol, market = args[1].upper(), args[2].upper()
        try:
            quantity = float(args[3])
        except ValueError:
            await update.message.reply_text("數量請填數字")
            return
        result = sell_position(symbol, market, quantity)
        unit = "股"
        if result["insufficient"]:
            await update.message.reply_text(f"⚠️ 持有不足，實際賣出 {result['sold']:.2f}{unit}（FIFO）")
        else:
            await update.message.reply_text(f"✅ 已記錄賣出：{symbol}({market}) {result['sold']:.2f}{unit}（FIFO）")
        return

    # /pos lots SYMBOL MARKET
    if sub == "lots":
        if len(args) < 3:
            await update.message.reply_text("用法：/pos lots <代碼> <TW|US>")
            return
        symbol, market = args[1].upper(), args[2].upper()
        lots = get_position_lots(symbol, market)
        if not lots:
            await update.message.reply_text(f"找不到 {symbol}({market}) 的 lot 記錄")
            return
        unit = "股"
        lines = [f"📋 <b>{symbol}({market}) Lot 明細</b>\n"]
        for lot in lots:
            status = f"剩{lot['remaining']:.2f}" if lot["remaining"] > 0 else "已清空"
            lines.append(f"  {lot['trade_date']} | {lot['quantity']:.2f}{unit} @ {lot['unit_cost']:.2f} | {status}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    await update.message.reply_text("未知子指令。用法：/pos [buy|sell|lots]")


@auth_required
async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("用法：/price <代碼> <TW|US>\n範例：/price 2330 TW")
        return
    symbol, market = args[0].upper(), args[1].upper()
    rows = get_recent_prices(symbol, market, days=30)
    if not rows:
        await update.message.reply_text(f"找不到 {symbol}（{market}），請確認代碼已加入觀察清單")
        return
    latest = rows[-1]
    close  = latest.get('close_price')
    ma5    = latest.get('ma5')
    ma20   = latest.get('ma20')
    vol    = latest.get('volume')
    vol_ma = latest.get('vol_ma20')
    n_high = latest.get('n_day_high')

    if ma5 and ma20:
        if ma5 > ma20 and close and close > ma5:
            trend = "多頭 📈"
        elif ma5 < ma20 and close and close < ma5:
            trend = "空頭 📉"
        else:
            trend = "盤整 ➡️"
    else:
        trend = "資料不足"

    dist_str = ""
    if n_high and close and n_high > 0:
        dist = (close - n_high) / n_high * 100
        dist_str = "創60日新高 🔝" if dist >= 0 else f"距60日高 {dist:.1f}%"

    vol_str = ""
    if vol and vol_ma and vol_ma > 0:
        ratio = vol / vol_ma
        if ratio >= 1.5:
            vol_str = f"放量 ({ratio:.1f}x) 🔊"
        elif ratio <= 0.5:
            vol_str = f"縮量 ({ratio:.1f}x) 🔇"
        else:
            vol_str = f"量能正常 ({ratio:.1f}x)"

    lines = [
        f"<b>{symbol}</b>（{market}）{latest.get('trade_date', '')}",
        f"  收盤：{close}",
        f"  趨勢：{trend}",
    ]
    if dist_str:
        lines.append(f"  位置：{dist_str}")
    if vol_str:
        lines.append(f"  量能：{vol_str}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@auth_required
async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    positions = get_positions()
    if not positions:
        await update.message.reply_text("目前無持倉記錄")
        return
    total_val = sum(p['quantity'] * p['avg_cost'] for p in positions)
    if total_val <= 0:
        await update.message.reply_text("持倉成本資料不足，無法計算風險")
        return
    tw_val     = sum(p['quantity'] * p['avg_cost'] for p in positions if p['market'] == 'TW')
    us_val     = sum(p['quantity'] * p['avg_cost'] for p in positions if p['market'] == 'US')
    sorted_pos = sorted(positions, key=lambda x: x['quantity'] * x['avg_cost'], reverse=True)

    lines = [
        "🔍 <b>持倉風險分析</b>",
        f"台股比例：{tw_val/total_val:.1%}　美股比例：{us_val/total_val:.1%}",
        f"持倉總數：{len(positions)} 檔",
        "",
    ]
    for p in sorted_pos:
        val  = p['quantity'] * p['avg_cost']
        pct  = val / total_val
        flag = "🇹🇼" if p['market'] == 'TW' else "🇺🇸"
        warn = "⚠️" if pct > 0.3 else ("⚡" if pct > 0.2 else "✓")
        lines.append(f"{flag} {p['symbol']}：{pct:.1%} {warn}（均價 {p['avg_cost']:.2f}）")

    max_p      = sorted_pos[0]
    max_pct    = max_p['quantity'] * max_p['avg_cost'] / total_val
    risk_level = "⚠️ 集中度高" if max_pct > 0.3 else ("尚可" if max_pct > 0.2 else "✓ 分散良好")
    lines.append(f"\n最大單一部位：{max_p['symbol']} {max_pct:.1%}　{risk_level}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@auth_required
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    if not user_text:
        return
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await handle_claude_message(user_text)
    await update.message.reply_text(reply)


def run_bot():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN 未設定，Bot 無法啟動")
        return
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("del",    cmd_del))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pos",    cmd_pos))
    app.add_handler(CommandHandler("price",  cmd_price))
    app.add_handler(CommandHandler("risk",   cmd_risk))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Telegram Bot 啟動中...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()
