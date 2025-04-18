from difflib import get_close_matches  # Импорт для поиска похожих строк
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from zeep import Client
import asyncio
import nest_asyncio
import pandas as pd
import os
import chardet

# Активация поддержки вложенных циклов событий для macOS
nest_asyncio.apply()

# Данные для двух личных кабинетов DPD
DPD_ACCOUNTS = [
    {
        'clientNumber': '1021006899',
        'clientKey': 'bdb325b2-c4dc-4574-9fcb-02712cf4e16c'
    },
    {
        'clientNumber': '1021006900',
        'clientKey': '3eaafd2d-543a-4ab9-9fd1-6a33286e10e8'
    }
]

DPD_WSDL_URL = 'https://ws.dpd.ru/services/calculator2?wsdl'
TELEGRAM_BOT_TOKEN = '7377796192:AAEuQOJ3KedwCz_C2WwrR7QMU-PHD74jFQI'

# Разворачивание пути к файлу
file_path = os.path.expanduser('~/Desktop/GeographyDPD_20250211.csv')
print(f"Путь к файлу: {file_path}")

# Определение правильной кодировки файла (читаем только первые 10000 байт)
try:
    with open(file_path, 'rb') as f:
        print("Файл открыт успешно, определяем кодировку...")
        result = chardet.detect(f.read(10000))  # Чтение первых 10 КБ для определения кодировки
    correct_encoding = result['encoding']
    print(f"Определена кодировка файла: {correct_encoding}")
except FileNotFoundError:
    print(f"Файл не найден по пути: {file_path}")
    exit()

# Загрузка данных о городах с правильной кодировкой
try:
    cities_df = pd.read_csv(file_path, encoding=correct_encoding, sep=';')
    city_names = cities_df.iloc[:, 3].dropna().unique().tolist()  # Используем 4-й столбец для городов
    print(f"Данные о городах успешно загружены. Количество городов: {len(city_names)}")
except Exception as e:
    print(f"Ошибка при загрузке данных о городах: {e}")
    exit()

# Функция для поиска кода города по названию
def find_city_code(city_name):
    match = cities_df[cities_df.iloc[:, 3].str.contains(f'^{city_name}$', case=False, na=False)]
    if not match.empty:
        return match.iloc[0, 0]  # Берем первый найденный код
    
    suggestions = get_close_matches(city_name, city_names, n=3, cutoff=0.6)
    if suggestions:
        return f"Не удалось найти точное совпадение для '{city_name}'. Возможно, вы имели в виду: {', '.join(suggestions)}"
    return f"Город '{city_name}' не найден в базе данных."

# Функция для расчета стоимости доставки через API DPD
async def calculate_shipping(pickup_city, delivery_city, weight, length, width, height, pickup_type, delivery_type, declared_value):
    print(f"Расчет стоимости доставки: {pickup_city} -> {delivery_city}, вес: {weight} кг, размеры: {length}x{width}x{height} см, объявленная стоимость: {declared_value} руб.")
    client = Client(DPD_WSDL_URL)

    pickup_code = find_city_code(pickup_city)
    delivery_code = find_city_code(delivery_city)

    if isinstance(pickup_code, str) or isinstance(delivery_code, str):
        return f"{pickup_code if isinstance(pickup_code, str) else ''}\n{delivery_code if isinstance(delivery_code, str) else ''}"

    volume = (length * width * height) / 1_000_000
    print(f"Рассчитанный объем: {volume} м³")

    self_pickup = False if pickup_type.lower() == 'курьер' else True
    self_delivery = False if delivery_type.lower() == 'курьер' else True

    results = []

    # Расчет стоимости через DPD
    for account in DPD_ACCOUNTS:
        request_data = {
            'auth': {
                'clientNumber': account['clientNumber'],
                'clientKey': account['clientKey']
            },
            'pickup': {
                'cityId': pickup_code
            },
            'delivery': {
                'cityId': delivery_code
            },
            'selfPickup': self_pickup,
            'selfDelivery': self_delivery,
            'weight': weight,
            'volume': volume,
            'declaredValue': declared_value
        }

        print(f"Отправка запроса в API DPD (ЛК {account['clientNumber']}) с данными: {request_data}")

        try:
            response = client.service.getServiceCost2(request=request_data)
            if response:
                filtered_response = [service for service in response if 'MAX domestic' not in service['serviceName']]
                if filtered_response:
                    cheapest_service = min(filtered_response, key=lambda x: x['cost'])
                    results.append({
                        'account': account['clientNumber'],
                        'service': cheapest_service['serviceName'],
                        'cost': cheapest_service['cost'],
                        'days': cheapest_service['days']
                    })
                else:
                    results.append({
                        'account': account['clientNumber'],
                        'error': "Все доступные тарифы - DPD MAX domestic, которые исключены."
                    })
            else:
                results.append({
                    'account': account['clientNumber'],
                    'error': "Не удалось получить стоимость доставки."
                })
        except Exception as e:
            results.append({
                'account': account['clientNumber'],
                'error': f"Ошибка при расчете стоимости: {e}"
            })

    # Сравниваем результаты и выбираем два самых выгодных варианта
    successful_results = [res for res in results if 'cost' in res]
    if successful_results:
        best_results = sorted(successful_results, key=lambda x: x['cost'])[:2]
        response_message = "\n\n".join([
            f"Транспортная компания: {res['account']}\nТариф: {res['service']}\nСтоимость: {res['cost']} руб.\nСрок доставки: {res['days']} дней"
            for res in best_results
        ])
        return response_message
    else:
        return "\n".join([res.get('error', f"Ошибка в ЛК {res['account']}") for res in results])

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Получена команда /start от пользователя: {update.effective_user.first_name}")
    await update.message.reply_text(
        "Привет! Я помогу рассчитать стоимость доставки через DPD.\n"
        "Введите данные в формате: Город_отправки, Город_доставки, Вес_в_кг, Длина_см, Ширина_см, Высота_см, Забор(курьер/пункт), Доставка(курьер/пункт), Объявленная_стоимость_руб.\n"
        "Пример: Моск, Спб, 5, 30, 20, 15, курьер, пункт, 5000"
    )

# Обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    print(f"Получено сообщение: {text}")
    try:
        pickup_city, delivery_city, weight, length, width, height, pickup_type, delivery_type, declared_value = [x.strip() for x in text.split(',')]
        weight = float(weight)
        length = int(length)
        width = int(width)
        height = int(height)
        declared_value = float(declared_value)
        
        result = await calculate_shipping(pickup_city, delivery_city, weight, length, width, height, pickup_type, delivery_type, declared_value)
        await update.message.reply_text(result)
    except ValueError:
        await update.message.reply_text("Пожалуйста, используйте правильный формат: Город_отправки, Город_доставки, Вес_в_кг, Длина_см, Ширина_см, Высота_см, Забор(курьер/пункт), Доставка(курьер/пункт), Объявленная_стоимость_руб.")

# Основная функция для запуска бота
async def main():
    print("Инициализация приложения...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Бот подключается к Telegram API...")
    await app.run_polling(drop_pending_updates=True)  # Очистка старых сообщений

if __name__ == '__main__':
    print("Запуск бота...")
    asyncio.run(main())
