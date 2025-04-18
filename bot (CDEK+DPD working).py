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
import chardet

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Токен бота
TOKEN = "7377796192:AAEuQOJ3KedwCz_C2WwrR7QMU-PHD74jFQI"

# Данные авторизации СДЭК
CDEK_CLIENT_ID = "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK"
CDEK_CLIENT_SECRET = "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"
CDEK_AUTH_URL = "https://api.cdek.ru/v2/oauth/token"
CDEK_TARIFFLIST_URL = "https://api.cdek.ru/v2/calculator/tarifflist"
CDEK_CITY_URL = "https://api.cdek.ru/v2/location/cities"

# Данные авторизации DPD (два личных кабинета)
DPD_ACCOUNTS = [
    {"clientNumber": "1021006899", "clientKey": "bdb325b2-c4dc-4574-9fcb-02712cf4e16c"},
    {"clientNumber": "1021006900", "clientKey": "3eaafd2d-543a-4ab9-9fd1-6a33286e10e8"}
]
DPD_WSDL_URL = "https://ws.dpd.ru/services/calculator2?wsdl"

# Загрузка базы городов для DPD с автоопределением кодировки
file_path = os.path.expanduser('~/Desktop/GeographyDPD_20250211.csv')
with open(file_path, 'rb') as f:
    detected = chardet.detect(f.read(10000))
correct_encoding = detected['encoding']
cities_df = pd.read_csv(file_path, sep=';', encoding=correct_encoding)
city_names = cities_df.iloc[:, 3].dropna().unique().tolist()

def find_city_code(city_name):
    match = cities_df[cities_df.iloc[:, 3].str.contains(f'^{city_name}$', case=False, na=False)]
    if not match.empty:
        return match.iloc[0, 0]
    suggestions = get_close_matches(city_name, city_names, n=3, cutoff=0.6)
    return None

# Получение токена СДЭК
def get_cdek_token():
    response = requests.post(CDEK_AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": CDEK_CLIENT_ID,
        "client_secret": CDEK_CLIENT_SECRET
    })
    return response.json().get("access_token") if response.status_code == 200 else None

# Получение кода города СДЭК
def get_cdek_city_code(city_name, token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(CDEK_CITY_URL, headers=headers, params={"city": city_name})
    return response.json()[0].get("code") if response.status_code == 200 and response.json() else None

# Расчет доставки через СДЭК
def calculate_cdek_delivery(city_from, city_to, length, width, height, weight):
    token = get_cdek_token()
    if not token:
        return "Ошибка авторизации в СДЭК."

    city_from_code = get_cdek_city_code(city_from, token)
    city_to_code = get_cdek_city_code(city_to, token)
    if not city_from_code or not city_to_code:
        return "Ошибка: не удалось определить коды городов."

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "from_location": {"code": city_from_code},
        "to_location": {"code": city_to_code},
        "packages": [{
            "weight": weight * 1000,
            "length": length,
            "width": width,
            "height": height
        }]
    }

    response = requests.post(CDEK_TARIFFLIST_URL, headers=headers, json=payload, verify=certifi.where())
    if response.status_code != 200:
        return f"Ошибка при расчете: {response.status_code} - {response.text}"

    data = response.json()
    if "tariff_codes" not in data:
        return "Не удалось получить тарифы."

    categories = {
        "дверь-дверь": None,
        "дверь-склад": None,
        "склад-дверь": None,
        "склад-склад": None
    }

    for tariff in data["tariff_codes"]:
        tariff_name = tariff.get("tariff_name", "").lower()
        delivery_sum = tariff.get("delivery_sum")
        min_days = tariff.get("period_min", "?")
        max_days = tariff.get("period_max", "?")
        delivery_term = f"{min_days} - {max_days} дней"

        for category in categories:
            if category in tariff_name and (categories[category] is None or delivery_sum < categories[category]["price"]):
                categories[category] = {"price": delivery_sum, "term": delivery_term, "name": tariff.get("tariff_name")}

    message = ""
    for cat, val in categories.items():
        if val:
            message += f"📦 {cat}: {val['price']} руб., срок {val['term']}\n"
        else:
            message += f"📦 {cat}: тариф недоступен\n"

    return message.strip()

# Расчет доставки через DPD
async def calculate_dpd_delivery(text):
    try:
        pickup_city, delivery_city, weight, length, width, height, pickup_type, delivery_type, declared_value = [x.strip() for x in text.split(",")]
        pickup_code = find_city_code(pickup_city)
        delivery_code = find_city_code(delivery_city)
        if pickup_code is None or delivery_code is None:
            return "Ошибка: не удалось найти один из городов в базе DPD."

        volume = (int(length) * int(width) * int(height)) / 1_000_000
        self_pickup = False if pickup_type.lower() == 'курьер' else True
        self_delivery = False if delivery_type.lower() == 'курьер' else True

        client = Client(DPD_WSDL_URL)
        results = []

        for account in DPD_ACCOUNTS:
            request_data = {
                'auth': {
                    'clientNumber': account['clientNumber'],
                    'clientKey': account['clientKey']
                },
                'pickup': {'cityId': pickup_code},
                'delivery': {'cityId': delivery_code},
                'selfPickup': self_pickup,
                'selfDelivery': self_delivery,
                'weight': float(weight),
                'volume': volume,
                'declaredValue': float(declared_value)
            }

            try:
                response = client.service.getServiceCost2(request=request_data)
                filtered = [s for s in response if 'MAX domestic' not in s['serviceName']]
                if filtered:
                    best = min(filtered, key=lambda x: x['cost'])
                    results.append(f"ЛК {account['clientNumber']}: {best['cost']} руб., срок {best['days']} дней")
                else:
                    results.append(f"ЛК {account['clientNumber']}: нет подходящих тарифов")
            except Exception as e:
                results.append(f"ЛК {account['clientNumber']}: ошибка расчета")

        return "Результат расчета:\n" + "\n".join(results)
    except Exception as e:
        return "Ошибка обработки данных для DPD. Убедитесь, что вы ввели все параметры правильно."

# Команда /start
async def start(update: Update, context: CallbackContext) -> None:
    keyboard = [["СДЭК"], ["DPD"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    context.user_data.clear()
    await update.message.reply_text("Выберите службу доставки:", reply_markup=reply_markup)

# Выбор службы доставки
async def choose_service(update: Update, context: CallbackContext) -> None:
    service = update.message.text
    context.user_data.clear()
    context.user_data["service"] = service
    if service == "СДЭК":
        await update.message.reply_text("Введите данные в формате: Город-отправитель, Город-получатель, Длина, Ширина, Высота, Вес")
    elif service == "DPD":
        await update.message.reply_text("Введите данные в формате: Город_отправки, Город_доставки, Вес_в_кг, Длина_см, Ширина_см, Высота_см, Забор(курьер/пункт), Доставка(курьер/пункт), Объявленная_стоимость_руб.\nПример: Моск, Спб, 5, 30, 20, 15, курьер, пункт, 5000")

# Обработка ввода пользователя
async def handle_input(update: Update, context: CallbackContext) -> None:
    if "service" not in context.user_data:
        await update.message.reply_text("Пожалуйста, сначала выберите службу доставки командой /start")
        return

    service = context.user_data["service"]
    text = update.message.text.strip()

    if service == "СДЭК":
        try:
            city_from, city_to, length, width, height, weight = [x.strip() for x in text.split(",")]
            result = calculate_cdek_delivery(city_from, city_to, int(length), int(width), int(height), float(weight))
            await update.message.reply_text("Результат расчета:\n" + result)
        except Exception as e:
            await update.message.reply_text("Ошибка обработки данных. Убедитесь, что вы ввели все параметры правильно.")
    elif service == "DPD":
        result = await calculate_dpd_delivery(text)
        await update.message.reply_text(result)

# Запуск бота
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.Regex("^(СДЭК|DPD)$"), choose_service))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

if __name__ == "__main__":
    application.run_polling()
