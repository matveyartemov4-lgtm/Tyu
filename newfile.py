import asyncio
import logging
import random
import re
import os
import time

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup
from english_words import get_english_words_set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.errors import FloodWaitError, UsernameInvalidError

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = "8932397702:AAGY7wdLp4dy96MeSH1lce86HqzClmmMEqk"
API_ID = 33248398
API_HASH = "6543087387b7b14fcafcca74d28b1158"

MIN_LOCAL_SCORE = 30  
WORKER_SLEEP = 15      

if os.path.exists("/data") and os.access("/data", os.W_OK):
    DB_PATH = "/data/usernames.db"
else:
    DB_PATH = "usernames.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

msg_lock = None 

def estimate_ton_price(username: str) -> str:
    """Примерная оценка стоимости в TON на основе длины"""
    length = len(username)
    if length == 4: return "~1000 TON"
    if length == 5: return "~500 TON"
    if length == 6: return "~200 TON"
    if length == 7: return "~100 TON"
    if length == 8: return "~50 TON"
    return "Менее 10 TON"

# ==========================================
# 2. БАЗА ДАННЫХ (SQLite)
# ==========================================
async def init_db():
    dirname = os.path.dirname(DB_PATH)
    if dirname: 
        os.makedirs(dirname, exist_ok=True)
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                username TEXT PRIMARY KEY,
                total_score INTEGER,
                is_dict BOOLEAN,
                readability INTEGER,
                fragment_score INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                username TEXT PRIMARY KEY,
                score INTEGER
            )
        """)
        await db.commit()

async def save_username(username: str, score: int, is_dict: bool, read: int, frag: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO usernames VALUES (?, ?, ?, ?, ?)",
                (username, score, is_dict, read, frag)
            )
            await db.commit()
    except Exception as e:
        logging.error(f"Ошибка сохранения в БД: {e}")

async def add_to_favorites(username: str, score: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?)", (username, score))
            await db.commit()
            return True
    except Exception as e:
        logging.error(f"Ошибка добавления в избранное: {e}")
        return False

async def get_favorites():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, score FROM favorites LIMIT 30") as cursor:
            return await cursor.fetchall()

async def get_top_10():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, total_score, is_dict FROM usernames ORDER BY total_score DESC LIMIT 10") as cursor:
            return await cursor.fetchall()

# ==========================================
# 3. ДВИЖОК: ГЕНЕРАЦИЯ И ОЦЕНКА
# ==========================================
class Engine:
    def __init__(self):
        print("⏳ Загрузка словаря...")
        words = get_english_words_set(['web2'], lower=True)
        # Добавлены длины 6, 7 и 8
        self.dict_words = {w for w in words if len(w) in [5, 6, 7, 8] and w.isalpha()}
        self.vowels = set("aeiouy")
        self.consonants = "bcdfghjklmnpqrstvwxz"
        print("✅ Словарь загружен!")

    def generate_word(self) -> str:
        # Вероятность выбора длины
        length = random.choices([5, 6, 7, 8], weights=[40, 30, 20, 10])[0]
        
        if random.random() > 0.5:
            words_len = [w for w in self.dict_words if len(w) == length]
            if words_len:
                return random.choice(words_len)
        
        # Генерация псевдослучайной читаемой строки нужной длины
        return "".join(random.choice(self.consonants + "aeiou") for _ in range(length))

    def local_score(self, word: str) -> tuple[int, bool, int]:
        length = len(word)
        is_dict = word in self.dict_words
        dict_score = 40 if is_dict else 0
        
        read_score = 30
        for i in range(len(word) - 2):
            chunk = word[i:i+3]
            v_count = sum(1 for char in chunk if char in self.vowels)
            if v_count == 3 or v_count == 0:
                read_score -= 15
        read_score = max(0, read_score)
        
        total = dict_score + read_score
        
        # Строгие фильтры оценки по длине
        if length == 6 and total < 60:
            return 0, False, 0
        if length in [7, 8] and total < 80:
            return 0, False, 0
        if length == 5 and total < MIN_LOCAL_SCORE:
            return 0, False, 0
            
        return total, is_dict, read_score

    async def fragment_score(self, word: str) -> int:
        url = f"https://fragment.com/?query={word}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status != 200:
                        return 0
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    
                    price_elements = soup.find_all('div', class_='table-cell-value tm-value icon-before icon-ton')
                    prices = []
                    for el in price_elements:
                        match = re.search(r'([\d,]+)', el.text)
                        if match:
                            prices.append(int(match.group(1).replace(',', '')))
                    
                    if not prices: return 0
                    
                    avg_price = sum(prices) / len(prices)
                    if avg_price >= 500: return 30
                    if avg_price >= 100: return 20
                    if avg_price > 10: return 10
                    return 0
        except Exception as e:
            logging.error(f"Ошибка Fragment: {e}")
            return 0

# ==========================================
# 4. РОТАЦИЯ СЕССИЙ + ИНТЕГРАЦИЯ С USERATE_BOT
# ==========================================
class MultiSessionChecker:
    def __init__(self, api_id: int, api_hash: str):
        self.clients = [
            TelegramClient('checker_session_1', api_id, api_hash),
            TelegramClient('checker_session_2', api_id, api_hash)
        ]
        self.current_idx = 0 
        self.last_userrate_call = 0  
        
    async def start(self):
        for i, client in enumerate(self.clients, 1):
            logging.info(f"Запуск клиента {i}/2...")
            await client.start()
            logging.info(f"✅ Telethon клиент {i} успешно авторизован.")

    async def is_username_free(self, username: str) -> bool:
        client = self.clients[self.current_idx]
        self.current_idx = (self.current_idx + 1) % len(self.clients)
        
        try:
            result = await asyncio.wait_for(client(CheckUsernameRequest(username=username)), timeout=7.0)
            return result
        except asyncio.TimeoutError:
            logging.warning(f"⏰ Таймаут проверки Telegram для @{username}. Пропуск.")
            return False
        except FloodWaitError as e:
            logging.warning(f"⚠️ FloodWait: Пауза {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return False
        except Exception:
            return False

    async def get_external_data(self, username: str) -> tuple[str, str]:
        now = time.time()
        time_passed = now - self.last_userrate_call
        if time_passed < 45.0:
            wait_time = 45.0 - time_passed
            logging.info(f"⏳ Ожидание {wait_time:.1f} сек. из-за ограничений @UserRate_bot...")
            await asyncio.sleep(wait_time)
            
        self.last_userrate_call = time.time()
        
        client = self.clients[self.current_idx]
        target_bot = "@UserRate_bot"
        
        async def _fetch():
            await client.send_message(target_bot, f"@{username}")
            await asyncio.sleep(15.0)  
            
            messages = await client.get_messages(target_bot, limit=5)
            rank_info, potential_info = "Не найден", "Не найден"
            
            if messages:
                for message in messages:
                    if not message.text or message.out:
                        continue
                        
                    text = message.text
                    lines = text.split("\n")
                    found_keywords = False
                    for line in lines:
                        line_lower = line.lower()
                        if "ранг" in line_lower or "rank" in line_lower:
                            rank_info = line.strip()
                            found_keywords = True
                        elif "потенциал" in line_lower or "potential" in line_lower:
                            potential_info = line.strip()
                            found_keywords = True
                    if found_keywords: break
                        
                if rank_info == "Не найден" and potential_info == "Не найден":
                    for message in messages:
                        if not message.out and message.text:
                            lines = message.text.split("\n")
                            rank_info = lines[0].strip()
                            potential_info = lines[1].strip() if len(lines) > 1 else "Не определен"
                            break

            return rank_info, potential_info

        try:
            return await asyncio.wait_for(_fetch(), timeout=25.0)
        except asyncio.TimeoutError:
            return "Таймаут (Нет ответа)", "Таймаут (Нет ответа)"
        except Exception as e:
            logging.error(f"❌ Ошибка парсинга @UserRate_bot: {e}")
            return "Ошибка", "Ошибка"

# =====================================
# 5. AIOGRAM: ИНТЕРФЕЙС И ФОНОВЫЙ ПРОЦЕСС
# ==========================================
router = Router()
search_task = None

def get_keyboard():
    # Полностью кнопочный интерфейс
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать поиск", callback_data="start"),
         InlineKeyboardButton(text="⏸ Остановить поиск", callback_data="stop")],
        [InlineKeyboardButton(text="📊 Топ-10", callback_data="top"), 
         InlineKeyboardButton(text="⭐ Избранное", callback_data="view_favs")]
    ])

def get_found_keyboard(username: str, score: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Добавить в избранное", callback_data=f"fav_{username}_{score}")]
    ])

async def process_free_username(word, loc_score, is_dict, read_score, checker, engine, bot, chat_id):
    try:
        logging.info(f"🔥 Найдено свободное имя: @{word}! Запрашиваем внешнюю аналитику...")
        frag_score = await engine.fragment_score(word)
        total_score = loc_score + frag_score
        
        await save_username(word, total_score, is_dict, read_score, frag_score)
        
        ton_price = estimate_ton_price(word)
        rank, potential = await checker.get_external_data(word)
        
        msg = (f"🔥 <b>НАЙДЕН ЮЗЕРНЕЙМ:</b> @{word}\n"
               f"💰 <b>Примерная цена:</b> {ton_price}\n\n"
               f"📊 Наш общий балл: <b>{total_score}/100</b>\n"
               f"📖 В словаре: {'Да' if is_dict else 'Нет'}\n"
               f"🗣 Читаемость: {read_score}/30\n"
               f"💎 Fragment: {frag_score}/30\n\n"
               f"👑 <b>Данные @UserRate_bot:</b>\n"
               f"🔹 {rank}\n"
               f"⚡ {potential}")
        
        await bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=get_found_keyboard(word, total_score))
    except Exception as e:
        logging.error(f"Ошибка в фоновом процессоре имени @{word}: {e}")

async def search_worker(checker: MultiSessionChecker, engine: Engine, bot: Bot, chat_id: int):
    try:
        while True:
            word = engine.generate_word()
            logging.info(f"🔍 Проверка юзернейма: @{word}")

            loc_score, is_dict, read_score = engine.local_score(word)
            
            # Если оценка 0 (не прошел пороги), пропускаем
            if loc_score == 0:
                logging.info(f"   ↳ Пропущен локально (Не прошел фильтр по баллам для длины {len(word)})")
                await asyncio.sleep(WORKER_SLEEP)
                continue 

            is_free = await checker.is_username_free(word)
            
            if is_free:
                asyncio.create_task(
                    process_free_username(word, loc_score, is_dict, read_score, checker, engine, bot, chat_id)
                )
            else:
                logging.info(f"   ↳ Юзернейм @{word} занят в Telegram.")

            await asyncio.sleep(WORKER_SLEEP)

    except asyncio.CancelledError:
        logging.info("Поиск принудительно остановлен.")

@router.message(CommandStart())
async def cmd_start(message: Message):
    global msg_lock
    if message.text != "/start":
        return
        
    async with msg_lock:
        await message.answer(
            "👋 Панель управления снайпером юзернеймов.", 
            reply_markup=get_keyboard()
        )

@router.callback_query(F.data == "start")
async def start_search(cb: CallbackQuery, bot: Bot, checker: MultiSessionChecker, engine: Engine):
    global search_task, msg_lock
    async with msg_lock:
        if search_task and not search_task.done():
            return await cb.answer("⏳ Поиск уже идет!", show_alert=True)
        
        search_task = asyncio.create_task(search_worker(checker, engine, bot, cb.message.chat.id))
        await cb.message.edit_text(
            "▶️ <b>Поиск запущен!</b>\nСледи за обновлением логов каждые 15 секунд.", 
            reply_markup=get_keyboard(), 
            parse_mode="HTML"
        )
        await cb.answer()

@router.callback_query(F.data == "stop")
async def stop_search(cb: CallbackQuery):
    global search_task, msg_lock
    async with msg_lock:
        if search_task and not search_task.done():
            search_task.cancel()
            await cb.message.edit_text("⏸ <b>Поиск остановлен.</b>", reply_markup=get_keyboard(), parse_mode="HTML")
        else:
            await cb.answer("Поиск не активен.", show_alert=True)
        await cb.answer()

@router.callback_query(F.data == "top")
async def show_top(cb: CallbackQuery):
    global msg_lock
    async with msg_lock:
        top_list = await get_top_10()
        if not top_list:
            return await cb.answer("База пока пуста. Запустите поиск.", show_alert=True)
        
        text = "📊 <b>ТОП-10 ЮЗЕРНЕЙМОВ:</b>\n\n"
        # Распаковка строго 3 переменных, так как SELECT возвращает 3 столбца
        for i, (username, score, is_dict) in enumerate(top_list, 1):
            icon = "📖" if is_dict else "🎲"
            text += f"{i}. @{username} — {score} баллов {icon}\n"
            
        await cb.message.edit_text(text, reply_markup=get_keyboard(), parse_mode="HTML")
        await cb.answer()

@router.callback_query(F.data.startswith("fav_"))
async def handle_add_favorite(cb: CallbackQuery):
    parts = cb.data.split("_")
    username = parts[1]
    score = int(parts[2])
    
    success = await add_to_favorites(username, score)
    if success:
        await cb.answer(f"⭐ @{username} добавлен в Избранное!", show_alert=False)
        await cb.message.edit_reply_markup(reply_markup=None)
    else:
        await cb.answer("Ошибка при сохранении.", show_alert=True)

@router.callback_query(F.data == "view_favs")
async def show_favorites(cb: CallbackQuery):
    global msg_lock
    async with msg_lock:
        favs = await get_favorites()
        if not favs:
            return await cb.answer("Список избранного пуст.", show_alert=True)
        
        text = "⭐ <b>ВАШЕ ИЗБРАННОЕ:</b>\n\n"
        for i, (username, score) in enumerate(favs, 1):
            text += f"{i}. @{username} (Балл: {score})\n"
            
        await cb.message.edit_text(text, reply_markup=get_keyboard(), parse_mode="HTML")
        await cb.answer()

# ==========================================
# 6. ТОЧКА ВХОДА
# ==========================================
async def main():
    global msg_lock
    msg_lock = asyncio.Lock()  
    
    await init_db()
    
    # Использование AiohttpSession для избежания "Unclosed client session"
    session = AiohttpSession()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(), session=session)
    
    dp = Dispatcher()
    dp.include_router(router)
    
    checker = MultiSessionChecker(API_ID, API_HASH)
    engine = Engine()
    
    dp.workflow_data.update({"checker": checker, "engine": engine})
    
    await checker.start() 
    
    print("✅ Бот готов к работе. Напиши /start в Telegram.")
    
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot, handle_as_tasks=True, relaxation=0.5)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
    
