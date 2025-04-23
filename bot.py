import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import requests
import json
import certifi
from zeep import Client
import pandas as pd
from difflib import get_close_matches
import os
from dotenv import load_dotenv
load_dotenv()
import chardet
import re
import asyncio
from flask import Flask, request
import threading

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Токен бота из .env
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная TELEGRAM_BOT_TOKEN не загружена из .env файла!")

# Авторизация СДЭК
CDEK_CLIENT_ID = "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK"
CDEK_CLIENT_SECRET = "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"
CDEK_AUTH_URL = "https://api.cdek.ru/v2/oauth/token"
CDEK_TARIFFLIST_URL = "https://api.cdek.ru/v2/calculator/tarifflist"
CDEK_CITY_URL = "https://api.cdek.ru/v2/location/cities"

# Авторизация DPD
DPD_ACCOUNTS = [
    {"clientNumber": "1021006899", "clientKey": "bdb325b2-c4dc-4574-9fcb-02712cf4e16c"},
    {"clientNumber": "1021006900", "clientKey": "3eaafd2d-543a-4ab9-9fd1-6a33286e10e8"}
]
DPD_WSDL_URL = "https://ws.dpd.ru/services/calculator2?wsdl"

# Загрузка базы городов для DPD
cities_df = pd.read_csv("GeographyDPD_20250211.csv", sep=";", encoding=chardet.detect(open("GeographyDPD_20250211.csv", "rb").read())['encoding'])

# Шаблоны габаритов и веса
PRESETS = {
    "2-секции": (95, 76, 20, 17),
    "3-секции": (95, 76, 20, 20),
    "4-секции": (96, 76, 34, 30),
    "фикс-мт2": (187, 79, 21, 37),
    "фикс-1а": (187, 79, 24, 40),
    "фикс-0а": (187, 79, 24, 40),
    "ммкм-1": (188, 73, 47, 76),
    "ммкк-3ко176": (157, 61, 64, 54),
    "ммкк-3ко172": (180, 65, 72, 75),
    "ммкм-2": [(171, 64.5, 51, 30), (127, 22.5, 76, 60)],
    "км-3007": (212, 88, 84, 80),
    "ммкм-2 ко-152": (200, 85, 65, 130),
    "ммкм-2 ко-153": (207, 85, 78, 140),
    "ммкм-2 ко-154": (199, 92, 74, 171),
    "ммкм-2 ко-155": (213, 82, 69, 142),
    "ммкм-2 ко-156": (208, 80, 70, 125),
    "ммкм-2 ко-157": (213, 92, 87, 200),
    "ммкм-2 ко-158": (213, 93, 83, 189),
    "ммкм-2 ко-159": (213, 93, 86, 162),
    "ммкм-2 ко-160": (206, 90, 67, 123),
    "ммкк-3 ко-177": (150, 61, 61, 68)
}

ALT_PRESETS = {}
for key in PRESETS:
    base = key.lower().replace('-', ' ').replace('–', ' ').replace('ко', ' ко')
    parts = base.split()
    variants = set()
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            sub = parts[i:j]
            variants.add(''.join(sub))
            variants.add('-'.join(sub))
            variants.add(' '.join(sub))
    for variant in variants:
        ALT_PRESETS[variant.strip()] = key

# Вспомогательные функции
def find_city_code(city_name):
    match = cities_df[cities_df.iloc[:, 3].str.contains(f'^{city_name}$', case=False, na=False)]
    return match.iloc[0, 0] if not match.empty else None

def get_cdek_token():
    response = requests.post(CDEK_AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": CDEK_CLIENT_ID,
        "client_secret": CDEK_CLIENT_SECRET
    })
    return response.json().get("access_token") if response.status_code == 200 else None

def get_cdek_city_code(city_name, token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(CDEK_CITY_URL, headers=headers, params={"city": city_name})
    return response.json()[0].get("code") if response.status_code == 200 and response.json() else None

def extract_preset_key(text):
    clean = text.lower().replace('-', ' ').strip()
    return ALT_PRESETS.get(clean)

async def calculate_dpd_delivery(text):
    try:
        parts = re.split(r'[\s,;]+', text.strip().lower())
        name = ' '.join(parts[2:-3]).lower().replace('-', ' ').strip()
        key = extract_preset_key(name)
        dims = PRESETS.get(key)
        pickup_city, delivery_city = parts[0], parts[1]
        pickup_type, delivery_type, declared_value = parts[-3:]
        pickup_code = find_city_code(pickup_city)
        delivery_code = find_city_code(delivery_city)
        if pickup_code is None or delivery_code is None:
            return "Ошибка: не найден город."
        self_pickup = pickup_type != 'курьер'
        self_delivery = delivery_type != 'курьер'
        client = Client(DPD_WSDL_URL)
        results = []
        for account in DPD_ACCOUNTS:
            total_cost, max_days = 0, 0
            dims_list = dims if isinstance(dims, list) else [dims]
            for length, width, height, weight in dims_list:
                volume = (length * width * height) / 1_000_000
                req = {
                    'auth': account,
                    'pickup': {'cityId': pickup_code},
                    'delivery': {'cityId': delivery_code},
                    'selfPickup': self_pickup,
                    'selfDelivery': self_delivery,
                    'weight': weight,
                    'volume': volume,
                    'declaredValue': float(declared_value)
                }
                try:
                    resp = client.service.getServiceCost2(request=req)
                    filtered = [s for s in resp if 'Economy' in s['serviceName'] or 'Classic' in s['serviceName']]
                    if filtered:
                        best = min(filtered, key=lambda x: x['cost'])
                        total_cost += best['cost']
                        max_days = max(max_days, best['days'])
                except:
                    return f"ЛК {account['clientNumber']}: ошибка при расчёте."
            results.append(f"ЛК {account['clientNumber']}: {round(total_cost, 2)} руб., срок {max_days} дней")
        return "Результат расчёта:\n" + "\n".join(results)
    except Exception as e:
        return f"Ошибка: {e}"

def calculate_cdek_delivery(city_from, city_to, dims):
    token = get_cdek_token()
    if not token:
        return "Ошибка авторизации в СДЭК."
    from_code = get_cdek_city_code(city_from, token)
    to_code = get_cdek_city_code(city_to, token)
    if not from_code or not to_code:
        return "Ошибка определения городов."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    packages = []
    dims_list = dims if isinstance(dims, list) else [dims]
    for l, w, h, weight in dims_list:
        packages.append({"weight": int(weight * 1000), "length": int(l), "width": int(w), "height": int(h)})
    payload = {"from_location": {"code": from_code}, "to_location": {"code": to_code}, "packages": packages}
    response = requests.post(CDEK_TARIFFLIST_URL, headers=headers, json=payload, verify=certifi.where())
    if response.status_code != 200:
        return f"Ошибка СДЭК: {response.status_code}"
    data = response.json()
    categories = {"дверь-дверь": None, "дверь-склад": None, "склад-дверь": None, "склад-склад": None}
    for tariff in data.get("tariff_codes", []):
        name = tariff.get("tariff_name", "").lower()
        delivery_sum = tariff.get("delivery_sum")
        term = f"{tariff.get('period_min', '?')}–{tariff.get('period_max', '?')} дн."
        for cat in categories:
            if cat in name and (categories[cat] is None or delivery_sum < categories[cat]["price"]):
                categories[cat] = {"price": delivery_sum, "term": term}
    return "\n".join([f"📦 {k}: {v['price']} руб., срок {v['term']}" if v else f"📦 {k}: тариф недоступен" for k, v in categories.items()])

# Flask
app = Flask(__name__)

@app.route('/', methods=['GET', 'HEAD'])
def index():
    return 'OK'

@app.route('/', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.get_running_loop().create_task(application.process_update(update))
    return 'OK'

# Обработчики
async def start(update: Update, context: CallbackContext):
    keyboard = [["СДЭК"], ["DPD"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    context.user_data.clear()
    await update.message.reply_text("Выберите службу доставки:", reply_markup=reply_markup)

async def choose_service(update: Update, context: CallbackContext):
    context.user_data.clear()
    context.user_data["service"] = update.message.text
    if update.message.text == "СДЭК":
        await update.message.reply_text("Введите: Город-отправитель, Город-получатель, Шаблон")
    else:
        await update.message.reply_text("Формат: Город_отправки, Город_доставки, Шаблон, Забор, Доставка, Страховка")

async def handle_input(update: Update, context: CallbackContext):
    if "service" not in context.user_data:
        await update.message.reply_text("Сначала выберите службу доставки через /start")
        return
    text = update.message.text.strip()
    parts = re.split(r'[\s,;]+', text.lower())
    service = context.user_data["service"]
    if service == "СДЭК":
        try:
            name = ' '.join(parts[2:]).lower().replace('-', ' ').strip()
            key = extract_preset_key(name)
            dims = PRESETS.get(key)
            result = calculate_cdek_delivery(parts[0], parts[1], dims)
            await update.message.reply_text("Результат расчёта:\n" + result)
        except Exception:
            await update.message.reply_text("Ошибка! Проверьте формат.")
    elif service == "DPD":
        result = await calculate_dpd_delivery(text)
        await update.message.reply_text(result)

# Telegram приложение
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.Regex("^(СДЭК|DPD)$"), choose_service))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

# Запуск
if __name__ == "__main__":
    async def main():
        await application.initialize()
        await application.start()
        await application.bot.set_webhook("https://telegram-delivery-bot.onrender.com")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

    threading.Thread(target=lambda: asyncio.run(main())).start()
