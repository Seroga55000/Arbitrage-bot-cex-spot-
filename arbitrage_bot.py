import ccxt
import time
import requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import threading
import asyncio

# ============ НАЛАШТУВАННЯ ============
TELEGRAM_BOT_TOKEN = 'your bot token'
TELEGRAM_CHAT_ID = 'your chat id'

# Глобальні змінні
monitoring_active = True
min_spread = 0.15
check_interval = 60
selected_exchanges = ['Binance', 'Bybit', 'OKX', 'KuCoin', 'MEXC', 'Bitget', 'Digifinex']
min_volume = 10000
max_volume = 100000
all_symbols = []
current_spreads = []
last_update = None

EXCHANGES = {
    'Binance': ccxt.binance(),
    'Bybit': ccxt.bybit(),
    'OKX': ccxt.okx(),
    'KuCoin': ccxt.kucoin(),
    'Gate.io': ccxt.gateio(),
    'MEXC': ccxt.mexc(),
    'Bitget': ccxt.bitget(),
    'Digifinex': ccxt.digifinex(),
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

EXCLUDE_COINS = ['USDT', 'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDD', 'USDP']
stats = {'checks': 0, 'opportunities': 0, 'total_pairs': 0}
ITEMS_PER_PAGE = 7

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
        ['0.1%', '0.15%', '0.2%'],
        ['0.3%', '0.5%', '1%'],
        ['◀️ Назад']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_interval_keyboard():
    keyboard = [
        ['30с', '60с', '90с'],
        ['120с', '180с', '300с'],
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
        
        if spread['max_trade'] >= 1000:
            volume_str = f"{int(spread['max_trade']/1000)}k"
        else:
            volume_str = f"{int(spread['max_trade'])}"
        
        button_text = f"{spread['coin']}: {volume_str} +{spread['profit']:.0f}$ ({spread['pct']:.1f}%)"
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

# ============ МОНІТОРИНГ ============

def get_all_usdt_pairs_from_exchanges():
    all_pairs = {}
    
    print("\n" + "="*70)
    print("🔄 Завантаження USDT пар...")
    print("="*70)
    
    active_exchanges = {k: v for k, v in EXCHANGES.items() if k in selected_exchanges}
    
    for name, exchange in active_exchanges.items():
        try:
            print(f"📡 {name}...", end=" ", flush=True)
            markets = exchange.load_markets()
            
            usdt_pairs = []
            for symbol in markets.keys():
                # СПРОЩЕНА ФІЛЬТРАЦІЯ - тільки базові перевірки
                if (
                    symbol.endswith('/USDT') and  # Закінчується на /USDT
                    ':' not in symbol and  # Не ф'ючерс
                    not any(x in symbol for x in ['UP/', 'DOWN/', 'BEAR/', 'BULL/', '3L/', '3S/', 'BEAR', 'BULL']) and  # Не левередж
                    not any(excl == symbol.split('/')[0] for excl in EXCLUDE_COINS)  # Не стейблкоїн
                ):
                    usdt_pairs.append(symbol)
                    if symbol not in all_pairs:
                        all_pairs[symbol] = []
                    all_pairs[symbol].append(name)
            
            print(f"✅ {len(usdt_pairs)}")
            
        except Exception as e:
            print(f"❌ {str(e)[:30]}")
    
    valid_pairs = {s: e for s, e in all_pairs.items() if len(e) >= 2}
    print(f"\n✅ Знайдено: {len(valid_pairs)} пар на ≥2 біржах")
    print("="*70 + "\n")
    
    return sorted(list(valid_pairs.keys()))

def calculate_spread_with_profit(symbol, prices):
    min_ex = min(prices, key=lambda x: prices[x]['price'])
    max_ex = max(prices, key=lambda x: prices[x]['price'])
    
    min_price = prices[min_ex]['price']
    max_price = prices[max_ex]['price']
    
    diff = max_price - min_price
    pct = (diff / min_price) * 100
    
    min_volume_available = min(prices[min_ex]['volume'], prices[max_ex]['volume'])
    max_trade_usd = min(min_volume_available * 0.01, max_volume)
    
    if max_trade_usd < min_volume:
        max_trade_usd = 0
    
    buy_fee = FEES.get(min_ex, 0.1) / 100
    amount_bought = max_trade_usd / min_price if min_price > 0 else 0
    amount_after_buy_fee = amount_bought * (1 - buy_fee)
    
    sell_fee = FEES.get(max_ex, 0.1) / 100
    revenue = amount_after_buy_fee * max_price
    amount_after_sell_fee = revenue * (1 - sell_fee)
    
    net_profit = amount_after_sell_fee - max_trade_usd
    profit_pct = (net_profit / max_trade_usd * 100) if max_trade_usd > 0 else 0
    
    return {
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
        'amount': amount_bought
    }

def monitor_once(symbols):
    global stats, current_spreads, last_update
    stats['checks'] += 1
    
    print(f"\n🔍 #{stats['checks']}: {datetime.now().strftime('%H:%M:%S')}")
    
    active_exchanges = {k: v for k, v in EXCHANGES.items() if k in selected_exchanges}
    results = []
    checked = 0
    
    for symbol in symbols:
        prices = {}
        
        for name, exchange in active_exchanges.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                prices[name] = {
                    'price': ticker['last'],
                    'volume': ticker.get('quoteVolume', 0)
                }
            except:
                continue
        
        if len(prices) < 2:
            continue
        
        checked += 1
        spread_data = calculate_spread_with_profit(symbol, prices)
        
        if spread_data['max_trade'] < min_volume or spread_data['pct'] < min_spread:
            continue
        
        coin = symbol.replace('/USDT', '')
        
        results.append({
            'coin': coin,
            'symbol': symbol,
            **spread_data,
            'all_prices': prices
        })
    
    results.sort(key=lambda x: x['profit'], reverse=True)
    current_spreads = results
    last_update = datetime.now()
    
    print(f"✅ Перевірено: {checked} | Знайдено: {len(results)}")
    
    if results:
        stats['opportunities'] += len(results)
        for i, opp in enumerate(results[:3], 1):
            vol_str = f"{int(opp['max_trade']/1000)}k" if opp['max_trade'] >= 1000 else str(int(opp['max_trade']))
            print(f"{i}. {opp['coin']:8} {vol_str:>6} +{opp['profit']:>6.0f}$ ({opp['pct']:>5.2f}%)")

def monitoring_loop(symbols):
    global monitoring_active
    
    print("✅ Моніторинг запущено!\n")
    
    while monitoring_active:
        try:
            monitor_once(symbols)
            time.sleep(check_interval)
        except Exception as e:
            print(f"❌ Помилка: {e}")
            time.sleep(check_interval)

# ============ ФОРМАТУВАННЯ ============

def format_spreads_list(page=0):
    if not current_spreads:
        return "📊 <b>Список спредів</b>\n\n⏳ Зачекайте, йде сканування..."
    
    total_pages = (len(current_spreads) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    message = f"📊 <b>Список спредів</b>\n\n"
    message += f"🕐 {last_update.strftime('%H:%M:%S')} | "
    message += f"📈 {len(current_spreads)} | "
    message += f"📄 {page + 1}/{total_pages}\n"
    
    return message

def format_spread_detail(spread_idx):
    if spread_idx >= len(current_spreads):
        return "❌ Спред не знайдено"
    
    opp = current_spreads[spread_idx]
    
    message = f"💎 <b>{opp['coin']}/USDT</b>\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    message += f"💰 Об'єм: ${opp['max_trade']:,.0f}\n"
    message += f"💵 Прибуток: <b>+${opp['profit']:,.2f}</b>\n"
    message += f"📊 Спред: {opp['pct']:.2f}%\n"
    message += f"📈 ROI: {opp['profit_pct']:.2f}%\n\n"
    
    message += f"<b>1️⃣ КУПИТИ на {opp['min_ex']}</b>\n"
    message += f"   💲 ${opp['min_price']:,.4f}\n"
    message += f"   🪙 {opp['amount']:,.4f} {opp['coin']}\n"
    message += f"   💸 Комісія: {opp['buy_fee']:.2f}%\n\n"
    
    message += f"<b>2️⃣ ПРОДАТИ на {opp['max_ex']}</b>\n"
    message += f"   💲 ${opp['max_price']:,.4f}\n"
    message += f"   💰 ${opp['max_trade'] + opp['profit']:,.2f}\n"
    message += f"   💸 Комісія: {opp['sell_fee']:.2f}%\n\n"
    
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"<b>📊 Всі ціни:</b>\n\n"
    
    sorted_prices = sorted(opp['all_prices'].items(), key=lambda x: x[1]['price'])
    
    for exchange, data in sorted_prices:
        price = data['price']
        
        if exchange == opp['min_ex']:
            emoji = "🟢"
        elif exchange == opp['max_ex']:
            emoji = "🔴"
        else:
            emoji = "⚪️"
        
        message += f"{emoji} {exchange}: ${price:,.4f}\n"
    
    return message

# ============ ОБРОБНИКИ ============

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖", reply_markup=get_main_keyboard())
    
    if not all_symbols:
        threading.Thread(target=load_and_start_monitoring, daemon=True).start()

def load_and_start_monitoring():
    global all_symbols, stats
    
    all_symbols = get_all_usdt_pairs_from_exchanges()
    stats['total_pairs'] = len(all_symbols)
    
    if all_symbols:
        monitoring_loop(all_symbols)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global min_spread, check_interval, selected_exchanges, min_volume, max_volume
    
    text = update.message.text
    
    # ГОЛОВНЕ МЕНЮ
    if text == '📊 Список спредів':
        context.user_data['list_page'] = 0
        message_text = format_spreads_list(0)
        await update.message.reply_text(
            message_text,
            parse_mode='HTML',
            reply_markup=get_spreads_list_keyboard(0)
        )
    
    elif text == '📈 Статистика':
        top = ""
        if current_spreads:
            top = "\n\n<b>ТОП-3:</b>\n"
            for i, opp in enumerate(current_spreads[:3], 1):
                vol = f"{int(opp['max_trade']/1000)}k" if opp['max_trade'] >= 1000 else str(int(opp['max_trade']))
                top += f"{i}. {opp['coin']}: {vol} +{opp['profit']:.0f}$ ({opp['pct']:.1f}%)\n"
        
        msg = (
            f"📈 <b>Статистика</b>\n\n"
            f"Пар: {stats['total_pairs']}\n"
            f"Перевірок: {stats['checks']}\n"
            f"Поточних: {len(current_spreads)}"
            f"{top}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Спред: {min_spread}% | Інтервал: {check_interval}с\n"
            f"Об'єм: ${min_volume/1000:.0f}k-${max_volume/1000:.0f}k\n"
            f"Біржі: {len(selected_exchanges)}/8"
        )
        await update.message.reply_text(msg, parse_mode='HTML')
    
    elif text == '⚙️ Налаштування':
        msg = (
            f"⚙️ <b>Налаштування</b>\n\n"
            f"📊 Спред: {min_spread}%\n"
            f"⏱️ Інтервал: {check_interval}с\n"
            f"💰 Об'єм: ${min_volume/1000:.0f}k-${max_volume/1000:.0f}k\n"
            f"💱 Біржі: {len(selected_exchanges)}/8"
        )
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_settings_keyboard())
    
    elif text == '◀️ Назад':
        await update.message.reply_text("⚙️", reply_markup=get_main_keyboard())
    
    # НАЛАШТУВАННЯ СПРЕДУ
    elif text == '📊 Мін. спред':
        await update.message.reply_text(
            f"Поточний: {min_spread}%",
            reply_markup=get_spread_keyboard()
        )
    
    elif text in ['0.1%', '0.15%', '0.2%', '0.3%', '0.5%', '1%']:
        min_spread = float(text.replace('%', ''))
        await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
    
    # НАЛАШТУВАННЯ ІНТЕРВАЛУ
    elif text == '⏱️ Інтервал':
        await update.message.reply_text(
            f"Поточний: {check_interval}с",
            reply_markup=get_interval_keyboard()
        )
    
    elif text in ['30с', '60с', '90с', '120с', '180с', '300с']:
        check_interval = int(text.replace('с', ''))
        await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
    
    # НАЛАШТУВАННЯ ОБ'ЄМУ
    elif text == '💰 Об\'єм':
        await update.message.reply_text(
            f"Поточний: ${min_volume/1000:.0f}k - ${max_volume/1000:.0f}k\n\n"
            f"Введіть мінімум (наприклад: 5k або 5000):",
            parse_mode='HTML'
        )
        context.user_data['awaiting'] = 'volume_min'
    
    elif context.user_data.get('awaiting') == 'volume_min':
        try:
            new_min = float(text.replace(',', '').replace('$', '').replace('k', '000').replace('K', '000'))
            if new_min > 0:
                context.user_data['temp_min_volume'] = new_min
                context.user_data['awaiting'] = 'volume_max'
                await update.message.reply_text(f"Мін: ${new_min/1000:.0f}k\n\nВведіть максимум:")
        except:
            await update.message.reply_text("❌ Введіть число")
    
    elif context.user_data.get('awaiting') == 'volume_max':
        try:
            new_max = float(text.replace(',', '').replace('$', '').replace('k', '000').replace('K', '000'))
            new_min = context.user_data.get('temp_min_volume', min_volume)
            
            if new_max > new_min:
                min_volume = new_min
                max_volume = new_max
                context.user_data['awaiting'] = None
                await update.message.reply_text("✅", reply_markup=get_settings_keyboard())
        except:
            await update.message.reply_text("❌ Введіть число")
    
    # НАЛАШТУВАННЯ БІРЖ
    elif text == '💱 Біржі':
        await update.message.reply_text(
            f"Активно: {len(selected_exchanges)}/8",
            reply_markup=get_exchanges_keyboard()
        )
    
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
        
        await query.edit_message_text(
            format_spreads_list(page),
            parse_mode='HTML',
            reply_markup=get_spreads_list_keyboard(page)
        )
    
    elif data.startswith('detail_'):
        spread_idx = int(data.split('_')[1])
        
        await query.edit_message_text(
            format_spread_detail(spread_idx),
            parse_mode='HTML',
            reply_markup=get_detail_keyboard()
        )
    
    elif data == 'back_to_list':
        page = context.user_data.get('list_page', 0)
        
        await query.edit_message_text(
            format_spreads_list(page),
            parse_mode='HTML',
            reply_markup=get_spreads_list_keyboard(page)
        )
    
    elif data == 'refresh_spreads':
        await query.edit_message_text("🔄")
        
        if all_symbols:
            await asyncio.to_thread(monitor_once, all_symbols)
        
        page = context.user_data.get('list_page', 0)
        
        await query.edit_message_text(
            format_spreads_list(page),
            parse_mode='HTML',
            reply_markup=get_spreads_list_keyboard(page)
        )
    
    elif data == 'close_spreads':
        await query.delete_message()
    
    elif data == 'current_page':
        await query.answer(f"Всього: {len(current_spreads)}", show_alert=True)

# ============ ЗАПУСК ============

def main():
    print("="*70)
    print("🤖 АРБІТРАЖНИЙ БОТ")
    print("="*70)
    
    threading.Thread(target=load_and_start_monitoring, daemon=True).start()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("\n✅ Бот запущено!\n")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()