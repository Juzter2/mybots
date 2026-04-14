import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))

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
    text = "🏠 <b>Головне меню</b>\n\nОбери розділ:"
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

def kb_gifts():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список", callback_data="gifts_list"),
         InlineKeyboardButton("🏷 For Sale", callback_data="gifts_forsale")],
        [InlineKeyboardButton("➕ Додати", callback_data="gifts_add"),
         InlineKeyboardButton("🔄 Оновити", callback_data="gifts_update")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_invest():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Акції", callback_data="invest_stocks"),
         InlineKeyboardButton("🪙 Крипто", callback_data="invest_crypto")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_analytics():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Загальна аналітика", callback_data="analytics_general")],
        [InlineKeyboardButton("🏆 Топ ріст / просадка", callback_data="analytics_topworst")],
        [InlineKeyboardButton("📅 Тиждень vs тиждень", callback_data="analytics_weekvweek")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Зберегти снапшот", callback_data="settings_snapshot")],
        [InlineKeyboardButton("🔄 Оновити всі ціни", callback_data="settings_updateall")],
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

def kb_stocks():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати", callback_data="stocks_add"),
         InlineKeyboardButton("🔄 Оновити", callback_data="stocks_update")],
        [InlineKeyboardButton("🗑 Видалити", callback_data="stocks_deletelist"),
         InlineKeyboardButton("📉 PnL", callback_data="stocks_pnl")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_invest")],
    ])

def kb_crypto():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати", callback_data="crypto_add"),
         InlineKeyboardButton("🔄 Оновити", callback_data="crypto_update")],
        [InlineKeyboardButton("🗑 Видалити", callback_data="crypto_deletelist"),
         InlineKeyboardButton("📉 PnL", callback_data="crypto_pnl")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_invest")],
    ])

def kb_back(data: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=data)]])

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update):
        return
    text, kb = kb_main()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query):
        return
    await query.answer()
    
    data = query.data
    parts = data.split("_")
    section = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    
    if section == "main":
        if action in ("home", "start"):
            text, kb = kb_main()
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        elif action == "portfolio":
            await query.edit_message_text(
                "📊 <b>Портфель</b>\n\nФункція в розробці...",
                reply_markup=kb_portfolio(),
                parse_mode="HTML"
            )
        elif action == "steam":
            await query.edit_message_text(
                "🎮 <b>Steam</b>\n\nФункція в розробці...",
                reply_markup=kb_steam(),
                parse_mode="HTML"
            )
        elif action == "gifts":
            await query.edit_message_text(
                "🎁 <b>Подарунки</b>\n\nФункція в розробці...",
                reply_markup=kb_gifts(),
                parse_mode="HTML"
            )
        elif action == "invest":
            await query.edit_message_text(
                "📈 <b>Інвестиції</b>\n\nФункція в розробці...",
                reply_markup=kb_invest(),
                parse_mode="HTML"
            )
        elif action == "analytics":
            await query.edit_message_text(
                "🔍 <b>Аналітика</b>\n\nФункція в розробці...",
                reply_markup=kb_analytics(),
                parse_mode="HTML"
            )
        elif action == "settings":
            await query.edit_message_text(
                "⚙️ <b>Налаштування</b>\n\nФункція в розробці...",
                reply_markup=kb_settings(),
                parse_mode="HTML"
            )

def main():
    logger.info("Starting bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
