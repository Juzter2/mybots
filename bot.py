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
BOT_TOKEN = os.environ.get(\"BOT_TOKEN\", \"\")
ALLOWED_USER = int(os.environ.get(\"ALLOWED_USER\", \"0\"))
DB_PATH = \"bot_data.db\"
ADDING_ITEM = 1

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS inventory
                 (id INTEGER PRIMARY KEY, category TEXT, name TEXT, price REAL, amount INTEGER, date TEXT)''')
    conn.commit()
    conn.close()

init_db()

async def fetch_steam_price(item_name: str, game: str):
    app_ids = {\"cs2\": 730, \"dota2\": 570}
    appid = app_ids.get(game.lower())
    if not appid: return None
    url = f\"https://steamcommunity.com/market/priceoverview/?currency=1&appid={appid}&market_hash_name={item_name}\"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get(\"success\"):
                        p_str = data.get(\"lowest_price\") or data.get(\"median_price\")
                        if p_str:
                            return float(p_str.replace(\"$\", \"\").replace(\",\", \"\").strip())
    except: pass
    return None

async def fetch_crypto_price(symbol: str):
    url = f\"https://api.coingecko.com/api/v3/simple/price?ids={symbol.lower()}&vs_currencies=usd\"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if symbol.lower() in data:
                        return float(data[symbol.lower()][\"usd\"])
    except: pass
    return None

async def fetch_stock_price(ticker: str):
    url = f\"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}\"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['chart']['result'][0]['meta']['regularMarketPrice'])
    except: pass
    return None

async def guard_cb(query):
    uid = query.from_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await query.answer(\"❌ Доступ заборонено\", show_alert=True)
        return False
    return True

async def guard_msg(update: Update):
    uid = update.effective_user.id
    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text(\"❌ Доступ заборонено\")
        return False
    return True

def kb_main():
    text = \"🏠 <b>Головне меню</b>\
\
Оберіть розділ для роботи:\"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(\"💼 Портфель\", callback_data=\"main_portfolio\"), InlineKeyboardButton(\"🎮 Steam\", callback_data=\"main_steam\")],
        [InlineKeyboardButton(\"📈 Акції\", callback_data=\"main_stocks\"), InlineKeyboardButton(\"₿ Криптовалюта\", callback_data=\"main_crypto\")],
        [InlineKeyboardButton(\"📊 Аналітика\", callback_data=\"main_analytics\"), InlineKeyboardButton(\"⚙️ Налаштування\", callback_data=\"main_settings\")]
    ])
    return text, kb

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton(\"⬅️ До меню\", callback_data=\"main_home\")]])

def kb_steam():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(\"CS2\", callback_data=\"steam_cs2\"), InlineKeyboardButton(\"Dota 2\", callback_data=\"steam_dota2\")],
        [InlineKeyboardButton(\"🔄 Оновити ціни\", callback_data=\"steam_update\")],
        [InlineKeyboardButton(\"⬅️ Назад\", callback_data=\"main_home\")]
    ])

def kb_category(cat: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(\"➕ Додати\", callback_data=f\"add_item_{cat}\"), InlineKeyboardButton(\"📋 Список\", callback_data=f\"list_items_{cat}\")],
        [InlineKeyboardButton(\"⬅️ Назад\", callback_data=\"main_home\")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return
    t, k = kb_main()
    await update.message.reply_text(t, reply_markup=k, parse_mode=\"HTML\")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query): return
    data = query.data

    if data == \"main_home\":
        t, k = kb_main()
        await query.edit_message_text(t, reply_markup=k, parse_mode=\"HTML\")
    
    elif data == \"main_portfolio\":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute(\"SELECT category, SUM(price * amount) FROM inventory GROUP BY category\")
        totals = c.fetchall(); conn.close()
        text = \"💼 <b>Загальний Портфель</b>\
\
\"
        grand_total = 0
        if not totals:
            text += \"Портфель порожній.\"
        else:
            for cat, tot in totals:
                text += f\"🔹 <b>{cat.upper()}</b>: ${tot:.2f}\
\"
                grand_total += tot
            text += f\"\
💰 <b>Разом: ${grand_total:.2f}</b>\"
        await query.edit_message_text(text, reply_markup=kb_back(), parse_mode=\"HTML\")

    elif data == \"main_steam\":
        await query.edit_message_text(\"🎮 <b>Steam Інвентар</b>\
Оберіть гру:\", reply_markup=kb_steam(), parse_mode=\"HTML\")
    
    elif data == \"main_stocks\":
        await query.edit_message_text(\"📈 <b>Акції</b>\
Керування акціями:\", reply_markup=kb_category(\"stocks\"), parse_mode=\"HTML\")
    
    elif data == \"main_crypto\":
        await query.edit_message_text(\"₿ <b>Криптовалюта</b>\
Керування криптою:\", reply_markup=kb_category(\"crypto\"), parse_mode=\"HTML\")

    elif data == \"steam_update\":
        await query.edit_message_text(\"🔄 Оновлюю ціни Steam...\")
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute(\"SELECT id, name, category FROM inventory WHERE category IN ('cs2', 'dota2')\")
        items = c.fetchall()
        upd = 0
        for i_id, name, cat in items:
            p = await fetch_steam_price(name, cat)
            if p:
                c.execute(\"UPDATE inventory SET price=? WHERE id=?\", (p, i_id))
                upd += 1
        conn.commit(); conn.close()
        await query.edit_message_text(f\"✅ Оновлено {upd} цін Steam.\", reply_markup=kb_steam())

    elif data.startswith(\"steam_\"):
        game = data.split(\"_\")[1]
        await query.edit_message_text(f\"🎮 <b>{game.upper()}</b>\", reply_markup=kb_category(game), parse_mode=\"HTML\")

    elif data.startswith(\"list_items_\"):
        cat = data.split(\"_\")[2]
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute(\"SELECT name, price, amount FROM inventory WHERE category=?\", (cat,))
        items = c.fetchall(); conn.close()
        if not items:
            await query.edit_message_text(f\"📭 Список {cat.upper()} порожній.\", reply_markup=kb_category(cat))
        else:
            text = f\"📋 <b>{cat.upper()} Предмети:</b>\
\"
            total = 0
            for n, p, a in items:
                sub = p * a; total += sub
                text += f\"\
🔹 {n} - {a} шт. (${p:.2f})\"
            text += f\"\
\
💰 <b>Разом: ${total:.2f}</b>\"
            await query.edit_message_text(text, reply_markup=kb_category(cat), parse_mode=\"HTML\")

    elif data == \"main_analytics\":
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute(\"SELECT category, SUM(price * amount) FROM inventory GROUP BY category\")
        totals = c.fetchall(); conn.close()
        text = \"📊 <b>Аналітика</b>\
\
\"
        if not totals:
            text += \"Немає даних для аналізу.\"
        else:
            grand_total = sum(t[1] for t in totals)
            for cat, tot in totals:
                perc = (tot / grand_total) * 100 if grand_total > 0 else 0
                text += f\"🔸 {cat.upper()}: {perc:.1f}% (${tot:.2f})\
\"
            text += f\"\
💎 Загальна вартість: ${grand_total:.2f}\"
        await query.edit_message_text(text, reply_markup=kb_back(), parse_mode=\"HTML\")

    elif data == \"main_settings\":
        await query.edit_message_text(\"⚙️ <b>Налаштування</b>\
\
Бот працює для користувача: &lt;code&gt;{}&lt;/code&gt;\".format(ALLOWED_USER), reply_markup=kb_back(), parse_mode=\"HTML\")

async def add_item_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.callback_query.data.split(\"_\")[2]
    context.user_data['category'] = cat
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        f\"📝 <b>Додавання в {cat.upper()}</b>\
\
\"
        \"Формат: &lt;code&gt;Назва; Кількість&lt;/code&gt;\
\"
        \"Або: &lt;code&gt;Назва; Ціна; Кількість&lt;/code&gt;\
\
\"
        \"&lt;i&gt;Для Steam використовуйте Market Hash Name.\
Для крипти - повну назву (напр. bitcoin).&lt;/i&gt;\",
        parse_mode=\"HTML\"
    )
    return ADDING_ITEM

async def process_item_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update): return ConversationHandler.END
    cat = context.user_data.get('category')
    try:
        p = [x.strip() for x in update.message.text.split(\";\")]
        if len(p) == 2:
            name, amount = p[0], int(p[1])
            await update.message.reply_text(f\"🔍 Шукаю ціну для {name}...\")
            price = None
            if cat in [\"cs2\", \"dota2\"]:
                price = await fetch_steam_price(name, cat)
            elif cat == \"crypto\":
                price = await fetch_crypto_price(name)
            elif cat == \"stocks\":
                price = await fetch_stock_price(name)
            
            if not price:
                await update.message.reply_text(\"❌ Ціну не знайдено автоматично. Введіть вручну: Назва; Ціна; Кількість\")
                return ADDING_ITEM
        elif len(p) == 3:
            name, price, amount = p[0], float(p[1].replace(\",\", \".\")), int(p[2])
        else:
            await update.message.reply_text(\"❌ Помилка формату. Використовуйте ';' як роздільник.\")
            return ADDING_ITEM

        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute(\"INSERT INTO inventory (category, name, price, amount, date) VALUES (?,?,?,?,?)\",
                  (cat, name, price, amount, datetime.now().isoformat()))
        conn.commit(); conn.close()
        await update.message.reply_text(f\"✅ Додано: {name} ({amount} шт.) за ${price:.2f}\")
        t, k = kb_main(); await update.message.reply_text(t, reply_markup=k, parse_mode=\"HTML\")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f\"❌ Помилка: {e}\")
        return ADDING_ITEM

def main():
    if not BOT_TOKEN:
        print(\"BOT_TOKEN is not set!\")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_item_start, pattern=\"^add_item_\")],
        states={
            ADDING_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_item_addition)]
        },
        fallbacks=[CommandHandler(\"cancel\", start)]
    )
    
    app.add_handler(CommandHandler(\"start\", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print(\"Bot is running...\")
    app.run_polling()

if __name__ == \"__main__\":
    main()
