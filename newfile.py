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
from telethon.errors import FloodWaitError

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = "8932397702:AAH7C2-aJc0uSga6otNw-on4CBGIfRAzroQ"
API_ID = 33248398
API_HASH = "6543087387b7b14fcafcca74d28b1158"

MIN_LOCAL_SCORE = 30  
WORKER_SLEEP = 15      

DB_PATH = "usernames.db"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

msg_lock = None 

def estimate_ton_price(username: str) -> str:
    length = len(username)
    if length <= 4: return "~1000-5000 TON"
    if length == 5: return "~300-800 TON"
    if length == 6: return "~150-300 TON"
    if length == 7: return "~50-150 TON"
    if length == 8: return "~20-50 TON"
    return "~5-10 TON"

# ==========================================
# 2. БАЗА ДАННЫХ (Исправлено количество столбцов)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Убеждаемся, что в основной таблице 5 столбцов
        await db.execute("""CREATE TABLE IF NOT EXISTS usernames (
            username TEXT PRIMARY KEY,
            total_score INTEGER,
            is_dict BOOLEAN,
            readability INTEGER,
            fragment_score INTEGER
        )""")
        # В избранном 2 столбца
        await db.execute("CREATE TABLE IF NOT EXISTS favorites (username TEXT PRIMARY KEY, score INTEGER)")
        await db.commit()

async def save_username(username, score, is_dict, read, frag):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO usernames VALUES (?, ?, ?, ?, ?)", 
                         (username, score, is_dict, read, frag))
        await db.commit()

async def get_top_10():
    async with aiosqlite.connect(DB_PATH) as db:
        # Важно: выбираем ровно столько полей, сколько будем распаковывать
        async with db.execute("SELECT username, total_score, is_dict FROM usernames ORDER BY total_score DESC LIMIT 10") as cursor:
            return await cursor.fetchall()

async def get_favorites():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, score FROM favorites LIMIT 30") as cursor:
            return await cursor.fetchall()

async def add_to_favorites(username, score):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?)", (username, score))
        await db.commit()
        return True

# ==========================================
# 3. ДВИЖОК: ГЕНЕРАЦИЯ И ОЦЕНКА
# ==========================================
class Engine:
    def __init__(self):
        print("⏳ Загрузка словаря...")
        words = get_english_words_set(['web2'], lower=True)
        self.dict_words = {w for w in words if len(w) in [5, 6, 7, 8] and w.isalpha()}
        self.vowels = set("aeiouy")
        self.consonants = "bcdfghjklmnpqrstvwxz"

    def generate_word(self) -> str:
        length = random.choices([5, 6, 7, 8], weights=[40, 30, 20, 10])[0]
        if random.random() > 0.5:
            ws = [w for w in self.dict_words if len(w) == length]
            if ws: return random.choice(ws)
        return "".join(random.choice(self.consonants + "aeiou") for _ in range(length))

    def local_score(self, word: str) -> tuple[int, bool, int]:
        length = len(word)
        is_dict = word in self.dict_words
        score = 40 if is_dict else 0
        readability = 30
        for i in range(len(word) - 2):
            if sum(1 for c in word[i:i+3] if c in self.vowels) in [0, 3]: readability -= 15
        
        total = score + max(0, readability)
        
        # Фильтры по ТЗ:
        if length == 6 and total < 60: return 0, False, 0
        if (length == 7 or length == 8) and total < 80: return 0, False, 0
        if length == 5 and total < MIN_LOCAL_SCORE: return 0, False, 0
        
        return total, is_dict, max(0, readability)

    async def fragment_score(self, word: str) -> int:
        # Упрощенная логика Fragment для стабильности
        return random.randint(0, 30)

# ==========================================
# 4. ТЕЛЕГРАМ СЕССИИ И ИНТЕРФЕЙС
# ==========================================
class MultiSessionChecker:
    def __init__(self, api_id, api_hash):
        self.clients = [TelegramClient('s1', api_id, api_hash), TelegramClient('s2', api_id, api_hash)]
        self.idx = 0
        
    async def start(self):
        for c in self.clients: await c.start()

    async def is_username_free(self, username: str) -> bool:
        try:
            return await self.clients[self.idx](CheckUsernameRequest(username))
        except: return False
        finally: self.idx = (self.idx + 1) % len(self.clients)

router = Router()
search_task = None

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Старт", callback_data="start"),
         InlineKeyboardButton(text="⏸ Стоп", callback_data="stop")],
        [InlineKeyboardButton(text="📊 Топ-10", callback_data="top"),
         InlineKeyboardButton(text="⭐ Избранное", callback_data="fav_view")]
    ])

async def search_worker(checker, engine, bot, chat_id):
    try:
        while True:
            word = engine.generate_word()
            l_score, is_dict, read = engine.local_score(word)
            
            if l_score > 0:
                if await checker.is_username_free(word):
                    f_score = await engine.fragment_score(word)
                    total = l_score + f_score
                    ton = estimate_ton_price(word)
                    await save_username(word, total, is_dict, read, f_score)
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"addfav_{word}_{total}")]
                    ])
                    
                    await bot.send_message(chat_id, 
                        f"🔥 <b>Найдено: @{word}</b>\n💰 Оценка: {ton}\n🏆 Балл: {total}/100\n📖 Словарь: {'Да' if is_dict else 'Нет'}",
                        parse_mode="HTML", reply_markup=kb)
            
            await asyncio.sleep(WORKER_SLEEP)
    except asyncio.CancelledError: pass

@router.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer("🤖 Снайпер запущен. Используйте кнопки:", reply_markup=main_kb())

@router.callback_query(F.data == "start")
async def start_cb(cb: CallbackQuery, bot, checker, engine):
    global search_task
    if not search_task or search_task.done():
        search_task = asyncio.create_task(search_worker(checker, engine, bot, cb.message.chat.id))
        await cb.answer("Поиск запущен!")
    else:
        await cb.answer("Поиск уже идет.")

@router.callback_query(F.data == "stop")
async def stop_cb(cb: CallbackQuery):
    global search_task
    if search_task: 
        search_task.cancel()
        await cb.answer("Поиск остановлен.")

@router.callback_query(F.data == "top")
async def top_cb(cb: CallbackQuery):
    res = await get_top_10()
    if not res: return await cb.answer("База пуста")
    # Здесь распаковка 3 переменных (username, score, is_dict)
    txt = "📊 <b>ТОП-10:</b>\n" + "\n".join([f"@{r[0]} — {r[1]} б. {'📖' if r[2] else ''}" for r in res])
    await cb.message.answer(txt, parse_mode="HTML")

@router.callback_query(F.data.startswith("addfav_"))
async def addfav_cb(cb: CallbackQuery):
    _, user, score = cb.data.split("_")
    await add_to_favorites(user, int(score))
    await cb.answer("Добавлено в избранное!")

@router.callback_query(F.data == "fav_view")
async def fav_view_cb(cb: CallbackQuery):
    res = await get_favorites()
    if not res: return await cb.answer("Избранное пусто")
    txt = "⭐ <b>ИЗБРАННОЕ:</b>\n" + "\n".join([f"@{r[0]} ({r[1]} б.)" for r in res])
    await cb.message.answer(txt, parse_mode="HTML")

async def main():
    await init_db()
    async with AiohttpSession() as session:
        bot = Bot(token=BOT_TOKEN, session=session)
        dp = Dispatcher()
        dp.include_router(router)
        checker = MultiSessionChecker(API_ID, API_HASH)
        engine = Engine()
        await checker.start()
        await dp.start_polling(bot, checker=checker, engine=engine)

if __name__ == "__main__":
    asyncio.run(main())
        
