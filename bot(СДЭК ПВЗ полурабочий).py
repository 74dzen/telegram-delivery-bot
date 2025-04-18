import requests
import json
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
import re
from geopy.distance import geodesic

CDEK_ACCOUNT = "8sADkKf1pgUQtUdSYNFJZulRpLYjOBRK"
CDEK_PASSWORD = "EfPTHHB8uPkhY4HBdprA709zNUpF0BcJ"
TELEGRAM_BOT_TOKEN = "7377796192:AAEuQOJ3KedwCz_C2WwrR7QMU-PHD74jFQI"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

async def get_cdek_token():
    url = "https://api.cdek.ru/v2/oauth/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CDEK_ACCOUNT,
        "client_secret": CDEK_PASSWORD
    }
    response = requests.post(url, data=data)
    return response.json().get("access_token")

async def get_city_code(city_name):
    token = await get_cdek_token()
    url = "https://api.cdek.ru/v2/location/cities"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"country_codes": "RU", "city": city_name}
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code != 200:
        return None
    
    cities = response.json()
    if not cities:
        return None
    
    return cities[0]['code']

async def get_address_coordinates(address, city):
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "CDEK_Bot/1.0 (your_email@example.com)"}
    params = {"q": f"{address}, {city}", "format": "json"}
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code == 200 and response.json():
        location = response.json()[0]
        return float(location['lat']), float(location['lon'])
    else:
        print(f"Ошибка получения координат: {response.status_code}, ответ: {response.text}")
    return None

async def get_cdek_pvz(city_code, length, width, height, weight, user_coords):
    token = await get_cdek_token()
    url = "https://api.cdek.ru/v2/deliverypoints"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"city_code": city_code}
    response = requests.get(url, headers=headers, params=params)
    
    if response.status_code != 200:
        return "Ошибка при получении данных от CDEK"
    
    pvz_list = response.json()
    suitable_pvz = []
    
    for pvz in pvz_list:
        weight_min = pvz.get('weight_min', 0)
        weight_max = pvz.get('weight_max', 100)
        dimensions = pvz.get('dimension_limit', {})
        location = pvz.get('location', {})
        type_pvz = pvz.get('type', '')
        
        if type_pvz == "POSTOMAT":  # Исключаем постоматы
            continue
        
        max_length = dimensions.get('length', 9999)
        max_width = dimensions.get('width', 9999)
        max_height = dimensions.get('height', 9999)
        
        if (weight_min <= weight <= weight_max and
            max_length >= length and
            max_width >= width and
            max_height >= height and
            'latitude' in location and 'longitude' in location):
            
            pvz_coords = (location['latitude'], location['longitude'])
            distance = geodesic(user_coords, pvz_coords).km
            suitable_pvz.append((pvz, distance))
    
    suitable_pvz.sort(key=lambda x: x[1])
    return [pvz[0] for pvz in suitable_pvz[:3]]

@dp.message()
async def process_message(message: Message):
    text = message.text
    match = re.match(r"(.+?),\s(.+?)\s(\d+)x(\d+)x(\d+)\s(\d+)", text)
    if not match:
        await message.reply("Некорректный формат запроса. Пример: Челябинск, ул Братьев Кашириных 12Д 95x76x20 17")
        return
    
    city, address, length, width, height, weight = match.groups()
    length, width, height, weight = map(int, [length, width, height, weight])
    
    city_code = await get_city_code(city)
    if not city_code:
        await message.reply("Город не найден в базе CDEK")
        return
    
    user_coords = await get_address_coordinates(address, city)
    if not user_coords:
        await message.reply(f"Не удалось определить координаты адреса: {address}, {city}")
        return
    
    pvz_list = await get_cdek_pvz(city_code, length, width, height, weight, user_coords)
    
    if isinstance(pvz_list, str):
        await message.reply(pvz_list)
    elif not pvz_list:
        await message.reply("Нет подходящих пунктов выдачи")
    else:
        response_text = "Найденные ПВЗ:\n"
        for pvz in pvz_list:
            response_text += f"{pvz['code']}: {pvz['location']['address']}, {pvz['location']['city']} (расстояние: {geodesic(user_coords, (pvz['location']['latitude'], pvz['location']['longitude'])).km:.2f} км)\n"
        await message.reply(response_text)

async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
