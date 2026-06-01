import asyncio
import logging
import random
import re
from typing import List, Dict, Optional

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup
from english_words import get_english_words_set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart

from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import FloodWaitError, UsernameInvalidError

# ==========================================
# 1. КОНФИГУРАЦИЯ (Заполни своими данными)
# ==========================================
BOT_TOKEN = "8932397702:AAGAIflL_RdUYXXFzZ5xPUH-FaWjgmaRvec"
API_ID = 33248398       # Твой API ID от my.telegram.org
API_HASH = "6543087387b7b14fcafcca74d28b1158"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==========================================
# 2. БАЗА ДАННЫХ (SQLite)
# ==========================================
async def init_db():
    """Создает таблицу для хранения юзернеймов, если её нет"""
    async with aiosqlite.connect("found_usernames.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usernames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                total_score INTEGER,
                is_dict BOOLEAN,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def save_to_db(username: str, score: int, is_dict: bool):
    """Безопасно сохраняет юзернейм в базу данных"""
    try:
        async with aiosqlite.connect("found_usernames.db") as db:
            await db.execute(
                "INSERT OR IGNORE INTO usernames (username, total_score, is_dict) VALUES (?, ?, ?)",
                (username, score, is_dict)
            )
            await db.commit()
    except Exception as e:
        logging.error(f"Ошибка записи в БД: {e}")

async def get_top_from_db() -> List[Dict]:
    """Достает ТОП-50 юзернеймов из базы данных"""
    async with aiosqlite.connect("found_usernames.db") as db:
        async with db.execute(
            "SELECT username, total_score, is_dict FROM usernames ORDER BY total_score DESC LIMIT 50"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"username": row[0], "total_score": row[1], "is_dict": bool(row[2])}
                for row in rows
            ]

# ==========================================
# 3. ГЕНЕРАТОР И СЛОВАРЬ
# ==========================================
class UsernameGenerator:
    def __init__(self):
        words = get_english_words_set(['web2'], lower=True)
        self.dictionary_words = {w for w in words if len(w) == 5 and w.isalpha()}
        self.used_combinations = set()

    def get_next_batch(self, batch_size=10) -> List[str]:
        batch = []
        available_words = list(self.dictionary_words - self.used_combinations)
        
        for _ in range(batch_size):
            if available_words:
                word = random.choice(available_words)
                available_words.remove(word)
            else:
                letters = "abcdefghijklmnopqrstuvwxyz"
                word = "".join(random.choices(letters, k=5))
            
            self.used_combinations.add(word)
            batch.append(word)
        return batch

# ==========================================
# 4. СИСТЕМА ОЦЕНКИ
# ==========================================
class Scorer:
    def __init__(self, dictionary_words: set):
        self.dictionary_words = dictionary_words

    def check_readability(self, word: str) -> int:
        vowels = set("aeiouy")
        score = 30
        for i in range(len(word) - 2):
            chunk = word[i:i+3]
            v_count = sum(1 for c in chunk if c in vowels)
            if v_count == 3 or v_count == 0:
                score -= 15
        return max(0, score)

    async def parse_fragment_value(self, word: str) -> int:
        url = f"https://fragment.com/?query={word}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    price_elements = soup.find_all('div', class_='table-cell-value tm-value icon-before icon-ton')
                    
                    if not price_elements:
                        return 0
                    
                    prices = []
                    for el in price_elements:
                        match = re.search(r'([\d,]+)', el.text)
                        if match:
                            prices.append(int(match.group(1).replace(',', '')))
                    
                    if prices:
                        avg_price = sum(prices) / len(prices)
                        if avg_price > 1000: return 30
                        if avg_price > 500: return 20
                        if avg_price > 100: return 10
                    return 0
        except Exception as e:
            logging.error(f"Fragment parse error: {e}")
            return 0

    async def evaluate(self, username: str) -> Dict:
        dict_score = 40 if username in self.dictionary_words else 0
        read_score = self.check_readability(username)
        frag_score = await self.parse_fragment_value(username)
        
        total = dict_score + read_score + frag_score
        return {
            "username": username,
            "total_score": total,
            "is_dict": dict_score > 0,
            "readability": read_score,
            "fragment_potential": frag_score
        }

# ==========================================
# 5. TELETHON CHECKER (С жесткой защитой)
# ==========================================
class UserSessionChecker:
    def __init__(self, api_id: int, api_hash: str):
        self.client = TelegramClient('checker_session', api_id, api_hash)
        
    async def start(self):
        await self.client.start()
        logging.info("Telethon клиент успешно авторизован.")

    async def check_username(self, username: str) -> bool:
        try:
            result = await self.client(CheckUsernameRequest(username=username))
            return result
        except FloodWaitError as e:
            logging.warning(f"⚠️ FloodWait! Засыпаем на {e.seconds} сек.")
            await asyncio.sleep(e.seconds + 5)
            return await self.check_username(username)
        except UsernameInvalidError:
            return False
        except RPCError as e:
            logging.error(f"🛑 Telegram API заблокировал запрос (caused by CheckUsernameRequest): {e}")
            logging.warning("Включаем аварийную паузу на 5 минут (300 секунд)...")
            await asyncio.sleep(300) 
            return False
        except Exception as e:
            logging.error(f"Неизвестная ошибка проверки {username}: {e}")
            return False

# ==========================================
# 6. ИНТЕРФЕЙС И ЛОГИКА БОТА
# ==========================================
router = Router()
search_task: Optional[asyncio.Task] = None

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать поиск", callback_data="start_search")],
        [InlineKeyboardButton(text="⏸ Остановить поиск", callback_data="stop_search")],
        [InlineKeyboardButton(text="📊 Рейтинг (Топ)", callback_data="show_top")]
    ])

async def search_worker(checker: UserSessionChecker, generator: UsernameGenerator, scorer: Scorer, chat_id: int, bot: Bot):
    try:
        while True:
            batch = generator.get_next_batch(1)
            username = batch[0]
            
            logging.info(f"Проверяю юзернейм: {username}...")
            is_available = await checker.check_username(username)
            
            if is_available:
                score_data = await scorer.evaluate(username)
                
                # Сохраняем в SQLite
                await save_to_db(username, score_data['total_score'], score_data['is_dict'])
                
                msg = (f"🔥 <b>Найден свободный юзернейм:</b> @{username}\n"
                       f"📊 Оценка: {score_data['total_score']}/100\n"
                       f"Словарное: {'Да' if score_data['is_dict'] else 'Нет'}")
                await bot.send_message(chat_id, msg)
            
            # Задержка 12 секунд для безопасности сессии
            await asyncio.sleep(12) 
            
    except asyncio.CancelledError:
        logging.info("Поиск был принудительно остановлен.")

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я сканер 5-буквенных юзернеймов.\n"
        "Управление осуществляется кнопками ниже.",
        reply_markup=get_main_keyboard()
    )

@router.callback_query(F.data == "start_search")
async def process_start_search(callback: CallbackQuery, bot: Bot, checker: UserSessionChecker, generator: UsernameGenerator, scorer: Scorer):
    global search_task
    if search_task and not search_task.done():
        await callback.answer("⏳ Поиск уже запущен!", show_alert=True)
        return

    search_task = asyncio.create_task(search_worker(checker, generator, scorer, callback.message.chat.id, bot))
    await callback.message.edit_text("▶️ <b>Поиск запущен!</b>\nЯ уведомлю вас при нахождении свободных вариантов.", reply_markup=get_main_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "stop_search")
async def process_stop_search(callback: CallbackQuery):
    global search_task
    if search_task and not search_task.done():
        search_task.cancel()
        await callback.message.edit_text("⏸ <b>Поиск остановлен.</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")
    else:
        await callback.answer("⚠️ Поиск не активен.", show_alert=True)

@router.callback_query(F.data == "show_top")
async def process_show_top(callback: CallbackQuery):
    top_usernames = await get_top_from_db()
    
    if not top_usernames:
        await callback.answer("🤷‍♂️ База данных пока пуста. Запустите поиск.", show_alert=True)
        return
    
    text = "📊 <b>ТОП НАЙДЕННЫХ ЮЗЕРНЕЙМОВ (Из БД):</b>\n\n"
    for i, item in enumerate(top_usernames[:15], 1):
        dict_label = "📖" if item['is_dict'] else "🎲"
        text += f"{i}. @{item['username']} — {item['total_score']} б. {dict_label}\n"
        
    await callback.message.edit_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

# ==========================================
# 7. ТОЧКА ВХОДА
# ==========================================
async def main():
    await init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    checker = UserSessionChecker(API_ID, API_HASH)
    generator = UsernameGenerator()
    scorer = Scorer(generator.dictionary_words)
    
    dp.workflow_data.update({
        "checker": checker,
        "generator": generator,
        "scorer": scorer,
        "bot": bot
    })
    
    await checker.start()
    
    logging.info("Бот и SQLite успешно запущены.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
