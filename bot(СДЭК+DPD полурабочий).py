import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import requests
import json

# Токен бота
TOKEN = "7377796192:AAEuQOJ3KedwCz_C2WwrR7QMU-PHD74jFQI"

# Функция для получения токена авторизации СДЭК
def get_cdek_token():
    url = "https://api.cdek.ru/v2/oauth/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK",
        "client_secret": "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"
    }
    response = requests.post(url, data=data)
    if response.status_code == 200:
        return response.json().get("access_token")
    return None

# Функция для получения кода города по его названию
def get_city_code(city_name, token):
    url = "https://api.cdek.ru/v2/location/cities"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"country_codes": "RU", "city": city_name}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200 and response.json():
        return response.json()[0].get("code")
    return None

# Функция старта
async def start(update: Update, context: CallbackContext) -> None:
    keyboard = [["СДЭК", "DPD"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    context.user_data.clear()
    await update.message.reply_text("Привет! Я бот для расчета доставки. Выберите службу доставки:", reply_markup=reply_markup)

# Функция обработки выбора доставки
async def choose_service(update: Update, context: CallbackContext) -> None:
    text = update.message.text.strip()
    if text in ["СДЭК", "DPD"]:
        context.user_data['service'] = text
        context.user_data['awaiting_data'] = True
        await update.message.reply_text(
            f"Вы выбрали {text}. Пришлите информацию по доставке в формате: \n"
            f"вес(кг) город_отправления город_получения длина(см) ширина(см) высота(см)"
        )
        return
    await update.message.reply_text("Ошибка выбора. Пожалуйста, выберите службу доставки снова.")

# Функция обработки данных для расчета доставки
async def process_delivery(update: Update, context: CallbackContext) -> None:
    if not context.user_data.get('awaiting_data'):
        return
    
    data = update.message.text.split()
    if len(data) != 6:
        await update.message.reply_text("Ошибка ввода. Проверьте, что данные введены в правильном формате.")
        return
    
    try:
        weight = float(data[0])
        length = int(float(data[3]))
        width = int(float(data[4]))
        height = int(float(data[5]))
        loc_from = data[1]
        loc_to = data[2]
    except ValueError:
        await update.message.reply_text("Ошибка ввода. Убедитесь, что вес и габариты указаны числом.")
        return
    
    service = context.user_data['service']
    context.user_data['awaiting_data'] = False
    
    if service == "СДЭК":
        token = get_cdek_token()
        if not token:
            await update.message.reply_text("Ошибка авторизации в СДЭК. Попробуйте позже.")
            return
        
        from_code = get_city_code(loc_from, token)
        to_code = get_city_code(loc_to, token)
        
        if not from_code or not to_code:
            await update.message.reply_text("Ошибка определения кодов городов. Проверьте корректность ввода.")
            return
        
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = "https://api.cdek.ru/v2/calculator/tarifflist"
        payload = {
            "from_location": {"code": from_code},
            "to_location": {"code": to_code},
            "packages": [{
                "weight": weight,
                "length": length,
                "width": width,
                "height": height
            }]
        }
    else:
        return
    
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        tariffs = response.json().get("tariff_codes", [])
        best_tariffs = {}
        
        for tariff in tariffs:
            mode = tariff.get("delivery_mode")
            price = tariff.get("delivery_sum")
            if mode in [1, 2, 3, 4]:
                if mode not in best_tariffs or price < best_tariffs[mode]["delivery_sum"]:
                    best_tariffs[mode] = tariff
        
        result = "\n".join([
            f"📦 {tariff['tariff_name']} - {tariff['delivery_sum']}₽ ({tariff['period_min']}-{tariff['period_max']} дн.)"
            for tariff in best_tariffs.values()
        ])
        
        await update.message.reply_text(f"Результат расчета {service}:\n{result}")
    else:
        await update.message.reply_text(f"Ошибка при запросе к API. Код: {response.status_code}, Ответ: {response.text}")

# Основная функция
def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("^(СДЭК|DPD)$"), choose_service))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex("^(СДЭК|DPD)$"), process_delivery))
    app.run_polling()

if __name__ == "__main__":
    main()
