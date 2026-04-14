import logging
import json
import os
import io
import re
import shutil
import sqlite3
import asyncio
import time as time_module
from datetime import datetime, time, timedelta, date

import requests

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============== КОНФІГ ==============

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8731260970:AAFOPneNNiSpnCWPByDHe8C7P67zbFsrSQ")
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "8422579443"))

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

STEAM_FEE = 0.15
GIFTS_FEE = 0.05

APPID_CS2 = 730
APPID_DOTA2 = 570

INVEST_INPUT_IS_UAH = True
UAH_BUDGET_DEFAULT = 5000.0
HISTORY_DAYS_TO_KEEP = 60
WEBHOOK_URL = ""

# ============== ЛОГЕР ==============

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============== КЕШ ==============

_cache: dict = {}

def cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return None
    val, ts = entry
    if time_module.time() - ts > 900:
        del _cache[key]
        return None
    return val

def cache_set(key: str, val):
    _cache[key] = (val, time_module.time())

# ============== БАЗА ДАНИХ ==============

def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS steam_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game TEXT,
      name TEXT,
      quantity INTEGER DEFAULT 1,
      buy_price_usd REAL,
      current_price_usd REAL,
      net_price_usd REAL,
      added_date TEXT,
      sold_date TEXT,
      status TEXT
    );

    CREATE TABLE IF NOT EXISTS gifts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      fragment_slug TEXT,
      ton REAL,
      floor_ton REAL,
      usd_at_add REAL,
      current_usd REAL,
      net_usd REAL,
      added_date TEXT,
      sold_date TEXT,
      status TEXT
    );

    CREATE TABLE IF NOT EXISTS gift_price_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      gift_id INTEGER,
      price_usd REAL,
      recorded_at TEXT
    );

    CREATE TABLE IF NOT EXISTS portfolio_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      date TEXT UNIQUE,
      portfolio_usd REAL
    );

    CREATE TABLE IF NOT EXISTS balance (
      key TEXT PRIMARY KEY,
      value REAL
    );

    CREATE TABLE IF NOT EXISTS transactions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      type TEXT,
      item_name TEXT,
      amount_usd REAL,
      amount_uah REAL,
      note TEXT,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS expenses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      amount_uah REAL,
      amount_usd REAL,
      category TEXT,
      note TEXT,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS alerts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      asset_type TEXT,
      asset_name TEXT,
      condition TEXT,
      threshold REAL,
      is_active INTEGER DEFAULT 1,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS recurring_expenses (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      amount_uah REAL,
      day_of_month INTEGER,
      category TEXT,
      is_active INTEGER DEFAULT 1,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS price_targets (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      asset_type TEXT,
      asset_id INTEGER,
      asset_name TEXT,
      target_price_usd REAL,
      condition TEXT,
      is_active INTEGER DEFAULT 1,
      created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS nft_tracked (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      collection_name TEXT,
      collection_slug TEXT,
      nft_number INTEGER,
      slug TEXT,
      own_price_ton REAL,
      floor_ton REAL,
      usd_value REAL,
      added_date TEXT,
      status TEXT DEFAULT 'tracking'
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
    """)
    conn.commit()

    # Міграції: додаємо нові колонки якщо не існують
    # Міграція steam_items: quantity
    existing_steam = [row[1] for row in conn.execute("PRAGMA table_info(steam_items)").fetchall()]
    if "quantity" not in existing_steam:
        conn.execute("ALTER TABLE steam_items ADD COLUMN quantity INTEGER DEFAULT 1")
    conn.commit()

    existing_gifts = [row[1] for row in conn.execute("PRAGMA table_info(gifts)").fetchall()]
    if "fragment_slug" not in existing_gifts:
        conn.execute("ALTER TABLE gifts ADD COLUMN fragment_slug TEXT")
    if "floor_ton" not in existing_gifts:
        conn.execute("ALTER TABLE gifts ADD COLUMN floor_ton REAL")
    conn.commit()

    existing_balance = [row[1] for row in conn.execute("PRAGMA table_info(balance)").fetchall()]
    if "monthly_budget_uah" not in existing_balance:
        try:
            conn.execute("ALTER TABLE balance ADD COLUMN monthly_budget_uah REAL")
            conn.commit()
        except Exception:
            pass

    conn.commit()
    conn.close()
    logger.info("DB initialized.")

def get_balance(key: str) -> float:
    conn = get_db_conn()
    row = conn.execute("SELECT value FROM balance WHERE key=?", (key,)).fetchone()
    conn.close()
    return float(row["value"]) if row else 0.0

def set_balance(key: str, value: float):
    conn = get_db_conn()
    conn.execute(
        "INSERT INTO balance(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def add_balance(key: str, delta: float):
    current = get_balance(key)
    set_balance(key, current + delta)

def add_transaction(type_: str, item_name: str, amount_usd: float, amount_uah: float = 0.0, note: str = ""):
    conn = get_db_conn()
    conn.execute(
        "INSERT INTO transactions(type,item_name,amount_usd,amount_uah,note,created_at) VALUES(?,?,?,?,?,?)",
        (type_, item_name, amount_usd, amount_uah, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

# ============== STEAM API ==============

def fetch_steam_price_usd(appid: int, market_name: str):
    cache_key = f"steam_{appid}_{market_name}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": appid, "currency": 1, "market_hash_name": market_name}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as e:
        logger.exception("Error fetching priceoverview: %s", e)
        return None
    if not data.get("success"):
        return None
    price_str = data.get("median_price") or data.get("lowest_price")
    if not price_str:
        return None
    cleaned = ""
    for ch in price_str:
        if ch.isdigit() or ch in [".", ","]:
            cleaned += ch
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    cache_set(cache_key, value)
    return value

def fetch_steam_market_search(query: str, appid: int):
    """Шукає предмети на Steam Market і повертає список (name, price_usd)."""
    import urllib.parse
    encoded = urllib.parse.quote(query)
    url = (
        f"https://steamcommunity.com/market/search/render/"
        f"?query={encoded}&appid={appid}&search_descriptions=0"
        f"&sort_column=popular&sort_dir=desc&start=0&count=5&norender=1"
    )
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = data.get("results", [])
        items = []
        for r in results:
            name = r.get("name", "")
            sell_price = r.get("sell_price", 0)
            price_usd = sell_price / 100.0
            items.append({"name": name, "price_usd": price_usd})
        return items[:5]
    except Exception as e:
        logger.exception("Error steam search: %s", e)
        return []

def fetch_stock_price(ticker: str):
    """Отримує поточну ціну акції з Yahoo Finance."""
    cache_key = f"stock_{ticker}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        result = float(price) if price else None
        if result:
            cache_set(cache_key, result)
        return result
    except Exception as e:
        logger.exception("Error fetching stock price %s: %s", ticker, e)
        return None

def update_all_stocks():
    """Оновлює поточні ціни всіх активних акцій."""
    conn = get_db_conn()
    stocks = conn.execute("SELECT * FROM stocks WHERE status='active'").fetchall()
    conn.close()
    results = []
    for s in stocks:
        price = fetch_stock_price(s["ticker"])
        if price is not None:
            conn = get_db_conn()
            conn.execute(
                "UPDATE stocks SET current_price_usd=?, updated_at=? WHERE id=?",
                (price, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), s["id"])
            )
            conn.commit()
            conn.close()
            results.append(f"✅ {s['ticker']}: ${price:.2f}")
        else:
            results.append(f"❌ {s['ticker']}: не вдалось")
    return results

# ============== КУРС UAH / USD (НБУ) ==============

def get_uah_to_usd_rate():
    cached = cache_get("uah_usd_rate")
    if cached is not None:
        return cached
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        rate_uah_per_usd = float(data[0]["rate"])
        if rate_uah_per_usd <= 0:
            return None
        result = 1.0 / rate_uah_per_usd
        cache_set("uah_usd_rate", result)
        return result
    except Exception as e:
        logger.exception("Error fetching NBU rate: %s", e)
        return None

def get_usd_to_uah_rate():
    r = get_uah_to_usd_rate()
    if r is None or r == 0:
        return None
    return 1.0 / r

def uah_to_usd(amount_uah: float):
    rate = get_uah_to_usd_rate()
    if rate is None:
        return None, "Не вдалось отримати курс НБУ."
    return amount_uah * rate, None

def usd_to_uah(amount_usd: float):
    rate = get_usd_to_uah_rate()
    if rate is None:
        return None
    return amount_usd * rate

def format_usd_uah(usd: float) -> str:
    uah = usd_to_uah(usd)
    if uah is not None:
        return f"${usd:.2f} (~{uah:.0f} грн)"
    return f"${usd:.2f}"

# ============== КУРС TON / USD (CoinGecko) ==============

def get_ton_to_usd_rate():
    cached = cache_get("ton_usd_rate")
    if cached is not None:
        return cached
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "the-open-network", "vs_currencies": "usd"}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        usd = data.get("the-open-network", {}).get("usd")
        if usd is None or usd <= 0:
            return None
        result = float(usd)
        cache_set("ton_usd_rate", result)
        return result
    except Exception as e:
        logger.exception("Error fetching TON rate: %s", e)
        return None

# ============== FRAGMENT ==============

def name_to_fragment_slug(name: str) -> str:
    return name.lower().replace(" ", "").replace("'", "").replace("-", "").replace(".", "")

def fetch_fragment_floor_price_ton(collection_slug: str):
    cached = cache_get(f"fragment_{collection_slug}")
    if cached is not None:
        return cached
    url = f"https://fragment.com/gifts/{collection_slug}?sort=price_asc&filter=sale"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
        })
        text = r.text
        matches = re.findall(r'class="tm-grid-item-value[^"]*icon-ton[^"]*"[^>]*>([\d,\.]+)<', text)
        if not matches:
            matches = re.findall(r'icon-ton[^>]*>([\d,\.]+)<', text)
        if matches:
            prices = []
            for m in matches:
                try:
                    prices.append(float(m.replace(",", "")))
                except Exception:
                    pass
            if prices:
                floor = min(prices)
                cache_set(f"fragment_{collection_slug}", floor)
                return floor
    except Exception:
        pass
    return None

def fetch_fragment_nft_price_ton(slug: str):
    """Отримує ціну конкретного NFT з fragment.com/gift/{slug}."""
    cached = cache_get(f"fragment_nft_{slug}")
    if cached is not None:
        return cached
    url = f"https://fragment.com/gift/{slug}"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
        })
        text = r.text
        # Шукаємо ціну конкретного NFT
        matches = re.findall(r'tm-item-main-value[^>]*>([\d,\.]+)<', text)
        if not matches:
            matches = re.findall(r'"price"[^>]*>([\d,\.]+)\s*TON', text)
        if not matches:
            matches = re.findall(r'data-price="([\d\.]+)"', text)
        if matches:
            try:
                price = float(matches[0].replace(",", ""))
                cache_set(f"fragment_nft_{slug}", price)
                return price
            except Exception:
                pass
    except Exception:
        pass
    return None

# ============== ХЕЛПЕРИ ==============

def calc_net(price_usd: float) -> float:
    return price_usd * (1 - STEAM_FEE)

def calc_gift_net(price_usd: float) -> float:
    return price_usd * (1 - GIFTS_FEE)

def parse_float(text: str) -> float | None:
    """Парсить float: 0,09421 / 0.09421 / $12.5 / 1 234.56"""
    t = text.strip().replace("$", "").replace(" ", "").replace("\u00a0", "")
    if not t or t == "-":
        return None
    if "," in t and "." in t:
        if t.rindex(",") > t.rindex("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None

TICKER_DB = {
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "NVDA": "NVIDIA",
    "TSLA": "Tesla", "AMD": "AMD", "INTC": "Intel",
    "NFLX": "Netflix", "ORCL": "Oracle", "CRM": "Salesforce",
    "ADBE": "Adobe", "QCOM": "Qualcomm", "AVGO": "Broadcom",
    "JPM": "JPMorgan", "BAC": "Bank of America", "GS": "Goldman Sachs",
    "V": "Visa", "MA": "Mastercard", "BRK.B": "Berkshire",
    "SPY": "S&P 500 ETF", "QQQ": "Nasdaq ETF", "VOO": "Vanguard S&P",
    "COIN": "Coinbase", "MSTR": "MicroStrategy", "MARA": "Marathon Digital",
    "DIS": "Disney", "PYPL": "PayPal", "UBER": "Uber",
    "SPOT": "Spotify", "SHOP": "Shopify", "SQ": "Block (Square)",
}

def search_tickers(query: str, limit: int = 6) -> list:
    q = query.upper().strip()
    if not q:
        top = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "AMZN"]
        return [(t, TICKER_DB[t]) for t in top]
    results = []
    for ticker, name in TICKER_DB.items():
        if ticker.startswith(q):
            results.append((ticker, name))
    for ticker, name in TICKER_DB.items():
        if q in name.upper() and (ticker, name) not in results:
            results.append((ticker, name))
    return results[:limit]

def kb_ticker_suggestions(query: str = "") -> InlineKeyboardMarkup:
    matches = search_tickers(query)
    buttons = []
    row = []
    for ticker, name in matches:
        row.append(InlineKeyboardButton(f"{ticker}", callback_data=f"ticker_pick:{ticker}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")])
    return InlineKeyboardMarkup(buttons)

def calc_current_portfolio_value_db() -> float:
    conn = get_db_conn()
    steam_rows = conn.execute("SELECT net_price_usd FROM steam_items WHERE status='active'").fetchall()
    steam_net = sum((r["net_price_usd"] or 0.0) for r in steam_rows)
    gift_rows = conn.execute("SELECT net_usd FROM gifts WHERE status IN ('active','for_sale')").fetchall()
    gifts_net = sum((r["net_usd"] or 0.0) for r in gift_rows)
    # Акції: включаємо поточну вартість (поточна ціна або ціна покупки)
    stock_rows = conn.execute(
        "SELECT quantity, current_price_usd, buy_price_usd FROM stocks WHERE status='active'"
    ).fetchall()
    stocks_val = sum((r["quantity"] or 0) * (r["current_price_usd"] or r["buy_price_usd"] or 0.0) for r in stock_rows)
    conn.close()
    cash = get_balance("free_balance_usd")
    return steam_net + gifts_net + stocks_val + cash

def get_steam_net_total(game=None) -> float:
    conn = get_db_conn()
    if game:
        rows = conn.execute("SELECT net_price_usd FROM steam_items WHERE status='active' AND game=?", (game,)).fetchall()
    else:
        rows = conn.execute("SELECT net_price_usd FROM steam_items WHERE status='active'").fetchall()
    conn.close()
    return sum((r["net_price_usd"] or 0.0) for r in rows)

def get_gifts_net_total() -> float:
    conn = get_db_conn()
    rows = conn.execute("SELECT net_usd FROM gifts WHERE status IN ('active','for_sale')").fetchall()
    conn.close()
    return sum((r["net_usd"] or 0.0) for r in rows)

def get_portfolio_history_db(days: int = 30) -> list:
    conn = get_db_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT date, portfolio_usd FROM portfolio_history WHERE date >= ? ORDER BY date ASC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [{"date": r["date"], "portfolio_usd": r["portfolio_usd"]} for r in rows]

def get_pnl_for_period(days: int):
    history = get_portfolio_history_db(days=days + 5)
    if not history:
        return None, None
    current = calc_current_portfolio_value_db()
    today = datetime.utcnow().date()
    target_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    candidates = [h for h in history if h["date"] == target_date]
    if not candidates and days > 1:
        for i in range(1, min(days, 7)):
            target_date = (today - timedelta(days=days - i)).strftime("%Y-%m-%d")
            candidates = [h for h in history if h["date"] == target_date]
            if candidates:
                break
    if not candidates:
        return None, None
    start_val = candidates[-1]["portfolio_usd"]
    profit = current - start_val
    pct = (profit / start_val * 100.0) if start_val > 0 else None
    return profit, pct

def get_monthly_expenses_uah() -> float:
    conn = get_db_conn()
    now = datetime.utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT COALESCE(SUM(amount_uah),0) as total FROM expenses WHERE created_at >= ?",
        (start,)
    ).fetchone()
    conn.close()
    return float(rows["total"] or 0.0)

def record_snapshot():
    val = calc_current_portfolio_value_db()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn = get_db_conn()
    conn.execute(
        "INSERT INTO portfolio_history(date,portfolio_usd) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET portfolio_usd=excluded.portfolio_usd",
        (today, val)
    )
    conn.commit()
    conn.close()
    return val

def check_price_targets():
    """Перевіряє price_targets і повертає список сповіщень."""
    alerts_fired = []
    conn = get_db_conn()
    targets = conn.execute("SELECT * FROM price_targets WHERE is_active=1").fetchall()
    conn.close()
    for t in targets:
        current_price = None
        if t["asset_type"] == "steam":
            conn2 = get_db_conn()
            row = conn2.execute("SELECT current_price_usd FROM steam_items WHERE id=?", (t["asset_id"],)).fetchone()
            conn2.close()
            if row:
                current_price = row["current_price_usd"]
        elif t["asset_type"] == "gift":
            conn2 = get_db_conn()
            row = conn2.execute("SELECT current_usd FROM gifts WHERE id=?", (t["asset_id"],)).fetchone()
            conn2.close()
            if row:
                current_price = row["current_usd"]
        if current_price is None:
            continue
        triggered = False
        if t["condition"] == "above" and current_price >= t["target_price_usd"]:
            triggered = True
        elif t["condition"] == "below" and current_price <= t["target_price_usd"]:
            triggered = True
        if triggered:
            cond_str = "вище" if t["condition"] == "above" else "нижче"
            alerts_fired.append(
                f"🎯 {t['asset_name']}: ціна {format_usd_uah(current_price)} ({cond_str} {format_usd_uah(t['target_price_usd'])})"
            )
    return alerts_fired

def check_alerts_db():
    """Перевіряє старі alerts і повертає список сповіщень."""
    alerts_fired = []
    conn = get_db_conn()
    active_alerts = conn.execute("SELECT * FROM alerts WHERE is_active=1").fetchall()
    conn.close()
    ton_rate = get_ton_to_usd_rate() or 0.0
    portfolio_val = calc_current_portfolio_value_db()
    for alert in active_alerts:
        current_val = None
        if alert["asset_type"] == "ton":
            current_val = ton_rate
        elif alert["asset_type"] == "portfolio_pct":
            invest = get_balance("total_invest_usd")
            if invest > 0:
                current_val = (portfolio_val - invest) / invest * 100.0
        elif alert["asset_type"] == "steam":
            conn2 = get_db_conn()
            row = conn2.execute(
                "SELECT current_price_usd FROM steam_items WHERE name=? AND status='active' LIMIT 1",
                (alert["asset_name"],)
            ).fetchone()
            conn2.close()
            if row:
                current_val = row["current_price_usd"]
        if current_val is None:
            continue
        triggered = False
        if "вище" in alert["condition"] and current_val >= alert["threshold"]:
            triggered = True
        elif "нижче" in alert["condition"] and current_val <= alert["threshold"]:
            triggered = True
        elif "зріс" in alert["condition"] and current_val >= alert["threshold"]:
            triggered = True
        elif "впав" in alert["condition"] and current_val <= -alert["threshold"]:
            triggered = True
        if triggered:
            alerts_fired.append(
                f"🔔 {alert['asset_name']}: {alert['condition']} {alert['threshold']:.2f} (зараз: {current_val:.2f})"
            )
    return alerts_fired

# ============== СТАНИ КОРИСТУВАЧІВ ==============

user_states: dict = {}

def set_state(user_id: int, mode=None, **kwargs):
    if mode is None:
        user_states.pop(user_id, None)
    else:
        user_states[user_id] = {"mode": mode, **kwargs}

def get_state(user_id: int):
    return user_states.get(user_id)

# ============== INLINE КЛАВІАТУРИ ==============

def kb_main():
    portfolio_val = calc_current_portfolio_value_db()
    pnl_1d, _ = get_pnl_for_period(1)
    pnl_str = ""
    if pnl_1d is not None:
        sign = "+" if pnl_1d >= 0 else ""
        pnl_str = f"\n📈 За добу: {sign}{format_usd_uah(pnl_1d)}"
    text = (
        f"👋 Привіт!\n"
        f"💼 Портфель: {format_usd_uah(portfolio_val)}"
        f"{pnl_str}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Активи", callback_data="main:assets"),
            InlineKeyboardButton("🎁 Подарунки", callback_data="main:gifts"),
        ],
        [
            InlineKeyboardButton("💼 Портфель", callback_data="main:portfolio"),
            InlineKeyboardButton("💰 Фінанси", callback_data="main:finance"),
        ],
        [
            InlineKeyboardButton("📊 Аналітика", callback_data="main:analytics"),
            InlineKeyboardButton("⚙️ Інше", callback_data="main:other"),
        ],
        [
            InlineKeyboardButton("📈 Акції", callback_data="assets:stocks"),
        ],
    ])
    return text, kb

def kb_assets():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔫 CS2", callback_data="assets:cs2"),
            InlineKeyboardButton("🛡 Dota 2", callback_data="assets:dota2"),
        ],
        [InlineKeyboardButton("📦 Всі інвентарі", callback_data="assets:all")],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data="assets:update")],
        [InlineKeyboardButton("🏠 Назад", callback_data="main:home")],
    ])
    return kb

def kb_game(game: str):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Додати", callback_data=f"game:add:{game}"),
            InlineKeyboardButton("✅ Продати", callback_data=f"game:sell:{game}"),
        ],
        [
            InlineKeyboardButton("🗑 Видалити", callback_data=f"game:delete:{game}"),
            InlineKeyboardButton("📋 Продані", callback_data=f"game:sold:{game}"),
        ],
        [
            InlineKeyboardButton("🏆 Топ дорогих", callback_data=f"game:top:{game}"),
            InlineKeyboardButton("◀️ Назад", callback_data="main:assets"),
        ],
    ])
    return kb

def kb_gifts():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📜 Мої подарунки", callback_data="gifts:list"),
            InlineKeyboardButton("💼 На продажі", callback_data="gifts:forsale"),
        ],
        [
            InlineKeyboardButton("➕ Додати", callback_data="gifts:add"),
            InlineKeyboardButton("🔍 Відстежити NFT", callback_data="gifts:tracknft"),
        ],
        [InlineKeyboardButton("🔄 Оновити ціни", callback_data="gifts:update")],
        [InlineKeyboardButton("🏠 Назад", callback_data="main:home")],
    ])
    return kb

def kb_portfolio():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Баланс", callback_data="portfolio:balance"),
            InlineKeyboardButton("📈 Графік", callback_data="portfolio:chart"),
        ],
        [
            InlineKeyboardButton("🍕 Розподіл", callback_data="portfolio:pie"),
            InlineKeyboardButton("💰 PnL", callback_data="portfolio:pnl"),
        ],
        [InlineKeyboardButton("📸 Snapshot", callback_data="portfolio:snapshot")],
        [InlineKeyboardButton("🏠 Назад", callback_data="main:home")],
    ])
    return kb

def kb_finance():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 Вільні кошти", callback_data="finance:free"),
            InlineKeyboardButton("📥 Поповнення", callback_data="finance:topup"),
        ],
        [InlineKeyboardButton("💸 Витрати", callback_data="finance:expense")],
        [InlineKeyboardButton("📊 Історія", callback_data="finance:history")],
        [
            InlineKeyboardButton("🔁 Регулярні", callback_data="finance:recurring"),
            InlineKeyboardButton("🏠 Назад", callback_data="main:home"),
        ],
    ])
    return kb

def kb_expense_categories():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🍕 Їжа", callback_data="expense:cat:food"),
            InlineKeyboardButton("🎮 Ігри", callback_data="expense:cat:games"),
        ],
        [
            InlineKeyboardButton("🚗 Транспорт", callback_data="expense:cat:transport"),
            InlineKeyboardButton("👕 Одяг", callback_data="expense:cat:clothes"),
        ],
        [
            InlineKeyboardButton("💊 Здоров'я", callback_data="expense:cat:health"),
            InlineKeyboardButton("📦 Інше", callback_data="expense:cat:other"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="main:finance")],
    ])
    return kb

def kb_analytics():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Топ активів", callback_data="analytics:top"),
            InlineKeyboardButton("📉 Найгірші", callback_data="analytics:worst"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="analytics:stats"),
            InlineKeyboardButton("🤖 Рекомендації", callback_data="analytics:recommend"),
        ],
        [InlineKeyboardButton("📅 Тиждень vs тиждень", callback_data="analytics:weekvweek")],
        [InlineKeyboardButton("🏠 Назад", callback_data="main:home")],
    ])
    return kb

def kb_other():
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 Сповіщення", callback_data="other:alerts"),
            InlineKeyboardButton("📋 Цільові ціни", callback_data="other:targets"),
        ],
        [
            InlineKeyboardButton("🔁 Регулярні", callback_data="other:recurring"),
            InlineKeyboardButton("🧹 Очистити", callback_data="other:clean"),
        ],
        [InlineKeyboardButton("🏠 Назад", callback_data="main:home")],
    ])
    return kb

def kb_recurring():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати", callback_data="recurring:add")],
        [InlineKeyboardButton("📋 Список", callback_data="recurring:list")],
        [InlineKeyboardButton("🗑 Видалити", callback_data="recurring:delete_list")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main:finance")],
    ])
    return kb

def kb_back_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Назад", callback_data="main:home")]])

def kb_back_assets():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:assets")]])

def kb_back_gifts():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:gifts")]])

def kb_back_portfolio():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:portfolio")]])

def kb_back_finance():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:finance")]])

# ============== GUARD ==============

async def guard_callback(query) -> bool:
    if query.from_user.id != ALLOWED_USER:
        await query.answer("⛔ Доступ заборонено.", show_alert=True)
        return False
    return True

async def guard_message(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER:
        await update.message.reply_text("⛔ Доступ заборонено.")
        return False
    return True

# ============== MATPLOTLIB HELPERS ==============

def make_portfolio_chart_bytes() -> bytes:
    history = get_portfolio_history_db(30)
    fig, ax = plt.subplots(figsize=(10, 5))
    if len(history) < 2:
        ax.text(0.5, 0.5, "Недостатньо даних", ha="center", va="center", fontsize=14)
    else:
        dates = [datetime.strptime(h["date"], "%Y-%m-%d") for h in history]
        vals = [h["portfolio_usd"] for h in history]
        ax.plot(dates, vals, color="#4CAF50", linewidth=2.5, marker="o", markersize=4)
        ax.fill_between(dates, vals, alpha=0.15, color="#4CAF50")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
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

def make_pie_chart_bytes() -> bytes:
    cs2 = get_steam_net_total("cs2")
    dota = get_steam_net_total("dota2")
    gifts = get_gifts_net_total()
    cash = get_balance("free_balance_usd")
    # Акції: рахуємо загальну поточну вартість акцій з БД
    conn_s = get_db_conn()
    stock_rows = conn_s.execute(
        "SELECT quantity, current_price_usd, buy_price_usd FROM stocks WHERE status='active'"
    ).fetchall()
    conn_s.close()
    stocks_val = sum((r["quantity"] or 0) * (r["current_price_usd"] or r["buy_price_usd"] or 0.0) for r in stock_rows)
    labels, sizes, colors = [], [], []
    if cs2 > 0:
        labels.append(f"CS2 ${cs2:.1f}")
        sizes.append(cs2)
        colors.append("#FF5722")
    if dota > 0:
        labels.append(f"Dota ${dota:.1f}")
        sizes.append(dota)
        colors.append("#9C27B0")
    if gifts > 0:
        labels.append(f"Подарунки ${gifts:.1f}")
        sizes.append(gifts)
        colors.append("#2196F3")
    if stocks_val > 0:
        labels.append(f"Акції ${stocks_val:.1f}")
        sizes.append(stocks_val)
        colors.append("#FF9800")
    if cash > 0:
        labels.append(f"Кеш ${cash:.1f}")
        sizes.append(cash)
        colors.append("#4CAF50")
    fig, ax = plt.subplots(figsize=(8, 8))
    if sizes:
        ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%", startangle=140)
        ax.set_title("Розподіл портфеля")
    else:
        ax.text(0.5, 0.5, "Немає даних", ha="center", va="center", fontsize=14)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def make_pnl_chart_bytes() -> bytes:
    periods = [1, 7, 30]
    labels = ["1д", "7д", "30д"]
    pnls = []
    for d in periods:
        p, _ = get_pnl_for_period(d)
        pnls.append(p if p is not None else 0.0)
    # All time
    total_invest = get_balance("total_invest_usd")
    current = calc_current_portfolio_value_db()
    all_time = current - total_invest
    pnls.append(all_time)
    labels.append("Весь час")
    fig, ax = plt.subplots(figsize=(8, 5))
    bar_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in pnls]
    bars = ax.bar(labels, pnls, color=bar_colors)
    ax.axhline(y=0, color="black", linewidth=0.8, linestyle="--")
    ax.set_title("PnL за periodами")
    ax.set_ylabel("USD")
    for bar, val in zip(bars, pnls):
        sign = "+" if val >= 0 else ""
        ax.text(bar.get_x() + bar.get_width() / 2., bar.get_height(),
                f"{sign}{val:.2f}", ha="center", va="bottom", fontsize=9)
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ============== COMMAND HANDLERS ==============

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_message(update):
        return
    user_id = update.effective_user.id
    set_state(user_id, None)
    text, kb = kb_main()
    await update.message.reply_text(text, reply_markup=kb)

# ============== CALLBACK QUERY HANDLER ==============

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await guard_callback(query):
        return
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    parts = data.split(":")
    section = parts[0]
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else ""

    # ===== MAIN =====
    if section == "main":
        if action in ("home", ""):
            text, kb = kb_main()
            await query.edit_message_text(text, reply_markup=kb)

        elif action == "assets":
            cs2_total = get_steam_net_total("cs2")
            dota_total = get_steam_net_total("dota2")
            text = (
                f"🎮 Активи\n"
                f"🔫 CS2: {format_usd_uah(cs2_total)}\n"
                f"🛡 Dota 2: {format_usd_uah(dota_total)}\n"
                f"Разом: {format_usd_uah(cs2_total + dota_total)}"
            )
            await query.edit_message_text(text, reply_markup=kb_assets())

        elif action == "gifts":
            conn = get_db_conn()
            gifts_count = conn.execute("SELECT COUNT(*) FROM gifts WHERE status IN ('active','for_sale')").fetchone()[0]
            nft_count = conn.execute("SELECT COUNT(*) FROM nft_tracked WHERE status='tracking'").fetchone()[0]
            conn.close()
            gifts_total = get_gifts_net_total()
            text = (
                f"🎁 Подарунки\n"
                f"Активних: {gifts_count}\n"
                f"NFT відстежується: {nft_count}\n"
                f"Вартість: {format_usd_uah(gifts_total)}"
            )
            await query.edit_message_text(text, reply_markup=kb_gifts())

        elif action == "portfolio":
            text = "💼 Портфель — обери дію:"
            await query.edit_message_text(text, reply_markup=kb_portfolio())

        elif action == "finance":
            bal = get_balance("free_balance_usd")
            budget = get_balance("monthly_budget_uah") or UAH_BUDGET_DEFAULT
            spent = get_monthly_expenses_uah()
            text = (
                f"💰 Фінанси\n"
                f"💵 Вільний баланс: {format_usd_uah(bal)}\n"
                f"📊 Бюджет місяця: {budget:.0f} грн (залишок: {max(0, budget - spent):.0f} грн)"
            )
            await query.edit_message_text(text, reply_markup=kb_finance())

        elif action == "analytics":
            text = "📊 Аналітика — обери дію:"
            await query.edit_message_text(text, reply_markup=kb_analytics())

        elif action == "other":
            text = "⚙️ Інше — обери дію:"
            await query.edit_message_text(text, reply_markup=kb_other())

    # ===== ASSETS =====
    elif section == "assets":
        if action == "cs2":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game='cs2' AND status='active' ORDER BY current_price_usd DESC"
            ).fetchall()
            conn.close()
            if items:
                lines = ["🔫 CS2 — активні скіни:"]
                for it in items:
                    p = it["current_price_usd"] or 0.0
                    n = it["net_price_usd"] or 0.0
                    lines.append(f"• {it['name']}: {format_usd_uah(p)} (нетто: ${n:.2f})")
                total = get_steam_net_total("cs2")
                lines.append(f"\nРазом нетто: {format_usd_uah(total)}")
                text = "\n".join(lines)
            else:
                text = "🔫 CS2: немає активних скінів"
            await query.edit_message_text(text, reply_markup=kb_game("cs2"))

        elif action == "dota2":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game='dota2' AND status='active' ORDER BY current_price_usd DESC"
            ).fetchall()
            conn.close()
            if items:
                lines = ["🛡 Dota 2 — активні скіни:"]
                for it in items:
                    p = it["current_price_usd"] or 0.0
                    n = it["net_price_usd"] or 0.0
                    lines.append(f"• {it['name']}: {format_usd_uah(p)} (нетто: ${n:.2f})")
                total = get_steam_net_total("dota2")
                lines.append(f"\nРазом нетто: {format_usd_uah(total)}")
                text = "\n".join(lines)
            else:
                text = "🛡 Dota 2: немає активних скінів"
            await query.edit_message_text(text, reply_markup=kb_game("dota2"))

        elif action == "all":
            conn = get_db_conn()
            cs2_items = conn.execute(
                "SELECT * FROM steam_items WHERE game='cs2' AND status='active' ORDER BY current_price_usd DESC"
            ).fetchall()
            dota_items = conn.execute(
                "SELECT * FROM steam_items WHERE game='dota2' AND status='active' ORDER BY current_price_usd DESC"
            ).fetchall()
            conn.close()
            lines = ["📦 Всі інвентарі:"]
            cs2_total = 0.0
            if cs2_items:
                lines.append("\n🔫 CS2:")
                for it in cs2_items:
                    n = it["net_price_usd"] or 0.0
                    cs2_total += n
                    lines.append(f"  • {it['name']}: ${it['current_price_usd']:.2f}")
                lines.append(f"  Підсумок CS2: {format_usd_uah(cs2_total)}")
            else:
                lines.append("🔫 CS2: порожньо")
            dota_total = 0.0
            if dota_items:
                lines.append("\n🛡 Dota 2:")
                for it in dota_items:
                    n = it["net_price_usd"] or 0.0
                    dota_total += n
                    lines.append(f"  • {it['name']}: ${it['current_price_usd']:.2f}")
                lines.append(f"  Підсумок Dota: {format_usd_uah(dota_total)}")
            else:
                lines.append("🛡 Dota 2: порожньо")
            total = cs2_total + dota_total
            lines.append(f"\nCS2: {format_usd_uah(cs2_total)} | Dota: {format_usd_uah(dota_total)} | Разом: {format_usd_uah(total)}")
            await query.edit_message_text("\n".join(lines), reply_markup=kb_back_assets())

        elif action == "update":
            await query.edit_message_text("🔄 Оновлюю ціни Steam...")
            conn = get_db_conn()
            items = conn.execute("SELECT * FROM steam_items WHERE status='active'").fetchall()
            conn.close()
            if not items:
                await query.edit_message_text("Немає активних скінів.", reply_markup=kb_back_assets())
                return
            updated, failed = 0, 0
            appid_map = {"cs2": APPID_CS2, "dota2": APPID_DOTA2}
            conn = get_db_conn()
            for it in items:
                appid = appid_map.get(it["game"], APPID_CS2)
                price = await asyncio.to_thread(fetch_steam_price_usd, appid, it["name"])
                if price is None:
                    failed += 1
                    continue
                net = calc_net(price)
                conn.execute(
                    "UPDATE steam_items SET current_price_usd=?, net_price_usd=? WHERE id=?",
                    (price, net, it["id"])
                )
                updated += 1
            conn.commit()
            conn.close()
            alerts_fired = check_price_targets()
            text = f"✅ Оновлено: {updated}\n❌ Не вдалося: {failed}"
            if alerts_fired:
                text += "\n\n" + "\n".join(alerts_fired)
            await query.edit_message_text(text, reply_markup=kb_back_assets())

        elif action == "stocks":
            conn = get_db_conn()
            stocks = conn.execute("SELECT * FROM stocks WHERE status='active' ORDER BY current_price_usd*quantity DESC").fetchall()
            conn.close()

            if not stocks:
                text = "📈 Акції\n\nПортфель порожній."
            else:
                total_val = sum(s["current_price_usd"] * s["quantity"] for s in stocks)
                total_buy = sum(s["buy_price_usd"] * s["quantity"] for s in stocks)
                total_pnl = total_val - total_buy
                sign_total = "+" if total_pnl >= 0 else ""
                lines = [
                    f"📈 Акції",
                    f"💰 Вартість: ${total_val:,.2f}",
                    f"📥 Вкладено: ${total_buy:,.2f}",
                    f"📊 PnL: {sign_total}${total_pnl:,.2f}",
                    "",
                ]
                for s in stocks:
                    val = s["current_price_usd"] * s["quantity"]
                    pnl = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                    pct = (pnl / (s["buy_price_usd"] * s["quantity"]) * 100) if s["buy_price_usd"] else 0
                    sign = "+" if pnl >= 0 else ""
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    lines.append(f"{emoji} {s['ticker']} × {s['quantity']:.2f}")
                    lines.append(f"   ${s['current_price_usd']:.2f} = ${val:,.2f}  {sign}{pct:.1f}%")
                text = "\n".join(lines)

            kb_stocks = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Купити акцію", callback_data="stockaction:add")],
                [InlineKeyboardButton("🔄 Оновити ціни", callback_data="stockaction:update")],
                [InlineKeyboardButton("🗑 Видалити акцію", callback_data="stockaction:delete_list")],
                [InlineKeyboardButton("◀️ Назад", callback_data="main:home")],
            ])
            await query.edit_message_text(text, reply_markup=kb_stocks)

    # ===== GAME =====
    elif section == "game":
        game = param  # cs2 або dota2
        game_title = "CS2" if game == "cs2" else "Dota 2"

        if action == "add":
            set_state(user_id, "await_steam_search", game=game, prompt_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🔍 Введи частину назви предмета {game_title}:"
            )
            set_state(user_id, "await_steam_search", game=game, prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "sell":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' ORDER BY current_price_usd DESC",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(f"Немає активних скінів {game_title}.", reply_markup=kb_game(game))
                return
            buttons = []
            for it in items:
                p = it["current_price_usd"] or 0.0
                buttons.append([InlineKeyboardButton(
                    f"{it['name']} (${p:.2f})",
                    callback_data=f"sellitem:{it['id']}:{game}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"assets:{game}")])
            await query.edit_message_text(
                f"✅ Виберіть скін {game_title} для продажу:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif action == "delete":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' ORDER BY name",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                await query.edit_message_text(f"Немає активних скінів {game_title}.", reply_markup=kb_game(game))
                return
            buttons = []
            for it in items:
                buttons.append([InlineKeyboardButton(
                    f"🗑 {it['name']}",
                    callback_data=f"delitem:{it['id']}:{game}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"assets:{game}")])
            await query.edit_message_text(
                f"🗑 Виберіть скін {game_title} для видалення:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif action == "sold":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='sold' ORDER BY sold_date DESC LIMIT 20",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                text = f"Немає проданих скінів {game_title}."
            else:
                lines = [f"📋 Продані скіни {game_title}:"]
                for it in items:
                    buy = it["buy_price_usd"] or 0.0
                    net = it["net_price_usd"] or 0.0
                    profit = net - buy
                    sign = "+" if profit >= 0 else ""
                    lines.append(f"• {it['name']}: куп. ${buy:.2f}, продано ~${net:.2f} ({sign}${profit:.2f})")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_game(game))

        elif action == "top":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT * FROM steam_items WHERE game=? AND status='active' ORDER BY current_price_usd DESC LIMIT 5",
                (game,)
            ).fetchall()
            conn.close()
            if not items:
                text = f"Немає активних скінів {game_title}."
            else:
                lines = [f"🏆 Топ-5 дорогих {game_title}:"]
                for idx, it in enumerate(items, 1):
                    p = it["current_price_usd"] or 0.0
                    lines.append(f"{idx}. {it['name']} — {format_usd_uah(p)}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_game(game))

    # ===== SELLITEM (підтвердження продажу) =====
    elif section == "sellitem":
        item_id = int(action)
        game = param
        conn = get_db_conn()
        it = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        conn.close()
        if not it:
            await query.edit_message_text("Предмет не знайдено.", reply_markup=kb_game(game))
            return
        p = it["current_price_usd"] or 0.0
        net = it["net_price_usd"] or 0.0
        text = (
            f"✅ Продати скін?\n\n"
            f"🎮 {it['name']}\n"
            f"Поточна ціна: {format_usd_uah(p)}\n"
            f"Нетто (−15%): {format_usd_uah(net)}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Підтвердити", callback_data=f"sellconfirm:{item_id}:{game}"),
                InlineKeyboardButton("❌ Скасувати", callback_data=f"assets:{game}"),
            ]
        ])
        await query.edit_message_text(text, reply_markup=kb)

    elif section == "sellconfirm":
        item_id = int(action)
        game = param
        conn = get_db_conn()
        it = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            conn.close()
            await query.edit_message_text("Предмет не знайдено.", reply_markup=kb_game(game))
            return
        net = it["net_price_usd"] or 0.0
        buy = it["buy_price_usd"] or 0.0
        sold_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE steam_items SET status='sold', sold_date=? WHERE id=?",
            (sold_date, item_id)
        )
        conn.commit()
        conn.close()
        add_balance("free_balance_usd", net)
        add_transaction("sell_steam", it["name"], net, 0.0, f"Продаж {it['game']}")
        profit = net - buy
        sign = "+" if profit >= 0 else ""
        text = (
            f"✅ Продано!\n\n"
            f"🎮 {it['name']}\n"
            f"Надійшло: {format_usd_uah(net)}\n"
            f"Прибуток: {sign}${profit:.2f}"
        )
        await query.edit_message_text(text, reply_markup=kb_game(game))

    # ===== DELITEM =====
    elif section == "delitem":
        item_id = int(action)
        game = param
        conn = get_db_conn()
        it = conn.execute("SELECT * FROM steam_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            conn.close()
            await query.edit_message_text("Предмет не знайдено.", reply_markup=kb_game(game))
            return
        conn.execute("UPDATE steam_items SET status='deleted' WHERE id=?", (item_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"🗑 Видалено: {it['name']}",
            reply_markup=kb_game(game)
        )

    # ===== STEAM SEARCH RESULT =====
    elif section == "steamresult":
        idx = int(action)
        game = param
        state_data = get_state(user_id)
        results = state_data.get("search_results", []) if state_data else []
        if idx >= len(results):
            await query.edit_message_text("❌ Помилка.", reply_markup=kb_game(game))
            set_state(user_id, None)
            return
        item = results[idx]
        name = item["name"]
        cur_price = item["price_usd"]
        # Now ask qty
        set_state(user_id, "await_steam_qty_confirm",
                  game=game, name=name, cur_price=cur_price,
                  main_msg_id=query.message.message_id)
        kb_qty_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1", callback_data=f"steamqty:{game}:1"),
                InlineKeyboardButton("2", callback_data=f"steamqty:{game}:2"),
                InlineKeyboardButton("3", callback_data=f"steamqty:{game}:3"),
                InlineKeyboardButton("5", callback_data=f"steamqty:{game}:5"),
            ],
            [InlineKeyboardButton("❌ Скасувати", callback_data=f"assets:{game}")],
        ])
        await query.edit_message_text(
            f"🎮 {name}\n💰 Поточна ціна: ${cur_price:.2f}\n\nСкільки штук купив?",
            reply_markup=kb_qty_buttons
        )

    elif section == "steamqty":
        game = action
        qty = int(param)
        state_data = get_state(user_id)
        name = state_data.get("name", "") if state_data else ""
        cur_price = state_data.get("cur_price", 0.0) if state_data else 0.0
        set_state(user_id, "await_steam_buyprice",
                  game=game, name=name, qty=qty, cur_price=cur_price,
                  main_msg_id=query.message.message_id)
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"🎮 {name} × {qty}\n💵 За скільки купив 1 штуку? (в USD, напр. 12.50 або 12,50)"
        )
        await query.edit_message_text(
            f"🎮 {name} × {qty}\n⏳ Введи ціну купівлі...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data=f"assets:{game}")]])
        )
        set_state(user_id, "await_steam_buyprice",
                  game=game, name=name, qty=qty, cur_price=cur_price,
                  prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

    elif section == "steamadd":
        # kept for backward compatibility — old path no longer used, but left in place
        idx = int(action)
        game = param
        state = get_state(user_id)
        search_results = state.get("search_results", []) if state else []
        if idx >= len(search_results):
            await query.edit_message_text("Помилка.", reply_markup=kb_game(game))
            return
        item = search_results[idx]
        name = item["name"]
        price = item["price_usd"]
        net = calc_net(price)
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO steam_items(game,name,buy_price_usd,current_price_usd,net_price_usd,added_date,status) VALUES(?,?,?,?,?,?,?)",
            (game, name, price, price, net, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "active")
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)
        await query.edit_message_text(
            f"✅ Додано: {name}\nЦіна: {format_usd_uah(price)}",
            reply_markup=kb_game(game)
        )

    # ===== GIFTS =====
    elif section == "gifts":
        if action == "list":
            conn = get_db_conn()
            gifts = conn.execute(
                "SELECT * FROM gifts WHERE status IN ('active','for_sale') ORDER BY current_usd DESC"
            ).fetchall()
            nfts = conn.execute(
                "SELECT * FROM nft_tracked WHERE status='tracking' ORDER BY id DESC"
            ).fetchall()
            conn.close()
            lines = ["📜 Мої подарунки:"]
            if gifts:
                for g in gifts:
                    cur = g["current_usd"] or 0.0
                    ton = g["ton"] or 0.0
                    status_str = "💼 На продажі" if g["status"] == "for_sale" else "✅ Активний"
                    lines.append(f"• {g['name']} ({ton:.0f} TON) — {format_usd_uah(cur)} [{status_str}]")
            else:
                lines.append("Подарунків немає.")
            if nfts:
                lines.append("\n🔍 NFT відстежувані:")
                for n in nfts:
                    floor = n["floor_ton"] or 0.0
                    own = n["own_price_ton"]
                    own_str = f"{own:.0f} TON" if own else "N/A"
                    lines.append(f"• {n['collection_name']} #{n['nft_number']}: ваша {own_str}, floor {floor:.0f} TON")
            buttons = []
            for g in gifts:
                buttons.append([InlineKeyboardButton(
                    f"📋 {g['name']}",
                    callback_data=f"giftdetail:{g['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="main:gifts")])
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif action == "forsale":
            conn = get_db_conn()
            gifts = conn.execute(
                "SELECT * FROM gifts WHERE status='for_sale' ORDER BY current_usd DESC"
            ).fetchall()
            conn.close()
            if not gifts:
                text = "💼 Немає подарунків на продажі."
            else:
                ton_rate = get_ton_to_usd_rate() or 0.0
                lines = ["💼 Подарунки на продажі:"]
                for g in gifts:
                    cur = g["current_usd"] or 0.0
                    ton = g["ton"] or 0.0
                    lines.append(f"• {g['name']}: {ton:.0f} TON = {format_usd_uah(cur)}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_gifts())

        elif action == "add":
            set_state(user_id, "await_gift_name_new", prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🎁 Введи назву подарунка:"
            )
            set_state(user_id, "await_gift_name_new", prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "tracknft":
            set_state(user_id, "await_nft_input", prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🔍 Введи назву і номер NFT:\n(наприклад: Plush Pepe 1315)"
            )
            set_state(user_id, "await_nft_input", prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "update":
            await query.edit_message_text("🔄 Оновлюю ціни подарунків...")
            conn = get_db_conn()
            gifts = conn.execute("SELECT * FROM gifts WHERE status IN ('active','for_sale')").fetchall()
            nfts = conn.execute("SELECT * FROM nft_tracked WHERE status='tracking'").fetchall()
            conn.close()
            ton_rate = await asyncio.to_thread(get_ton_to_usd_rate)
            if ton_rate is None:
                await query.edit_message_text("❌ Не вдалось отримати курс TON.", reply_markup=kb_gifts())
                return
            updated_g, updated_n = 0, 0
            conn = get_db_conn()
            for g in gifts:
                slug = g["fragment_slug"]
                if not slug:
                    slug = name_to_fragment_slug(g["name"])
                floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, slug)
                if floor is not None:
                    cur_usd = floor * ton_rate
                    net_usd = calc_gift_net(cur_usd)
                    conn.execute(
                        "UPDATE gifts SET floor_ton=?, current_usd=?, net_usd=? WHERE id=?",
                        (floor, cur_usd, net_usd, g["id"])
                    )
                    conn.execute(
                        "INSERT INTO gift_price_history(gift_id,price_usd,recorded_at) VALUES(?,?,?)",
                        (g["id"], cur_usd, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    updated_g += 1
            for n in nfts:
                price_ton = await asyncio.to_thread(fetch_fragment_nft_price_ton, n["slug"])
                floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, n["collection_slug"])
                usd_val = (price_ton or floor or 0.0) * ton_rate
                conn.execute(
                    "UPDATE nft_tracked SET own_price_ton=?, floor_ton=?, usd_value=? WHERE id=?",
                    (price_ton, floor, usd_val, n["id"])
                )
                updated_n += 1
            conn.commit()
            conn.close()
            alerts_fired = check_price_targets()
            text = f"✅ Подарунки оновлено: {updated_g}\n✅ NFT оновлено: {updated_n}"
            if alerts_fired:
                text += "\n\n" + "\n".join(alerts_fired)
            await query.edit_message_text(text, reply_markup=kb_gifts())

    # ===== GIFT DETAIL =====
    elif section == "giftdetail":
        gift_id = int(action)
        conn = get_db_conn()
        g = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("Подарунок не знайдено.", reply_markup=kb_gifts())
            return
        cur = g["current_usd"] or 0.0
        add_usd = g["usd_at_add"] or 0.0
        ton = g["ton"] or 0.0
        floor = g["floor_ton"] or 0.0
        profit = cur - add_usd
        sign = "+" if profit >= 0 else ""
        text = (
            f"🎁 {g['name']}\n"
            f"TON: {ton:.2f}\n"
            f"Floor: {floor:.0f} TON\n"
            f"Вартість при додаванні: {format_usd_uah(add_usd)}\n"
            f"Поточна: {format_usd_uah(cur)}\n"
            f"PnL: {sign}{format_usd_uah(profit)}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💼 На продаж", callback_data=f"giftsell:{gift_id}"),
                InlineKeyboardButton("📈 Історія", callback_data=f"gifthistory:{gift_id}"),
            ],
            [InlineKeyboardButton("💡 Рекомендація", callback_data=f"giftrecommend:{gift_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="gifts:list")],
        ])
        await query.edit_message_text(text, reply_markup=kb)

    elif section == "giftsale":
        gift_id = int(action)
        conn = get_db_conn()
        conn.execute("UPDATE gifts SET status='for_sale' WHERE id=?", (gift_id,))
        conn.commit()
        g = conn.execute("SELECT name FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        name = g["name"] if g else "?"
        await query.edit_message_text(
            f"💼 {name} переміщено на продаж.",
            reply_markup=kb_gifts()
        )

    elif section == "gifthistory":
        gift_id = int(action)
        conn = get_db_conn()
        g = conn.execute("SELECT name FROM gifts WHERE id=?", (gift_id,)).fetchone()
        hist = conn.execute(
            "SELECT * FROM gift_price_history WHERE gift_id=? ORDER BY recorded_at DESC LIMIT 10",
            (gift_id,)
        ).fetchall()
        conn.close()
        name = g["name"] if g else "?"
        if not hist:
            text = f"📈 {name}: немає історії цін."
        else:
            lines = [f"📈 Історія цін: {name}"]
            for h in hist:
                lines.append(f"  {h['recorded_at'][:10]}: {format_usd_uah(h['price_usd'])}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data=f"giftdetail:{gift_id}")]
        ]))

    elif section == "giftrecommend":
        gift_id = int(action)
        conn = get_db_conn()
        g = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("Не знайдено.", reply_markup=kb_gifts())
            return
        cur = g["current_usd"] or 0.0
        add_usd = g["usd_at_add"] or 0.0
        if add_usd > 0:
            pct = (cur - add_usd) / add_usd * 100
            if pct >= 10:
                rec = f"✅ Рекомендую продати! Зріс на {pct:.1f}%"
            elif pct <= -10:
                rec = f"⚠️ Подарунок просів на {abs(pct):.1f}% — утримай або дочекайся відновлення"
            else:
                rec = f"📊 Зміна {pct:+.1f}% — утримуй"
        else:
            rec = "📊 Недостатньо даних для рекомендації"
        await query.edit_message_text(
            f"💡 Рекомендація: {g['name']}\n{rec}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data=f"giftdetail:{gift_id}")]
            ])
        )

    # ===== GIFT ADD CONFIRM =====
    elif section == "giftaddconfirm":
        # action: yes/no, param: encoded data in context
        state = get_state(user_id)
        if action == "yes" and state and state.get("pending_gift"):
            pg = state["pending_gift"]
            name = pg["name"]
            ton = pg["ton"]
            floor = pg["floor"]
            ton_rate = pg["ton_rate"]
            cur_usd = pg["cur_usd"]
            net_usd = calc_gift_net(cur_usd)
            slug = name_to_fragment_slug(name)
            conn = get_db_conn()
            conn.execute(
                "INSERT INTO gifts(name,fragment_slug,ton,floor_ton,usd_at_add,current_usd,net_usd,added_date,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (name, slug, ton, floor, cur_usd, cur_usd, net_usd, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "active")
            )
            gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO gift_price_history(gift_id,price_usd,recorded_at) VALUES(?,?,?)",
                (gid, cur_usd, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
            conn.close()
            set_state(user_id, None)
            await query.edit_message_text(
                f"✅ Подарунок додано!\n{name} ({ton:.1f} TON)\nВартість: {format_usd_uah(cur_usd)}",
                reply_markup=kb_gifts()
            )
        else:
            set_state(user_id, None)
            await query.edit_message_text("❌ Скасовано.", reply_markup=kb_gifts())

    # ===== NFT TRACK CONFIRM =====
    elif section == "nftaddconfirm":
        state = get_state(user_id)
        if action == "yes" and state and state.get("pending_nft"):
            pn = state["pending_nft"]
            conn = get_db_conn()
            conn.execute(
                "INSERT INTO nft_tracked(collection_name,collection_slug,nft_number,slug,own_price_ton,floor_ton,usd_value,added_date,status) VALUES(?,?,?,?,?,?,?,?,?)",
                (pn["collection_name"], pn["collection_slug"], pn["nft_number"], pn["slug"],
                 pn["own_price"], pn["floor"], pn["usd_value"],
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tracking")
            )
            conn.commit()
            conn.close()
            set_state(user_id, None)
            await query.edit_message_text(
                f"✅ NFT {pn['collection_name']} #{pn['nft_number']} додано до відстеження!",
                reply_markup=kb_gifts()
            )
        else:
            set_state(user_id, None)
            await query.edit_message_text("❌ Скасовано.", reply_markup=kb_gifts())

    # ===== PORTFOLIO =====
    elif section == "portfolio":
        if action == "balance":
            cs2 = get_steam_net_total("cs2")
            dota = get_steam_net_total("dota2")
            gifts = get_gifts_net_total()
            cash = get_balance("free_balance_usd")
            total = cs2 + dota + gifts + cash
            invest = get_balance("total_invest_usd")
            pnl = total - invest
            pct = (pnl / invest * 100) if invest > 0 else 0.0
            sign = "+" if pnl >= 0 else ""
            conn = get_db_conn()
            nft_count = conn.execute("SELECT COUNT(*) FROM nft_tracked WHERE status='tracking'").fetchone()[0]
            conn.close()
            text = (
                f"📊 Баланс портфеля\n\n"
                f"🎮 Steam CS2: {format_usd_uah(cs2)}\n"
                f"🎮 Steam Dota 2: {format_usd_uah(dota)}\n"
                f"🎁 Подарунки: {format_usd_uah(gifts)}\n"
                f"🔍 NFT відстежувані: {nft_count} шт\n"
                f"💵 Кеш: {format_usd_uah(cash)}\n"
                f"━━━━━━━━━━━━━━\n"
                f"💼 Разом: {format_usd_uah(total)}\n"
                f"📥 Вкладено: {format_usd_uah(invest)}\n"
                f"💹 PnL: {sign}{format_usd_uah(pnl)} ({sign}{pct:.2f}%)"
            )
            await query.edit_message_text(text, reply_markup=kb_portfolio())

        elif action == "chart":
            await query.edit_message_text("📈 Будую графік...")
            chart_bytes = await asyncio.to_thread(make_portfolio_chart_bytes)
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=io.BytesIO(chart_bytes),
                caption="📈 Портфель за 30 днів"
            )
            text, kb = kb_main()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=kb
            )

        elif action == "pie":
            await query.edit_message_text("🍕 Будую діаграму...")
            chart_bytes = await asyncio.to_thread(make_pie_chart_bytes)
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=io.BytesIO(chart_bytes),
                caption="🍕 Розподіл портфеля"
            )
            text, kb = kb_main()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=kb
            )

        elif action == "pnl":
            await query.edit_message_text("💰 Будую PnL графік...")
            chart_bytes = await asyncio.to_thread(make_pnl_chart_bytes)
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=io.BytesIO(chart_bytes),
                caption="💰 PnL за periodами"
            )
            text, kb = kb_main()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=kb
            )

        elif action == "snapshot":
            val = await asyncio.to_thread(record_snapshot)
            await query.edit_message_text(
                f"📸 Snapshot збережено!\nПортфель: {format_usd_uah(val)}",
                reply_markup=kb_portfolio()
            )

    # ===== FINANCE =====
    elif section == "finance":
        if action == "free":
            bal = get_balance("free_balance_usd")
            budget = get_balance("monthly_budget_uah") or UAH_BUDGET_DEFAULT
            spent = get_monthly_expenses_uah()
            text = (
                f"💵 Вільний баланс: {format_usd_uah(bal)}\n"
                f"📊 Місячний бюджет: {budget:.0f} грн\n"
                f"💸 Витрачено цього місяця: {spent:.0f} грн\n"
                f"✅ Залишок: {max(0, budget - spent):.0f} грн"
            )
            await query.edit_message_text(text, reply_markup=kb_finance())

        elif action == "topup":
            set_state(user_id, "await_topup", prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📥 Введи суму поповнення в UAH:"
            )
            set_state(user_id, "await_topup", prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "expense":
            await query.edit_message_text(
                "💸 Виберіть категорію витрати:",
                reply_markup=kb_expense_categories()
            )

        elif action == "history":
            conn = get_db_conn()
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            conn.close()
            if not rows:
                text = "📊 Немає транзакцій."
            else:
                lines = ["📊 Останні транзакції:"]
                for r in rows:
                    dt = r["created_at"][:10] if r["created_at"] else "?"
                    uah = f"{r['amount_uah']:.0f} грн" if r["amount_uah"] else ""
                    usd = f"${r['amount_usd']:.2f}"
                    lines.append(f"• {dt} | {r['type']} | {r['item_name']}: {usd} {uah}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_finance())

        elif action == "recurring":
            await query.edit_message_text(
                "🔁 Регулярні витрати:",
                reply_markup=kb_recurring()
            )

    # ===== EXPENSE CATEGORY =====
    elif section == "expense":
        if action == "cat":
            cat_map = {
                "food": "🍕 Їжа",
                "games": "🎮 Ігри",
                "transport": "🚗 Транспорт",
                "clothes": "👕 Одяг",
                "health": "💊 Здоров'я",
                "other": "📦 Інше",
            }
            cat_name = cat_map.get(param, param)
            set_state(user_id, "await_expense_amount", category=param, cat_name=cat_name,
                      prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"💸 Введи суму витрати в UAH ({cat_name}):"
            )
            set_state(user_id, "await_expense_amount", category=param, cat_name=cat_name,
                      prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

    # ===== TOPUP REINVEST CHOICE =====
    elif section == "topup":
        state = get_state(user_id)
        if not state or "pending_amount_uah" not in state:
            await query.edit_message_text("Помилка стану.", reply_markup=kb_finance())
            return
        amount_uah = state["pending_amount_uah"]
        amount_usd, err = uah_to_usd(amount_uah)
        if err or amount_usd is None:
            await query.edit_message_text("❌ Помилка конвертації.", reply_markup=kb_finance())
            return
        if action == "reinvest":
            add_balance("free_balance_usd", amount_usd)
            add_balance("total_reinvest_usd", amount_usd)
            add_transaction("reinvest", "Реінвестиція", amount_usd, amount_uah)
            text = f"♻️ Реінвест: {amount_uah:.0f} грн ≈ {format_usd_uah(amount_usd)} додано до балансу"
        else:
            add_balance("free_balance_usd", amount_usd)
            add_balance("total_invest_usd", amount_usd)
            add_transaction("invest", "Нове вкладення", amount_usd, amount_uah)
            text = f"📥 Поповнення: {amount_uah:.0f} грн ≈ {format_usd_uah(amount_usd)} додано до балансу"
        set_state(user_id, None)
        await query.edit_message_text(text, reply_markup=kb_finance())

    # ===== ANALYTICS =====
    elif section == "analytics":
        if action == "top":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT *, (current_price_usd - buy_price_usd) / NULLIF(buy_price_usd, 0) * 100 as pct "
                "FROM steam_items WHERE status='active' AND buy_price_usd > 0 "
                "ORDER BY pct DESC LIMIT 5"
            ).fetchall()
            conn.close()
            if not items:
                text = "🏆 Немає даних."
            else:
                lines = ["🏆 Топ-5 активів за зростанням:"]
                for it in items:
                    pct = it["pct"] or 0.0
                    lines.append(f"• {it['name']}: {pct:+.1f}%")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_analytics())

        elif action == "worst":
            conn = get_db_conn()
            items = conn.execute(
                "SELECT *, (current_price_usd - buy_price_usd) / NULLIF(buy_price_usd, 0) * 100 as pct "
                "FROM steam_items WHERE status='active' AND buy_price_usd > 0 "
                "ORDER BY pct ASC LIMIT 5"
            ).fetchall()
            conn.close()
            if not items:
                text = "📉 Немає даних."
            else:
                lines = ["📉 Топ-5 найгірших активів:"]
                for it in items:
                    pct = it["pct"] or 0.0
                    lines.append(f"• {it['name']}: {pct:+.1f}%")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_analytics())

        elif action == "stats":
            conn = get_db_conn()
            cs2_count = conn.execute("SELECT COUNT(*) FROM steam_items WHERE game='cs2' AND status='active'").fetchone()[0]
            dota_count = conn.execute("SELECT COUNT(*) FROM steam_items WHERE game='dota2' AND status='active'").fetchone()[0]
            gifts_count = conn.execute("SELECT COUNT(*) FROM gifts WHERE status IN ('active','for_sale')").fetchone()[0]
            now = datetime.utcnow()
            month_start = now.replace(day=1).strftime("%Y-%m-%d")
            sold_month = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(net_price_usd),0) as total "
                "FROM steam_items WHERE status='sold' AND sold_date >= ?",
                (month_start,)
            ).fetchone()
            # Best sale
            best = conn.execute(
                "SELECT name, net_price_usd - buy_price_usd as profit "
                "FROM steam_items WHERE status='sold' ORDER BY profit DESC LIMIT 1"
            ).fetchone()
            total_sold = conn.execute("SELECT COUNT(*) FROM steam_items WHERE status='sold'").fetchone()[0]
            profitable = conn.execute(
                "SELECT COUNT(*) FROM steam_items WHERE status='sold' AND net_price_usd > buy_price_usd"
            ).fetchone()[0]
            conn.close()
            win_rate = (profitable / total_sold * 100) if total_sold > 0 else 0.0
            lines = [
                "📊 Статистика:",
                f"🔫 CS2 активів: {cs2_count}",
                f"🛡 Dota активів: {dota_count}",
                f"🎁 Подарунки: {gifts_count}",
                f"📦 Продано цього місяця: {sold_month[0]} шт ({format_usd_uah(sold_month[1])})",
            ]
            if best:
                lines.append(f"🏆 Найприбутковіший продаж: {best['name']} (+${best['profit']:.2f})")
            lines.append(f"🎯 Win Rate: {win_rate:.1f}% ({profitable}/{total_sold})")
            await query.edit_message_text("\n".join(lines), reply_markup=kb_analytics())

        elif action == "recommend":
            conn = get_db_conn()
            items = conn.execute("SELECT * FROM steam_items WHERE status='active' AND buy_price_usd > 0").fetchall()
            gifts = conn.execute("SELECT * FROM gifts WHERE status IN ('active','for_sale') AND usd_at_add > 0").fetchall()
            nfts = conn.execute("SELECT * FROM nft_tracked WHERE status='tracking'").fetchall()
            conn.close()
            lines = ["🤖 Рекомендації:"]
            has_recs = False
            for it in items:
                buy = it["buy_price_usd"]
                cur = it["current_price_usd"] or buy
                pct = (cur - buy) / buy * 100
                if pct >= 20:
                    lines.append(f"✅ Продай {it['name']}: +{pct:.0f}%")
                    has_recs = True
                elif pct <= -10:
                    lines.append(f"⚠️ {it['name']} просів на {abs(pct):.0f}%")
                    has_recs = True
            for g in gifts:
                add_usd = g["usd_at_add"]
                cur_usd = g["current_usd"] or add_usd
                pct = (cur_usd - add_usd) / add_usd * 100
                if pct >= 10:
                    lines.append(f"🎁 {g['name']} виріс на {pct:.0f}% — вигідно продати")
                    has_recs = True
            ton_rate = get_ton_to_usd_rate() or 0.0
            for n in nfts:
                own = n["own_price_ton"]
                floor = n["floor_ton"] or 0.0
                if own and floor > 0:
                    own_usd = own * ton_rate
                    floor_usd = floor * ton_rate
                    lines.append(f"🔍 {n['collection_name']} #{n['nft_number']}: {format_usd_uah(own_usd)} (floor: {format_usd_uah(floor_usd)})")
                    has_recs = True
            if not has_recs:
                lines.append("Всі активи в нормі. Рекомендацій немає.")
            await query.edit_message_text("\n".join(lines), reply_markup=kb_analytics())

        elif action == "weekvweek":
            history = get_portfolio_history_db(days=21)
            today = datetime.utcnow().date()
            week_start = today - timedelta(days=today.weekday())
            prev_week_start = week_start - timedelta(days=7)
            curr_vals = [h for h in history if str(week_start) <= h["date"] <= str(today)]
            prev_vals = [h for h in history if str(prev_week_start) <= h["date"] < str(week_start)]
            curr_avg = sum(h["portfolio_usd"] for h in curr_vals) / len(curr_vals) if curr_vals else None
            prev_avg = sum(h["portfolio_usd"] for h in prev_vals) / len(prev_vals) if prev_vals else None
            lines = ["📅 Тиждень vs тиждень:"]
            if curr_avg and prev_avg:
                diff = curr_avg - prev_avg
                pct = diff / prev_avg * 100 if prev_avg > 0 else 0.0
                sign = "+" if diff >= 0 else ""
                lines.append(f"Цей тиждень: {format_usd_uah(curr_avg)}")
                lines.append(f"Минулий тиждень: {format_usd_uah(prev_avg)}")
                lines.append(f"Зміна: {sign}{format_usd_uah(diff)} ({sign}{pct:.1f}%)")
                if curr_vals:
                    best_day = max(curr_vals, key=lambda x: x["portfolio_usd"])
                    worst_day = min(curr_vals, key=lambda x: x["portfolio_usd"])
                    lines.append(f"Найкращий день: {best_day['date']} ({format_usd_uah(best_day['portfolio_usd'])})")
                    lines.append(f"Найгірший день: {worst_day['date']} ({format_usd_uah(worst_day['portfolio_usd'])})")
            else:
                lines.append("Недостатньо даних для порівняння.")
            await query.edit_message_text("\n".join(lines), reply_markup=kb_analytics())

    # ===== OTHER =====
    elif section == "other":
        if action == "alerts":
            conn = get_db_conn()
            active_alerts = conn.execute("SELECT * FROM alerts WHERE is_active=1").fetchall()
            conn.close()
            lines = ["🔔 Активні сповіщення:"]
            if active_alerts:
                for a in active_alerts:
                    lines.append(f"• {a['asset_name']}: {a['condition']} {a['threshold']:.2f}")
            else:
                lines.append("Немає активних сповіщень.")
            buttons = [
                [InlineKeyboardButton("➕ Додати", callback_data="alert:add")],
                [InlineKeyboardButton("🗑 Видалити", callback_data="alert:delete_list")],
                [InlineKeyboardButton("◀️ Назад", callback_data="main:other")],
            ]
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

        elif action == "targets":
            conn = get_db_conn()
            targets = conn.execute("SELECT * FROM price_targets WHERE is_active=1").fetchall()
            conn.close()
            lines = ["📋 Цільові ціни:"]
            if targets:
                for t in targets:
                    cond_str = "вище" if t["condition"] == "above" else "нижче"
                    lines.append(f"• {t['asset_name']}: {cond_str} {format_usd_uah(t['target_price_usd'])}")
            else:
                lines.append("Немає цільових цін.")
            buttons = [
                [InlineKeyboardButton("➕ Додати", callback_data="target:add")],
                [InlineKeyboardButton("🗑 Видалити", callback_data="target:delete_list")],
                [InlineKeyboardButton("◀️ Назад", callback_data="main:other")],
            ]
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))

        elif action == "recurring":
            await query.edit_message_text(
                "🔁 Регулярні витрати:",
                reply_markup=kb_recurring()
            )

        elif action == "clean":
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗑 Очистити sold/deleted", callback_data="clean:confirm"),
                    InlineKeyboardButton("❌ Скасувати", callback_data="main:other"),
                ]
            ])
            await query.edit_message_text(
                "🧹 Видалити всі sold/deleted записи зі Steam та Подарунків?",
                reply_markup=kb
            )

    elif section == "clean":
        if action == "confirm":
            conn = get_db_conn()
            r1 = conn.execute("DELETE FROM steam_items WHERE status IN ('sold','deleted')").rowcount
            r2 = conn.execute("DELETE FROM gifts WHERE status IN ('sold','deleted')").rowcount
            conn.commit()
            conn.close()
            await query.edit_message_text(
                f"🧹 Очищено!\nSteam: {r1} записів\nПодарунки: {r2} записів",
                reply_markup=kb_other()
            )

    # ===== ALERT =====
    elif section == "alert":
        if action == "add":
            set_state(user_id, "await_alert_asset", prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🔔 Введи тип активу для сповіщення:\n(ton / portfolio / steam назва предмета)"
            )
            set_state(user_id, "await_alert_asset", prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "delete_list":
            conn = get_db_conn()
            alerts_list = conn.execute("SELECT * FROM alerts WHERE is_active=1").fetchall()
            conn.close()
            if not alerts_list:
                await query.edit_message_text("Немає сповіщень для видалення.", reply_markup=kb_other())
                return
            buttons = []
            for a in alerts_list:
                buttons.append([InlineKeyboardButton(
                    f"🗑 {a['asset_name']}: {a['condition']} {a['threshold']:.2f}",
                    callback_data=f"alertdel:{a['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="other:alerts")])
            await query.edit_message_text("Виберіть сповіщення для видалення:", reply_markup=InlineKeyboardMarkup(buttons))

    elif section == "alertdel":
        alert_id = int(action)
        conn = get_db_conn()
        conn.execute("UPDATE alerts SET is_active=0 WHERE id=?", (alert_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Сповіщення видалено.", reply_markup=kb_other())

    # ===== TARGET =====
    elif section == "target":
        if action == "add":
            conn = get_db_conn()
            items = conn.execute("SELECT id, name, game FROM steam_items WHERE status='active' ORDER BY name").fetchall()
            gifts = conn.execute("SELECT id, name FROM gifts WHERE status IN ('active','for_sale') ORDER BY name").fetchall()
            conn.close()
            buttons = []
            for it in items[:10]:
                buttons.append([InlineKeyboardButton(
                    f"🎮 {it['name']}",
                    callback_data=f"targetset:steam:{it['id']}"
                )])
            for g in gifts[:5]:
                buttons.append([InlineKeyboardButton(
                    f"🎁 {g['name']}",
                    callback_data=f"targetset:gift:{g['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="other:targets")])
            await query.edit_message_text("📋 Вибери актив для цільової ціни:", reply_markup=InlineKeyboardMarkup(buttons))

        elif action == "delete_list":
            conn = get_db_conn()
            targets = conn.execute("SELECT * FROM price_targets WHERE is_active=1").fetchall()
            conn.close()
            if not targets:
                await query.edit_message_text("Немає цільових цін.", reply_markup=kb_other())
                return
            buttons = []
            for t in targets:
                cond_str = "вище" if t["condition"] == "above" else "нижче"
                buttons.append([InlineKeyboardButton(
                    f"🗑 {t['asset_name']}: {cond_str} ${t['target_price_usd']:.2f}",
                    callback_data=f"targetdel:{t['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="other:targets")])
            await query.edit_message_text("Виберіть ціль для видалення:", reply_markup=InlineKeyboardMarkup(buttons))

    elif section == "targetset":
        asset_type = action  # steam або gift
        asset_id = int(param)
        conn = get_db_conn()
        if asset_type == "steam":
            row = conn.execute("SELECT name FROM steam_items WHERE id=?", (asset_id,)).fetchone()
        else:
            row = conn.execute("SELECT name FROM gifts WHERE id=?", (asset_id,)).fetchone()
        conn.close()
        asset_name = row["name"] if row else "?"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📈 Вище", callback_data=f"targetcond:above:{asset_type}:{asset_id}"),
                InlineKeyboardButton("📉 Нижче", callback_data=f"targetcond:below:{asset_type}:{asset_id}"),
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="other:targets")],
        ])
        await query.edit_message_text(
            f"📋 {asset_name}\nВибери умову:",
            reply_markup=kb
        )

    elif section == "targetcond":
        condition = action  # above або below
        rest = param  # steam:id або gift:id
        rest_parts = rest.split(":")
        asset_type = rest_parts[0]
        asset_id = int(rest_parts[1]) if len(rest_parts) > 1 else 0
        conn = get_db_conn()
        if asset_type == "steam":
            row = conn.execute("SELECT name FROM steam_items WHERE id=?", (asset_id,)).fetchone()
        else:
            row = conn.execute("SELECT name FROM gifts WHERE id=?", (asset_id,)).fetchone()
        conn.close()
        asset_name = row["name"] if row else "?"
        set_state(user_id, "await_target_price",
                  condition=condition, asset_type=asset_type, asset_id=asset_id, asset_name=asset_name,
                  prompt_msg_id=None, main_msg_id=query.message.message_id)
        cond_str = "вище" if condition == "above" else "нижче"
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📋 Введи цільову ціну в USD ({asset_name}, {cond_str}):"
        )
        set_state(user_id, "await_target_price",
                  condition=condition, asset_type=asset_type, asset_id=asset_id, asset_name=asset_name,
                  prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

    elif section == "targetdel":
        target_id = int(action)
        conn = get_db_conn()
        conn.execute("UPDATE price_targets SET is_active=0 WHERE id=?", (target_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Ціль видалено.", reply_markup=kb_other())

    # ===== RECURRING =====
    elif section == "recurring":
        if action == "add":
            set_state(user_id, "await_recurring_name", prompt_msg_id=None, main_msg_id=query.message.message_id)
            prompt = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🔁 Введи: назва, сума UAH, день місяця, категорія\n(через кому, наприклад: Netflix, 250, 15, ігри)"
            )
            set_state(user_id, "await_recurring_name", prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

        elif action == "list":
            conn = get_db_conn()
            recs = conn.execute("SELECT * FROM recurring_expenses WHERE is_active=1 ORDER BY day_of_month").fetchall()
            conn.close()
            if not recs:
                text = "🔁 Немає активних регулярних витрат."
            else:
                lines = ["🔁 Регулярні витрати:"]
                today_day = datetime.utcnow().day
                for r in recs:
                    next_day = r["day_of_month"]
                    if next_day < today_day:
                        next_month = (datetime.utcnow().replace(day=1) + timedelta(days=32)).replace(day=next_day)
                        next_str = next_month.strftime("%d.%m")
                    else:
                        next_str = f"{next_day:02d}.{datetime.utcnow().month:02d}"
                    lines.append(f"• {r['name']}: {r['amount_uah']:.0f} грн ({r['category']}) — {next_str}")
                text = "\n".join(lines)
            await query.edit_message_text(text, reply_markup=kb_recurring())

        elif action == "delete_list":
            conn = get_db_conn()
            recs = conn.execute("SELECT * FROM recurring_expenses WHERE is_active=1").fetchall()
            conn.close()
            if not recs:
                await query.edit_message_text("Немає регулярних витрат.", reply_markup=kb_recurring())
                return
            buttons = []
            for r in recs:
                buttons.append([InlineKeyboardButton(
                    f"🗑 {r['name']} ({r['amount_uah']:.0f} грн)",
                    callback_data=f"recdel:{r['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="recurring:list")])
            await query.edit_message_text("Виберіть для видалення:", reply_markup=InlineKeyboardMarkup(buttons))

    elif section == "recdel":
        rec_id = int(action)
        conn = get_db_conn()
        conn.execute("UPDATE recurring_expenses SET is_active=0 WHERE id=?", (rec_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Регулярну витрату видалено.", reply_markup=kb_recurring())

    elif section == "stockaction":
        if action == "add":
            set_state(user_id, "await_stock_ticker", prompt_msg_id=None, main_msg_id=query.message.message_id)
            await query.edit_message_text(
                "📈 Введи тикер або натисни кнопку:",
                reply_markup=kb_ticker_suggestions("")
            )
            set_state(user_id, "await_stock_ticker", prompt_msg_id=None, main_msg_id=query.message.message_id)

        elif action == "update":
            await query.edit_message_text("🔄 Оновлюю ціни акцій...")
            results = update_all_stocks()
            text = "📈 Оновлення акцій:\n" + "\n".join(results) if results else "📈 Немає акцій"
            kb_back_stocks = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")]])
            await query.edit_message_text(text, reply_markup=kb_back_stocks)

        elif action == "delete_list":
            conn = get_db_conn()
            stocks = conn.execute("SELECT * FROM stocks WHERE status='active'").fetchall()
            conn.close()
            if not stocks:
                await query.edit_message_text("Немає акцій.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")]]))
                return
            buttons = []
            for s in stocks:
                val = s["current_price_usd"] * s["quantity"]
                pnl = (s["current_price_usd"] - s["buy_price_usd"]) * s["quantity"]
                sign = "+" if pnl >= 0 else ""
                buttons.append([InlineKeyboardButton(
                    f"🗑 {s['ticker']} ×{s['quantity']:.2f} ({sign}${pnl:.2f})",
                    callback_data=f"stockdel:{s['id']}"
                )])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")])
            await query.edit_message_text("Оберіть акцію для видалення:", reply_markup=InlineKeyboardMarkup(buttons))

    elif section == "ticker_pick":
        ticker = action
        set_state(user_id, "await_stock_qty", ticker=ticker, main_msg_id=query.message.message_id)
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📈 {ticker} — {TICKER_DB.get(ticker, '')}\nВведи кількість акцій (напр. 10 або 0,09421):"
        )
        await query.edit_message_text(
            f"📈 {ticker} — {TICKER_DB.get(ticker, '')}\n⏳ Введи кількість...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="stockaction:add")]])
        )
        set_state(user_id, "await_stock_qty", ticker=ticker, prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

    elif section == "stock_use_cur":
        # format: stock_use_cur:TICKER:QTY:CUR_PRICE
        parts_s = data.split(":")
        ticker = parts_s[1]
        qty = float(parts_s[2])
        cur_price = float(parts_s[3])
        buy_price = cur_price
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO stocks (ticker, name, quantity, buy_price_usd, current_price_usd, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, ticker, qty, buy_price, cur_price, "active",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)
        await query.edit_message_text(
            f"✅ {ticker} × {qty} додано!\n💵 Куплено за поточною: ${cur_price:.2f}\n📊 PnL: $0.00 (щойно куплено)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")]])
        )

    elif section == "stockdel":
        stock_id = int(action)
        conn = get_db_conn()
        conn.execute("UPDATE stocks SET status='removed' WHERE id=?", (stock_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Акцію видалено.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")]]))

    elif section == "giftsell":
        gift_id = int(action)
        conn = get_db_conn()
        g = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("Подарунок не знайдено.", reply_markup=kb_gifts())
            return
        floor = g["floor_ton"] or 0.0
        kb_sell = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ За флором ({floor:.1f} TON)" if floor else "✅ За флором",
                callback_data=f"giftsellfloor:{gift_id}"
            )],
            [InlineKeyboardButton("📝 Своя ціна", callback_data=f"giftsellcustom:{gift_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data=f"giftdetail:{gift_id}")],
        ])
        await query.edit_message_text(
            f"🎁 {g['name']}\n📊 Поточний флор: {floor:.1f} TON\n\nВиберіть ціну продажу:",
            reply_markup=kb_sell
        )

    elif section == "giftsellfloor":
        gift_id = int(action)
        conn = get_db_conn()
        g = conn.execute("SELECT * FROM gifts WHERE id=?", (gift_id,)).fetchone()
        conn.close()
        if not g:
            await query.edit_message_text("Подарунок не знайдено.", reply_markup=kb_gifts())
            return
        floor = g["floor_ton"] or g["ton"] or 0.0
        ton_rate = get_ton_to_usd_rate() or 0.0
        cur_usd = floor * ton_rate
        net_usd = calc_gift_net(cur_usd)
        conn = get_db_conn()
        conn.execute(
            "UPDATE gifts SET status='for_sale', floor_ton=?, current_usd=?, net_usd=? WHERE id=?",
            (floor, cur_usd, net_usd, gift_id)
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"✅ {g['name']} виставлено на продаж за флором!\n💎 {floor:.1f} TON = ${cur_usd:.2f}\nНетто: ${net_usd:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="gifts:list")]])
        )

    elif section == "giftsellcustom":
        gift_id = int(action)
        set_state(user_id, "await_gift_sell_ton", gift_id=gift_id, main_msg_id=query.message.message_id)
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="💎 Введи ціну продажу в TON (напр. 150 або 0,5):"
        )
        await query.edit_message_text(
            "⏳ Введи ціну продажу в TON...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"giftsell:{gift_id}")]])
        )
        set_state(user_id, "await_gift_sell_ton", gift_id=gift_id, prompt_msg_id=prompt.message_id, main_msg_id=query.message.message_id)

# ============== MESSAGE HANDLER (текстовий ввід) ==============

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_message(update):
        return
    user_id = update.effective_user.id
    state = get_state(user_id)

    if not state:
        # Немає стану — показуємо головне меню
        text, kb = kb_main()
        await update.message.reply_text(text, reply_markup=kb)
        return

    mode = state.get("mode")
    prompt_msg_id = state.get("prompt_msg_id")
    main_msg_id = state.get("main_msg_id")
    user_text = update.message.text.strip()

    # Видаляємо повідомлення користувача і підказку
    try:
        await update.message.delete()
    except Exception:
        pass
    if prompt_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        except Exception:
            pass

    # ===== STEAM SEARCH =====
    if mode == "await_steam_search":
        game = state.get("game", "cs2")
        appid = APPID_CS2 if game == "cs2" else APPID_DOTA2
        game_title = "CS2" if game == "cs2" else "Dota 2"
        results = await asyncio.to_thread(fetch_steam_market_search, user_text, appid)
        if not results:
            msg_text = f"❌ Нічого не знайдено за запитом '{user_text}'."
            if main_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=main_msg_id,
                        text=msg_text,
                        reply_markup=kb_game(game)
                    )
                except Exception:
                    await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_game(game))
            else:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_game(game))
            set_state(user_id, None)
            return
        set_state(user_id, "await_steam_search_result", game=game, search_results=results, main_msg_id=main_msg_id)
        buttons = []
        for idx, r in enumerate(results):
            net = calc_net(r["price_usd"])
            buttons.append([InlineKeyboardButton(
                f"{r['name']} (${r['price_usd']:.2f})",
                callback_data=f"steamresult:{idx}:{game}"
            )])
        buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"assets:{game}")])
        msg_text = f"🔍 Результати пошуку '{user_text}' [{game_title}]:"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=InlineKeyboardMarkup(buttons))

    # ===== GIFT INPUT (new flow) =====
    elif mode == "await_gift_name_new":
        name = user_text
        slug = name_to_fragment_slug(name)
        # Show "fetching floor" message
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=f"⏳ Перевіряю флор {name} на Fragment..."
                )
            except Exception:
                pass
        floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, slug)
        ton_rate = await asyncio.to_thread(get_ton_to_usd_rate) or 0.0

        floor_info = ""
        if floor is not None:
            floor_info = f"\n📊 Флор зараз: {floor:.1f} TON (~${floor * ton_rate:.2f})"
        else:
            floor_info = "\n📊 Флор: не знайдено"

        set_state(user_id, "await_gift_ton_new",
                  name=name, slug=slug, floor=floor, ton_rate=ton_rate,
                  main_msg_id=main_msg_id)
        prompt = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🎁 {name}{floor_info}\n\n💎 За скільки купив (TON)?"
        )
        set_state(user_id, "await_gift_ton_new",
                  name=name, slug=slug, floor=floor, ton_rate=ton_rate,
                  prompt_msg_id=prompt.message_id, main_msg_id=main_msg_id)
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=f"🎁 {name}{floor_info}\n\n💎 Введи ціну купівлі в TON...",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="gifts")]])
                )
            except Exception:
                pass

    elif mode == "await_gift_ton_new":
        ton = parse_float(user_text)
        if ton is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи ціну в TON (напр. 150 або 0,5):"
            )
            set_state(user_id, "await_gift_ton_new",
                      name=state.get("name"), slug=state.get("slug"),
                      floor=state.get("floor"), ton_rate=state.get("ton_rate"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        name = state.get("name", "")
        slug = state.get("slug", name_to_fragment_slug(name))
        floor = state.get("floor")
        ton_rate = state.get("ton_rate") or (await asyncio.to_thread(get_ton_to_usd_rate) or 0.0)

        usd_at_add = ton * ton_rate
        if floor is None:
            floor = ton
        current_usd = floor * ton_rate
        net_usd = calc_gift_net(current_usd)

        conn = get_db_conn()
        conn.execute(
            "INSERT INTO gifts (name, fragment_slug, ton, floor_ton, usd_at_add, current_usd, net_usd, added_date, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, slug, ton, floor, usd_at_add, current_usd, net_usd,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "active")
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)

        floor_str = f"{floor:.1f} TON = ${current_usd:.2f}"
        pnl = current_usd - usd_at_add
        sign = "+" if pnl >= 0 else ""
        msg_text = (
            f"✅ {name} додано!\n"
            f"💎 Куплено: {ton:.1f} TON = ${usd_at_add:.2f}\n"
            f"📊 Флор: {floor_str}\n"
            f"📈 PnL: {sign}${pnl:.2f}"
        )
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:gifts")]])
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text)
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text)

    # ===== NFT INPUT =====
    elif mode == "await_nft_input":
        parts = user_text.rsplit(" ", 1)
        if len(parts) != 2:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи назву і номер через пробіл (наприклад: Plush Pepe 1315):"
            )
            set_state(user_id, "await_nft_input", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        collection_name = parts[0].strip()
        try:
            nft_number = int(parts[1].strip())
        except ValueError:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Номер NFT має бути цілим числом. Спробуй ще раз:"
            )
            set_state(user_id, "await_nft_input", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        collection_slug = name_to_fragment_slug(collection_name)
        nft_slug = f"{collection_slug}-{nft_number}"
        own_price = await asyncio.to_thread(fetch_fragment_nft_price_ton, nft_slug)
        floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, collection_slug)
        ton_rate = await asyncio.to_thread(get_ton_to_usd_rate) or 0.0
        own_str = f"{own_price:.0f} TON" if own_price else "Не на продажі"
        floor_str = f"{floor:,.0f} TON (~{format_usd_uah(floor * ton_rate)})" if floor else "невідомо"
        usd_value = (own_price or floor or 0.0) * ton_rate
        set_state(user_id, "await_nft_confirm",
                  pending_nft={
                      "collection_name": collection_name,
                      "collection_slug": collection_slug,
                      "nft_number": nft_number,
                      "slug": nft_slug,
                      "own_price": own_price,
                      "floor": floor,
                      "usd_value": usd_value,
                  },
                  main_msg_id=main_msg_id)
        msg_text = (
            f"🔍 {collection_name} #{nft_number}\n"
            f"Ваша ціна: {own_str}\n"
            f"Floor: {floor_str}\n"
            f"USD: {format_usd_uah(usd_value)}\n\n"
            f"Додати до відстеження?"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Так", callback_data="nftaddconfirm:yes:"),
                InlineKeyboardButton("❌ Ні", callback_data="nftaddconfirm:no:"),
            ]
        ])
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb)
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb)

    # ===== TOPUP =====
    elif mode == "await_topup":
        try:
            amount_uah = float(user_text.replace(",", "."))
        except ValueError:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи суму числом (наприклад: 1000):"
            )
            set_state(user_id, "await_topup", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        amount_usd, err = uah_to_usd(amount_uah)
        if err:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ {err}"
            )
            set_state(user_id, "await_topup", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        set_state(user_id, "await_topup_type", pending_amount_uah=amount_uah, main_msg_id=main_msg_id)
        msg_text = (
            f"📥 Поповнення: {amount_uah:.0f} грн ≈ {format_usd_uah(amount_usd)}\n\n"
            f"Це реінвест або нове вкладення?"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💼 Це реінвест", callback_data="topup:reinvest"),
                InlineKeyboardButton("➕ Нове вкладення", callback_data="topup:new"),
            ]
        ])
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb)
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb)

    # ===== EXPENSE AMOUNT =====
    elif mode == "await_expense_amount":
        try:
            amount_uah = float(user_text.replace(",", "."))
        except ValueError:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи суму числом (в UAH):"
            )
            set_state(user_id, "await_expense_amount",
                      category=state.get("category"), cat_name=state.get("cat_name"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        category = state.get("category", "other")
        cat_name = state.get("cat_name", "Інше")
        amount_usd, err = uah_to_usd(amount_uah)
        if err or amount_usd is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Помилка конвертації. Спробуй ще раз:"
            )
            set_state(user_id, "await_expense_amount",
                      category=category, cat_name=cat_name,
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        # Додаємо витрату
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO expenses(amount_uah,amount_usd,category,note,created_at) VALUES(?,?,?,?,?)",
            (amount_uah, amount_usd, category, "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        add_balance("free_balance_usd", -amount_usd)
        add_transaction("expense", cat_name, -amount_usd, amount_uah, f"Витрата: {cat_name}")
        budget = get_balance("monthly_budget_uah") or UAH_BUDGET_DEFAULT
        spent = get_monthly_expenses_uah()
        remaining = max(0, budget - spent)
        set_state(user_id, None)
        msg_text = (
            f"💸 Витрата записана!\n"
            f"Категорія: {cat_name}\n"
            f"Сума: {amount_uah:.0f} грн ≈ {format_usd_uah(amount_usd)}\n"
            f"Залишок бюджету: {remaining:.0f} грн"
        )
        bal = get_balance("free_balance_usd")
        msg_text += f"\n💵 Вільний баланс: {format_usd_uah(bal)}"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb_finance()
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_finance())
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_finance())

    # ===== ALERT ASSET =====
    elif mode == "await_alert_asset":
        asset_name = user_text
        set_state(user_id, "await_alert_condition", asset_name=asset_name,
                  prompt_msg_id=None, main_msg_id=main_msg_id)
        new_prompt = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🔔 {asset_name}\nВведи умову (вище X або нижче X, де X — число):\nНаприклад: вище 5.5"
        )
        set_state(user_id, "await_alert_condition", asset_name=asset_name,
                  prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)

    elif mode == "await_alert_condition":
        asset_name = state.get("asset_name", "")
        cond_text = user_text.lower()
        condition = None
        threshold = None
        try:
            parts_c = cond_text.split()
            if len(parts_c) >= 2:
                condition = f"📈 {parts_c[0]} X {parts_c[0]}" if "вище" in parts_c[0] else f"📉 {parts_c[0]} X"
                condition = cond_text
                threshold = float(parts_c[-1].replace(",", "."))
        except Exception:
            pass
        if threshold is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Не вдалось розібрати умову. Спробуй ще раз (наприклад: вище 5.5):"
            )
            set_state(user_id, "await_alert_condition", asset_name=asset_name,
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO alerts(asset_type,asset_name,condition,threshold,is_active,created_at) VALUES(?,?,?,?,?,?)",
            ("custom", asset_name, condition, threshold, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)
        msg_text = f"✅ Сповіщення додано!\n{asset_name}: {condition}"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb_other()
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_other())
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_other())

    # ===== TARGET PRICE =====
    elif mode == "await_target_price":
        try:
            target_price = float(user_text.replace(",", "."))
        except ValueError:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи ціну числом в USD:"
            )
            set_state(user_id, "await_target_price",
                      condition=state.get("condition"), asset_type=state.get("asset_type"),
                      asset_id=state.get("asset_id"), asset_name=state.get("asset_name"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        asset_type = state.get("asset_type")
        asset_id = state.get("asset_id")
        asset_name = state.get("asset_name")
        condition = state.get("condition")
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO price_targets(asset_type,asset_id,asset_name,target_price_usd,condition,is_active,created_at) VALUES(?,?,?,?,?,?,?)",
            (asset_type, asset_id, asset_name, target_price, condition, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)
        cond_str = "вище" if condition == "above" else "нижче"
        msg_text = f"✅ Ціль додано!\n{asset_name}: {cond_str} {format_usd_uah(target_price)}"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb_other()
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_other())
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_other())

    # ===== RECURRING NAME =====
    elif mode == "await_recurring_name":
        parts_r = [p.strip() for p in user_text.split(",")]
        if len(parts_r) < 3:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи: назва, сума UAH, день, категорія (через кому):\nНаприклад: Netflix, 250, 15, ігри"
            )
            set_state(user_id, "await_recurring_name", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        rec_name = parts_r[0]
        try:
            rec_amount = float(parts_r[1].replace(",", "."))
            rec_day = int(parts_r[2])
            rec_cat = parts_r[3] if len(parts_r) > 3 else "інше"
        except ValueError:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Помилка формату. Спробуй: Netflix, 250, 15, ігри"
            )
            set_state(user_id, "await_recurring_name", prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        rec_day = max(1, min(28, rec_day))
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO recurring_expenses(name,amount_uah,day_of_month,category,is_active,created_at) VALUES(?,?,?,?,?,?)",
            (rec_name, rec_amount, rec_day, rec_cat, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)
        msg_text = f"✅ Регулярну витрату додано!\n{rec_name}: {rec_amount:.0f} грн, кожного {rec_day}-го числа"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb_recurring()
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_recurring())
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_recurring())

    # ===== STEAM BUY PRICE =====
    elif mode == "await_steam_buyprice":
        buy_price = parse_float(user_text)
        if buy_price is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи ціну числом (напр. 12.50 або 12,50):"
            )
            set_state(user_id, "await_steam_buyprice",
                      game=state.get("game"), name=state.get("name"),
                      qty=state.get("qty"), cur_price=state.get("cur_price"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        game = state.get("game", "cs2")
        name = state.get("name", "")
        qty = state.get("qty", 1)
        cur_price = state.get("cur_price", buy_price)
        net_price = calc_net(cur_price)

        conn = get_db_conn()
        conn.execute(
            "INSERT INTO steam_items (game, name, quantity, buy_price_usd, current_price_usd, net_price_usd, added_date, status) VALUES (?,?,?,?,?,?,?,?)",
            (game, name, qty, buy_price, cur_price, net_price,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "active")
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)

        pnl = (cur_price - buy_price) * qty
        sign = "+" if pnl >= 0 else ""
        msg_text = (
            f"✅ {name} × {qty} додано!\n"
            f"💵 Куплено: ${buy_price:.2f} × {qty}\n"
            f"📊 Зараз: ${cur_price:.2f} → PnL: {sign}${pnl:.2f}"
        )
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=kb_game(game)
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_game(game))
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text, reply_markup=kb_game(game))

    # ===== STOCK TICKER =====
    elif mode == "await_stock_ticker":
        query_text = user_text.upper().strip()
        matches = search_tickers(query_text)
        # If exact match go directly to qty
        if query_text in TICKER_DB or (len(matches) == 1 and matches[0][0] == query_text):
            ticker = query_text if query_text in TICKER_DB else matches[0][0]
            set_state(user_id, "await_stock_qty", ticker=ticker, main_msg_id=main_msg_id)
            prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📈 {ticker} — {TICKER_DB.get(ticker, '')}\nВведи кількість акцій (напр. 10 або 0,09421):"
            )
            set_state(user_id, "await_stock_qty", ticker=ticker, prompt_msg_id=prompt.message_id, main_msg_id=main_msg_id)
            if main_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=main_msg_id,
                        text=f"📈 {ticker} обрано. Введи кількість...",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="stockaction:add")]])
                    )
                except Exception:
                    pass
        else:
            hint = f"🔍 {query_text}" if matches else f"❓ {query_text} — не знайдено, але можна ввести вручну"
            if matches:
                hint += " — обери або введи повний тикер:"
            set_state(user_id, "await_stock_ticker", main_msg_id=main_msg_id)
            prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=hint,
                reply_markup=kb_ticker_suggestions(query_text)
            )
            set_state(user_id, "await_stock_ticker", prompt_msg_id=prompt.message_id, main_msg_id=main_msg_id)

    # ===== STOCK QTY =====
    elif mode == "await_stock_qty":
        qty = parse_float(user_text)
        if qty is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи число (напр. 10, 0,09421 або 2.5):"
            )
            set_state(user_id, "await_stock_qty",
                      ticker=state.get("ticker"), prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        ticker = state.get("ticker", "")
        # Fetch current price immediately
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=f"⏳ Перевіряю {ticker} на Yahoo Finance..."
                )
            except Exception:
                pass
        cur_price = await asyncio.to_thread(fetch_stock_price, ticker)

        price_hint = f"Зараз: ${cur_price:.2f}\n" if cur_price else "Поточну ціну не вдалось отримати\n"
        kb_cur = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ За поточною (${cur_price:.2f})" if cur_price else "✅ Ціна невідома",
                callback_data=f"stock_use_cur:{ticker}:{qty}:{cur_price or 0}"
            )],
            [InlineKeyboardButton("◀️ Назад", callback_data="stockaction:add")],
        ])
        set_state(user_id, "await_stock_price", ticker=ticker, qty=qty, cur_price=cur_price, main_msg_id=main_msg_id)
        prompt = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📈 {ticker} × {qty}\n{price_hint}💵 За скільки купив? (або натисни кнопку якщо купив за поточною)",
            reply_markup=kb_cur
        )
        set_state(user_id, "await_stock_price", ticker=ticker, qty=qty, cur_price=cur_price,
                  prompt_msg_id=prompt.message_id, main_msg_id=main_msg_id)
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=f"📈 {ticker} × {qty}\n{price_hint}",
                    reply_markup=kb_cur
                )
            except Exception:
                pass

    # ===== STOCK PRICE =====
    elif mode == "await_stock_price":
        buy_price = parse_float(user_text)
        if buy_price is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи ціну в USD (напр. 150.25 або 150,25):"
            )
            set_state(user_id, "await_stock_price",
                      ticker=state.get("ticker"), qty=state.get("qty"), cur_price=state.get("cur_price"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        ticker = state.get("ticker", "")
        qty = state.get("qty", 1)
        cur_price = state.get("cur_price") or buy_price

        conn = get_db_conn()
        conn.execute(
            "INSERT INTO stocks (ticker, name, quantity, buy_price_usd, current_price_usd, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, ticker, qty, buy_price, cur_price, "active",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)

        pnl = (cur_price - buy_price) * qty
        sign = "+" if pnl >= 0 else ""
        msg_text = (
            f"✅ {ticker} × {qty} додано!\n"
            f"💵 Куплено: ${buy_price:.2f} → Зараз: ${cur_price:.2f}\n"
            f"📊 PnL: {sign}${pnl:.2f}"
        )
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="assets:stocks")]])
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text)
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text)

    # ===== GIFT SELL TON =====
    elif mode == "await_gift_sell_ton":
        ton_sell = parse_float(user_text)
        if ton_sell is None:
            new_prompt = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Введи ціну в TON (напр. 150 або 0,5):"
            )
            set_state(user_id, "await_gift_sell_ton",
                      gift_id=state.get("gift_id"),
                      prompt_msg_id=new_prompt.message_id, main_msg_id=main_msg_id)
            return
        gift_id = state.get("gift_id")
        ton_rate = await asyncio.to_thread(get_ton_to_usd_rate) or 0.0
        cur_usd = ton_sell * ton_rate
        net_usd = calc_gift_net(cur_usd)

        conn = get_db_conn()
        conn.execute(
            "UPDATE gifts SET status='for_sale', floor_ton=?, current_usd=?, net_usd=? WHERE id=?",
            (ton_sell, cur_usd, net_usd, gift_id)
        )
        conn.commit()
        conn.close()
        set_state(user_id, None)

        msg_text = f"✅ Подарунок виставлено на продаж!\n💎 {ton_sell:.1f} TON = ${cur_usd:.2f}\nНетто: ${net_usd:.2f}"
        if main_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=main_msg_id,
                    text=msg_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="main:gifts")]])
                )
            except Exception:
                await context.bot.send_message(update.effective_chat.id, msg_text)
        else:
            await context.bot.send_message(update.effective_chat.id, msg_text)

    else:
        # Невідомий стан
        set_state(user_id, None)
        text, kb = kb_main()
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=kb)

# ============== JOB FUNCTIONS ==============

async def job_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = calc_current_portfolio_value_db()
        pnl_night, pnl_night_pct = get_pnl_for_period(1)
        cs2 = get_steam_net_total("cs2")
        dota = get_steam_net_total("dota2")
        steam_total = cs2 + dota
        conn = get_db_conn()
        cs2_count = conn.execute("SELECT COUNT(*) FROM steam_items WHERE status='active' AND game='cs2'").fetchone()[0]
        dota_count = conn.execute("SELECT COUNT(*) FROM steam_items WHERE status='active' AND game='dota2'").fetchone()[0]
        gifts_count = conn.execute("SELECT COUNT(*) FROM gifts WHERE status IN ('active','for_sale')").fetchone()[0]
        stocks_count = conn.execute("SELECT COUNT(*) FROM stocks WHERE status='active'").fetchone()[0]
        stock_rows = conn.execute("SELECT quantity, current_price_usd, buy_price_usd FROM stocks WHERE status='active'").fetchall()
        stocks_val = sum((r["quantity"] or 0) * (r["current_price_usd"] or r["buy_price_usd"] or 0.0) for r in stock_rows)
        conn.close()
        gifts_total = get_gifts_net_total()
        cash = get_balance("free_balance_usd")

        pnl_str = ""
        if pnl_night is not None:
            sign = "+" if pnl_night >= 0 else ""
            pct_str = f" ({sign}{pnl_night_pct:.1f}%)" if pnl_night_pct is not None else ""
            pnl_str = f"\n📈 За ніч: {sign}{format_usd_uah(pnl_night)}{pct_str}"

        stocks_str = f"\n📈 Акції: {stocks_count} ({format_usd_uah(stocks_val)})" if stocks_count > 0 else ""
        text = (
            f"🌅 Доброго ранку!\n"
            f"💼 Портфель: {format_usd_uah(portfolio)}"
            f"{pnl_str}\n"
            f"🎮 Steam: {cs2_count + dota_count} активів ({format_usd_uah(steam_total)})\n"
            f"🎁 Подарунки: {gifts_count} ({format_usd_uah(gifts_total)})"
            f"{stocks_str}\n"
            f"💵 Кеш: {format_usd_uah(cash)}"
        )

        # Рекомендації
        recs = []
        conn2 = get_db_conn()
        items = conn2.execute("SELECT * FROM steam_items WHERE status='active' AND buy_price_usd > 0").fetchall()
        conn2.close()
        for it in items:
            buy = it["buy_price_usd"]
            cur = it["current_price_usd"] or buy
            pct_change = (cur - buy) / buy * 100 if buy > 0 else 0
            if pct_change >= 20:
                recs.append(f"✅ Продай {it['name']}: +{pct_change:.0f}%")
        alerts_fired = check_price_targets() + check_alerts_db()
        if alerts_fired:
            recs.extend(alerts_fired)
        if recs:
            text += "\n\n🤖 Що варто зробити:\n" + "\n".join(f"• {r}" for r in recs[:5])

        await context.bot.send_message(chat_id=ALLOWED_USER, text=text)
    except Exception as e:
        logger.exception("job_morning_digest error: %s", e)

async def job_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    try:
        history = get_portfolio_history_db(days=21)
        today = datetime.utcnow().date()
        week_start = today - timedelta(days=today.weekday())
        prev_week_start = week_start - timedelta(days=7)
        curr_vals = [h for h in history if str(week_start) <= h["date"] <= str(today)]
        prev_vals = [h for h in history if str(prev_week_start) <= h["date"] < str(week_start)]
        curr_val = curr_vals[-1]["portfolio_usd"] if curr_vals else 0.0
        prev_val = prev_vals[-1]["portfolio_usd"] if prev_vals else 0.0
        diff = curr_val - prev_val
        pct = (diff / prev_val * 100) if prev_val > 0 else 0.0
        sign = "+" if diff >= 0 else ""

        now = datetime.utcnow()
        week_start_dt = now - timedelta(days=now.weekday())
        conn = get_db_conn()
        sold_week = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(net_price_usd - buy_price_usd),0) as profit "
            "FROM steam_items WHERE status='sold' AND sold_date >= ?",
            (week_start_dt.strftime("%Y-%m-%d"),)
        ).fetchone()
        expenses_week = conn.execute(
            "SELECT COALESCE(SUM(amount_uah),0) as total FROM expenses WHERE created_at >= ?",
            (week_start_dt.strftime("%Y-%m-%d %H:%M:%S"),)
        ).fetchone()
        conn.close()

        best_day_str = ""
        if curr_vals:
            best_day = max(curr_vals, key=lambda x: x["portfolio_usd"])
            best_day_str = f"\n🏆 Найкращий день: {best_day['date']} ({format_usd_uah(best_day['portfolio_usd'])})"

        text = (
            f"📅 Тижневий звіт\n\n"
            f"Цей тиждень: {format_usd_uah(curr_val)}\n"
            f"Минулий тиждень: {format_usd_uah(prev_val)}\n"
            f"Зміна: {sign}{format_usd_uah(diff)} ({sign}{pct:.1f}%)\n"
            f"Продано за тиждень: {sold_week['cnt']} предметів\n"
            f"Заробіток: {format_usd_uah(sold_week['profit'])}\n"
            f"Витрати: {expenses_week['total']:.0f} грн"
            f"{best_day_str}"
        )
        await context.bot.send_message(chat_id=ALLOWED_USER, text=text)
    except Exception as e:
        logger.exception("job_weekly_report error: %s", e)

async def job_daily_snapshot(context: ContextTypes.DEFAULT_TYPE):
    try:
        val = await asyncio.to_thread(record_snapshot)
        logger.info("Daily snapshot: $%.2f", val)
    except Exception as e:
        logger.exception("job_daily_snapshot error: %s", e)

async def job_daily_backup(context: ContextTypes.DEFAULT_TYPE):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_name = f"bot_data_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, backup_name))
        # Видаляємо старі backup старше 7 днів
        cutoff = datetime.utcnow() - timedelta(days=7)
        for fname in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, fname)
            if os.path.getmtime(fpath) < cutoff.timestamp():
                os.remove(fpath)
        logger.info("Backup created: %s", backup_name)
    except Exception as e:
        logger.exception("job_daily_backup error: %s", e)

async def job_auto_update_prices(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = get_db_conn()
        items = conn.execute("SELECT * FROM steam_items WHERE status='active'").fetchall()
        conn.close()
        appid_map = {"cs2": APPID_CS2, "dota2": APPID_DOTA2}
        conn = get_db_conn()
        for it in items:
            appid = appid_map.get(it["game"], APPID_CS2)
            price = await asyncio.to_thread(fetch_steam_price_usd, appid, it["name"])
            if price is not None:
                net = calc_net(price)
                conn.execute(
                    "UPDATE steam_items SET current_price_usd=?, net_price_usd=? WHERE id=?",
                    (price, net, it["id"])
                )
        conn.commit()

        gifts = conn.execute("SELECT * FROM gifts WHERE status IN ('active','for_sale')").fetchall()
        nfts = conn.execute("SELECT * FROM nft_tracked WHERE status='tracking'").fetchall()
        conn.close()

        ton_rate = await asyncio.to_thread(get_ton_to_usd_rate)
        if ton_rate:
            conn = get_db_conn()
            for g in gifts:
                slug = g["fragment_slug"] or name_to_fragment_slug(g["name"])
                floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, slug)
                if floor is not None:
                    cur_usd = floor * ton_rate
                    net_usd = calc_gift_net(cur_usd)
                    conn.execute(
                        "UPDATE gifts SET floor_ton=?, current_usd=?, net_usd=? WHERE id=?",
                        (floor, cur_usd, net_usd, g["id"])
                    )
            for n in nfts:
                price_ton = await asyncio.to_thread(fetch_fragment_nft_price_ton, n["slug"])
                floor = await asyncio.to_thread(fetch_fragment_floor_price_ton, n["collection_slug"])
                usd_val = (price_ton or floor or 0.0) * ton_rate
                conn.execute(
                    "UPDATE nft_tracked SET own_price_ton=?, floor_ton=?, usd_value=? WHERE id=?",
                    (price_ton, floor, usd_val, n["id"])
                )
            conn.commit()
            conn.close()

        # Update stocks
        stock_updates = update_all_stocks()
        if stock_updates:
            logger.info("Stocks updated: %s", stock_updates)

        alerts_fired = check_price_targets() + check_alerts_db()
        if alerts_fired:
            msg = "🔔 Спрацювали алерти:\n" + "\n".join(alerts_fired)
            await context.bot.send_message(chat_id=ALLOWED_USER, text=msg)
        logger.info("Auto price update done.")
    except Exception as e:
        logger.exception("job_auto_update_prices error: %s", e)

async def job_check_recurring_expenses(context: ContextTypes.DEFAULT_TYPE):
    try:
        today_day = datetime.utcnow().day
        conn = get_db_conn()
        recs = conn.execute(
            "SELECT * FROM recurring_expenses WHERE is_active=1 AND day_of_month=?",
            (today_day,)
        ).fetchall()
        conn.close()
        for r in recs:
            amount_uah = r["amount_uah"]
            amount_usd, err = uah_to_usd(amount_uah)
            if err or amount_usd is None:
                continue
            add_balance("free_balance_usd", -amount_usd)
            conn2 = get_db_conn()
            conn2.execute(
                "INSERT INTO expenses(amount_uah,amount_usd,category,note,created_at) VALUES(?,?,?,?,?)",
                (amount_uah, amount_usd, r["category"], f"Авто: {r['name']}",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn2.commit()
            conn2.close()
            add_transaction("expense_auto", r["name"], -amount_usd, amount_uah, "Авто-регулярна")
            await context.bot.send_message(
                chat_id=ALLOWED_USER,
                text=f"🔁 Регулярна витрата: {r['name']} — {amount_uah:.0f} грн списано ({format_usd_uah(amount_usd)})"
            )
        logger.info("Recurring expenses checked for day %d", today_day)
    except Exception as e:
        logger.exception("job_check_recurring_expenses error: %s", e)

# ============== POST INIT ==============

async def post_init(app):
    init_db()
    try:
        await app.bot.send_message(chat_id=ALLOWED_USER, text="✅ Бот v3 запущено!")
    except Exception as e:
        logger.warning("Could not send startup message: %s", e)

# ============== MAIN ==============

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Jobs
    jq = app.job_queue

    # Morning digest 06:00 UTC (09:00 EEST)
    jq.run_daily(job_morning_digest, time=time(hour=6, minute=0, second=0))

    # Weekly report Monday 07:00 UTC
    jq.run_daily(
        job_weekly_report,
        time=time(hour=7, minute=0, second=0),
        days=(0,),  # Monday
    )

    # Daily snapshot 23:59 UTC
    jq.run_daily(job_daily_snapshot, time=time(hour=23, minute=59, second=0))

    # Daily backup 03:00 UTC
    jq.run_daily(job_daily_backup, time=time(hour=3, minute=0, second=0))

    # Auto price update 06:00 UTC
    jq.run_daily(job_auto_update_prices, time=time(hour=6, minute=5, second=0))

    # Check recurring expenses 08:00 UTC
    jq.run_daily(job_check_recurring_expenses, time=time(hour=8, minute=0, second=0))

    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
