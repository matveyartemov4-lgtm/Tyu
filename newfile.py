print("⏳ Скрипт запускается... Загрузка библиотек (это может занять 10-20 секунд).")

import asyncio
import logging
import random
import re
import os

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup
from english_words import get_english_words_set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart

from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.errors import FloodWaitError, UsernameInvalidError

# ==========================================
# 1. КОНФИГУРАЦИЯ (Данные вставлены)
# ==========================================
BOT_TOKEN = "8932397702:AAGY7wdLp4dy96MeSH1lce86HqzClmmMEqk"
API_ID = 33248398
API_HASH = "6543087387b7b14fcafcca74d28b1158"

MIN_LOCAL_SCORE = 30  
WORKER_SLEEP = 30      

# Автоматический выбор пути: Amvera (/data) или локальный Pydroid
if os.path.exists("/data") and os.access("/data", os.W_OK):
    DB_PATH = "/data/usernames.db"
else:
    DB_PATH = "usernames.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Переменная для защиты от дублирования кнопок
msg_lock = None 

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
        self.dict_words = {w for w in words if len(w) == 5 and w.isalpha()}
        self.vowels = set("aeiouy")
        print("✅ Словарь загружен!")

    def generate_word(self) -> str:
        if random.random() > 0.5 and self.dict_words:
            return random.choice(list(self.dict_words))
        else:
            c = "bcdfghjklmnpqrstvwxz"
            v = "aeiouy"
            return random.choice(c) + random.choice(v) + random.choice(c) + random.choice(v) + random.choice(c)

    def local_score(self, word: str) -> tuple[int, bool, int]:
        is_dict = word in self.dict_words
        dict_score = 40 if is_dict else 0
        
        read_score = 30
        for i in range(len(word) - 2):
            chunk = word[i:i+3]
            v_count = sum(1 for char in chunk if char in self.vowels)
            if v_count == 3 or v_count == 0:
                read_score -= 15
        read_score = max(0, read_score)
        
        return dict_score + read_score, is_dict, read_score

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
# 4. РОТАЦИЯ СЕССИЙ (Multi-Session)
# ==========================================
class MultiSessionChecker:
    def __init__(self, api_id: int, api_hash: str):
        self.clients = [
            TelegramClient('checker_session_1', api_id, api_hash),
            TelegramClient('checker_session_2', api_id, api_hash)
        ]
        self.current_idx = 0 
        
    async def start(self):
        for i, client in enumerate(self.clients, 1):
            logging.info(f"Запуск клиента {i}/2... (Следуй инструкциям в консоли)")
            await client.start()
            logging.info(f"✅ Telethon клиент {i} успешно авторизован.")
                async def is_username_free(self, username: str) -> bool:
        client = self.clients[self.current_idx]
        self.current_idx = (self.current_idx + 1) % len(self.clients)
        
        try:
            # Используем более простой метод проверки
            result = await client(CheckUsernameRequest(username=username))
            return result
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            return False
        except Exception:
            # Теперь бот не будет забивать логи ошибками, а просто пропустит имя
            return False
            
# ==========================================
# 5. AIOGRAM: ИНТЕРФЕЙС
# ==========================================
router = Router()
search_task = None

def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать поиск", callback_data="start")],
        [InlineKeyboardButton(text="⏸ Остановить поиск", callback_data="stop")],
        [InlineKeyboardButton(text="📊 Рейтинг (Топ 10)", callback_data="top")]
    ])

async def search_worker(checker: MultiSessionChecker, engine: Engine, bot: Bot, chat_id: int):
    try:
        while True:
            word = engine.generate_word()
            
            loc_score, is_dict, read_score = engine.local_score(word)
            if loc_score < MIN_LOCAL_SCORE:
                continue 

            logging.info(f"🔍 Проверка: @{word} (Лок. балл: {loc_score})")

            is_free = await checker.is_username_free(word)
            
            if is_free:
                frag_score = await engine.fragment_score(word)
                total_score = loc_score + frag_score
                
                await save_username(word, total_score, is_dict, read_score, frag_score)
                msg = (f"🔥 <b>НАЙДЕН ЮЗЕРНЕЙМ:</b> @{word}\n"
                       f"📊 Общий балл: <b>{total_score}/100</b>\n"
                       f"📖 В словаре: {'Да' if is_dict else 'Нет'}\n"
                       f"🗣 Читаемость: {read_score}/30\n"
                       f"💎 Fragment: {frag_score}/30")
                await bot.send_message(chat_id, msg, parse_mode="HTML")

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
            "👋 Панель управления снайпером юзернеймов (Multi-Session).", 
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
            "▶️ <b>Поиск запущен!</b>\nНагрузка распределяется между 2 аккаунтами.", 
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
        for i, (username, score, is_dict) in enumerate(top_list, 1):
            icon = "📖" if is_dict else "🎲"
            text += f"{i}. @{username} — {score} баллов {icon}\n"
            
        await cb.message.edit_text(text, reply_markup=get_keyboard(), parse_mode="HTML")
        await cb.answer()

# ==========================================
# 6. ТОЧКА ВХОДА
# ==========================================
async def main():
    global msg_lock
    msg_lock = asyncio.Lock()  # Инициализация замка внутри Event Loop
    
    await init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    checker = MultiSessionChecker(API_ID, API_HASH)
    engine = Engine()
    
    dp.workflow_data.update({"checker": checker, "engine": engine})
    
    await checker.start() 
    
    print("✅ Бот готов к работе. Напиши /start в Telegram.")
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
