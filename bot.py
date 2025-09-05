import os
import logging
import asyncio
from datetime import date
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, JobQueue
import requests
import certifi
from zeep import Client
from zeep.exceptions import Fault, TransportError
from zeep.transports import Transport
import httpx
import pandas as pd
import chardet
import re
import pytz
from difflib import get_close_matches
import unicodedata

# ------------------------------ ЛОГИ ------------------------------ #
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ----------------------------- ОКРУЖЕНИЕ -------------------------- #
# Telegram
TOKEN = os.getenv("TELEGRAM_TOKEN")

# CDEK
CDEK_CLIENT_ID = os.getenv("CDEK_CLIENT_ID")
CDEK_CLIENT_SECRET = os.getenv("CDEK_CLIENT_SECRET")
CDEK_AUTH_URL = "https://api.cdek.ru/v2/oauth/token"
CDEK_TARIFFLIST_URL = "https://api.cdek.ru/v2/calculator/tarifflist"
CDEK_CITY_URL = "https://api.cdek.ru/v2/location/cities"

# DPD
DPD_ACCOUNTS = [
    {"clientNumber": os.getenv("DPD_CLIENT_NUMBER_1"), "clientKey": os.getenv("DPD_CLIENT_KEY_1")},
    {"clientNumber": os.getenv("DPD_CLIENT_NUMBER_2"), "clientKey": os.getenv("DPD_CLIENT_KEY_2")}
]
DPD_WSDL_URL = "https://ws.dpd.ru/services/calculator2?wsdl"

# Проверяем переменные окружения
_required_env = {
    "TELEGRAM_TOKEN": TOKEN,
    "CDEK_CLIENT_ID": CDEK_CLIENT_ID,
    "CDEK_CLIENT_SECRET": CDEK_CLIENT_SECRET,
    "DPD_CLIENT_NUMBER_1": os.getenv("DPD_CLIENT_NUMBER_1"),
    "DPD_CLIENT_KEY_1": os.getenv("DPD_CLIENT_KEY_1"),
    "DPD_CLIENT_NUMBER_2": os.getenv("DPD_CLIENT_NUMBER_2"),
    "DPD_CLIENT_KEY_2": os.getenv("DPD_CLIENT_KEY_2"),
}
_missing = [k for k, v in _required_env.items() if not v]
if _missing:
    raise RuntimeError(f"Отсутствуют переменные окружения: {', '.join(_missing)}")

# Путь к CSV со справочником городов DPD
CSV_ENV = os.getenv("DPD_GEO_CSV")
if CSV_ENV and os.path.exists(CSV_ENV):
    GEO_CSV_PATH = CSV_ENV
else:
    GEO_CSV_PATH = os.path.join(os.path.dirname(__file__), "GeographyDPD_20250211.csv")

# ---------------------- МАПИНГ ТАРИФОВ DPD ----------------------- #
# Человеческие имена тарифов (как в ЛК)
DPD_SERVICE_ALIASES = {
    "MXO": "DPD Standard",
    "CL":  "DPD Standard",
    "ECN": "DPD ECONOMY",
    "PCL": "DPD OPTIMUM",
    "BZP": "DPD 18:00",
}
# Тарифы, которые показываем (как в веб-кабинете)
DPD_WHITELIST = {"MXO", "CL", "ECN", "PCL", "BZP"}
# Для каких сервисов «как на сайте» принудительно ставим курьерский забор
DPD_FORCE_COURIER_PICKUP = {"BZP"}  # 18:00

# ---------------------------- ПРЕСЕТЫ ---------------------------- #
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
    base = key.lower().replace('-', ' ').replace('–', ' ')
    parts = base.split()
    variants = set()
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            sub = parts[i:j]
            variants.add(''.join(sub)); variants.add('-'.join(sub)); variants.add(' '.join(sub))
    for variant in variants:
        ALT_PRESETS[variant.strip()] = key

def extract_preset_key(text: str):
    clean = text.lower().replace('-', ' ').strip()
    return ALT_PRESETS.get(clean)

# ---------------------- БАЗА ГОРОДОВ DPD ------------------------ #
with open(GEO_CSV_PATH, 'rb') as _f:
    _det = chardet.detect(_f.read(10000))
_encoding = _det['encoding'] or 'utf-8'
cities_df = pd.read_csv(GEO_CSV_PATH, sep=';', encoding=_encoding)

# ------------------------ НОРМАЛИЗАЦИЯ ГОРОДОВ ------------------- #
def _norm(s: str) -> str:
    s = s.strip().lower().replace('ё', 'е').replace('-', ' ')
    s = ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch))
    s = re.sub(r'\s+', ' ', s)
    return s

def _subject_from_row(row) -> str:
    """Пытаемся достать «область/край/респ/округ» из любых колонок."""
    candidates = []
    for i in range(len(row)):
        val = row.iloc[i]
        if pd.isna(val):
            continue
        s = str(val).strip()
        if len(s) < 3:
            continue
        if re.search(r'(обл|область|край|респ|республика|АО|ао|округ)', s, flags=re.IGNORECASE):
            candidates.append(s)
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return candidates[0]

# norm_name -> список [(code, name, type_abbr, subject)]
_norm_city_bucket = {}
for _, row in cities_df.iterrows():
    code = str(row.iloc[0])
    name = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ''
    if not name:
        continue
    type_abbr = ''
    try:
        if len(row) > 1 and pd.notna(row.iloc[1]):
            type_abbr = str(row.iloc[1]).strip()
    except Exception:
        pass
    subject = _subject_from_row(row)
    key = _norm(name)
    _norm_city_bucket.setdefault(key, []).append((code, name, type_abbr, subject))
_norm_city_names = list(_norm_city_bucket.keys())

def code_exists(code: str) -> bool:
    return not cities_df[cities_df.iloc[:, 0].astype(str) == str(code)].empty

def name_by_code(code: str):
    row = cities_df[cities_df.iloc[:, 0].astype(str) == str(code)]
    if not row.empty:
        return str(row.iloc[0, 3])
    return None

def find_candidates_by_text(fragment: str):
    norm = _norm(fragment)
    if norm in _norm_city_bucket:
        return list(_norm_city_bucket[norm])
    keys = get_close_matches(norm, _norm_city_names, n=3, cutoff=0.86)
    out = []
    for k in keys:
        out.extend(_norm_city_bucket[k])
    seen = set(); uniq = []
    for code, name, t, subj in out:
        if code in seen: continue
        seen.add(code); uniq.append((code, name, t, subj))
    return uniq

def _scan_city_candidates(tokens, start_idx=0):
    max_len = min(len(tokens) - start_idx, 8)
    for L in range(max_len, 0, -1):
        chunk = ' '.join(tokens[start_idx:start_idx+L])
        cands = find_candidates_by_text(chunk)
        if cands:
            return (chunk, start_idx + L, cands)
    return (None, start_idx, [])

# ------------------- БЕЗОПАСНАЯ ОТПРАВКА ДЛИННЫХ ТЕКСТОВ -------- #
async def send_long_message(update: Update, text: str, chunk_size: int = 3500):
    """Режем ответ, чтобы не упереться в лимит Telegram (~4096)."""
    lines = text.split("\n")
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 <= chunk_size:
            buf += (line + "\n")
        else:
            await update.message.reply_text(buf.rstrip("\n"))
            buf = line + "\n"
    if buf.strip():
        await update.message.reply_text(buf.rstrip("\n"))

# ---------------------- ПАРСЕР ГОРОДОВ + ИНТЕРАКТ ---------------- #
def extract_cities_and_rest_interactive(text: str):
    """
    Возвращает:
    - ('ok', code_from, code_to, rest_tokens)
    - ('error', message)
    - ('ask_one', which, candidates, rest_tokens, other_code)
    - ('ask_both', candidates_from, candidates_to, rest_tokens)
    Поддерживает: '<code_from> <code_to> ...' и 'Москва, Киров, ...'
    """
    # коды в начале
    m = re.match(r'^\s*(\d+)\s*[, ]+\s*(\d+)\b(.*)$', text.strip())
    if m:
        c1, c2, tail = m.group(1), m.group(2), m.group(3)
        if code_exists(c1) and code_exists(c2):
            rest_tokens = [t for t in re.split(r'\s+', tail.strip()) if t]
            return ('ok', c1, c2, rest_tokens)

    # c запятыми
    if ',' in text or ';' in text:
        parts = re.split(r'[;,]', text)
        if len(parts) >= 2:
            c1_raw = parts[0].strip()
            c2_raw = parts[1].strip()
            rest = ' '.join(parts[2:]) if len(parts) > 2 else ''
            rest_tokens = [t for t in re.split(r'\s+', rest) if t]

            cands1 = find_candidates_by_text(c1_raw)
            cands2 = find_candidates_by_text(c2_raw)

            if len(cands1) == 1 and len(cands2) == 1:
                return ('ok', cands1[0][0], cands2[0][0], rest_tokens)

            if len(cands1) == 0:
                return ('error', f"Не нашёл город «{c1_raw}». Уточните, пожалуйста.")
            if len(cands2) == 0:
                return ('error', f"Не нашёл город «{c2_raw}». Уточните, пожалуйста.")

            if len(cands1) > 1 and len(cands2) > 1:
                return ('ask_both', cands1, cands2, rest_tokens)
            if len(cands1) > 1:
                return ('ask_one', 'from', cands1, rest_tokens, (cands2[0][0] if len(cands2) == 1 else None))
            if len(cands2) > 1:
                return ('ask_one', 'to', cands2, rest_tokens, (cands1[0][0] if len(cands1) == 1 else None))

    # без запятых
    text_sp = re.sub(r'[;,]+', ' ', text.strip())
    tokens = [t for t in re.split(r'\s+', text_sp) if t]

    chunk1, idx1, cands1 = _scan_city_candidates(tokens, 0)
    if not cands1:
        return ('error', "Не удалось распознать первый город. Совет: используйте запятые между городами.")
    chunk2, idx2, cands2 = _scan_city_candidates(tokens, idx1)
    if not cands2:
        return ('error', "Не удалось распознать второй город. Совет: используйте запятые между городами.")
    rest_tokens = tokens[idx2:]

    if len(cands1) == 1 and len(cands2) == 1:
        return ('ok', cands1[0][0], cands2[0][0], rest_tokens)
    if len(cands1) > 1 and len(cands2) > 1:
        return ('ask_both', cands1, cands2, rest_tokens)
    if len(cands1) > 1:
        return ('ask_one', 'from', cands1, rest_tokens, cands2[0][0])
    if len(cands2) > 1:
        return ('ask_one', 'to', cands2, rest_tokens, cands1[0][0])

    return ('ok', cands1[0][0], cands2[0][0], rest_tokens)

# ------------------------- ВСПОМОГАТЕЛЬНОЕ ---------------------- #
def parse_dims_tokens(tokens):
    nums = []
    for t in tokens:
        tt = t.replace(',', '.')
        try:
            nums.append(float(tt))
        except ValueError:
            continue
    if len(nums) >= 4:
        l, w, h, weight = nums[:4]
        return (l, w, h, weight)
    name = ' '.join(tokens).replace('-', ' ').strip()
    key = ALT_PRESETS.get(name.lower())
    if key:
        return PRESETS.get(key)
    return PRESETS.get(name.lower()) or PRESETS.get(extract_preset_key(name))

# ------------------------------ CDEK ----------------------------- #
def get_cdek_token():
    response = requests.post(CDEK_AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": CDEK_CLIENT_ID,
        "client_secret": CDEK_CLIENT_SECRET
    }, timeout=25)
    if response.status_code != 200:
        logger.error("CDEK auth error: %s %s", response.status_code, response.text)
        return None
    return response.json().get("access_token")

def get_cdek_city_code(city_name, token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(CDEK_CITY_URL, headers=headers, params={"city": city_name}, timeout=25)
    if response.status_code == 200 and response.json():
        return response.json()[0].get("code")
    return None

def calculate_cdek_delivery(city_from, city_to, dims):
    token = get_cdek_token()
    if not token:
        return "Ошибка авторизации в СДЭК."
    city_from_code = get_cdek_city_code(city_from, token)
    city_to_code = get_cdek_city_code(city_to, token)
    if not city_from_code or not city_to_code:
        return "Ошибка: не удалось определить коды городов."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    packages = []
    if isinstance(dims, list):
        for l, w, h, weight in dims:
            packages.append({"weight": int(float(weight) * 1000),
                             "length": int(round(float(l))),
                             "width": int(round(float(w))),
                             "height": int(round(float(h)))})
    else:
        l, w, h, weight = dims
        packages.append({"weight": int(float(weight) * 1000),
                         "length": int(round(float(l))),
                         "width": int(round(float(w))),
                         "height": int(round(float(h)))})
    payload = {"from_location": {"code": city_from_code},
               "to_location": {"code": city_to_code},
               "packages": packages}
    response = requests.post(CDEK_TARIFFLIST_URL, headers=headers, json=payload,
                             verify=certifi.where(), timeout=25)
    if response.status_code != 200:
        return f"Ошибка при расчете: {response.status_code} - {response.text}"
    data = response.json()
    categories = {"дверь-дверь": None, "дверь-склад": None, "склад-дверь": None, "склад-склад": None}
    for tariff in data.get("tariff_codes", []):
        name = (tariff.get("tariff_name") or "").lower()
        delivery_sum = tariff.get("delivery_sum")
        delivery_term = f"{tariff.get('period_min', '?')} - {tariff.get('period_max', '?')} дней"
        for cat in categories:
            if cat in name and (categories[cat] is None or delivery_sum < categories[cat]["price"]):
                categories[cat] = {"price": delivery_sum, "term": delivery_term}
    return "\n".join([f"📦 {k}: {v['price']} руб., срок {v['term']}" if v else f"📦 {k}: тариф недоступен"
                      for k, v in categories.items()])

# ------------------------- DPD: SOAP клиент ---------------------- #
def _build_dpd_client() -> Client:
    # HTTP-клиент с таймаутами: 10с connect, 20с read/write
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=None)
    http_client = httpx.Client(timeout=timeout)
    transport = Transport(client=http_client)
    return Client(DPD_WSDL_URL, transport=transport)

# ------------------------------- DPD ----------------------------- #
def calculate_dpd_delivery_by_codes(code_from: str, code_to: str, rest_tokens):
    """Считаем тарифы из белого списка; по каждому ЛК отдельно. Для BZP показываем «как на сайте».
       СИНХРОННАЯ функция — вызывается в фоне через asyncio.to_thread."""
    if len(rest_tokens) < 3:
        return "Ошибка: укажите габариты/пресет и 3 параметра (забор, доставка, объявл. стоимость)."

    pickup_type, delivery_type, declared_value = rest_tokens[-3:]
    tokens = rest_tokens[:-3]
    dims = parse_dims_tokens(tokens)
    if dims is None:
        return ("Ошибка: не распознаны габариты.\n"
                "Укажите 4 числа (Д Ш В Вес, см/см/см/кг) или название шаблона (например, «2-секции»).")

    user_self_pickup = (pickup_type != 'курьер')
    user_self_delivery = (delivery_type != 'курьер')

    client = _build_dpd_client()
    dims_list = dims if isinstance(dims, list) else [dims]

    lines = ["Результат расчета (полный список тарифов):"]

    for account in DPD_ACCOUNTS:
        per_user = {}
        per_site = {}

        def _acc(dst, sc, alias, cost, days):
            item = dst.setdefault(sc, {'alias': alias, 'cost': 0.0, 'days': 0})
            item['cost'] += float(cost or 0)
            item['days'] = max(item['days'], int(days or 0))

        for length, width, height, weight in dims_list:
            volume = (float(length) * float(width) * float(height)) / 1_000_000.0
            base_req = {
                'auth': {"clientNumber": account['clientNumber'], "clientKey": account['clientKey']},
                'pickup': {'cityId': code_from},
                'delivery': {'cityId': code_to},
                'selfPickup': user_self_pickup,
                'selfDelivery': user_self_delivery,
                'weight': float(weight),
                'volume': float(volume),
                'pickupDate': date.today().isoformat(),
                'declaredValue': float(declared_value),
            }
            try:
                resp = client.service.getServiceCost2(request=base_req)
            except Exception as e:
                lines.append(f"ЛК {account['clientNumber']}:")
                lines.append(f"• ошибка расчёта: {type(e).__name__}: {e}")
                break

            if not resp:
                continue

            # как ввёл пользователь
            for s in resp:
                name = getattr(s, 'serviceName', '') or ''
                sc = getattr(s, 'serviceCode', '') or ''
                if 'MAX domestic' in name:
                    continue
                if sc not in DPD_WHITELIST:
                    continue
                alias = DPD_SERVICE_ALIASES.get(sc, name or sc)
                _acc(per_user, sc, alias, getattr(s, 'cost', 0), getattr(s, 'days', 0))

            # «как на сайте» — точечно для сервисов с принудительным курьерским забором
            for sc in list(per_user.keys()):
                if sc in DPD_FORCE_COURIER_PICKUP and user_self_pickup:
                    req2 = dict(base_req)
                    req2['selfPickup'] = False
                    try:
                        resp2 = client.service.getServiceCost2(request=req2)
                        for s2 in resp2 or []:
                            if getattr(s2, 'serviceCode', '') == sc:
                                alias = DPD_SERVICE_ALIASES.get(sc, getattr(s2, 'serviceName', sc))
                                _acc(per_site, sc, alias, getattr(s2, 'cost', 0), getattr(s2, 'days', 0))
                                break
                    except Exception:
                        pass

        lines.append(f"ЛК {account['clientNumber']}:")
        if not per_user:
            lines.append("• тарифы не найдены")
            continue

        for sc in sorted(per_user.keys(), key=lambda k: per_user[k]['cost']):
            u = per_user[sc]
            v = per_site.get(sc)
            lines.append(f"• {u['alias']} ({sc}): {round(u['cost'], 2)} ₽, срок {u['days']} дн")
            if v and (abs(v['cost'] - u['cost']) > 0.01 or v['days'] != u['days']):
                lines.append(f"   • как на сайте: {round(v['cost'], 2)} ₽, срок {v['days']} дн")

    return "\n".join(lines)

# ---------------------- фоновые воркеры-обёртки ------------------ #
async def _do_calc_and_reply_dpd(update: Update, context: CallbackContext, code_from: str, code_to: str, rest_tokens):
    try:
        result = await asyncio.to_thread(calculate_dpd_delivery_by_codes, code_from, code_to, rest_tokens)
        await send_long_message(update, result)
    except Exception as e:
        logger.exception("DPD calc failed")
        await update.message.reply_text(f"Не удалось выполнить расчёт DPD: {type(e).__name__}: {e}")

async def _do_calc_and_reply_cdek(update: Update, context: CallbackContext, city_from_name: str, city_to_name: str, dims):
    try:
        result = await asyncio.to_thread(calculate_cdek_delivery, city_from_name, city_to_name, dims)
        await send_long_message(update, "Результат расчета:\n" + result)
    except Exception as e:
        logger.exception("CDEK calc failed")
        await update.message.reply_text(f"Не удалось выполнить расчёт СДЭК: {type(e).__name__}: {e}")

# ------------------------------ ПОДСКАЗКИ ------------------------ #
def _format_candidate_line(i, code, name, type_abbr, subject):
    typ = f", {type_abbr}" if type_abbr else ""
    subj = f" — {subject}" if subject else ""
    return f"{i}) {name}{typ}{subj} — код {code}"

async def _prompt_city_selection_single(update: Update, which: str, candidates):
    # сортируем: города ("г") выше, ограничиваем до 25
    def _score(c):
        _code, _name, t, _subj = c
        return 0 if str(t).strip().lower().startswith("г") else 1
    total = len(candidates)
    candidates = sorted(candidates, key=_score)[:25]

    header = (f"Найдено несколько вариантов для "
              f"{'города отправки' if which=='from' else 'города доставки'}.\n"
              f"Выберите номер (со скобкой) или введите код. Примеры: «1)» или «49694102».")
    if total > len(candidates):
        header += (f"\nПоказано {len(candidates)} из {total}. "
                   f"Можно уточнить область: «Октябрьский, Башкортостан».")

    lines = [header, ""]
    for i, (code, name, t, subj) in enumerate(candidates, start=1):
        lines.append(_format_candidate_line(i, code, name, t, subj))

    await send_long_message(update, "\n".join(lines))

async def _prompt_city_selection_both(update: Update, cands_from, cands_to):
    def _score(c):
        _code, _name, t, _subj = c
        return 0 if str(t).strip().lower().startswith("г") else 1
    total_from, total_to = len(cands_from), len(cands_to)
    cands_from = sorted(cands_from, key=_score)[:25]
    cands_to   = sorted(cands_to,   key=_score)[:25]

    header = ["Найдено несколько вариантов для обоих городов.",
              "Отправьте два значения (номера с ')' и/или коды) через пробел/запятую:",
              "Примеры: «1) 2)», «49694102 195733089», «1) 195733089»."]

    extra = []
    if total_from > len(cands_from):
        extra.append(f"отправки: показано {len(cands_from)} из {total_from}")
    if total_to > len(cands_to):
        extra.append(f"доставки: показано {len(cands_to)} из {total_to}")
    if extra:
        header.append("Уточняйте область, например: «Октябрьский, Башкортостан».")
        header.append(" (" + "; ".join(extra) + ")")

    blocks = []
    blocks.append("\n".join(header))
    blocks.append("\n".join(
        ["", "Город отправки:"] +
        [_format_candidate_line(i, c, n, t, s) for i, (c, n, t, s) in enumerate(cands_from, start=1)]
    ))
    blocks.append("\n".join(
        ["", "Город доставки:"] +
        [_format_candidate_line(i, c, n, t, s) for i, (c, n, t, s) in enumerate(cands_to, start=1)]
    ))

    for block in blocks:
        await send_long_message(update, block)

# ---------------------- ПАРСИНГ ВЫБОРА ПОЛЬЗОВАТЕЛЯ -------------- #
def _tokenize_choices(s: str):
    raw = re.split(r'[,\s;]+', s.strip())
    return [t for t in raw if t]

def _parse_choice_token(tok: str, max_index: int):
    """
    '1)' или '(1)' -> индекс (с проверкой диапазона)
    чистые цифры -> считаем КОДОМ города
    """
    if re.fullmatch(r'\(?\d+\)?', tok) and tok.endswith(')'):
        n = int(re.sub(r'\D', '', tok))
        if 1 <= n <= max_index:
            return ('index', n)
        return None
    if re.fullmatch(r'\d+', tok):
        return ('code', tok)
    return None

def _parse_two_selections(s: str, max_from: int, max_to: int):
    toks = _tokenize_choices(s)
    sel_from = sel_to = None
    for tok in toks:
        if sel_from is None:
            parsed = _parse_choice_token(tok, max_from)
            if parsed:
                sel_from = parsed
                continue
        if sel_from is not None and sel_to is None:
            parsed = _parse_choice_token(tok, max_to)
            if parsed:
                sel_to = parsed
                break
    return sel_from, sel_to

def _parse_one_selection(s: str, max_index: int):
    toks = _tokenize_choices(s)
    for tok in toks:
        parsed = _parse_choice_token(tok, max_index)
        if parsed:
            return parsed
    return None

# ------------------------------ ХЕНДЛЕРЫ ------------------------ #
async def start(update: Update, context: CallbackContext):
    keyboard = [["СДЭК"], ["DPD"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    context.user_data.clear()
    await update.message.reply_text("Выберите службу доставки:", reply_markup=reply_markup)

async def choose_service(update: Update, context: CallbackContext):
    context.user_data.clear()
    context.user_data["service"] = update.message.text
    if update.message.text == "СДЭК":
        await update.message.reply_text(
            "Введите: Город-отправитель, Город-получатель, затем 4 числа (Д Ш В Вес) или название шаблона.\n"
            "Можно кодами городов: «196 174 95 76 20 20».\n"
            "Надёжнее с запятыми: «Москва, Санкт-Петербург, 95 76 20 20» или «Москва, Санкт-Петербург, ММКМ-1»."
        )
    else:
        await update.message.reply_text(
            "Введите: Город_отправки, Город_доставки, <4 числа или пресет> Забор(курьер/пункт) Доставка(курьер/пункт) Объявленная_стоимость.\n"
            "Можно кодами: «196 174 2-секции пункт курьер 12200».\n"
            "Если вариантов много, используйте индексы со скобкой: «1) 3) …»"
        )

async def _resolve_cities_or_ask(update: Update, context: CallbackContext, service: str, text: str):
    status = extract_cities_and_rest_interactive(text)
    tag = status[0]
    if tag == 'ok':
        _, c_from, c_to, rest = status
        return (c_from, c_to, rest)
    if tag == 'error':
        await update.message.reply_text(status[1]); return None
    if tag == 'ask_one':
        _, which, candidates, rest, other_code = status
        context.user_data['city_select_single'] = {
            'which': which, 'candidates': candidates, 'rest': rest,
            'other_code': other_code, 'service': service
        }
        await _prompt_city_selection_single(update, which, candidates)
        return None
    if tag == 'ask_both':
        _, cands_from, cands_to, rest = status
        context.user_data['city_select_both'] = {
            'from': cands_from, 'to': cands_to, 'rest': rest, 'service': service
        }
        await _prompt_city_selection_both(update, cands_from, cands_to)
        return None
    await update.message.reply_text("Неожиданный формат разбора. Попробуйте уточнить города.")
    return None

async def handle_input(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    logger.info("update in, text=%r", text)

    # Режим выбора обоих городов
    if 'city_select_both' in context.user_data:
        st = context.user_data['city_select_both']
        sel_from, sel_to = _parse_two_selections(text, len(st['from']), len(st['to']))
        if not sel_from or not sel_to:
            await update.message.reply_text("Введите два значения: «1) 2)» (индексы) или «код1 код2».")
            return
        if sel_from[0] == 'index':
            code_from = st['from'][sel_from[1]-1][0]
        else:
            code_from = sel_from[1]
            if not code_exists(code_from):
                await update.message.reply_text("Неверный код для города отправки."); return
        if sel_to[0] == 'index':
            code_to = st['to'][sel_to[1]-1][0]
        else:
            code_to = sel_to[1]
            if not code_exists(code_to):
                await update.message.reply_text("Неверный код для города доставки."); return

        rest = st['rest']; service = st['service']
        context.user_data.pop('city_select_both', None)
        if service == "DPD":
            await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
            asyncio.create_task(_do_calc_and_reply_dpd(update, context, code_from, code_to, rest))
        else:
            name_from = name_by_code(code_from); name_to = name_by_code(code_to)
            if not name_from or not name_to:
                await update.message.reply_text("Коды городов выбраны, но не удалось восстановить названия для CDEK."); return
            dims = parse_dims_tokens(rest)
            if dims is None:
                await update.message.reply_text("Ошибка: не распознаны габариты. Укажите 4 числа или шаблон."); return
            await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
            asyncio.create_task(_do_calc_and_reply_cdek(update, context, name_from, name_to, dims))
        return

    # Режим выбора одного города
    if 'city_select_single' in context.user_data:
        st = context.user_data['city_select_single']
        sel = _parse_one_selection(text, len(st['candidates']))
        if not sel:
            await update.message.reply_text("Введите «1)» (индекс) или код города."); return
        if sel[0] == 'index':
            chosen_code = st['candidates'][sel[1]-1][0]
        else:
            chosen_code = sel[1]
            if not code_exists(chosen_code):
                await update.message.reply_text("Неверный код города."); return

        which = st['which']; other_code = st['other_code']; rest = st['rest']; service = st['service']
        if which == 'from':
            code_from = chosen_code; code_to = other_code
        else:
            code_to = chosen_code; code_from = other_code

        if not code_from or not code_to:
            await update.message.reply_text("Принял выбор. Теперь уточните второй город (пришлите два значения или новый запрос).")
            context.user_data.pop('city_select_single', None); return

        context.user_data.pop('city_select_single', None)
        if service == "DPD":
            await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
            asyncio.create_task(_do_calc_and_reply_dpd(update, context, code_from, code_to, rest))
        else:
            name_from = name_by_code(code_from); name_to = name_by_code(code_to)
            if not name_from or not name_to:
                await update.message.reply_text("Коды городов выбраны, но не удалось восстановить названия для CDEK."); return
            dims = parse_dims_tokens(rest)
            if dims is None:
                await update.message.reply_text("Ошибка: не распознаны габариты. Укажите 4 числа или шаблон."); return
            await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
            asyncio.create_task(_do_calc_and_reply_cdek(update, context, name_from, name_to, dims))
        return

    # Обычный режим
    if "service" not in context.user_data:
        await update.message.reply_text("Пожалуйста, сначала выберите службу доставки командой /start")
        return

    service = context.user_data["service"]
    resolved = await _resolve_cities_or_ask(update, context, service, text)
    if resolved is None:
        return
    code_from, code_to, rest = resolved

    if service == "СДЭК":
        dims = parse_dims_tokens(rest)
        if dims is None:
            await update.message.reply_text("Ошибка: не распознаны габариты. Укажите 4 числа (Д Ш В Вес) или шаблон.")
            return
        city_from_name = name_by_code(code_from); city_to_name = name_by_code(code_to)
        if not city_from_name or not city_to_name:
            await update.message.reply_text("Нашёл коды городов, но не смог восстановить названия. Проверьте ввод.")
            return
        await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
        asyncio.create_task(_do_calc_and_reply_cdek(update, context, city_from_name, city_to_name, dims))
    else:
        await update.message.reply_text("Считаю… это может занять до 10–20 секунд.")
        asyncio.create_task(_do_calc_and_reply_dpd(update, context, code_from, code_to, rest))

# --------------- ФИКС PTB/APScheduler: pytz-таймзона ---------------- #
JobQueue.scheduler_configuration = {"timezone": pytz.UTC}
job_queue = JobQueue()

application = Application.builder().token(TOKEN).job_queue(job_queue).build()

# error-handler — чтобы видеть любые исключения
async def on_error(update: object, context: CallbackContext) -> None:
    logger.exception("Unhandled error", exc_info=context.error)

application.add_error_handler(on_error)

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.Regex("^(СДЭК|DPD)$"), choose_service))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

if __name__ == "__main__":
    # --- Webhook для Render Web Service ---
    port = int(os.getenv("PORT", "10000"))  # Render передаёт порт в переменной PORT
    base_url = os.getenv("WEBHOOK_BASE_URL")  # например: https://telegram-delivery-bot-1.onrender.com
    if not base_url:
        raise RuntimeError("Не задан WEBHOOK_BASE_URL в переменных окружения Render")
    path = os.getenv("WEBHOOK_PATH", "webhook")  # можно задать свой секретный путь

    # PTB поднимет aiohttp-сервер и выставит вебхук у Telegram
    logger.info("Starting webhook on port %s, path '%s'", port, path)
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=path,
        webhook_url=f"{base_url.rstrip('/')}/{path}",
    )
