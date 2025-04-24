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
from flask import Flask, request

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

def find_city_code(city_name):
    match = cities_df[cities_df.iloc[:, 3].str.contains(f'^{city_name}$', case=False, na=False)]
    return match.iloc[0, 0] if not match.empty else None

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

# Flask-приложение для Webhook
app = Flask(__name__)

@app.route('/', methods=['GET', 'HEAD'])
def index():
    return 'OK'

@app.route('/', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.create_task(application.process_update(update))
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
        await update.message.reply_text("Введите: Город-отправитель, Город-получатель, Шаблон или Д/Ш/В/Вес")
    else:
        await update.message.reply_text("Формат: Город_отправки, Город_доставки, Шаблон или Д Ш В Вес, Забор, Доставка, Страховка")

# Telegram приложение
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.Regex("^(СДЭК|DPD)$"), choose_service))

# Подключаем для gunicorn
gunicorn_app = app

# Локальный запуск (если нужно тестировать на компе)
if __name__ == "__main__":
    import asyncio
    async def main():
        await application.initialize()
        await application.start()
        await application.bot.set_webhook("https://telegram-delivery-bot.onrender.com")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    asyncio.run(main())

