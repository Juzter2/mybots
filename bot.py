import os
import logging
import sqlite3
import json
import asyncio
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))
DB_PATH = "bot_data.db"

# DB Initialization
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS inventory 
                 (id INTEGER PRIMARY KEY, type TEXT, name TEXT, price REAL, amount INTEGER, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS gifts 
                 (id INTEGER PRIMARY KEY, name TEXT, price REAL, status TEXT, date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Steam API Helper (Placeholder for real integration)
async def get_steam_price(item_name, game="cs2"):
    # В реальному боті тут буде запит до Steam Market API або стороннього сервісу
    # Для демонстрації повертаємо випадкову ціну
    return 15.50

# Guards
async def guard_cb(query):
    uid = query.from_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await query.answer("❌ Доступ заборонено")
        return False
    return True

async def guard_msg(update: Update):
    uid = update.effective_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text("❌ Доступ заборонено")
        return False
    return True

# Keyboards
def kb_main():
    text = "🏠 <b>Головне меню</b>

Обери розділ:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Портфель", callback_data="main_portfolio"),
         InlineKeyboardButton("🎮 Steam", callback_data="main_steam")],
        [InlineKeyboardButton("🎁 Подарунки", callback_data="main_gifts"),
         InlineKeyboardButton("📈 Інвестиції", callback_data="main_invest")],
        [InlineKeyboardButton("🔍 Аналітика", callback_data="main_analytics"),
         InlineKeyboardButton("⚙️ Налаштування", callback_data="main_settings")],
    ])
    return text, kb

def kb_portfolio():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Графік", callback_data="portfolio_chart"),
         InlineKeyboardButton("🥧 Діаграма", callback_data="portfolio_pie")],
        [InlineKeyboardButton("⚡ Швидкий огляд", callback_data="portfolio_quick")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_steam():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CS2", callback_data="steam_cs2"),
         InlineKeyboardButton("Dota 2", callback_data="steam_dota2")],
        [InlineKeyboardButton("📋 Всі скіни", callback_data="steam_all")],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data="steam_update")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_game(game: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати", callback_data=f"game_add_{game}"),
         InlineKeyboardButton("💰 Продати", callback_data=f"game_sell_{game}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"game_delete_{game}"),
         InlineKeyboardButton("📋 Продані", callback_data=f"game_sold_{game}")],
        [InlineKeyboardButton("📉 PnL", callback_data=f"game_pnl_{game}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_steam")],
    ])

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return
    text, kb = kb_main()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query): return
    await query.answer()
    
    data = query.data
    if data == "main_home":
        text, kb = kb_main()
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    elif data == "main_portfolio":
        await query.edit_message_text("📊 <b>Ваш Портфель</b>

Тут буде огляд всіх ваших активів.", 
                                      reply_markup=kb_portfolio(), parse_mode="HTML")
    elif data == "main_steam":
        await query.edit_message_text("🎮 <b>Steam Інвентар</b>

Оберіть гру:", 
                                      reply_markup=kb_steam(), parse_mode="HTML")
    elif data.startswith("steam_"):
        game = data.split("_")[1]
        if game in ["cs2", "dota2"]:
            await query.edit_message_text(f"🎮 <b>Управління {game.upper()}</b>", 
                                          reply_markup=kb_game(game), parse_mode="HTML")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
