import os
import logging
import sqlite3
import asyncio
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

def kb_steam():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CS2", callback_data="steam_cs2"),
         InlineKeyboardButton("Dota 2", callback_data="steam_dota2")],
        [InlineKeyboardButton("📋 Всі скіни", callback_data="steam_all")],
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
    elif data == "main_steam":
        await query.edit_message_text("🎮 <b>Steam Інвентар</b>

Оберіть гру:", reply_markup=kb_steam(), parse_mode="HTML")
    elif data.startswith("steam_"):
        game = data.split("_")[1]
        if game in ["cs2", "dota2"]:
            await query.edit_message_text(f"🎮 <b>Управління {game.upper()}</b>", reply_markup=kb_game(game), parse_mode="HTML")
    elif data.startswith("game_add_"):
        game = data.split("_")[2]
        context.user_data['game'] = game
        await query.message.reply_text(f"📝 Введіть дані для {game.upper()} у форматі:
Назва; Ціна; Кількість")
        return ADDING_ITEM
    elif data.startswith("game_list_"):
        game = data.split("_")[2]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name, price, amount FROM inventory WHERE game=?", (game,))
        items = c.fetchall()
        conn.close()
        if not items:
            await query.edit_message_text(f"📭 Список {game.upper()} порожній.", reply_markup=kb_game(game))
        else:
            text = f"📋 <b>Ваші предмети {game.upper()}:</b>

"
            total = 0
            for name, price, amount in items:
                subtotal = price * amount
                total += subtotal
                text += f"🔹 {name} - {amount} шт. за ${price:.2f} (Разом: ${subtotal:.2f})
"
            text += f"
💰 <b>Загальна вартість: ${total:.2f}</b>"
            await query.edit_message_text(text, reply_markup=kb_game(game), parse_mode="HTML")

async def process_item_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return ConversationHandler.END
    text = update.message.text
    try:
        parts = [p.strip() for p in text.split(";")]
        if len(parts) != 3:
            await update.message.reply_text("❌ Невірний формат. Спробуйте ще раз або введіть /cancel.")
            return ADDING_ITEM
        name = parts[0]
        price = float(parts[1])
        amount = int(parts[2])
        game = context.user_data.get('game', 'unknown')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO inventory (game, name, price, amount, date) VALUES (?, ?, ?, ?, ?)",
                  (game, name, price, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Додано: {name}")
        text, kb = kb_main()
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
        return ADDING_ITEM

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Дію скасовано.")
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_handler, pattern="^game_add_")],
        states={
            ADDING_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_item_add)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
