import asyncio
import io
import logging
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "0"))
DB_FILE      = os.environ.get("DB_PATH", "bot_data.db")
BACKUP_DIR   = "backups"
APP_ID_CS2   = 730
APP_ID_DOTA2 = 570

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── DB INIT ───────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS steam_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            game TEXT NOT NULL DEFAULT 'cs2',
            buy_price_usd REAL DEFAULT 0,
            current_price_usd REAL DEFAULT 0,
            net_price_usd REAL DEFAULT 0,
            quantity INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            sold_date TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gift_number TEXT,
            fragment_slug TEXT,
            ton REAL DEFAULT 0,
            floor_ton REAL DEFAULT 0,
            usd_at_add REAL DEFAULT 0,
            current_usd REAL DEFAULT 0,
            net_usd REAL DEFAULT 0,
            ton_rate_at_add REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS gift_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id INTEGER,
            price_usd REAL,
            recorded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT,
            quantity REAL DEFAULT 0,
            buy_price_usd REAL DEFAULT 0,
            current_price_usd REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS crypto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT,
            quantity REAL DEFAULT 0,
            buy_price_usd REAL DEFAULT 0,
            current_price_usd REAL DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS portfolio_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_usd REAL DEFAULT 0,
            recorded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    conn.close()
    logger.info("DB initialised")

# ── STATE ─────────────────────────────────────────────────────────────────────
user_states: dict = {}

def set_state(uid: int, mode=None, **kw):
    if mode is None:
        user_states.pop(uid, None)
    else:
        user_states[uid] = {"mode": mode, **kw}

def get_state(uid: int) -> dict:
    return user_states.get(uid, {})

# ── PRICE FETCHERS ────────────────────────────────────────────────────────────
_ton_cache = 0.0
_ton_ts    = 0.0
TON_TTL    = 900

def get_ton_rate() -> float:
    global _ton_cache, _ton_ts
    if time.time() - _ton_ts < TON_TTL and _ton_cache > 0:
        return _ton_cache
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        rate = float(data["the-open-network"]["usd"])
        _ton_cache, _ton_ts = rate, time.time()
        return rate
    except Exception as e:
        logger.warning(f"TON rate error: {e}")
        return _ton_cache or 3.0

def fetch_steam_price_usd(app_id: int, name: str):
    encoded = urllib.parse.quote(name)
    url = (f"https://steamcommunity.com/market/priceoverview/"
           f"?appid={app_id}&currency=1&market_hash_name={encoded}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        raw = data.get("lowest_price", "")
        return float(re.sub(r"[^\d.]", "", raw)) or None
    except Exception as e:
        logger.warning(f"Steam price {name}: {e}")
        return None

def fetch_steam_search(query: str, app_id: int) -> list:
    encoded = urllib.parse.quote(query)
    url = (f"https://steamcommunity.com/market/search/render/"
           f"?query={encoded}&appid={app_id}&norender=1&count=5")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        results = []
        for item in (data.get("results") or []):
            raw = item.get("sell_price_text", "")
            price = float(re.sub(r"[^\d.]", "", raw)) / 100 if raw else 0.0
            results.append({"name": item.get("hash_name", ""), "price_usd": price})
        return results
    except Exception as e:
        logger.warning(f"Steam search {query}: {e}")
        return []

def fetch_fragment_floor(slug: str):
    url = f"https://fragment.com/gifts/{slug}?sort=price_asc&filter=sale"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
        for p in [r'class="tm-grid-item-value"[^>]*>([\d,\.]+)<', r'icon-ton">([\d,\.]+)']:
            m = re.search(p, html)
            if m:
                return float(m.group(1).replace(",", ""))
        return None
    except Exception as e:
        logger.warning(f"Fragment floor {slug}: {e}")
        return None

def fetch_stock_price(ticker: str):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        price = data["chart"]["result"][0]["meta"].get("regularMarketPrice", 0)
        return float(price) if price else None
    except Exception as e:
        logger.warning(f"Stock {ticker}: {e}")
        return None

COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "TON": "the-open-network",
    "SOL": "solana", "BNB": "binancecoin", "USDT": "tether",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "DOT": "polkadot", "MATIC": "matic-network",
    "SHIB": "shiba-inu", "LTC": "litecoin", "UNI": "uniswap",
}

def fetch_crypto_price(symbol: str):
    try:
        coin_id = COIN_IDS.get(symbol.upper(), symbol.lower())
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return float(data[coin_id]["usd"])
    except Exception as e:
        logger.warning(f"Crypto {symbol}: {e}")
        return None

def name_to_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")

def calc_net_steam(p: float) -> float:
    return round(p * 0.85, 4)

def calc_net_gift(p: float) -> float:
    return round(p * 0.95, 4)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def fmt(val: float) -> str:
    return f"${val:,.2f}"
# ── DB HELPERS ────────────────────────────────────────────────────────────────
def get_steam_total(game: str = None) -> float:
    conn = get_db()
    if game:
        val = conn.execute(
            "SELECT COALESCE(SUM(current_price_usd * quantity), 0) FROM steam_items "
            "WHERE status='active' AND game=?", (game,)
        ).fetchone()[0]
    else:
        val = conn.execute(
            "SELECT COALESCE(SUM(current_price_usd * quantity), 0) FROM steam_items "
            "WHERE status='active'"
        ).fetchone()[0]
    conn.close()
    return float(val or 0.0)

def get_gifts_total() -> float:
    conn = get_db()
    val = conn.execute(
        "SELECT COALESCE(SUM(current_usd), 0) FROM gifts WHERE status IN ('active','forsale')"
    ).fetchone()[0]
    conn.close()
    return float(val or 0.0)

def get_stocks_total() -> float:
    conn = get_db()
    val = conn.execute(
        "SELECT COALESCE(SUM(current_price_usd * quantity), 0) FROM stocks WHERE status='active'"
    ).fetchone()[0]
    conn.close()
    return float(val or 0.0)

def get_crypto_total() -> float:
    conn = get_db()
    val = conn.execute(
        "SELECT COALESCE(SUM(current_price_usd * quantity), 0) FROM crypto WHERE status='active'"
    ).fetchone()[0]
    conn.close()
    return float(val or 0.0)

def calc_portfolio() -> float:
    return get_steam_total() + get_gifts_total() + get_stocks_total() + get_crypto_total()

def record_snapshot() -> float:
    val = calc_portfolio()
    conn = get_db()
    conn.execute(
        "INSERT INTO portfolio_history(portfolio_usd, recorded_at) VALUES(?,?)",
        (val, now_str())
    )
    conn.commit()
    conn.close()
    return val

def get_history(days: int = 30) -> list:
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT portfolio_usd, recorded_at FROM portfolio_history "
        "WHERE recorded_at >= ? ORDER BY recorded_at ASC", (cutoff,)
    ).fetchall()
    conn.close()
    return [{"portfolio_usd": r[0], "date": r[1][:10]} for r in rows]

def get_pnl(days: int):
    h = get_history(days + 1)
    if len(h) < 2:
        return None, None
    old = h[0]["portfolio_usd"]
    new = h[-1]["portfolio_usd"]
    diff = new - old
    pct = diff / old * 100 if old else 0.0
    return diff, pct

def update_all_steam() -> list:
    conn = get_db()
    items = conn.execute("SELECT * FROM steam_items WHERE status='active'").fetchall()
    conn.close()
    results = []
    app_map = {"cs2": APP_ID_CS2, "dota2": APP_ID_DOTA2}
    conn = get_db()
    for it in items:
        price = fetch_steam_price_usd(app_map.get(it["game"], APP_ID_CS2), it["name"])
        if price:
            net = calc_net_steam(price)
            conn.execute(
                "UPDATE steam_items SET current_price_usd=?, net_price_usd=?, updated_at=? WHERE id=?",
                (price, net, now_str(), it["id"])
            )
            results.append(f"✅ {it['name']} → {fmt(price)}")
        else:
            results.append(f"❌ {it['name']}")
    conn.commit()
    conn.close()
    return results

def update_all_gifts() -> list:
    ton = get_ton_rate()
    conn = get_db()
    gifts = conn.execute("SELECT * FROM gifts WHERE status IN ('active','forsale')").fetchall()
    conn.close()
    results = []
    conn = get_db()
    for g in gifts:
        slug = g["fragment_slug"] or name_to_slug(g["name"])
        floor = fetch_fragment_floor(slug)
        if floor:
            usd = floor * ton
            net = calc_net_gift(usd)
            conn.execute(
                "UPDATE gifts SET floor_ton=?, current_usd=?, net_usd=?, updated_at=? WHERE id=?",
                (floor, usd, net, now_str(), g["id"])
            )
            conn.execute(
                "INSERT INTO gift_price_history(gift_id, price_usd, recorded_at) VALUES(?,?,?)",
                (g["id"], usd, now_str())
            )
            results.append(f"✅ {g['name']} → {floor:.1f} TON {fmt(usd)}")
        else:
            results.append(f"❌ {g['name']}")
    conn.commit()
    conn.close()
    return results

def update_all_stocks() -> list:
    conn = get_db()
    stocks = conn.execute("SELECT * FROM stocks WHERE status='active'").fetchall()
    conn.close()
    results = []
    conn = get_db()
    for s in stocks:
        price = fetch_stock_price(s["ticker"])
        if price:
            conn.execute(
                "UPDATE stocks SET current_price_usd=?, updated_at=? WHERE id=?",
                (price, now_str(), s["id"])
            )
            results.append(f"✅ {s['ticker']} → {fmt(price)}")
        else:
            results.append(f"❌ {s['ticker']}")
    conn.commit()
    conn.close()
    return results

def update_all_crypto() -> list:
    conn = get_db()
    coins = conn.execute("SELECT * FROM crypto WHERE status='active'").fetchall()
    conn.close()
    results = []
    conn = get_db()
    for c in coins:
        price = fetch_crypto_price(c["symbol"])
        if price:
            conn.execute(
                "UPDATE crypto SET current_price_usd=?, updated_at=? WHERE id=?",
                (price, now_str(), c["id"])
            )
            results.append(f"✅ {c['symbol']} → {fmt(price)}")
        else:
            results.append(f"❌ {c['symbol']}")
    conn.commit()
    conn.close()
    return results

# ── CHARTS ────────────────────────────────────────────────────────────────────
def make_portfolio_chart() -> bytes:
    history = get_history(30)
    fig, ax = plt.subplots(figsize=(10, 5))
    if len(history) < 2:
        ax.text(0.5, 0.5, "Недостатньо даних", ha="center", va="center", fontsize=14)
    else:
        dates = [datetime.strptime(h["date"], "%Y-%m-%d") for h in history]
        vals  = [h["portfolio_usd"] for h in history]
        ax.plot(dates, vals, color="#4CAF50", linewidth=2.5, marker="o", markersize=4)
        ax.fill_between(dates, vals, alpha=0.15, color="#4CAF50")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate()
        ax.set_ylabel("USD")
        ax.set_title("Портфель за 30 днів")
        ax.grid(True, alpha=0.3)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def make_pie_chart() -> bytes:
    cs2    = get_steam_total("cs2")
    dota   = get_steam_total("dota2")
    gifts  = get_gifts_total()
    stocks = get_stocks_total()
    crypto = get_crypto_total()
    labels, sizes, colors = [], [], []
    if cs2    > 0: labels.append(f"CS2 ${cs2:.0f}");        sizes.append(cs2);    colors.append("#FF5722")
    if dota   > 0: labels.append(f"Dota ${dota:.0f}");      sizes.append(dota);   colors.append("#9C27B0")
    if gifts  > 0: labels.append(f"Подарунки ${gifts:.0f}"); sizes.append(gifts);  colors.append("#2196F3")
    if stocks > 0: labels.append(f"Акції ${stocks:.0f}");   sizes.append(stocks); colors.append("#FF9800")
    if crypto > 0: labels.append(f"Крипто ${crypto:.0f}");  sizes.append(crypto); colors.append("#4CAF50")
    fig, ax = plt.subplots(figsize=(8, 8))
    if sizes:
        ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%", startangle=140)
        ax.set_title("Склад портфеля")
    else:
        ax.text(0.5, 0.5, "Немає даних", ha="center", va="center", fontsize=14)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
  # ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def kb_main():
    total  = calc_portfolio()
    cs2    = get_steam_total("cs2")
    dota   = get_steam_total("dota2")
    gifts  = get_gifts_total()
    invest = get_stocks_total() + get_crypto_total()
    text = (
        f"💼 <b>Портфель</b>\n\n"
        f"🎮 Steam CS2:      <b>{fmt(cs2)}</b>\n"
        f"🎮 Steam Dota 2:   <b>{fmt(dota)}</b>\n"
        f"🎁 Подарунки:      <b>{fmt(gifts)}</b>\n"
        f"📈 Інвестиції:     <b>{fmt(invest)}</b>\n"
        f"{'─'*26}\n"
        f"💼 Разом:          <b>{fmt(total)}</b>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Портфель",     callback_data="main_portfolio"),
         InlineKeyboardButton("🎮 Steam",         callback_data="main_steam")],
        [InlineKeyboardButton("🎁 Подарунки",    callback_data="main_gifts"),
         InlineKeyboardButton("📈 Інвестиції",   callback_data="main_invest")],
        [InlineKeyboardButton("🔍 Аналітика",    callback_data="main_analytics")],
        [InlineKeyboardButton("⚙️ Налаштування", callback_data="main_settings")],
    ])
    return text, kb

def kb_portfolio():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Графік 30 днів", callback_data="portfolio_chart"),
         InlineKeyboardButton("🥧 Pie-діаграма",   callback_data="portfolio_pie")],
        [InlineKeyboardButton("⚡ Швидкий огляд",  callback_data="portfolio_quick")],
        [InlineKeyboardButton("🏠 Додому",          callback_data="main_home")],
    ])

def kb_steam():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CS2",    callback_data="steam_cs2"),
         InlineKeyboardButton("Dota 2", callback_data="steam_dota2"),
         InlineKeyboardButton("Усі",    callback_data="steam_all")],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data="steam_update")],
        [InlineKeyboardButton("🏠 Додому",        callback_data="main_home")],
    ])

def kb_game(game: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати",   callback_data=f"game_add_{game}"),
         InlineKeyboardButton("💰 Продати",  callback_data=f"game_sell_{game}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"game_delete_{game}"),
         InlineKeyboardButton("📋 Продані",  callback_data=f"game_sold_{game}")],
        [InlineKeyboardButton("📉 PnL",      callback_data=f"game_pnl_{game}")],
        [InlineKeyboardButton("◀️ Назад",    callback_data="main_steam")],
    ])

def kb_gifts():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список",        callback_data="gifts_list"),
         InlineKeyboardButton("🏷 For sale",       callback_data="gifts_forsale")],
        [InlineKeyboardButton("➕ Додати",         callback_data="gifts_add"),
         InlineKeyboardButton("🔄 Оновити ціни",  callback_data="gifts_update")],
        [InlineKeyboardButton("🏠 Додому",         callback_data="main_home")],
    ])

def kb_invest():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Акції",  callback_data="invest_stocks"),
         InlineKeyboardButton("🪙 Крипто", callback_data="invest_crypto")],
        [InlineKeyboardButton("🏠 Додому", callback_data="main_home")],
    ])

def kb_stocks():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати",         callback_data="stocks_add"),
         InlineKeyboardButton("🔄 Оновити ціни",  callback_data="stocks_update")],
        [InlineKeyboardButton("🗑 Видалити",       callback_data="stocks_deletelist"),
         InlineKeyboardButton("📊 PnL",            callback_data="stocks_pnl")],
        [InlineKeyboardButton("◀️ Назад",          callback_data="main_invest")],
    ])

def kb_crypto():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати",         callback_data="crypto_add"),
         InlineKeyboardButton("🔄 Оновити ціни",  callback_data="crypto_update")],
        [InlineKeyboardButton("🗑 Видалити",       callback_data="crypto_deletelist"),
         InlineKeyboardButton("📊 PnL",            callback_data="crypto_pnl")],
        [InlineKeyboardButton("◀️ Назад",          callback_data="main_invest")],
    ])

def kb_analytics():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Загальне",           callback_data="analytics_general"),
         InlineKeyboardButton("🏆 Топ по росту",       callback_data="analytics_top")],
        [InlineKeyboardButton("📉 Топ по просадці",    callback_data="analytics_worst"),
         InlineKeyboardButton("📅 Тиждень vs тиждень", callback_data="analytics_weekvweek")],
        [InlineKeyboardButton("🏠 Додому",              callback_data="main_home")],
    ])

def kb_settings():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Зробити снапшот",    callback_data="settings_snapshot"),
         InlineKeyboardButton("🔄 Оновити всі ціни",  callback_data="settings_updateall")],
        [InlineKeyboardButton("🏠 Додому",              callback_data="main_home")],
    ])

def kb_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Додому", callback_data="main_home")]])

def kb_back(cb: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])

# ── GUARDS ────────────────────────────────────────────────────────────────────
async def guard_cb(query) -> bool:
    if query.from_user.id != ALLOWED_USER:
        await query.answer("⛔ Немає доступу", show_alert=True)
        return False
    return True

async def guard_msg(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER:
        await update.message.reply_text("⛔ Немає доступу")
        return False
    return True

# ── COMMAND HANDLERS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_msg(update):
        return
    set_state(update.effective_user.id, None)
    text, kb = kb_main()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query):
        return
    await query.answer()
    uid  = query.from_user.id
    data = query.data
    parts   = data.split("_")
    section = parts[0]
    action  = parts[1] if len(parts) > 1 else ""
    param   = "_".join(parts[2:]) if len(parts) > 2 else ""

    # ── MAIN ──────────────────────────────────────────────────────────────────
    if section == "main":
        if action in ("home", "start"):
            set_state(uid, None)
            text, kb = kb_main()
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "portfolio":
            cs2    = get_steam_total("cs2")
            dota   = get_steam_total("dota2")
            gifts  = get_gifts_total()
            invest = get_stocks_total() + get_crypto_total()
            total  = cs2 + dota + gifts + invest
            text = (
                f"📊 <b>Портфель</b>\n\n"
                f"🎮 Steam CS2:      <b>{fmt(cs2)}</b>\n"
                f"🎮 Steam Dota 2:   <b>{fmt(dota)}</b>\n"
                f"🎁 Подарунки:      <b>{fmt(gifts)}</b>\n"
                f"📈 Інвестиції:     <b>{fmt(invest)}</b>\n"
                f"{'─'*26}\n"
                f"💼 Разом:          <b>{fmt(total)}</b>"
            )
            await query.edit_message_text(text, reply_markup=kb_portfolio(), parse_mode="HTML")

        elif action == "steam":
            cs2  = get_steam_total("cs2")
            dota = get_steam_total("dota2")
            text = (
                f"🎮 <b>Steam</b>\n\n"
                f"CS2:    <b>{fmt(cs2)}</b>\n"
                f"Dota 2: <b>{fmt(dota)}</b>\n"
                f"Разом:  <b>{fmt(cs2+dota)}</b>"
            )
            await query.edit_message_text(text, reply_markup=kb_steam(), parse_mode="HTML")

        elif action == "gifts":
            conn = get_db()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM gifts WHERE status IN ('active','forsale')"
            ).fetchone()[0]
            conn.close()
            total = get_gifts_total()
            text = (
                f"🎁 <b>Подарунки</b>\n\n"
                f"Кількість: <b>{cnt}</b>\n"
                f"Вартість:  <b>{fmt(total)}</b>"
            )
            await query.edit_message_text(text, reply_markup=kb_gifts(), parse_mode="HTML")

        elif action == "invest":
            stocks = get_stocks_total()
            crypto = get_crypto_total()
            text = (
                f"📈 <b>Інвестиції</b>\n\n"
                f"📊 Акції:  <b>{fmt(stocks)}</b>\n"
                f"🪙 Крипто: <b>{fmt(crypto)}</b>\n"
                f"{'─'*20}\n"
                f"Разом:     <b>{fmt(stocks+crypto)}</b>"
            )
            await query.edit_message_text(text, reply_markup=kb_invest(), parse_mode="HTML")

        elif action == "analytics":
            await query.edit_message_text(
                "🔍 <b>Аналітика</b>\n\nОбери розділ:",
                reply_markup=kb_analytics(), parse_mode="HTML"
            )

        elif action == "settings":
            await query.edit_message_text(
                "⚙️ <b>Налаштування</b>",
                reply_markup=kb_settings(), parse_mode="HTML"
            )

    # ── PORTFOLIO ─────────────────────────────────────────────────────────────
    elif section == "portfolio":
        if action == "chart":
            await query.edit_message_text("⏳ Будую графік...")
            chart = await asyncio.to_thread(make_portfolio_chart)
            text, kb = kb_main()
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=io.BytesIO(chart),
                caption="📈 Портфель за 30 днів"
            )
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text, reply_markup=kb, parse_mode="HTML"
            )

        elif action == "pie":
            await query.edit_message_text("⏳ Будую діаграму...")
            chart = await asyncio.to_thread(make_pie_chart)
            text, kb = kb_main()
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=io.BytesIO(chart),
                caption="🥧 Склад портфеля"
            )
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text, reply_markup=kb, parse_mode="HTML"
            )

        elif action == "quick":
            conn = get_db()
            steam = conn.execute(
                "SELECT name, current_price_usd, buy_price_usd, quantity FROM steam_items "
                "WHERE status='active' ORDER BY current_price_usd*quantity DESC LIMIT 5"
            ).fetchall()
            gifts = conn.execute(
                "SELECT name, current_usd, usd_at_add FROM gifts "
                "WHERE status IN ('active','forsale') ORDER BY current_usd DESC LIMIT 5"
            ).fetchall()
            stocks = conn.execute(
                "SELECT ticker, current_price_usd, buy_price_usd, quantity FROM stocks "
                "WHERE status='active' ORDER BY current_price_usd*quantity DESC LIMIT 3"
            ).fetchall()
            crypto = conn.execute(
                "SELECT symbol, current_price_usd, buy_price_usd, quantity FROM crypto "
                "WHERE status='active' ORDER BY current_price_usd*quantity DESC LIMIT 3"
            ).fetchall()
            conn.close()
            lines = ["⚡ <b>Швидкий огляд</b>\n"]
            if steam:
                lines.append("🎮 <b>Steam:</b>")
                for it in steam:
                    pnl = (it["current_price_usd"] - it["buy_price_usd"]) * it["quantity"]
                    sign = "+" if pnl >= 0 else ""
                    lines.append(f"  {it['name'][:25]} {fmt(it['current_price_usd']*it['quantity'])} ({sign}{fmt(pnl)})")
            if gifts:
                lines.append("\n🎁 <b>Подарунки:</b>")
                for g in gifts:
                    pnl = g["current_usd"] - g["usd_at_add"]
                    sign = "+" if pnl >= 0 else ""
                    lines.append(f"  {g['name'][:25]} {fmt(g['current_usd'])} ({sign}{fmt(pnl)})")
            if stocks:
                lines.append("\n📊 <b>Акції:</b>")
                for s in stocks:
                    pnl = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                    sign = "+" if pnl >= 0 else ""
                    lines.append(f"  {s['ticker']} {fmt(s['current_price_usd']*s['quantity'])} ({sign}{fmt(pnl)})")
            if crypto:
                lines.append("\n🪙 <b>Крипто:</b>")
                for c in crypto:
                    pnl = (c["current_price_usd"] - c["buy_price_usd"]) * c["quantity"]
                    sign = "+" if pnl >= 0 else ""
                    lines.append(f"  {c['symbol']} {fmt(c['current_price_usd']*c['quantity'])} ({sign}{fmt(pnl)})")
            await query.edit_message_text(
                "\n".join(lines), reply_markup=kb_portfolio(), parse_mode="HTML"
            )

    # ── STEAM ─────────────────────────────────────────────────────────────────
    elif section == "steam":
        if action in ("cs2", "dota2", "all"):
            game_map = {"cs2": "cs2", "dota2": "dota2", "all": None}
            game = game_map[action]
            title = {"cs2": "CS2", "dota2": "Dota 2", "all": "Всі скіни"}[action]
            conn = get_db()
            if game:
                items = conn.execute(
                    "SELECT * FROM steam_items WHERE status='active' AND game=? "
                    "ORDER BY current_price_usd DESC", (game,)
                ).fetchall()
            else:
                items = conn.execute(
                    "SELECT * FROM steam_items WHERE status='active' "
                    "ORDER BY current_price_usd DESC"
                ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(
                    f"🎮 <b>{title}</b>\n\nСписок порожній.",
                    reply_markup=kb_game(action if action != "all" else "cs2"),
                    parse_mode="HTML"
                )
                return
            total = sum(it["current_price_usd"] * it["quantity"] for it in items)
            lines = [f"🎮 <b>{title}</b> — {fmt(total)}\n"]
            for it in items:
                pnl  = (it["current_price_usd"] - it["buy_price_usd"]) * it["quantity"]
                sign = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                qty_str = f" x{it['quantity']}" if it["quantity"] > 1 else ""
                lines.append(
                    f"{emoji} {it['name'][:28]}{qty_str}\n"
                    f"    {fmt(it['current_price_usd'])} | PnL: {sign}{fmt(pnl)}"
                )
            kb_g = kb_game(action if action != "all" else "cs2")
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=kb_g, parse_mode="HTML"
            )

        elif action == "update":
            await query.edit_message_text("⏳ Оновлюю ціни Steam...")
            results = await asyncio.to_thread(update_all_steam)
            text = "🔄 <b>Steam оновлено:</b>\n" + "\n".join(results) if results else "Немає активних скінів."
            await query.edit_message_text(text[:4000], reply_markup=kb_steam(), parse_mode="HTML")

    # ── GAME ──────────────────────────────────────────────────────────────────
    elif section == "game":
        game       = param if param in ("cs2", "dota2") else "cs2"
        game_title = "CS2" if game == "cs2" else "Dota 2"

        if action == "add":
            set_state(uid, "await_steam_search", game=game,
                      main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔍 Введи назву скіну для пошуку ({game_title}):"
            )
            set_state(uid, "await_steam_search", game=game,
                      prompt_msg_id=prompt.message_id,
                      main_msg_id=query.message.message_id)

        elif action == "sell":
            conn = get_db()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' "
                "ORDER BY current_price_usd DESC", (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(
                    f"🎮 {game_title} — немає активних скінів.",
                    reply_markup=kb_game(game), parse_mode="HTML"
                )
                return
            buttons = []
            for it in items:
                p = it["current_price_usd"] or 0.0
                buttons.append([InlineKeyboardButton(
                    f"{it['name'][:30]} {fmt(p)}",
                    callback_data=f"sellitem_{it['id']}_{game}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"game_back_{game}")])
            await query.edit_message_text(
                f"💰 Вибери скін для продажу ({game_title}):",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )

        elif action == "delete":
            conn = get_db()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' ORDER BY name",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(
                    f"🎮 {game_title} — немає активних скінів.",
                    reply_markup=kb_game(game), parse_mode="HTML"
                )
                return
            buttons = []
            for it in items:
                buttons.append([InlineKeyboardButton(
                    f"🗑 {it['name'][:35]}",
                    callback_data=f"delitem_{it['id']}_{game}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"game_back_{game}")])
            await query.edit_message_text(
                f"🗑 Вибери скін для видалення ({game_title}):",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )
elif action == "sold":
            conn = get_db()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='sold' "
                "ORDER BY sold_date DESC LIMIT 20", (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(
                    f"🎮 {game_title} — немає проданих скінів.",
                    reply_markup=kb_game(game), parse_mode="HTML"
                )
                return
            lines = [f"📋 <b>Продані {game_title}:</b>\n"]
            for it in items:
                buy    = it["buy_price_usd"] or 0.0
                net    = it["net_price_usd"] or 0.0
                profit = net - buy
                sign   = "+" if profit >= 0 else ""
                lines.append(f"• {it['name'][:30]}\n  Куплено: {fmt(buy)} → Продано: {fmt(net)} ({sign}{fmt(profit)})")
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_game(game), parse_mode="HTML"
            )

        elif action == "pnl":
            conn = get_db()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' AND buy_price_usd>0",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(
                    f"📉 {game_title} — немає даних для PnL.",
                    reply_markup=kb_game(game), parse_mode="HTML"
                )
                return
            lines = [f"📉 <b>PnL {game_title}:</b>\n"]
            for it in items:
                pnl  = (it["current_price_usd"] - it["buy_price_usd"]) * it["quantity"]
                pct  = (it["current_price_usd"] - it["buy_price_usd"]) / it["buy_price_usd"] * 100 if it["buy_price_usd"] else 0
                sign = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(f"{emoji} {it['name'][:28]}\n   {sign}{fmt(pnl)} ({sign}{pct:.1f}%)")
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_game(game), parse_mode="HTML"
            )

        elif action == "back":
            await query.edit_message_text(
                f"🎮 <b>Steam</b>", reply_markup=kb_steam(), parse_mode="HTML"
            )

    # ── SELL ITEM ─────────────────────────────────────────────────────────────
    elif section == "sellitem":
        item_id = int(action)
        game    = param
        conn    = get_db()
        it      = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if not it:
            await query.edit_message_text("❌ Скін не знайдено.", reply_markup=kb_steam())
            return
        p   = it["current_price_usd"] or 0.0
        net = calc_net_steam(p)
        text = (
            f"💰 <b>Продати скін?</b>\n\n"
            f"Назва: <b>{it['name']}</b>\n"
            f"Ціна: <b>{fmt(p)}</b>\n"
            f"Нетто (−15%): <b>{fmt(net)}</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Продати", callback_data=f"sellconfirm_{item_id}_{game}"),
             InlineKeyboardButton("❌ Скасувати", callback_data=f"game_sell_{game}")],
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif section == "sellconfirm":
        item_id = int(action)
        game    = param
        conn    = get_db()
        it      = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            conn.close()
            await query.edit_message_text("❌ Скін не знайдено.", reply_markup=kb_steam())
            return
        net    = calc_net_steam(it["current_price_usd"] or 0.0)
        buy    = it["buy_price_usd"] or 0.0
        profit = net - buy
        sign   = "+" if profit >= 0 else ""
        conn.execute(
            "UPDATE steam_items SET status='sold', sold_date=?, updated_at=? WHERE id=?",
            (now_str(), now_str(), item_id)
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"✅ <b>Продано!</b>\n\n{it['name']}\nНетто: {fmt(net)}\nПрибуток: {sign}{fmt(profit)}",
            reply_markup=kb_game(game), parse_mode="HTML"
        )

    elif section == "delitem":
        item_id = int(action)
        game    = param
        conn    = get_db()
        it      = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        if it:
            conn.execute("UPDATE steam_items SET status='deleted' WHERE id=?", (item_id,))
            conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🗑 Видалено: <b>{it['name'] if it else '?'}</b>",
            reply_markup=kb_game(game), parse_mode="HTML"
        )

    # ── STEAM SEARCH RESULT ───────────────────────────────────────────────────
    elif section == "steamresult":
        idx  = int(action)
        game = param
        state = get_state(uid)
        results = state.get("search_results", [])
        if idx >= len(results):
            await query.edit_message_text("❌ Помилка.", reply_markup=kb_game(game))
            return
        item  = results[idx]
        name  = item["name"]
        price = item["price_usd"]
        net   = calc_net_steam(price)
        set_state(uid, "await_steam_buy_price", game=game, name=name,
                  cur_price=price, main_msg_id=state.get("main_msg_id"))
        buttons = [
            [InlineKeyboardButton(
                f"Використати поточну {fmt(price)}",
                callback_data=f"steamusecur_{game}_{price}"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data=f"game_add_{game}")],
        ]
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ <b>{name}</b>\nПоточна ціна: {fmt(price)}\nНетто: {fmt(net)}\n\nВведи ціну покупки (USD):",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
        set_state(uid, "await_steam_buy_price", game=game, name=name,
                  cur_price=price, prompt_msg_id=prompt.message_id,
                  main_msg_id=state.get("main_msg_id"))

    elif section == "steamusecur":
        game      = action
        cur_price = float(param)
        state     = get_state(uid)
        name      = state.get("name", "")
        net       = calc_net_steam(cur_price)
        conn      = get_db()
        conn.execute(
            "INSERT INTO steam_items(name,game,buy_price_usd,current_price_usd,"
            "net_price_usd,quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, game, cur_price, cur_price, net, 1, "active", now_str(), now_str())
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        await query.edit_message_text(
            f"✅ Додано: <b>{name}</b>\nЦіна: {fmt(cur_price)} | Нетто: {fmt(net)}",
            reply_markup=kb_game(game), parse_mode="HTML"
        )

    # ── GIFTS ─────────────────────────────────────────────────────────────────
    elif section == "gifts":
        if action == "list":
            conn = get_db()
            gifts = conn.execute(
                "SELECT * FROM gifts WHERE status IN ('active','forsale') ORDER BY current_usd DESC"
            ).fetchall()
            conn.close()
            if not gifts:
                await query.edit_message_text(
                    "🎁 <b>Подарунки</b>\n\nСписок порожній.",
                    reply_markup=kb_gifts(), parse_mode="HTML"
                )
                return
            ton  = get_ton_rate()
            total = sum(g["current_usd"] for g in gifts)
            lines = [f"🎁 <b>Подарунки</b> — {fmt(total)} | TON: ${ton:.3f}\n"]
            buttons = []
            for g in gifts:
                cur  = g["current_usd"] or 0.0
                add  = g["usd_at_add"] or 0.0
                pnl  = cur - add
                sign = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                floor_str = f"{g['floor_ton']:.1f} TON" if g["floor_ton"] else "?"
                status_str = " 🏷" if g["status"] == "forsale" else ""
                lines.append(
                    f"{emoji} {g['name'][:25]}{status_str}\n"
                    f"   Floor: {floor_str} | {fmt(cur)} | PnL: {sign}{fmt(pnl)}"
                )
                buttons.append([InlineKeyboardButton(
                    f"{g['name'][:35]}",
                    callback_data=f"giftdetail_{g['id']}"
                )])
            buttons.append([InlineKeyboardButton("🏠 Додому", callback_data="main_home")])
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )

        elif action == "forsale":
            conn = get_db()
            gifts = conn.execute(
                "SELECT * FROM gifts WHERE status='forsale' ORDER BY current_usd DESC"
            ).fetchall()
            conn.close()
            if not gifts:
                await query.edit_message_text(
                    "🏷 <b>For Sale</b>\n\nНемає подарунків на продажу.",
                    reply_markup=kb_gifts(), parse_mode="HTML"
                )
                return
            ton   = get_ton_rate()
            total = sum(g["current_usd"] for g in gifts)
            lines = [f"🏷 <b>For Sale</b> — {fmt(total)}\n"]
            for g in gifts:
                lines.append(
                    f"• {g['name'][:30]}\n"
                    f"  {g['floor_ton']:.1f} TON | {fmt(g['current_usd'])}"
                )
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_gifts(), parse_mode="HTML"
            )

        elif action == "add":
            set_state(uid, "await_gift_name", main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🎁 Введи назву подарунку і кількість TON через кому:\n<i>Наприклад: Plush Pepe, 10</i>",
                parse_mode="HTML"
            )
            set_state(uid, "await_gift_name",
                      prompt_msg_id=prompt.message_id,
                      main_msg_id=query.message.message_id)

        elif action == "update":
            await query.edit_message_text("⏳ Оновлюю ціни подарунків...")
            results = await asyncio.to_thread(update_all_gifts)
            text = "🔄 <b>Подарунки оновлено:</b>\n" + "\n".join(results) if results else "Немає активних подарунків."
            await query.edit_message_text(text[:4000], reply_markup=kb_gifts(), parse_mode="HTML")

    # ── GIFT DETAIL ───────────────────────────────────────────────────────────
    elif section == "giftdetail":
        gift_id = int(action)
        conn    = get_db()
        g       = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("❌ Подарунок не знайдено.", reply_markup=kb_gifts())
            return
        cur    = g["current_usd"] or 0.0
        add    = g["usd_at_add"] or 0.0
        ton    = g["ton"] or 0.0
        floor  = g["floor_ton"] or 0.0
        profit = cur - add
        sign   = "+" if profit >= 0 else ""
        text = (
            f"🎁 <b>{g['name']}</b>\n\n"
            f"TON куплено: <b>{ton:.2f} TON</b>\n"
            f"Floor: <b>{floor:.1f} TON</b>\n"
            f"Додано за: <b>{fmt(add)}</b>\n"
            f"Зараз: <b>{fmt(cur)}</b>\n"
            f"PnL: <b>{sign}{fmt(profit)}</b>\n"
            f"Статус: <b>{g['status']}</b>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏷 For Sale (floor)", callback_data=f"giftsellfloor_{gift_id}"),
             InlineKeyboardButton("💰 Кастомна ціна",   callback_data=f"giftsellcustom_{gift_id}")],
            [InlineKeyboardButton("🧾 Історія цін",     callback_data=f"gifthistory_{gift_id}"),
             InlineKeyboardButton("🗑 Видалити",         callback_data=f"giftdelete_{gift_id}")],
          ",        callback_data="gifts_list")],
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

    elif section == "giftsellfloor":
        gift_id = int(action)
        conn    = get_db()
        g       = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("❌ Подарунок не знайдено.", reply_markup=kb_gifts())
            return
        floor   = g["floor_ton"] or g["ton"] or 0.0
        ton_rate = get_ton_rate()
        cur_usd  = floor * ton_rate
        net_usd  = calc_net_gift(cur_usd)
        conn = get_db()
        conn.execute(
            "UPDATE gifts SET status='forsale', floor_ton=?, current_usd=?, net_usd=?, updated_at=? WHERE id=?",
            (floor, cur_usd, net_usd, now_str(), gift_id)
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"✅ <b>{g['name']}</b> виставлено на продаж!\n"
            f"Floor: {floor:.1f} TON | {fmt(cur_usd)} | Нетто: {fmt(net_usd)}",
            reply_markup=kb_gifts(), parse_mode="HTML"
        )

    elif section == "giftsellcustom":
        gift_id = int(action)
        set_state(uid, "await_gift_sell_ton", gift_id=gift_id,
                  main_msg_id=query.message.message_id)
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="💰 Введи ціну продажу в TON:\n<i>Наприклад: 15.5</i>",
            parse_mode="HTML"
        )
        set_state(uid, "await_gift_sell_ton", gift_id=gift_id,
                  prompt_msg_id=prompt.message_id,
                  main_msg_id=query.message.message_id)

    elif section == "gifthistory":
        gift_id = int(action)
        conn    = get_db()
        g       = conn.execute("SELECT name FROM gifts WHERE id=?", (gift_id,)).fetchone()
        hist    = conn.execute(
            "SELECT price_usd, recorded_at FROM gift_price_history "
            "WHERE gift_id=? ORDER BY recorded_at DESC LIMIT 10", (gift_id,)
        ).fetchall()
        conn.close()
        name = g["name"] if g else "?"
        if not hist:
            await query.edit_message_text(
                f"🧾 <b>{name}</b>\n\nІсторії цін немає.",
                reply_markup=kb_back(f"giftdetail_{gift_id}"), parse_mode="HTML"
            )
            return
        lines = [f"🧾 <b>Історія цін: {name}</b>\n"]
        for h in hist:
            lines.append(f"• {h['recorded_at'][:10]} — {fmt(h['price_usd'])}")
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=kb_back(f"giftdetail_{gift_id}"), parse_mode="HTML"
        )

    elif section == "giftdelete":
        gift_id = int(action)
        conn    = get_db()
        g       = conn.execute("SELECT name FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.execute("UPDATE gifts SET status='deleted' WHERE id=?", (gift_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🗑 Видалено: <b>{g['name'] if g else '?'}</b>",
            reply_markup=kb_gifts(), parse_mode="HTML"
        )

    # ── INVEST ────────────────────────────────────────────────────────────────
    elif section == "invest":
        if action == "stocks":
            conn = get_db()
            stocks = conn.execute(
                "SELECT * FROM stocks WHERE status='active' ORDER BY current_price_usd*quantity DESC"
            ).fetchall()
            conn.close()
            if not stocks:
                await query.edit_message_text(
                    "📊 <b>Акції</b>\n\nСписок порожній.",
                    reply_markup=kb_stocks(), parse_mode="HTML"
                )
                return
            total_val = sum(s["current_price_usd"] * s["quantity"] for s in stocks)
            total_buy = sum(s["buy_price_usd"] * s["quantity"] for s in stocks)
            total_pnl = total_val - total_buy
            sign      = "+" if total_pnl >= 0 else ""
            lines = [
                f"📊 <b>Акції</b>\n",
                f"Вартість:   <b>{fmt(total_val)}</b>",
                f"Куплено за: <b>{fmt(total_buy)}</b>",
                f"PnL:        <b>{sign}{fmt(total_pnl)}</b>\n"
            ]
            for s in stocks:
                val  = s["current_price_usd"] * s["quantity"]
                pnl  = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                pct  = pnl / (s["buy_price_usd"] * s["quantity"]) * 100 if s["buy_price_usd"] else 0
                sign2 = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"{emoji} <b>{s['ticker']}</b> x{s['quantity']:.2f}\n"
                    f"   {fmt(s['current_price_usd'])} | {fmt(val)} | {sign2}{fmt(pnl)} ({sign2}{pct:.1f}%)"
                )
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_stocks(), parse_mode="HTML"
            )

        elif action == "crypto":
            conn = get_db()
            coins = conn.execute(
                "SELECT * FROM crypto WHERE status='active' ORDER BY current_price_usd*quantity DESC"
            ).fetchall()
            conn.close()
            if not coins:
                await query.edit_message_text(
                    "🪙 <b>Крипто</b>\n\nСписок порожній.",
                    reply_markup=kb_crypto(), parse_mode="HTML"
                )
                return
            total_val = sum(c["current_price_usd"] * c["quantity"] for c in coins)
            total_buy = sum(c["buy_price_usd"] * c["quantity"] for c in coins)
            total_pnl = total_val - total_buy
            sign      = "+" if total_pnl >= 0 else ""
            lines = [
                f"🪙 <b>Крипто</b>\n",
                f"Вартість:   <b>{fmt(total_val)}</b>",
                f"Куплено за: <b>{fmt(total_buy)}</b>",
                f"PnL:        <b>{sign}{fmt(total_pnl)}</b>\n"
            ]
            for c in coins:
                val  = c["current_price_usd"] * c["quantity"]
                pnl  = (c["current_price_usd"] - c["buy_price_usd"]) * c["quantity"]
                pct  = pnl / (c["buy_price_usd"] * c["quantity"]) * 100 if c["buy_price_usd"] else 0
                sign2 = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"{emoji} <b>{c['symbol']}</b> x{c['quantity']:.4f}\n"
                    f"   {fmt(c['current_price_usd'])} | {fmt(val)} | {sign2}{fmt(pnl)} ({sign2}{pct:.1f}%)"
                )
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_crypto(), parse_mode="HTML"
            )

    # ── STOCKS ────────────────────────────────────────────────────────────────
    elif section == "stocks":
        if action == "add":
            set_state(uid, "await_stock_ticker", main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📊 Введи тикер акції:\n<i>Наприклад: AAPL, TSLA, NVDA, MSFT</i>",
                parse_mode="HTML"
            )
            set_state(uid, "await_stock_ticker",
                      prompt_msg_id=prompt.message_id,
                      main_msg_id=query.message.message_id)

        elif action == "update":
            await query.edit_message_text("⏳ Оновлюю ціни акцій...")
            results = await asyncio.to_thread(update_all_stocks)
            text = "🔄 <b>Акції оновлено:</b>\n" + "\n".join(results) if results else "Немає активних акцій."
            await query.edit_message_text(text[:4000], reply_markup=kb_stocks(), parse_mode="HTML")

        elif action == "deletelist":
            conn = get_db()
            stocks = conn.execute("SELECT * FROM stocks WHERE status='active'").fetchall()
            conn.close()
            if not stocks:
                await query.edit_message_text(
                    "📊 Немає активних акцій.", reply_markup=kb_stocks(), parse_mode="HTML"
                )
                return
            buttons = []
            for s in stocks:
                val  = s["current_price_usd"] * s["quantity"]
                pnl  = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                sign = "+" if pnl >= 0 else ""
                buttons.append([InlineKeyboardButton(
                    f"🗑 {s['ticker']} x{s['quantity']:.2f} {sign}{fmt(pnl)}",
                    callback_data=f"stockdel_{s['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="main_invest")])
            await query.edit_message_text(
                "🗑 Вибери акцію для видалення:",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )

        elif action == "pnl":
            conn = get_db()
            stocks = conn.execute(
                "SELECT * FROM stocks WHERE status='active' AND buy_price_usd>0 "
                "ORDER BY (current_price_usd-buy_price_usd)*quantity DESC"
            ).fetchall()
            conn.close()
            if not stocks:
                await query.edit_message_text(
                    "📊 Немає даних для PnL.", reply_markup=kb_stocks(), parse_mode="HTML"
                )
                return
            lines = ["📊 <b>PnL Акції:</b>\n"]
            for s in stocks:
                pnl  = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                pct  = pnl / (s["buy_price_usd"] * s["quantity"]) * 100 if s["buy_price_usd"] else 0
                sign = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(f"{emoji} <b>{s['ticker']}</b> — {sign}{fmt(pnl)} ({sign}{pct:.1f}%)")
            await query.edit_message_text(
                "\n".join(lines), reply_markup=kb_stocks(), parse_mode="HTML"
            )

    elif section == "stockdel":
        stock_id = int(action)
        conn     = get_db()
        s        = conn.execute("SELECT * FROM stocks WHERE id=?", (stock_id,)).fetchone()
        if s:
            conn.execute("UPDATE stocks SET status='removed' WHERE id=?", (stock_id,))
            conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🗑 Видалено: <b>{s['ticker'] if s else '?'}</b>",
            reply_markup=kb_stocks(), parse_mode="HTML"
        )

    # ── CRYPTO ────────────────────────────────────────────────────────────────
    elif section == "crypto":
        if action == "add":
            set_state(uid, "await_crypto_symbol", main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "🪙 Введи символ монети:\n"
                    "<i>Наприклад: BTC, ETH, TON, SOL, BNB, DOGE</i>"
                ),
                parse_mode="HTML"
            )
            set_state(uid, "await_crypto_symbol",
                      prompt_msg_id=prompt.message_id,
                      main_msg_id=query.message.message_id)

        elif action == "update":
            await query.edit_message_text("⏳ Оновлюю ціни крипто...")
            results = await asyncio.to_thread(update_all_crypto)
            text = "🔄 <b>Крипто оновлено:</b>\n" + "\n".join(results) if results else "Немає активних монет."
            await query.edit_message_text(text[:4000], reply_markup=kb_crypto(), parse_mode="HTML")

        elif action == "deletelist":
            conn = get_db()
            coins = conn.execute("SELECT * FROM crypto WHERE status='active'").fetchall()
            conn.close()
            if not coins:
                await query.edit_message_text(
                    "🪙 Немає активних монет.", reply_markup=kb_crypto(), parse_mode="HTML"
                )
                return
            buttons = []
            for c in coins:
                val  = c["current_price_usd"] * c["quantity"]
                pnl  = (c["current_price_usd"] - c["buy_price_usd"]) * c["quantity"]
                sign = "+" if pnl >= 0 else ""
                buttons.append(
(
                    f"🗑 {c['symbol']} x{c['quantity']:.4f} {sign}{fmt(pnl)}",
                    callback_data=f"cryptodel_{c['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="main_invest")])
            await query.edit_message_text(
                "🗑 Вибери монету для видалення:",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
            )

        elif action == "pnl":
            conn = get_db()
            coins = conn.execute(
                "SELECT * FROM crypto WHERE status='active' AND buy_price_usd>0 "
                "ORDER BY (current_price_usd-buy_price_usd)*quantity DESC"
            ).fetchall()
            conn.close()
            if not coins:
                await query.edit_message_text(
                    "🪙 Немає даних для PnL.", reply_markup=kb_crypto(), parse_mode="HTML"
                )
                return
            lines = ["🪙 <b>PnL Крипто:</b>\n"]
            for c in coins:
                pnl  = (c["current_price_usd"] - c["buy_price_usd"]) * c["quantity"]
                pct  = pnl / (c["buy_price_usd"] * c["quantity"]) * 100 if c["buy_price_usd"] else 0
                sign = "+" if pnl >= 0 else ""
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(f"{emoji} <b>{c['symbol']}</b> — {sign}{fmt(pnl)} ({sign}{pct:.1f}%)")
            await query.edit_message_text(
                "\n".join(lines), reply_markup=kb_crypto(), parse_mode="HTML"
            )

    elif section == "cryptodel":
        coin_id = int(action)
        conn    = get_db()
        c       = conn.execute("SELECT * FROM crypto WHERE id=?", (coin_id,)).fetchone()
        if c:
            conn.execute("UPDATE crypto SET status='removed' WHERE id=?", (coin_id,))
            conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🗑 Видалено: <b>{c['symbol'] if c else '?'}</b>",
            reply_markup=kb_crypto(), parse_mode="HTML"
        )

    # ── ANALYTICS ─────────────────────────────────────────────────────────────
    elif section == "analytics":
        if action == "general":
            total = calc_portfolio()
            conn  = get_db()
            steam_buy  = conn.execute("SELECT COALESCE(SUM(buy_price_usd*quantity),0) FROM steam_items WHERE status='active'").fetchone()[0] or 0.0
            gifts_buy  = conn.execute("SELECT COALESCE(SUM(usd_at_add),0) FROM gifts WHERE status IN ('active','forsale')").fetchone()[0] or 0.0
            stocks_buy = conn.execute("SELECT COALESCE(SUM(buy_price_usd*quantity),0) FROM stocks WHERE status='active'").fetchone()[0] or 0.0
            crypto_buy = conn.execute("SELECT COALESCE(SUM(buy_price_usd*quantity),0) FROM crypto WHERE status='active'").fetchone()[0] or 0.0
            conn.close()
            invested = steam_buy + gifts_buy + stocks_buy + crypto_buy
            pnl_all  = total - invested
            pct_all  = pnl_all / invested * 100 if invested else 0.0
            sign     = "+" if pnl_all >= 0 else ""
            pnl1d, pct1d = get_pnl(1)
            pnl7d, pct7d = get_pnl(7)
            pnl30d, pct30d = get_pnl(30)
            def pnl_str(p, pct):
                if p is None:
                    return "немає даних"
                s = "+" if p >= 0 else ""
                return f"{s}{fmt(p)} ({s}{pct:.1f}%)"
            text = (
                f"📊 <b>Загальна аналітика</b>\n\n"
                f"💼 Разом:      <b>{fmt(total)}</b>\n"
                f"💰 Інвестовано: <b>{fmt(invested)}</b>\n"
                f"📈 PnL all-time: <b>{sign}{fmt(pnl_all)} ({sign}{pct_all:.1f}%)</b>\n\n"
                f"⏱ PnL за 1д:  <b>{pnl_str(pnl1d, pct1d or 0)}</b>\n"
                f"⏱ PnL за 7д:  <b>{pnl_str(pnl7d, pct7d or 0)}</b>\n"
                f"⏱ PnL за 30д: <b>{pnl_str(pnl30d, pct30d or 0)}</b>"
            )
            await query.edit_message_text(text, reply_markup=kb_analytics(), parse_mode="HTML")

        elif action == "top":
            conn = get_db()
            steam = conn.execute(
                "SELECT name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM steam_items "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct DESC LIMIT 5"
            ).fetchall()
            stocks = conn.execute(
                "SELECT ticker as name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM stocks "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct DESC LIMIT 3"
            ).fetchall()
            crypto = conn.execute(
                "SELECT symbol as name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM crypto "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct DESC LIMIT 3"
            ).fetchall()
            gifts = conn.execute(
                "SELECT name, (current_usd-usd_at_add)/NULLIF(usd_at_add,0)*100 as pct, "
                "(current_usd-usd_at_add) as pnl FROM gifts "
                "WHERE status IN ('active','forsale') AND usd_at_add>0 ORDER BY pct DESC LIMIT 3"
            ).fetchall()
            conn.close()
            lines = ["🏆 <b>Топ по росту</b>\n"]
            if steam:
                lines.append("🎮 <b>Steam:</b>")
                for it in steam:
                    lines.append(f"  📈 {it['name'][:28]} +{it['pct']:.1f}% (+{fmt(it['pnl'])})")
            if gifts:
                lines.append("\n🎁 <b>Подарунки:</b>")
                for g in gifts:
                    lines.append(f"  📈 {g['name'][:28]} +{g['pct']:.1f}% (+{fmt(g['pnl'])})")
            if stocks:
                lines.append("\n📊 <b>Акції:</b>")
                for s in stocks:
                    lines.append(f"  📈 {s['name']} +{s['pct']:.1f}% (+{fmt(s['pnl'])})")
            if crypto:
                lines.append("\n🪙 <b>Крипто:</b>")
                for c in crypto:
                    lines.append(f"  📈 {c['name']} +{c['pct']:.1f}% (+{fmt(c['pnl'])})")
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_analytics(), parse_mode="HTML"
            )

        elif action == "worst":
            conn = get_db()
            steam = conn.execute(
                "SELECT name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM steam_items "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct ASC LIMIT 5"
            ).fetchall()
            stocks = conn.execute(
                "SELECT ticker as name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM stocks "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct ASC LIMIT 3"
            ).fetchall()
            crypto = conn.execute(
                "SELECT symbol as name, (current_price_usd-buy_price_usd)/NULLIF(buy_price_usd,0)*100 as pct, "
                "(current_price_usd-buy_price_usd)*quantity as pnl FROM crypto "
                "WHERE status='active' AND buy_price_usd>0 ORDER BY pct ASC LIMIT 3"
            ).fetchall()
            gifts = conn.execute(
                "SELECT name, (current_usd-usd_at_add)/NULLIF(usd_at_add,0)*100 as pct, "
                "(current_usd-usd_at_add) as pnl FROM gifts "
                "WHERE status IN ('active','forsale') AND usd_at_add>0 ORDER BY pct ASC LIMIT 3"
            ).fetchall()
            conn.close()
            lines = ["📉 <b>Топ по просадці</b>\n"]
            if steam:
                lines.append("🎮 <b>Steam:</b>")
                for it in steam:
                    lines.append(f"  📉 {it['name'][:28]} {it['pct']:.1f}% ({fmt(it['pnl'])})")
            if gifts:
                lines.append("\n🎁 <b>Подарунки:</b>")
                for g in gifts:
                    lines.append(f"  📉 {g['name'][:28]} {g['pct']:.1f}% ({fmt(g['pnl'])})")
            if stocks:
                lines.append("\n📊 <b>Акції:</b>")
                for s in stocks:
                    lines.append(f"  📉 {s['name']} {s['pct']:.1f}% ({fmt(s['pnl'])})")
            if crypto:
                lines.append("\n🪙 <b>Крипто:</b>")
                for c in crypto:
                    lines.append(f"  📉 {c['name']} {c['pct']:.1f}% ({fmt(c['pnl'])})")
            await query.edit_message_text(
                "\n".join(lines)[:4000], reply_markup=kb_analytics(), parse_mode="HTML"
            )

        elif action == "weekvweek":
            history = get_history(21)
            today   = datetime.utcnow().date()
            week_start = today - timedelta(days=today.weekday())
            prev_week  = week_start - timedelta(days=7)
            curr_vals  = [h for h in history if str(week_start) <= h["date"] <= str(today)]
            prev_vals  = [h for h in history if str(prev_week) <= h["date"] < str(week_start)]
            curr_avg   = sum(h["portfolio_usd"] for h in curr_vals) / len(curr_vals) if curr_vals else None
            prev_avg   = sum(h["portfolio_usd"] for h in prev_vals) / len(prev_vals) if prev_vals else None
            lines = ["📅 <b>Тиждень vs тиждень</b>\n"]
            if curr_avg and prev_avg:
                diff = curr_avg - prev_avg
                pct  = diff / prev_avg * 100 if prev_avg else 0.0
                sign = "+" if diff >= 0 else ""
                lines.append(f"Цей тиждень:  <b>{fmt(curr_avg)}</b>")
                lines.append(f"Минулий:      <b>{fmt(prev_avg)}</b>")
                lines.append(f"Різниця:      <b>{sign}{fmt(diff)} ({sign}{pct:.1f}%)</b>")
            else:
                lines.append("Недостатньо даних.")
            await query.edit_message_text(
                "\n".join(lines), reply_markup=kb_analytics(), parse_mode="HTML"
            )

    # ── SETTINGS ──────────────────────────────────────────────────────────────
    elif section == "settings":
        if action == "snapshot":
            val = await asyncio.to_thread(record_snapshot)
            await query.edit_message_text(
                f"📸 Снапшот збережено!\nВартість портфеля: <b>{fmt(val)}</b>",
                reply_markup=kb_settings(), parse_mode="HTML"
            )

        elif action == "updateall":
            await query.edit_message_text("⏳ Оновлюю всі ціни...")
            s = await asyncio.to_thread(update_all_steam)
            g = await asyncio.to_thread(update_all_gifts)
            st = await asyncio.to_thread(update_all_stocks)
            cr = await asyncio.to_thread(update_all_crypto)
            total = calc_portfolio()
            lines = [
                "✅ <b>Всі ціни оновлено!</b>\n",
                f"🎮 Steam: {len(s)} скінів",
                f"🎁 Подарунки: {len(g)}",
                f"📊 Акції: {len(st)}",
                f"🪙 Крипто: {len(cr)}",
                f"\n💼 Портфель: <b>{fmt(total)}</b>"
            ]
            await query.edit_message_text(
                "\n".join(lines), reply_markup=kb_settings(),
              parse_mode="HTML"
            )                  
# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not await guard_msg(update):
        return
    uid      = update.effective_user.id
    text     = update.message.text.strip()
    state    = get_state(uid)
    mode     = state.get("mode", "")
    main_msg = state.get("main_msg_id")
    prompt   = state.get("prompt_msg_id")

    async def clean_prompts():
        if prompt:
            try:
                await context.bot.delete_message(update.effective_chat.id, prompt)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass

    # ── STEAM SEARCH ──────────────────────────────────────────────────────────
    if mode == "await_steam_search":
        game    = state.get("game", "cs2")
        app_id  = APP_ID_CS2 if game == "cs2" else APP_ID_DOTA2
        title   = "CS2" if game == "cs2" else "Dota 2"
        results = await asyncio.to_thread(fetch_steam_search, text, app_id)
        await clean_prompts()
        if not results:
            msg = await context.bot.send_message(
                update.effective_chat.id,
                f"❌ Нічого не знайдено для «{text}» в {title}.",
                reply_markup=kb_game(game)
            )
            set_state(uid, None)
            return
        set_state(uid, "await_steam_search_result", game=game,
                  search_results=results, main_msg_id=main_msg)
        buttons = []
        for idx, r in enumerate(results):
            net = calc_net_steam(r["price_usd"])
            buttons.append([InlineKeyboardButton(
                f"{r['name'][:35]} {fmt(r['price_usd'])} (нетто {fmt(net)})",
                callback_data=f"steamresult_{idx}_{game}"
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"game_add_{game}")])
        await context.bot.send_message(
            update.effective_chat.id,
            f"🔍 Результати для «{text}» ({title}):",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ── STEAM BUY PRICE ───────────────────────────────────────────────────────
    elif mode == "await_steam_buy_price":
        try:
            buy_price = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число, наприклад: 15.50"
            )
            return
        await clean_prompts()
        game      = state.get("game", "cs2")
        name      = state.get("name", "")
        cur_price = state.get("cur_price", buy_price)
        net       = calc_net_steam(cur_price)
        conn      = get_db()
        conn.execute(
            "INSERT INTO steam_items(name,game,buy_price_usd,current_price_usd,"
            "net_price_usd,quantity,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, game, buy_price, cur_price, net, 1, "active", now_str(), now_str())
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        pnl  = cur_price - buy_price
        sign = "+" if pnl >= 0 else ""
        msg  = (
            f"✅ <b>Додано!</b>\n\n"
            f"Назва: <b>{name}</b>\n"
            f"Куплено за: <b>{fmt(buy_price)}</b>\n"
            f"Поточна ціна: <b>{fmt(cur_price)}</b>\n"
            f"Нетто: <b>{fmt(net)}</b>\n"
            f"PnL: <b>{sign}{fmt(pnl)}</b>"
        )
        await context.bot.send_message(
            update.effective_chat.id, msg,
            reply_markup=kb_game(game), parse_mode="HTML"
        )

    # ── GIFT NAME + TON ───────────────────────────────────────────────────────
    elif mode == "await_gift_name":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 2:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Формат: <b>Назва, TON</b>\nНаприклад: Plush Pepe, 10",
                parse_mode="HTML"
            )
            return
        name = parts[0]
        try:
            ton = float(parts[1].replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірна кількість TON. Наприклад: Plush Pepe, 10"
            )
            return
        await clean_prompts()
        ton_rate = await asyncio.to_thread(get_ton_rate)
        slug     = name_to_slug(name)
        floor    = await asyncio.to_thread(fetch_fragment_floor, slug)
        cur_usd  = (floor or ton) * ton_rate
        net_usd  = calc_net_gift(cur_usd)
        floor_str = f"{floor:.1f} TON" if floor else "не знайдено"
        set_state(uid, "await_gift_confirm",
                  name=name, ton=ton, floor=floor, ton_rate=ton_rate,
                  cur_usd=cur_usd, net_usd=net_usd, slug=slug,
                  main_msg_id=main_msg)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Додати",    callback_data="giftconfirm_yes"),
             InlineKeyboardButton("❌ Скасувати", callback_data="giftconfirm_no")],
        ])
        await context.bot.send_message(
            update.effective_chat.id,
            f"🎁 <b>{name}</b>\n\n"
            f"TON куплено: <b>{ton:.1f} TON</b>\n"
            f"Floor: <b>{floor_str}</b>\n"
            f"Поточна вартість: <b>{fmt(cur_usd)}</b>\n"
            f"Нетто (−5%): <b>{fmt(net_usd)}</b>\n\n"
            f"Додати?",
            reply_markup=kb, parse_mode="HTML"
        )

    # ── GIFT SELL CUSTOM TON ──────────────────────────────────────────────────
    elif mode == "await_gift_sell_ton":
        try:
            ton_sell = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число TON, наприклад: 15.5"
            )
            return
        await clean_prompts()
        gift_id  = state.get("gift_id")
        ton_rate = await asyncio.to_thread(get_ton_rate)
        cur_usd  = ton_sell * ton_rate
        net_usd  = calc_net_gift(cur_usd)
        conn     = get_db()
        conn.execute(
            "UPDATE gifts SET status='forsale', floor_ton=?, current_usd=?, net_usd=?, updated_at=? WHERE id=?",
            (ton_sell, cur_usd, net_usd, now_str(), gift_id)
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ Виставлено на продаж!\n"
            f"Ціна: <b>{ton_sell:.1f} TON</b> | {fmt(cur_usd)} | Нетто: {fmt(net_usd)}",
            reply_markup=kb_gifts(), parse_mode="HTML"
        )

    # ── STOCK TICKER ──────────────────────────────────────────────────────────
    elif mode == "await_stock_ticker":
        ticker = text.upper().strip()
        await clean_prompts()
        price = await asyncio.to_thread(fetch_stock_price, ticker)
        if not price:
            await context.bot.send_message(
                update.effective_chat.id,
                f"❌ Не вдалось знайти тикер <b>{ticker}</b>. Перевір назву.",
                reply_markup=kb_stocks(), parse_mode="HTML"
            )
            set_state(uid, None)
            return
        set_state(uid, "await_stock_qty", ticker=ticker, cur_price=price, main_msg_id=main_msg)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"Використати поточну {fmt(price)}",
                callback_data=f"stockusecur_{ticker}_{price}"
            )],
        ])
        prompt_msg = await context.bot.send_message(
            update.effective_chat.id,
            f"📊 <b>{ticker}</b> — поточна ціна: <b>{fmt(price)}</b>\n\nВведи кількість акцій:",
            reply_markup=kb, parse_mode="HTML"
        )
        set_state(uid, "await_stock_qty", ticker=ticker, cur_price=price,
                  prompt_msg_id=prompt_msg.message_id, main_msg_id=main_msg)

    # ── STOCK QTY ─────────────────────────────────────────────────────────────
    elif mode == "await_stock_qty":
        try:
            qty = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число, наприклад: 10 або 0.5"
            )
            return
        await clean_prompts()
        ticker    = state.get("ticker", "")
        cur_price = state.get("cur_price", 0.0)
        set_state(uid, "await_stock_buy_price", ticker=ticker, qty=qty,
                  cur_price=cur_price, main_msg_id=main_msg)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"Використати поточну {fmt(cur_price)}",
                callback_data=f"stockusecur_{ticker}_{cur_price}"
            )],
        ])
        prompt_msg = await context.bot.send_message(
            update.effective_chat.id,
            f"📊 <b>{ticker}</b> x{qty}\n\nВведи ціну покупки (USD):",
            reply_markup=kb, parse_mode="HTML"
        )
        set_state(uid, "await_stock_buy_price", ticker=ticker, qty=qty,
                  cur_price=cur_price, prompt_msg_id=prompt_msg.message_id,
                  main_msg_id=main_msg)

    # ── STOCK BUY PRICE ───────────────────────────────────────────────────────
    elif mode == "await_stock_buy_price":
        try:
            buy_price = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число, наприклад: 150.25"
            )
            return
        await clean_prompts()
        ticker    = state.get("ticker", "")
        qty       = state.get("qty", 1.0)
        cur_price = state.get("cur_price", buy_price)
        conn      = get_db()
        conn.execute(
            "INSERT INTO stocks(ticker,name,quantity,buy_price_usd,current_price_usd,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (ticker, ticker, qty, buy_price, cur_price, "active", now_str(), now_str())
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        pnl  = (cur_price - buy_price) * qty
        sign = "+" if pnl >= 0 else ""
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ <b>Додано!</b>\n\n"
            f"Тикер: <b>{ticker}</b>\n"
            f"Кількість: <b>{qty}</b>\n"
            f"Куплено за: <b>{fmt(buy_price)}</b>\n"
            f"Поточна: <b>{fmt(cur_price)}</b>\n"
            f"PnL: <b>{sign}{fmt(pnl)}</b>",
            reply_markup=kb_stocks(), parse_mode="HTML"
        )

    # ── CRYPTO SYMBOL ─────────────────────────────────────────────────────────
    elif mode == "await_crypto_symbol":
        symbol = text.upper().strip()
        await clean_prompts()
        price = await asyncio.to_thread(fetch_crypto_price, symbol)
        if not price:
            await context.bot.send_message(
                update.effective_chat.id,
                f"❌ Не вдалось знайти монету <b>{symbol}</b>.\n"
                f"Підтримувані: BTC, ETH, TON, SOL, BNB, DOGE, XRP, ADA, AVAX, DOT, MATIC",
                reply_markup=kb_crypto(), parse_mode="HTML"
            )
            set_state(uid, None)
            return
        set_state(uid, "await_crypto_qty", symbol=symbol, cur_price=price, main_msg_id=main_msg)
        prompt_msg = await context.bot.send_message(
            update.effective_chat.id,
            f"🪙 <b>{symbol}</b> — поточна ціна: <b>{fmt(price)}</b>\n\nВведи кількість монет:",
            parse_mode="HTML"
        )
        set_state(uid, "await_crypto_qty", symbol=symbol, cur_price=price,
                  prompt_msg_id=prompt_msg.message_id, main_msg_id=main_msg)

    #
        # ── CRYPTO QTY ────────────────────────────────────────────────────────────
    elif mode == "await_crypto_qty":
        try:
            qty = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число, наприклад: 0.5 або 100"
            )
            return
        await clean_prompts()
        symbol    = state.get("symbol", "")
        cur_price = state.get("cur_price", 0.0)
        set_state(uid, "await_crypto_buy_price", symbol=symbol, qty=qty,
                  cur_price=cur_price, main_msg_id=main_msg)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"Використати поточну {fmt(cur_price)}",
                callback_data=f"cryptousecur_{symbol}_{cur_price}"
            )],
        ])
        prompt_msg = await context.bot.send_message(
            update.effective_chat.id,
            f"🪙 <b>{symbol}</b> x{qty}\n\nВведи ціну покупки (USD):",
            reply_markup=kb, parse_mode="HTML"
        )
        set_state(uid, "await_crypto_buy_price", symbol=symbol, qty=qty,
                  cur_price=cur_price, prompt_msg_id=prompt_msg.message_id,
                  main_msg_id=main_msg)

    # ── CRYPTO BUY PRICE ──────────────────────────────────────────────────────
    elif mode == "await_crypto_buy_price":
        try:
            buy_price = float(text.replace(",", "."))
        except ValueError:
            await context.bot.send_message(
                update.effective_chat.id,
                "❌ Невірний формат. Введи число, наприклад: 85000.50"
            )
            return
        await clean_prompts()
        symbol    = state.get("symbol", "")
        qty       = state.get("qty", 1.0)
        cur_price = state.get("cur_price", buy_price)
        conn      = get_db()
        conn.execute(
            "INSERT INTO crypto(symbol,name,quantity,buy_price_usd,current_price_usd,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (symbol, symbol, qty, buy_price, cur_price, "active", now_str(), now_str())
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        pnl  = (cur_price - buy_price) * qty
        sign = "+" if pnl >= 0 else ""
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ <b>Додано!</b>\n\n"
            f"Монета: <b>{symbol}</b>\n"
            f"Кількість: <b>{qty}</b>\n"
            f"Куплено за: <b>{fmt(buy_price)}</b>\n"
            f"Поточна: <b>{fmt(cur_price)}</b>\n"
            f"PnL: <b>{sign}{fmt(pnl)}</b>",
            reply_markup=kb_crypto(), parse_mode="HTML"
        )

    else:
        set_state(uid, None)
        t, kb = kb_main()
        await context.bot.send_message(
            update.effective_chat.id, t,
            reply_markup=kb, parse_mode="HTML"
        )


# ── INLINE CONFIRMS ───────────────────────────────────────────────────────────
async def handle_gift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query):
        return
    await query.answer()
    uid   = query.from_user.id
    state = get_state(uid)
    data  = query.data

    if data == "giftconfirm_yes":
        if not state or state.get("mode") != "await_gift_confirm":
            await query.edit_message_text("❌ Помилка стану.", reply_markup=kb_gifts())
            return
        name     = state.get("name", "")
        ton      = state.get("ton", 0.0)
        floor    = state.get("floor")
        ton_rate = state.get("ton_rate", 0.0)
        cur_usd  = state.get("cur_usd", 0.0)
        net_usd  = state.get("net_usd", 0.0)
        slug     = state.get("slug", name_to_slug(name))
        conn     = get_db()
        conn.execute(
            "INSERT INTO gifts(name,fragment_slug,ton,floor_ton,usd_at_add,current_usd,"
            "net_usd,ton_rate_at_add,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (name, slug, ton, floor or ton, cur_usd, cur_usd,
             net_usd, ton_rate, "active", now_str(), now_str())
        )
        gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO gift_price_history(gift_id,price_usd,recorded_at) VALUES(?,?,?)",
            (gid, cur_usd, now_str())
        )
        conn.commit()
        conn.close()
        set_state(uid, None)
        await query.edit_message_text(
            f"✅ <b>Додано!</b>\n\n"
            f"🎁 {name}\n"
            f"TON: {ton:.1f} | {fmt(cur_usd)} | Нетто: {fmt(net_usd)}",
            reply_markup=kb_gifts(), parse_mode="HTML"
        )

    elif data == "giftconfirm_no":
        set_state(uid, None)
        await query.edit_message_text(
            "❌ Скасовано.", reply_markup=kb_gifts()
        )

async def handle_stockusecur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query):
        return
    await query.answer()
    uid   = query.from_user.id
    parts = query.data.split("_")
    ticker    = parts[1]
    cur_price = float(parts[2])
    state     = get_state(uid)
    qty       = state.get("qty", 1.0)
    conn      = get_db()
    conn.execute(
        "INSERT INTO stocks(ticker,name,quantity,buy_price_usd,current_price_usd,"
        "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (ticker, ticker, qty, cur_price, cur_price, "active", now_str(), now_str())
    )
    conn.commit()
    conn.close()
    set_state(uid, None)
    await query.edit_message_text(
        f"✅ <b>Додано!</b>\n\n"
        f"Тикер: <b>{ticker}</b>\n"
        f"Кількість: <b>{qty}</b>\n"
        f"Ціна: <b>{fmt(cur_price)}</b>\n"
        f"PnL: <b>$0.00</b>",
        reply_markup=kb_stocks(), parse_mode="HTML"
    )

async def handle_cryptousecur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_cb(query):
        return
    await query.answer()
    uid   = query.from_user.id
    parts = query.data.split("_")
    symbol    = parts[1]
    cur_price = float(parts[2])
    state     = get_state(uid)
    qty       = state.get("qty", 1.0)
    conn      = get_db()
    conn.execute(
        "INSERT INTO crypto(symbol,name,quantity,buy_price_usd,current_price_usd,"
        "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (symbol, symbol, qty, cur_price, cur_price, "active", now_str(), now_str())
    )
    conn.commit()
    conn.close()
    set_state(uid, None)
    await query.edit_message_text(
        f"✅ <b>Додано!</b>\n\n"
        f"Монета: <b>{symbol}</b>\n"
        f"Кількість: <b>{qty}</b>\n"
        f"Ціна: <b>{fmt(cur_price)}</b>\n"
        f"PnL: <b>$0.00</b>",
        reply_markup=kb_crypto(), parse_mode="HTML"
    )

# ── JOBS ──────────────────────────────────────────────────────────────────────
async def job_daily_snapshot(context: ContextTypes.DEFAULT_TYPE):
    try:
        val = await asyncio.to_thread(record_snapshot)
        logger.info(f"Daily snapshot: {val:.2f}")
    except Exception as e:
        logger.exception(f"job_daily_snapshot error: {e}")

async def job_auto_update(context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(update_all_steam)
        await asyncio.to_thread(update_all_gifts)
        await asyncio.to_thread(update_all_stocks)
        await asyncio.to_thread(update_all_crypto)
        logger.info("Auto price update done.")
    except Exception as e:
        logger.exception(f"job_auto_update error: {e}")

async def job_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    try:
        total  = calc_portfolio()
        pnl1d, pct1d = get_pnl(1)
        cs2    = get_steam_total("cs2")
        dota   = get_steam_total("dota2")
        gifts  = get_gifts_total()
        stocks = get_stocks_total()
        crypto = get_crypto_total()
        pnl_str = ""
        if pnl1d is not None:
            sign = "+" if pnl1d >= 0 else ""
            pnl_str = f"\n📈 За ніч: {sign}{fmt(pnl1d)} ({sign}{pct1d:.1f}%)"
        text = (
            f"☀️ <b>Ранковий дайджест</b>{pnl_str}\n\n"
            f"💼 Портфель: <b>{fmt(total)}</b>\n"
            f"🎮 Steam CS2: <b>{fmt(cs2)}</b>\n"
            f"🎮 Steam Dota: <b>{fmt(dota)}</b>\n"
            f"🎁 Подарунки: <b>{fmt(gifts)}</b>\n"
            f"📊 Акції: <b>{fmt(stocks)}</b>\n"
            f"🪙 Крипто: <b>{fmt(crypto)}</b>"
        )
        await context.bot.send_message(
            chat_id=ALLOWED_USER, text=text, parse_mode="HTML"
        )
    except Exception as e:
        logger.exception(f"job_morning_digest error: {e}")

async def job_daily_backup(context: ContextTypes.DEFAULT_TYPE):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_name = f"bot_data_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, backup_name))
        cutoff = datetime.utcnow() - timedelta(days=7)
        for fname in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, fname)
            if os.path.getmtime(fpath) < cutoff.timestamp():
                os.remove(fpath)
        logger.info(f"Backup created: {backup_name}")
    except Exception as e:
        logger.exception(f"job_daily_backup error: {e}")

# ── POST INIT ─────────────────────────────────────────────────────────────────
async def post_init(app):
    init_db()
    try:
        await app.bot.send_message(
            chat_id=ALLOWED_USER,
            text="🚀 <b>Бот запущено!</b>\nВсі системи працюють.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not send startup message: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_start))
    app.add_handler(CallbackQueryHandler(handle_gift_confirm,  pattern="^giftconfirm_"))
    app.add_handler(CallbackQueryHandler(handle_stockusecur,   pattern="^stockusecur_"))
    app.add_handler(CallbackQueryHandler(handle_cryptousecur,  pattern="^cryptousecur_"))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    jq = app.job_queue
    from datetime import time as dtime
    jq.run_daily(job_morning_digest,  time=dtime(hour=6,  minute=0))
    jq.run_daily(job_daily_snapshot,  time=dtime(hour=23, minute=0))
    jq.run_daily(job_daily_backup,    time=dtime(hour=3,  minute=0))
    jq.run_repeating(job_auto_update, interval=3600, first=60)

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
