import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import os
from dotenv import load_dotenv
import pandas as pd
import chardet
import re
from difflib import get_close_matches

# Загрузка переменных окружения
load_dotenv()

# Логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Получение токена
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная TELEGRAM_BOT_TOKEN не загружена из .env файла!")

# Авторизация DPD
DPD_ACCOUNTS = [
    {"clientNumber": "1021006899", "clientKey": "bdb325b2-c4dc-4574-9fcb-02712cf4e16c"},
    {"clientNumber": "1021006900", "clientKey": "3eaafd2d-543a-4ab9-9fd1-6a33286e10e8"},
]
DPD_WSDL_URL = "https://ws.dpd.ru/services/calculator2?wsdl"

# Авторизация СДЭК
CDEK_CLIENT_ID = "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK"
CDEK_CLIENT_SECRET = "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"

# Загрузка базы городов
cities_df = pd.read_csv("GeographyDPD_20250211.csv", sep=";", encoding=chardet.detect(open("GeographyDPD_20250211.csv", "rb").read())['encoding'])

def find_city_code(city_name):
    match = cities_df[cities_df.iloc[:, 3].str.contains(f'^{city_name}$', case=False, na=False)]
    return match.iloc[0, 0] if not match.empty else None

# Шаблоны
PRESETS = {
    "2-секции": (95, 76, 20, 17),
    "3-секции": (95, 76, 20, 20),
    "4-секции": (96, 76, 34, 30),
    "фикс-мт2": (187, 79, 21, 37),
    "фикс-1а": (187, 79, 24, 40),
    "ммкм-1": (188, 73, 47, 76),
    # ...
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

# Команды Telegram
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

# Инициализация приложения
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.Regex("^(СДЭК|DPD)$"), choose_service))

# Запуск POLLING (а не Webhook)
if __name__ == "__main__":
    application.run_polling()
