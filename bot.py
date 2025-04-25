import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import os
from dotenv import load_dotenv

load_dotenv()

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Токен
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная TELEGRAM_BOT_TOKEN не загружена из .env файла!")

# Обработчики
async def start(update: Update, context: CallbackContext):
    keyboard = [["СДЭК"], ["DPD"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
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

# Запуск бота через polling
if __name__ == "__main__":
    application.run_polling()
