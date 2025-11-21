import ccxt
import time
import requests
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import sys

# ============ НАЛАШТУВАННЯ ============
TELEGRAM_BOT_TOKEN = 'your bot token'
TELEGRAM_CHAT_ID = 'your chat id'

# Глобальні змінні
monitoring_active = True
min_spread = 0.15
check_interval = 15
selected_exchanges = ['Binance', 'Bybit', 'OKX', 'KuCoin']
min_volume = 30
max_volume = 1000
ABSOLUTE_MIN_VOLUME = 30
all_symbols = []  # ← ДОДАНО!

# Зберігання спредів
spreads_cache = {}
SPREAD_LIFETIME = 180
current_spreads = []
last_update = None
is_ready = False

# Фільтри для валідних спредів
MAX_REALISTIC_SPREAD = 5.0  # Максимум 5% - більше підозріло
MIN_PRICE = 0.00000001  # Мінімальна ціна

EXCHANGES = {
    'Binance': ccxt.binance({'timeout': 3000, 'enableRateLimit': False}),
    'Bybit': ccxt.bybit({'timeout': 3000, 'enableRateLimit': False}),
    'OKX': ccxt.okx({'timeout': 3000, 'enableRateLimit': False}),
    'KuCoin': ccxt.kucoin({'timeout': 3000, 'enableRateLimit': False}),
    'Gate.io': ccxt.gateio({'timeout': 3000, 'enableRateLimit': False}),
    'MEXC': ccxt.mexc({'timeout': 3000, 'enableRateLimit': False}),
    'Bitget': ccxt.bitget({'timeout': 3000, 'enableRateLimit': False}),
    'Digifinex': ccxt.digifinex({'timeout': 3000, 'enableRateLimit': False}),
}

FEES = {
    'Binance': 0.1,
    'Bybit': 0.1,
    'OKX': 0.08,
    'KuCoin': 0.1,
    'Gate.io': 0.15,
    'MEXC': 0.0,
    'Bitget': 0.1,
    'Digifinex': 0.2,
}

EXCLUDE_COINS = ['USDT', 'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDD', 'USDP', 'TEST', 'TST']
stats = {'checks': 0, 'opportunities': 0, 'total_pairs': 0}
ITEMS_PER_PAGE = 7

def signal_handler(sig, frame):
    global monitoring_active
    print('\n\n❌ Зупинка...')
    monitoring_active = False
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ============ КЛАВІАТУРИ ============

def get_main_keyboard():
    keyboard = [
        ['📊 Список спредів'],
        ['⚙️ Налаштування', '📈 Статистика'],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_settings_keyboard():
    keyboard = [
        ['📊 Мін. спред', '⏱️ Інтервал'],
        ['💰 Об\'єм', '💱 Біржі'],
        ['◀️ Назад']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_spread_keyboard():
    keyboard = [
        ['0.05%', '0.1%', '0.15%'],
        ['0.2%', '0.3%', '0.5%'],
        ['◀️ Назад']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_interval_keyboard():
    keyboard = [
        ['10с', '15с', '30с'],
        ['60с', '120с', '180с'],
        ['◀️ Назад']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_volume_keyboard():
    keyboard = [
        ['30-100', '30-200', '50-500'],
        ['100-1000', '50-1000'],
        ['Своє значення'],
        ['◀️ Назад']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_exchanges_keyboard():
    keyboard = []
    for exchange in EXCHANGES.keys():
        status = "✅" if exchange in selected_exchanges else "❌"
        fee = FEES.get(exchange, 0.1)
        keyboard.append([f"{status} {exchange} ({fee}%)"])
    keyboard.append(['◀️ Назад'])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_spreads_list_keyboard(page=0):
    if not current_spreads:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Оновити", callback_data="refresh_spreads")
        ]])
    
    keyboard = []
    total_pages = (len(current_spreads) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(current_spreads))
    
    for i in range(start_idx, end_idx):
        spread = current_spreads[i]
        volume_str = f"{int(spread['max_trade'])}"
        
        age = (datetime.now() - spread['found_at']).total_seconds()
        age_str = f"{int(age)}с" if age < 60 else f"{int(age/60)}м"
        
        button_text = f"{spread['coin']}: {volume_str} +{spread['profit']:.0f}$ ({spread['pct']:.2f}%) [{age_str}]"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"detail_{i}")])
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="current_page"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page_{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([
        InlineKeyboardButton("🔄 Оновити", callback_data="refresh_spreads"),
        InlineKeyboardButton("❌ Закрити", callback_data="close_spreads")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def get_detail_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_list")
    ]])

# ============ ШВИДКИЙ МОНІТОРИНГ ============

def get_all_usdt_pairs():
    """Швидке отримання всіх USDT пар"""
    all_pairs = set()
    
    print("\n" + "="*70)
    print("🔄 Завантаження пар...")
    print("="*70)
    
    active_exchanges = {k: v for k, v in EXCHANGES.items() if k in selected_exchanges}
    
    for name, exchange in active_exchanges.items():
        try:
            print(f"📡 {name}...", end=" ", flush=True)
            markets = exchange.load_markets()
            
            count = 0
            for symbol in markets.keys():
                if symbol.endswith('/USDT') and ':' not in symbol:
                    coin = symbol.split('/')[0]
                    if (
                        coin not in EXCLUDE_COINS and
                        len(coin) <= 10 and  # Не дуже довгі назви
                        not any(x in coin for x in ['UP', 'DOWN', 'BEAR', 'BULL', '3L', '3S', '2L', '2S', '5L', '5S', 'TEST'])
                    ):
                        all_pairs.add(symbol)
                        count += 1
            
            print(f"✅ {count}")
            
        except Exception as e:
            print(f"❌ {str(e)[:30]}")
    
    result = sorted(list(all_pairs))
    print(f"\n✅ Всього: {len(result)} унікальних пар")
    print("="*70 + "\n")
    
    return result

def fetch_all_tickers_fast(exchange_name, exchange):
    """ШВИДКЕ отримання всіх тікерів"""
    try:
        tickers = exchange.fetch_tickers()
        result = {}
        
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT') and ticker.get('last') and ticker.get('last') > MIN_PRICE:
                result[symbol] = {
                    'price': ticker['last'],
                    'volume': ticker.get('quoteVolume', 0)
                }
        
        return exchange_name, result
    except Exception as e:
        return exchange_name, {}

def is_price_realistic(prices_dict):
    """Перевірка що ціни реалістичні"""
    if len(prices_dict) < 2:
        return False
    
    price_values = [p['price'] for p in prices_dict.values()]
    
    # Всі ціни мають бути > 0
    if any(p <= MIN_PRICE for p in price_values):
        return False
    
    # Різниця між мін і макс не може бути > MAX_REALISTIC_SPREAD
    min_price = min(price_values)
    max_price = max(price_values)
    
    spread_pct = ((max_price - min_price) / min_price) * 100
    
    if spread_pct > MAX_REALISTIC_SPREAD:
        return False
    
    # Перевірка на аномалії - якщо одна ціна відрізняється в рази від інших
    avg_price = sum(price_values) / len(price_values)
    for price in price_values:
        deviation = abs(price - avg_price) / avg_price
        if deviation > 0.1:  # Більше 10% відхилення від середньої - підозріло
            return False
    
    return True

def calculate_spread(symbol, all_prices):
    """Швидкий розрахунок спреду з перевірками"""
    prices = {}
    
    # Збираємо ціни з бірж
    for exchange_name, price_data in all_prices.items():
        if symbol in price_data:
            prices[exchange_name] = price_data[symbol]
    
    if len(prices) < 2:
        return None
    
    # ПЕРЕВІРКА НА РЕАЛІСТИЧНІСТЬ ЦІН
    if not is_price_realistic(prices):
        return None
    
    min_ex = min(prices, key=lambda x: prices[x]['price'])
    max_ex = max(prices, key=lambda x: prices[x]['price'])
    
    min_price = prices[min_ex]['price']
    max_price = prices[max_ex]['price']
    
    if min_price <= MIN_PRICE:
        return None
    
    diff = max_price - min_price
    pct = (diff / min_price) * 100
    
    # Фільтр: спред має бути >= min_spread але <= MAX_REALISTIC_SPREAD
    if pct < min_spread or pct > MAX_REALISTIC_SPREAD:
        return None
    
    # Об'єм
    min_volume_available = min(prices[min_ex]['volume'], prices[max_ex]['volume'])
    
    # Додаткова перевірка об'єму
    if min_volume_available < 1000:  # Мінімум $1000 об'єму на біржі
        return None
    
    max_trade_usd = min(min_volume_available * 0.01, max_volume)
    
    if max_trade_usd < min_volume:
        return None
    
    # Прибуток
    buy_fee = FEES.get(min_ex, 0.1) / 100
    amount_bought = max_trade_usd / min_price
    amount_after_buy_fee = amount_bought * (1 - buy_fee)
    
    sell_fee = FEES.get(max_ex, 0.1) / 100
    revenue = amount_after_buy_fee * max_price
    amount_after_sell_fee = revenue * (1 - sell_fee)
    
    net_profit = amount_after_sell_fee - max_trade_usd
    profit_pct = (net_profit / max_trade_usd * 100) if max_trade_usd > 0 else 0
    
    # Фільтр: прибуток має бути позитивний
    if net_profit <= 0:
        return None
    
    coin = symbol.replace('/USDT', '')
    
    return {
        'coin': coin,
        'symbol': symbol,
        'min_ex': min_ex,
        'max_ex': max_ex,
        'min_price': min_price,
        'max_price': max_price,
        'diff': diff,
        'pct': pct,
        'volume': min_volume_available,
        'max_trade': max_trade_usd,
        'profit': net_profit,
        'profit_pct': profit_pct,
        'buy_fee': buy_fee * 100,
        'sell_fee': sell_fee * 100,
        'amount': amount_bought,
        'all_prices': prices,
        'found_at': datetime.now()
    }

def clean_old_spreads():
    """Видалення застарілих спредів"""
    global spreads_cache
    
    now = datetime.now()
    to_remove = []
    
    for symbol, data in spreads_cache.items():
        age = (now - data['timestamp']).total_seconds()
        if age > SPREAD_LIFETIME:
            to_remove.append(symbol)
    
    for symbol in to_remove:
        del spreads_cache[symbol]

def update_current_spreads():
    """Оновлення списку актуальних спредів"""
    global current_spreads
    
    clean_old_spreads()
    
    current_spreads = sorted(
        [data['spread'] for data in spreads_cache.values()],
        key=lambda x: x['profit'],
        reverse=True
    )

def monitor_once_fast(symbols):
    """ШВИДКЕ сканування"""
    global stats, last_update, is_ready, spreads_cache
    stats['checks'] += 1
    
    start_time = time.time()
    print(f"\n⚡ Сканування #{stats['checks']}: {datetime.now().strftime('%H:%M:%S')}")
    
    active_exchanges = {k: v for k, v in EXCHANGES.items() if k in selected_exchanges}
    
    print(f"📡 Отримання тікерів...", end=" ", flush=True)
    
    all_prices = {}
    with ThreadPoolExecutor(max_workers=len(active_exchanges)) as executor:
        futures = {
            executor.submit(fetch_all_tickers_fast, name, exchange): name
            for name, exchange in active_exchanges.items()
        }
        
        for future in as_completed(futures, timeout=10):
            try:
                exchange_name, prices = future.result(timeout=2)
                if prices:
                    all_prices[exchange_name] = prices
                    print(f"✅{exchange_name[:3]}", end=" ", flush=True)
            except:
                print(f"❌", end=" ", flush=True)
    
    print(f"\n⏳ Аналіз {len(symbols)} пар...", end=" ", flush=True)
    
    new_spreads = 0
    updated_spreads = 0
    filtered_out = 0
    
    for symbol in symbols:
        spread = calculate_spread(symbol, all_prices)
        
        if spread:
            if symbol in spreads_cache:
                updated_spreads += 1
            else:
                new_spreads += 1
            
            spreads_cache[symbol] = {
                'spread': spread,
                'timestamp': datetime.now()
            }
        else:
            filtered_out += 1
    
    update_current_spreads()
    
    last_update = datetime.now()
    is_ready = True
    
    elapsed = time.time() - start_time
    
    print(f"\n✅ {elapsed:.1f}с | Нових: {new_spreads} | Оновлено: {updated_spreads} | Всього: {len(current_spreads)}")
    
    if current_spreads:
        stats['opportunities'] = len(current_spreads)
        print(f"\n🏆 ТОП-5:")
        for i, opp in enumerate(current_spreads[:5], 1):
            age = (datetime.now() - opp['found_at']).total_seconds()
            print(f"{i}. {opp['coin']:10} | ${int(opp['max_trade']):>4} | +${opp['profit']:>6.2f} ({opp['pct']:>5.2f}%) | {int(age)}с")
    else:
        print(f"\n⚠️ Реалістичних спредів не знайдено (фільтр ≤{MAX_REALISTIC_SPREAD}%)")

def monitoring_loop(symbols):
    """Швидкий цикл моніторингу"""
    global monitoring_active
    
    print("⚡ ШВИДКИЙ моніторинг запущено!\n")
    
    while monitoring_active:
        try:
            monitor_once_fast(symbols)
            
            if monitoring_active:
                print(f"\n💤 Наступне сканування через {check_interval}с...\n")
                time.sleep(check_interval)
                
        except Exception as e:
            print(f"❌ Помилка: {e}")
            time.sleep(check_interval)

# ============ ФОРМАТУВАННЯ ============

def format_spreads_list(page=0):
    if not is_ready:
        return "📊 <b>Список спредів</b>\n\n⏳ Завантаження..."
    
    if not current_spreads:
        return (
            "📊 <b>Список спредів</b>\n\n"
            "❌ Актуальних спредів немає\n\n"
            f"Фільтри:\n"
            f"• Спред: {min_spread}% - {MAX_REALISTIC_SPREAD}%\n"
            f"• Об'єм: ${min_volume}-${max_volume}\n"
            f"• Актуальність: {SPREAD_LIFETIME}с\n\n"
            f"💡 Зачекайте наступного сканування ({check_interval}с)"
        )
    
    total_pages = (len(current_spreads) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    now = datetime.now()
    expiring_soon = sum(1 for s in current_spreads if (now - s['found_at']).total_seconds() > SPREAD_LIFETIME - 30)
    
    message = f"📊 <b>Список спредів</b>\n\n"
    message += f"🕐 {last_update.strftime('%H:%M:%S')} | "
    message += f"📈 {len(current_spreads)} актуальних\n"
    message += f"📄 Сторінка: {page + 1}/{total_pages}\n"
    
    if expiring_soon > 0:
        message += f"⚠️ {expiring_soon} застаріють <30с\n"
    
    return message

def format_spread_detail(spread_idx):
    if spread_idx >= len(current_spreads):
        return "❌ Не знайдено"
    
    opp = current_spreads[spread_idx]
    
    age = (datetime.now() - opp['found_at']).total_seconds()
    freshness = "🟢 Свіжий" if age < 60 else "🟡 Середній" if age < 120 else "🔴 Старий"
    
    message = f"💎 <b>{opp['coin']}/USDT</b>\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    message += f"⏰ Знайдено: {int(age)}с тому ({freshness})\n"
    message += f"💰 Об'єм: ${opp['max_trade']:.2f}\n"
    message += f"💵 Прибуток: <b>+${opp['profit']:.2f}</b>\n"
    message += f"📊 Спред: {opp['pct']:.2f}%\n"
    message += f"📈 ROI: {opp['profit_pct']:.2f}%\n\n"
    
    message += f"<b>1️⃣ КУПИТИ на {opp['min_ex']}</b>\n"
    message += f"   💲 ${opp['min_price']:.6f}\n"
    message += f"   🪙 {opp['amount']:.4f} {opp['coin']}\n"
    message += f"   💸 Комісія: {opp['buy_fee']:.2f}%\n\n"
    
    message += f"<b>2️⃣ ПРОДАТИ на {opp['max_ex']}</b>\n"
    message += f"   💲 ${opp['max_price']:.6f}\n"
    message += f"   💰 ${opp['max_trade'] + opp['profit']:.2f}\n"
    message += f"   💸 Комісія: {opp['sell_fee']:.2f}%\n\n"
    
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"<b>📊 Всі ціни:</b>\n\n"
    
    sorted_prices = sorted(opp['all_prices'].items(), key=lambda x: x[1]['price'])
    
    for exchange, data in sorted_prices:
        price = data['price']
        emoji = "🟢" if exchange == opp['min_ex'] else "🔴" if exchange == opp['max_ex'] else "⚪️"
        message += f"{emoji} {exchange}: ${price:.6f}\n"
    
    message += f"\n⚠️ Перевірте актуальність на біржах!"
    
    return message

# ============ ОБРОБНИКИ ============

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖", reply_markup=get_main_keyboard())
    
    if not all_symbols:
        threading.Thread(target=load_and_start_monitoring, daemon=True).start()

def load_and_start_monitoring():
    global all_symbols, stats
    
    all_symbols = get_all_usdt_pairs()
    stats['total_pairs'] = len(all_symbols)
    
    if all_symbols:
        monitoring_loop(all_symbols)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global min_spread, check_interval, selected_exchanges, min_volume, max_volume
    
    text = update.message.text
    
    if text == '📊 Список спредів':
        context.user_data['list_page'] = 0
        await update.message.reply_text(
            format_spreads_list(0),
            parse_mode='HTML',
            reply_markup=get_spreads_list_keyboard(0)
        )
    
    elif text == '📈 Статистика':
        total = len(spreads_cache)
        actual = len(current_spreads)
        
        top = ""
        if current_spreads:
            top = "\n\n<b>ТОП-3:</b>\n"
            for i, opp in enumerate(current_spreads[:3], 1):
                age = int((datetime.now() - opp['found_at']).total_seconds())
                top += f"{i}. {opp['coin']}: ${int(opp['max_trade'])} +{opp['profit']:.0f}$ ({opp['pct']:.2f}%) [{age}с]\n"
        
        msg = (
            f"📈 <b>Статистика</b>\n\n"
            f"Статус: {'✅' if is_ready else '⏳'}\n"
            f"Всього пар: {stats['total_pairs']}\n"
            f"Перевірок: {stats['checks']}\n"
            f"В кеші: {total} | Актуальних: {actual}"
            f"{top}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Спред: {min_spread}% - {MAX_REALISTIC_SPREAD}%\n"
            f"Інтервал: {check_interval}с\n"
            f"Актуальність: {SPREAD_LIFETIME}с\n"
            f"Об'єм: ${min_volume}-${max_volume}\n"
            f"Біржі ({len(selected_exchanges)}): {', '.join(selected_exchanges)}"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    
    elif text == '⚙️ Налаштування':
        msg = (
            f"⚙️ <b>Налаштування</b>\n\n"
            f"📊 Спред: {min_spread}%\n"
            f"⏱️ Інтервал: {check_interval}с\n"
            f"💰 Об'єм: ${min_volume}-${max_volume}\n"
            f"💱 Біржі: {len(selected_exchanges)}/8"
        )
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_settings_keyboard())
    
    elif text == '◀️ Назад':
        await update.message.reply_text("⚙️", reply_markup=get_main_keyboard())
    
    elif text == '📊 Мін. спред':
        await update.message.reply_text(f"Поточний: {min_spread}%", reply_markup=get_spread_keyboard())
    
    elif text in ['0.05%', '0.1%', '0.15%', '0.2%', '0.3%', '0.5%']:
        min_spread = float(text.replace('%', ''))
        await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
    
    elif text == '⏱️ Інтервал':
        await update.message.reply_text(f"Поточний: {check_interval}с", reply_markup=get_interval_keyboard())
    
    elif text in ['10с', '15с', '30с', '60с', '120с', '180с']:
        check_interval = int(text.replace('с', ''))
        await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
    
    elif text == '💰 Об\'єм':
        await update.message.reply_text("Оберіть:", reply_markup=get_volume_keyboard())
    
    elif text in ['30-100', '30-200', '50-500', '100-1000', '50-1000']:
        parts = text.split('-')
        min_volume, max_volume = int(parts[0]), int(parts[1])
        await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
    
    elif text == 'Своє значення':
        await update.message.reply_text(f"Введіть мінімум (від ${ABSOLUTE_MIN_VOLUME}):")
        context.user_data['awaiting'] = 'volume_min'
    
    elif context.user_data.get('awaiting') == 'volume_min':
        try:
            new_min = float(text.replace('$', '').replace(',', ''))
            if new_min >= ABSOLUTE_MIN_VOLUME:
                context.user_data['temp_min_volume'] = new_min
                context.user_data['awaiting'] = 'volume_max'
                await update.message.reply_text(f"Мін: ${new_min}\n\nВведіть максимум:")
            else:
                await update.message.reply_text(f"❌ Мінімум ${ABSOLUTE_MIN_VOLUME}!")
        except:
            await update.message.reply_text("❌ Введіть число")
    
    elif context.user_data.get('awaiting') == 'volume_max':
        try:
            new_max = float(text.replace('$', '').replace(',', ''))
            new_min = context.user_data.get('temp_min_volume', min_volume)
            
            if new_max > new_min:
                min_volume = new_min
                max_volume = new_max
                context.user_data['awaiting'] = None
                await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
        except:
            await update.message.reply_text("❌ Введіть число")
    
    elif text == '💱 Біржі':
        await update.message.reply_text(f"Активно: {len(selected_exchanges)}/8", reply_markup=get_exchanges_keyboard())
    
    elif text.startswith('✅ ') or text.startswith('❌ '):
        exchange = text[2:].split('(')[0].strip()
        
        if exchange in EXCHANGES:
            if exchange in selected_exchanges:
                if len(selected_exchanges) > 2:
                    selected_exchanges.remove(exchange)
                    await update.message.reply_text("❌", reply_markup=get_exchanges_keyboard())
            else:
                selected_exchanges.append(exchange)
                await update.message.reply_text("✅", reply_markup=get_exchanges_keyboard())

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith('page_'):
        page = int(data.split('_')[1])
        context.user_data['list_page'] = page
        await query.edit_message_text(format_spreads_list(page), parse_mode='HTML', reply_markup=get_spreads_list_keyboard(page))
    
    elif data.startswith('detail_'):
        spread_idx = int(data.split('_')[1])
        await query.edit_message_text(format_spread_detail(spread_idx), parse_mode='HTML', reply_markup=get_detail_keyboard())
    
    elif data == 'back_to_list':
        page = context.user_data.get('list_page', 0)
        await query.edit_message_text(format_spreads_list(page), parse_mode='HTML', reply_markup=get_spreads_list_keyboard(page))
    
    elif data == 'refresh_spreads':
        update_current_spreads()
        page = context.user_data.get('list_page', 0)
        await query.edit_message_text(format_spreads_list(page), parse_mode='HTML', reply_markup=get_spreads_list_keyboard(page))
    
    elif data == 'close_spreads':
        await query.delete_message()
    
    elif data == 'current_page':
        await query.answer(f"Всього актуальних: {len(current_spreads)}", show_alert=True)

def main():
    print("="*70)
    print("⚡ ШВИДКИЙ АРБІТРАЖНИЙ БОТ")
    print("="*70)
    print(f"⚡ Інтервал: {check_interval}с")
    print(f"⚡ Актуальність: {SPREAD_LIFETIME}с")
    print(f"⚡ Макс спред: {MAX_REALISTIC_SPREAD}% (фільтр аномалій)\n")
    
    threading.Thread(target=load_and_start_monitoring, daemon=True).start()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Бот запущено!\n")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
