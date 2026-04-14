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

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))
DB_PATH = "bot_data.db"
ADDING_ITEM = 1

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS inventory 
                 (id INTEGER PRIMARY KEY, game TEXT, name TEXT, price REAL, amount INTEGER, date TEXT)''')
    conn.commit()
    conn.close()

init_db()

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

async def guard_cb(query):
    uid = query.from_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await query.answer("❌ Доступ заборонено", show_alert=True)
        return False
    return True

async def guard_msg(update: Update):
    uid = update.effective_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text("❌ Доступ заборонено")
        return False
    return True

def kb_main():
    text = "🏠 <b>Головне меню</b>

Оберіть розділ для роботи:"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Портфель", callback_data="main_portfolio"), InlineKeyboardButton("🎮 Steam", callback_data="main_steam")],
        [InlineKeyboardButton("🎁 Подарунки", callback_data="main_gifts"), InlineKeyboardButton("📈 Інвестиції", callback_data="main_invest")],
        [InlineKeyboardButton("🔍 Аналітика", callback_data="main_analytics"), InlineKeyboardButton("⚙️ Налаштування", callback_data="main_settings")],
    ])
    return text, kb

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 До меню", callback_data="main_home")]])

def kb_steam():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CS2", callback_data="steam_cs2"), InlineKeyboardButton("Dota 2", callback_data="steam_dota2")],
        [InlineKeyboardButton("📋 Всі скіни", callback_data="steam_all")],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data="steam_update")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_game(game: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати предмет", callback_data=f"game_add_{game}")],
        [InlineKeyboardButton("📋 Список предметів", callback_data=f"game_list_{game}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_steam")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return
    t, k = kb_main()
    await update.message.reply_text(t, reply_markup=k, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query): return
    await query.answer()
    data = query.data

    if data == "main_home":
        t, k = kb_main()
        await query.edit_message_text(t, reply_markup=k, parse_mode="HTML")
    elif data == "main_portfolio":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT game, SUM(price * amount) FROM inventory GROUP BY game")
        totals = c.fetchall(); conn.close()
        text = "📊 <b>Загальний Портфель</b>
"
        grand_total = 0
        for g, tot in totals:
            text += f"
🔹 <b>{g.upper()}</b>: ${tot:.2f}"
            grand_total += tot
        if not totals: text += "
📭 Портфель порожній."
        else: text += f"

💰 <b>Разом: ${grand_total:.2f}</b>"
        await query.edit_message_text(text, reply_markup=kb_back(), parse_mode="HTML")
    elif data == "main_steam":
        await query.edit_message_text("🎮 <b>Steam Інвентар</b>
Оберіть гру:", reply_markup=kb_steam(), parse_mode="HTML")
    elif data == "steam_update":
        await query.edit_message_text("🔄 Оновлюю ціни...")
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT id, name, game FROM inventory"); items = c.fetchall()
        upd = 0
        for i_id, name, g in items:
            p = await fetch_steam_price(name, g)
            if p: c.execute("UPDATE inventory SET price=? WHERE id=?", (p, i_id)); upd += 1
        conn.commit(); conn.close()
        await query.edit_message_text(f"✅ Оновлено {upd} цін.", reply_markup=kb_steam())
    elif data.startswith("steam_"):
        game = data.split("_")[1]
        if game in ["cs2", "dota2"]: await query.edit_message_text(f"🎮 <b>{game.upper()}</b>", reply_markup=kb_game(game), parse_mode="HTML")
    elif data.startswith("game_list_"):
        game = data.split("_")[2]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT name, price, amount FROM inventory WHERE game=?", (game,)); items = c.fetchall(); conn.close()
        if not items: await query.edit_message_text(f"📭 {game.upper()} порожній.", reply_markup=kb_game(game))
        else:
            text = f"📋 <b>{game.upper()} Предмети:</b>
"; total = 0
            for n, p, a in items:
                sub = p * a; total += sub
                text += f"
🔹 {n} - {a} шт. (${p:.2f})"
            text += f"

💰 <b>Разом: ${total:.2f}</b>"
            await query.edit_message_text(text, reply_markup=kb_game(game), parse_mode="HTML")
    elif data in ["main_gifts", "main_invest", "main_analytics", "main_settings"]:
        await query.edit_message_text("🚧 В розробці...", reply_markup=kb_back())

async def add_item_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = update.callback_query.data.split("_")[2]
    context.user_data['game'] = game
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(f"📝 <b>Додавання в {game.upper()}</b>
Формат: <code>Назва; Кількість</code>
Або: <code>Назва; Ціна; Кількість</code>", parse_mode="HTML")
    return ADDING_ITEM

async def process_item_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return ConversationHandler.END
    game = context.user_data.get('game')
    try:
        p = [x.strip() for x in update.message.text.split(";")]
        if len(p) == 2:
            name, amount = p[0], int(p[1])
            await update.message.reply_text("🔍 Шукаю ціну...")
            price = await fetch_steam_price(name, game)
            if not price: await update.message.reply_text("❌ Ціну не знайдено. Введіть: Назва; Ціна; Кількість"); return ADDING_ITEM
        elif len(p) == 3: name, price, amount = p[0], float(p[1].replace(",", ".")), int(p[2])
        else: return ADDING_ITEM
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT INTO inventory (game, name, price, amount, date) VALUES (?,?,?,?,?)", (game, name, price, amount, datetime.now().isoformat()))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Додано: {name} (${price:.2f})")
        t, k = kb_main(); await update.message.reply_text(t, reply_markup=k, parse_mode="HTML")
        return ConversationHandler.END
    except: await update.message.reply_text("❌ Помилка формату."); return ADDING_ITEM

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_item_start, pattern="^game_add_")], states={ADDING_ITEM:[MessageHandler(filters.TEXT & ~filters.COMMAND, process_item_addition)]}, fallbacks=[CommandHandler("cancel", start)]))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.run_polling()

if __name__ == "__main__": main()
