import os
import logging
import sqlite3
import asyncio
import aiohttp
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler
)

# Логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Налаштування
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))
DB_PATH = "bot_data.db"
ADDING_ITEM = 1

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS inventory
                 (id INTEGER PRIMARY KEY, category TEXT, name TEXT, price REAL, amount INTEGER, date TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- Ціни ---
async def fetch_steam_price(item_name: str, game: str):
    app_ids = {"cs2": 730, "dota2": 570}
    appid = app_ids.get(game.lower())
    if not appid: return None
    url = f"https://steamcommunity.com/market/priceoverview/?currency=1&appid={appid}&market_hash_name={item_name}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("success"):
                        p_str = data.get("lowest_price") or data.get("median_price")
                        if p_str:
                            return float(p_str.replace("$", "").replace(",", "").strip())
    except: pass
    return None

async def fetch_crypto_price(symbol: str):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol.lower()}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data[symbol.lower()]["usd"])
    except: pass
    return None

# --- Меню ---
def kb_main():
    text = "🏠 <b>Головне меню</b>
Оберіть розділ:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Портфель", callback_data="p_all"), InlineKeyboardButton("🎮 Steam", callback_data="m_steam")],
        [InlineKeyboardButton("📈 Акції", callback_data="cat_stocks"), InlineKeyboardButton("₿ Крипто", callback_data="cat_crypto")],
        [InlineKeyboardButton("📊 Аналітика", callback_data="p_stats")]
    ])
    return text, kb

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text(f"❌ Доступ закритий (ID: {uid})")
        return
    t, k = kb_main()
    await update.message.reply_text(t, reply_markup=k, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_home":
        t, k = kb_main()
        await query.edit_message_text(t, reply_markup=k, parse_mode="HTML")
    
    elif data == "p_all":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT category, SUM(price * amount) FROM inventory GROUP BY category")
        res = c.fetchall(); conn.close()
        text = "💼 <b>Ваш Портфель:</b>

"
        total = 0
        for cat, val in res:
            text += f"🔹 {cat.upper()}: ${val:.2f}
"
            total += val
        text += f"
💰 <b>Разом: ${total:.2f}</b>"
        await query.edit_message_text(text, reply_markup=kb_main()[1], parse_mode="HTML")

    elif data == "m_steam":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("CS2", callback_data="cat_cs2"), InlineKeyboardButton("Dota 2", callback_data="cat_dota2")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main_home")]
        ])
        await query.edit_message_text("🎮 Оберіть гру:", reply_markup=kb)

    elif data.startswith("cat_"):
        cat = data.split("_")[1]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати", callback_data=f"add_{cat}"), InlineKeyboardButton("📋 Список", callback_data=f"list_{cat}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main_home")]
        ])
        await query.edit_message_text(f"📁 Категорія: {cat.upper()}", reply_markup=kb)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.callback_query.data.split("_")[1]
    context.user_data['cat'] = cat
    await update.callback_query.message.reply_text(f"📝 Додавання в {cat.upper()}
Формат: Назва; Кількість")
    return ADDING_ITEM

async def add_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data.get('cat')
    try:
        name, amount = [x.strip() for x in update.message.text.split(";")]
        await update.message.reply_text(f"🔍 Шукаю ціну для {name}...")
        price = 0
        if cat in ["cs2", "dota2"]: price = await fetch_steam_price(name, cat)
        elif cat == "crypto": price = await fetch_crypto_price(name)
        
        if not price:
            await update.message.reply_text("❌ Ціну не знайдено. Спробуйте ще раз або введіть: Назва; Ціна; Кількість")
            return ADDING_ITEM
            
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT INTO inventory (category, name, price, amount, date) VALUES (?,?,?,?,?)",
                  (cat, name, price, int(amount), datetime.now().isoformat()))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Додано: {name} за ${price}")
        return ConversationHandler.END
    except:
        await update.message.reply_text("❌ Помилка. Формат: Назва; Кількість")
        return ADDING_ITEM

def main():
    if not BOT_TOKEN: return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add_")],
        states={ADDING_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_process)]},
        fallbacks=[CommandHandler("start", start)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Бот запущений!")
    app.run_polling()

if __name__ == "__main__":
    main()
