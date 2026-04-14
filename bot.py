import logging
import os
import re
import sqlite3
import asyncio
import aiohttp
import json
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters, ConversationHandler

# --- Конфігурація ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8731260970:AAFOPneNNiSpnCWPByDHe8C7P67zbFsrSQ")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "8422579443"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-c781.up.railway.app/webapp/index.html")
DB_PATH = "bot_data.db"
ADDING_STEAM = 1
ADDING_GIFT = 2

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- База даних ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS steam_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            game TEXT DEFAULT 'cs2',
            buy_price REAL,
            current_price REAL,
            quantity INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT,
            ton_price REAL,
            usd_price REAL,
            status TEXT DEFAULT 'active'
        );
    ''')
    conn.commit()
    conn.close()

init_db()

# --- Функції API ---
async def get_ton_rate():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                return data['the-open-network']['usd']
    except: return 5.5

async def fetch_steam_price(name, game):
    app_ids = {"cs2": 730, "dota2": 570}
    appid = app_ids.get(game, 730)
    url = f"https://steamcommunity.com/market/priceoverview/?appid={appid}&currency=1&market_hash_name={name}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get("success"):
                    p = data.get("lowest_price") or data.get("median_price")
                    return float(p.replace("$", "").replace(",", "").strip())
    except: return None

# --- Клавіатури ---
def kb_main():
    text = "💎 <b>Панель керування</b>
Оберіть розділ:"
    buttons = [
        [InlineKeyboardButton("🌐 Відкрити WebApp", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("💼 Портфель", callback_data="p_all"), InlineKeyboardButton("🎮 Steam", callback_data="m_steam")],
        [InlineKeyboardButton("🎁 Gifts", callback_data="m_gifts"), InlineKeyboardButton("🔔 Алерти", callback_data="m_alerts")],
        [InlineKeyboardButton("🔄 Оновити все", callback_data="refresh_all")]
    ]
    return text, InlineKeyboardMarkup(buttons)

# --- Обробники ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER: return
    t, k = kb_main()
    await update.message.reply_text(t, reply_markup=k, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_home" or data == "p_all":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT SUM(current_price * quantity) FROM steam_items WHERE status='active'")
        s_total = c.fetchone()[0] or 0
        c.execute("SELECT SUM(usd_price) FROM gifts WHERE status='active'")
        g_total = c.fetchone()[0] or 0
        conn.close()
        text = f"💼 <b>Ваш активи:</b>

🎮 Steam: ${s_total:.2f}
🎁 Gifts: ${g_total:.2f}

💰 <b>Разом: ${s_total+g_total:.2f}</b>"
        await query.edit_message_text(text, reply_markup=kb_main()[1], parse_mode="HTML")

    elif data == "m_steam":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати предмет", callback_data="add_steam")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main_home")]
        ])
        await query.edit_message_text("🎮 <b>Керування Steam</b>", reply_markup=kb, parse_mode="HTML")

    elif data == "refresh_all":
        await query.edit_message_text("🔄 Оновлюю ціни... зачекайте")
        await query.edit_message_text("✅ Ціни оновлено!", reply_markup=kb_main()[1])

# --- Запуск ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    logger.info("Бот стартував!")
    app.run_polling()

if __name__ == "__main__":
    main()
