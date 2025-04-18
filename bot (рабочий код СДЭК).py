import requests
import json
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import certifi

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TOKEN = "7377796192:AAEuQOJ3KedwCz_C2WwrR7QMU-PHD74jFQI"
CDEK_API_URL = "https://api.cdek.ru/v2/calculator/tarifflist"
CDEK_AUTH_URL = "https://api.cdek.ru/v2/oauth/token"
CDEK_CITY_URL = "https://api.cdek.ru/v2/location/cities"
CDEK_ACCOUNT = "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK"
CDEK_PASSWORD = "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"

# Функция для получения токена
def get_cdek_token():
    try:
        response = requests.post(
            CDEK_AUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": CDEK_ACCOUNT.strip(),
                "client_secret": CDEK_PASSWORD.strip()
            },
            verify=certifi.where()
        )
        data = response.json()
        logging.info(f"Ответ СДЭК при авторизации: {data}")
        return data.get("access_token")
    except Exception as e:
        logging.error(f"Ошибка авторизации в СДЭК: {e}")
        return None

# Функция для получения кода города по его названию
def get_city_code(city_name: str, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"city": city_name}
    response = requests.get(
        CDEK_CITY_URL, 
        headers=headers, 
        params=params,
        verify=certifi.where()
    )
    if response.status_code == 200:
        cities = response.json()
        if cities:
            return cities[0]["code"]
    return None

# Основная функция для старта бота
async def start(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text("Привет! Отправь данные в формате: вес(кг) город_отправки город_получения ДхШхВ(см)")

# Функция для расчета тарифа
async def calculate_shipping(update: Update, context: CallbackContext) -> None:
    try:
        text = update.message.text.split()
        weight = float(text[0])
        from_city = text[1]
        to_city = text[2]
        # Заменяем английскую "x" на русскую "х"
        dimensions = text[3].replace('x', 'х').split('х')
        length, width, height = map(int, dimensions)

        token = get_cdek_token()
        if not token:
            await update.message.reply_text("Ошибка авторизации в СДЭК. Проверьте учетные данные.")
            return

        from_city_code = get_city_code(from_city, token)
        to_city_code = get_city_code(to_city, token)

        if not from_city_code or not to_city_code:
            await update.message.reply_text("Не удалось найти один из городов. Проверьте ввод.")
            return

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "from_location": {"code": from_city_code},
            "to_location": {"code": to_city_code},
            "packages": [{
                "weight": weight * 1000,
                "length": length,
                "width": width,
                "height": height
            }]
        }

        response = requests.post(
            CDEK_API_URL, 
            headers=headers, 
            json=payload,
            verify=certifi.where()
        )
        logging.info(f"Ответ СДЭК на расчет тарифа: {response.status_code} - {response.text}")

        if response.status_code == 200:
            data = response.json()
            logging.info(f"Ответ от СДЭК (данные о тарифах): {json.dumps(data, indent=2)}")

            if "tariff_codes" in data and data["tariff_codes"]:
                # Инициализация переменных для минимальных тарифов
                min_door_to_door = None
                min_warehouse_to_door = None
                min_door_to_warehouse = None
                min_warehouse_to_warehouse = None
                
                # Поиск минимальных тарифов для каждой категории
                for tariff in data["tariff_codes"]:
                    tariff_name = tariff.get("tariff_name", "")
                    price = tariff.get("delivery_sum", float('inf'))
                    delivery_min = tariff.get("period_min", "?")
                    delivery_max = tariff.get("period_max", "?")
                    delivery_term = f"{delivery_min}-{delivery_max} дней" if delivery_min != "?" and delivery_max != "?" else "Срок неизвестен"

                    # Выбираем минимальные тарифы по категориям
                    if "дверь-дверь" in tariff_name.lower() and (min_door_to_door is None or price < min_door_to_door['price']):
                        min_door_to_door = {"tariff_name": tariff_name, "price": price, "delivery_term": delivery_term}
                    elif "склад-дверь" in tariff_name.lower() and (min_warehouse_to_door is None or price < min_warehouse_to_door['price']):
                        min_warehouse_to_door = {"tariff_name": tariff_name, "price": price, "delivery_term": delivery_term}
                    elif "дверь-склад" in tariff_name.lower() and (min_door_to_warehouse is None or price < min_door_to_warehouse['price']):
                        min_door_to_warehouse = {"tariff_name": tariff_name, "price": price, "delivery_term": delivery_term}
                    elif "склад-склад" in tariff_name.lower() and (min_warehouse_to_warehouse is None or price < min_warehouse_to_warehouse['price']):
                        min_warehouse_to_warehouse = {"tariff_name": tariff_name, "price": price, "delivery_term": delivery_term}

                # Формирование сообщения с минимальными тарифами
                message = ""
                if min_door_to_door:
                    message += f"📦 Тариф (дверь-дверь): {min_door_to_door['tariff_name']}\n💰 Стоимость: {min_door_to_door['price']} руб.\n⏳ Срок: {min_door_to_door['delivery_term']}\n\n"
                if min_warehouse_to_door:
                    message += f"📦 Тариф (склад-дверь): {min_warehouse_to_door['tariff_name']}\n💰 Стоимость: {min_warehouse_to_door['price']} руб.\n⏳ Срок: {min_warehouse_to_door['delivery_term']}\n\n"
                if min_door_to_warehouse:
                    message += f"📦 Тариф (дверь-склад): {min_door_to_warehouse['tariff_name']}\n💰 Стоимость: {min_door_to_warehouse['price']} руб.\n⏳ Срок: {min_door_to_warehouse['delivery_term']}\n\n"
                if min_warehouse_to_warehouse:
                    message += f"📦 Тариф (склад-склад): {min_warehouse_to_warehouse['tariff_name']}\n💰 Стоимость: {min_warehouse_to_warehouse['price']} руб.\n⏳ Срок: {min_warehouse_to_warehouse['delivery_term']}\n\n"

                # Если нет тарифов, выводим соответствующее сообщение
                if not message:
                    message = "Не удалось найти тарифы по заданным категориям."

                await update.message.reply_text(message)
            else:
                await update.message.reply_text("Не удалось получить стоимость доставки.")
        else:
            await update.message.reply_text(f"Ошибка запроса к СДЭК: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Ошибка обработки запроса: {e}")
        await update.message.reply_text("Ошибка обработки запроса. Проверьте формат ввода.")

# Основная функция для запуска бота
def main() -> None:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, calculate_shipping))
    application.run_polling()

if __name__ == "__main__":
    main()
