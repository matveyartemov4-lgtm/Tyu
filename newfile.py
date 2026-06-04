import asyncio
import logging
import random
import os
import time

import aiohttp
import aiosqlite
from english_words import get_english_words_set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest

# ==========================================
# 1. КОНФИГУРАЦИЯ (ВСТАВЬТЕ СВОИ ДАННЫЕ)
# ==========================================
BOT_TOKEN = "8932397702:AAGY7wdLp4dy96MeSH1lce86HqzClmmMEqk"
API_ID = 33248398
API_HASH = "6543087387b7b14fcafcca74d28b1158"

# Параметры поиска
MIN_LOCAL_SCORE = 30  
WORKER_SLEEP = 15      
DB_PATH = "usernames_v3.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def estimate_ton_price(username: str) -> str:
    ln = len(username)
    if ln <= 4: return "~1500+ TON"
    if ln == 5: return "~400-900 TON"
    if ln == 6: return "~150-350 TON"
    if ln == 7: return "~50-180 TON"
    if ln == 8: return "~20-60 TON"
    return "~5-15 TON"

# ==========================================
# 2. БАЗА ДАННЫХ
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS usernames (
            username TEXT PRIMARY KEY,
            total_score INTEGER,
            is_dict INTEGER,
            readability INTEGER,
            fragment_score INTEGER
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS favorites (username TEXT PRIMARY KEY, score INTEGER)")
        await db.commit()

async def save_to_db(u, t, d, r, f):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO usernames VALUES (?, ?, ?, ?, ?)", (u, t, int(d), r, f))
        await db.commit()

# ==========================================
# 3. ДВИЖОК ГЕНЕРАЦИИ (5, 6, 7, 8 СИМВОЛОВ)
# ==========================================
class Engine:
    def __init__(self):
        logging.info("Загрузка словаря...")
        words = get_english_words_set(['web2'], lower=True)
        self.dict_words = {w for w in words if len(w) in [5, 6, 7, 8] and w.isalpha()}
        self.vowels = set("aeiouy")
        self.consonants = "bcdfghjklmnpqrstvwxz"

    def generate_word(self) -> str:
        # Распределение вероятности длин
        length = random.choices([5, 6, 7, 8], weights=[30, 30, 30, 10])[0]
        if random.random() > 0.4:
            ws = [w for w in self.dict_words if len(w) == length]
            if ws: return random.choice(ws)
        return "".join(random.choice(self.consonants + "aeiou") for _ in range(length))

    def local_score(self, word: str) -> tuple[int, bool, int]:
        ln = len(word)
        is_dict = word in self.dict_words
        score = 40 if is_dict else 0
        read = 30
        for i in range(len(word) - 2):
            if sum(1 for c in word[i:i+3] if c in self.vowels) in [0, 3]: read -= 15
        
        total = score + max(0, read)
        
        # Условия из ТЗ: пороги баллов для разных длин
        if ln == 6 and total < 60: return 0, False, 0
        if (ln == 7 or ln == 8) and total < 80: return 0, False, 0
        if ln == 5 and total < MIN_LOCAL_SCORE: return 0, False, 0
        
        return total, is_dict, max(0, read)

# ==========================================
# 4. TELETHON (БЕЗ ИНТЕРАКТИВНОГО ВВОДА)
# ==========================================
class MultiChecker:
    def __init__(self, aid, ahash):
        # Имя файла сессии должно совпадать с тем, что вы загрузили на Amvera
        self.client = TelegramClient('amvera_session', aid, ahash)
    
    async def start(self):
        logging.info("Подключение Telethon...")
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logging.error("!!! СЕССИЯ НЕ АВТОРИЗОВАНА !!!")
            logging.error("Создайте файл amvera_session.session на ПК и загрузите его на сервер.")
            # Не вызываем input(), чтобы не вешать сервер
            return False
        logging.info("Telethon успешно авторизован.")
        return True

    async def is_free(self, user):
        try:
            # Проверка доступности
            result = await self.client(CheckUsernameRequest(user))
            return result
        except Exception as e:
            logging.error(f"Ошибка проверки {user}: {e}")
            return False

# ==========================================
# 5. AIOGRAM ИНТЕРФЕЙС
# ==========================================
router = Router()
search_task = None

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ СТАРТ", callback_data="start"),
         InlineKeyboardButton(text="⏸ СТОП", callback_data="stop")],
        [InlineKeyboardButton(text="📊 ТОП-10", callback_data="top"),
         InlineKeyboardButton(text="⭐ ИЗБРАННОЕ", callback_data="favs")]
    ])

async def search_worker(checker, engine, bot, chat_id):
    try:
        while True:
            word = engine.generate_word()
            l_score, is_dict, read = engine.local_score(word)
            
            if l_score > 0:
                if await checker.is_free(word):
                    f_score = random.randint(10, 30) # Имитация доп. баллов (Fragment/UserRate)
                    total = l_score + f_score
                    ton = estimate_ton_price(word)
                    await save_to_db(word, total, is_dict, read, f_score)
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⭐ В Избранное", callback_data=f"add_{word}_{total}")]
                    ])
                    
                    await bot.send_message(
                        chat_id, 
                        f"🔥 <b>НАЙДЕНО: @{word}</b>\n💰 Оценка: {ton}\n🏆 Балл: {total}/100\n📏 Длина: {len(word)}", 
                        reply_markup=kb
                    )
            await asyncio.sleep(WORKER_SLEEP)
    except asyncio.CancelledError:
        logging.info("Поток поиска остановлен.")

@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer("🕹 Бот запущен на сервере Amvera.\nПоиск имен от 5 до 8 символов.", reply_markup=main_kb())

@router.callback_query(F.data == "start")
async def start_cb(cb: CallbackQuery, bot, checker, engine):
    global search_task
    if not search_task or search_task.done():
        search_task = asyncio.create_task(search_worker(checker, engine, bot, cb.message.chat.id))
        await cb.answer("Поиск запущен!")
        await cb.message.edit_text("✅ Поиск активен и генерирует варианты...", reply_markup=main_kb())
    else:
        await cb.answer("Поиск уже в процессе.")

@router.callback_query(F.data == "stop")
async def stop_cb(cb: CallbackQuery):
    global search_task
    if search_task:
        search_task.cancel()
        await cb.answer("Поиск остановлен.")
        await cb.message.edit_text("⏸ Поиск на паузе.", reply_markup=main_kb())

@router.callback_query(F.data == "top")
async def top_cb(cb: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, total_score, is_dict FROM usernames ORDER BY total_score DESC LIMIT 10") as cur:
            rows = await cur.fetchall()
            if not rows: return await cb.answer("База пока пуста.")
            res = "📊 <b>ТОП-10 НАХОДОК:</b>\n\n"
            for r in rows:
                res += f"@{r[0]} — {r[1]} б. {'📖' if r[2] else ''}\n"
            await cb.message.answer(res)

@router.callback_query(F.data.startswith("add_"))
async def add_fav(cb: CallbackQuery):
    _, user, sc = cb.data.split("_")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?)", (user, int(sc)))
        await db.commit()
    await cb.answer("Добавлено в избранное!")

async def main():
    await init_db()
    async with AiohttpSession() as session:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"), session=session)
        dp = Dispatcher()
        dp.include_router(router)
        
        checker = MultiChecker(API_ID, API_HASH)
        if not await checker.start():
            return # Останавливаем запуск, если нет авторизации

        engine = Engine()
        logging.info("Бот полностью готов к работе.")
        await dp.start_polling(bot, checker=checker, engine=engine)

if __name__ == "__main__":
    asyncio.run(main())
    
