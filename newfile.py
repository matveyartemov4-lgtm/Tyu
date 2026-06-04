print("⏳ Скрипт запускается... Загрузка библиотек (это может занять 10-20 секунд).")

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
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties

from telethon import TelegramClient
from telethon.tl.functions.account import CheckUsernameRequest
from telethon.errors import FloodWaitError

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN  = "8944531759:AAGLOXqzcM-25zXlt7D-dQeutqULTEqmfx4"
API_ID     = 33248398
API_HASH   = "6543087387b7b14fcafcca74d28b1158"

MIN_LOCAL_SCORE = 30   # порог для 5-буквенных слов
WORKER_SLEEP    = 15

DB_PATH = "/data/usernames.db" if (os.path.exists("/data") and os.access("/data", os.W_OK)) else "usernames.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

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
                username       TEXT PRIMARY KEY,
                total_score    INTEGER,
                is_dict        BOOLEAN,
                readability    INTEGER,
                fragment_score INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                username TEXT PRIMARY KEY,
                score    INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checked_history (
                username   TEXT PRIMARY KEY,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def is_in_blacklist(username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM checked_history WHERE username = ?", (username,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def add_to_blacklist(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO checked_history (username) VALUES (?)", (username,)
        )
        await db.commit()


async def get_statistics() -> tuple:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM checked_history") as c1:
            total_checked = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM usernames") as c2:
            total_found = (await c2.fetchone())[0]
    return total_checked, total_found


async def save_username(username: str, score: int, is_dict: bool, read: int, frag: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO usernames VALUES (?, ?, ?, ?, ?)",
                (username, score, is_dict, read, frag),
            )
            await db.commit()
    except Exception as e:
        logging.error(f"Ошибка сохранения в БД: {e}")


async def add_to_favorites(username: str, score: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO favorites VALUES (?, ?)", (username, score)
            )
            await db.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления в избранное: {e}")
        return False


async def get_favorites():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, score FROM favorites LIMIT 30"
        ) as cursor:
            return await cursor.fetchall()


async def get_top_10():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, total_score, is_dict FROM usernames ORDER BY total_score DESC LIMIT 10"
        ) as cursor:
            return await cursor.fetchall()


# ==========================================
# 3. ДВИЖОК: ГЕНЕРАЦИЯ И ОЦЕНКА
# ==========================================
class Engine:
    def __init__(self):
        print("⏳ Загрузка словаря...")
        words = get_english_words_set(["web2"], lower=True)
        self.dict_words = {w for w in words if 5 <= len(w) <= 8 and w.isalpha()}
        self.vowels     = set("aeiouy")
        self.consonants = "bcdfghjklmnpqrstvwxz"
        print("✅ Словарь загружен!")

    def generate_word(self) -> str:
        if random.random() > 0.5 and self.dict_words:
            return random.choice(list(self.dict_words))
        length = random.choice([5, 6, 7, 8])
        word = ""
        for i in range(length):
            word += (
                random.choice(self.consonants) if i % 2 == 0
                else random.choice(list(self.vowels))
            )
        return word

    def local_score(self, word: str) -> tuple:
        is_dict    = word in self.dict_words
        dict_score = 40 if is_dict else 0
        read_score = 30
        for i in range(len(word) - 2):
            chunk   = word[i : i + 3]
            v_count = sum(1 for ch in chunk if ch in self.vowels)
            if v_count == 3 or v_count == 0:
                read_score -= 15
        read_score = max(0, read_score)
        return dict_score + read_score, is_dict, read_score

    async def fragment_score(self, word: str) -> tuple:
        url = f"https://fragment.com/?query={word}"

        async def _fetch():
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    return await response.text()

        try:
            html = await asyncio.wait_for(_fetch(), timeout=10.0)
        except asyncio.TimeoutError:
            logging.warning(f"⏰ Таймаут Fragment для @{word}")
            return 0, "Таймаут"
        except Exception as e:
            logging.error(f"Ошибка Fragment: {e}")
            return 0, "Ошибка API"

        if html is None:
            return 0, "Нет данных"

        try:
            soup = BeautifulSoup(html, "html.parser")
            price_elements = soup.find_all(
                "div",
                class_="table-cell-value tm-value icon-before icon-ton",
            )
            prices = []
            for el in price_elements:
                match = re.search(r"([\d,]+)", el.text)
                if match:
                    prices.append(int(match.group(1).replace(",", "")))

            if not prices:
                return 0, "Нет данных"

            avg_price = sum(prices) // len(prices)
            if avg_price >= 500: return 30, f"~{avg_price} TON"
            if avg_price >= 100: return 20, f"~{avg_price} TON"
            if avg_price > 10:   return 10, f"~{avg_price} TON"
            return 0, f"~{avg_price} TON (Дешево)"
        except Exception as e:
            logging.error(f"Ошибка парсинга Fragment: {e}")
            return 0, "Ошибка парсинга"


# ==========================================
# 4. РОТАЦИЯ СЕССИЙ + ИНТЕГРАЦИЯ С USERATE_BOT
# ==========================================
class MultiSessionChecker:
    def __init__(self, api_id: int, api_hash: str):
        self.clients = [
            TelegramClient("checker_session_1", api_id, api_hash),
            TelegramClient("checker_session_2", api_id, api_hash),
        ]
        self.current_idx        = 0
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
            result = await asyncio.wait_for(
                client(CheckUsernameRequest(username=username)), timeout=7.0
            )
            return result
        except asyncio.TimeoutError:
            logging.warning(f"⏰ Таймаут Telegram для @{username}. Пропуск.")
            return False
        except FloodWaitError as e:
            logging.warning(f"⚠️ FloodWait: Пауза {e.seconds} сек.")
            await asyncio.sleep(e.seconds)
            return False
        except Exception:
            return False

    async def get_external_data(self, username: str) -> tuple:
        now         = time.time()
        time_passed = now - self.last_userrate_call
        if time_passed < 45.0:
            wait_time = 45.0 - time_passed
            logging.info(f"⏳ Ожидание {wait_time:.1f} сек. из-за ограничений @UserRate_bot...")
            await asyncio.sleep(wait_time)

        self.last_userrate_call = time.time()
        client     = self.clients[self.current_idx]
        target_bot = "@UserRate_bot"

        async def _fetch():
            await client.send_message(target_bot, f"@{username}")
            logging.info(f"📤 @{username} отправлен @UserRate_bot. Ждём 15 секунд...")
            await asyncio.sleep(15.0)

            messages = await client.get_messages(target_bot, limit=5)
            rank_info, potential_info = "Не найден", "Не найден"
            if messages:
                for message in messages:
                    if not message.text or message.out:
                        continue
                    text = message.text.lower()
                    if any(k in text for k in ("ранг", "rank", "потенциал", "potential")):
                        for line in message.text.split("\n"):
                            ll = line.lower()
                            if "ранг" in ll or "rank" in ll:
                                rank_info = line.strip()
                            elif "потенциал" in ll or "potential" in ll:
                                potential_info = line.strip()
                        break
            return rank_info, potential_info

        try:
            return await asyncio.wait_for(_fetch(), timeout=25.0)
        except Exception as e:
            logging.error(f"❌ Ошибка парсинга @UserRate_bot: {e}")
            return "Ошибка", "Ошибка"


# ==========================================
# 5. AIOGRAM: ИНТЕРФЕЙС И ФОНОВЫЙ ПРОЦЕСС
# ==========================================
router      = Router()
search_task = None


def get_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Начать поиск"),  KeyboardButton(text="⏸ Остановить поиск")],
            [KeyboardButton(text="📊 Топ-10"),         KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📈 Статистика")],
        ],
        resize_keyboard=True,
    )


def get_found_keyboard(username: str, score: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⭐ Добавить в избранное",
            callback_data=f"fav_{username}_{score}",
        )]
    ])


async def process_free_username(word, loc_score, is_dict, read_score, checker, engine, bot, chat_id):
    try:
        logging.info(f"🔥 Найдено свободное имя: @{word}! Запрашиваем внешнюю аналитику...")
        frag_score, est_price = await engine.fragment_score(word)
        total_score = loc_score + frag_score

        await save_username(word, total_score, is_dict, read_score, frag_score)
        rank, potential = await checker.get_external_data(word)

        is_high_rank = False
        m = re.search(r"\d+", rank)
        if m and int(m.group(0)) >= 5:
            is_high_rank = True

        msg_header = "🚨 <b>ЖИРНЫЙ АККАУНТ! ВЫСОКИЙ РАНГ!</b> 🚨\n\n" if is_high_rank else ""
        if is_high_rank:
            logging.warning(f"🚨🚨🚨 СУПЕР НАХОДКА! @{word} — ранг {rank}! 🚨🚨🚨")

        msg = (
            f"{msg_header}"
            f"🔥 <b>НАЙДЕН ЮЗЕРНЕЙМ:</b> @{word}\n\n"
            f"📊 Наш общий балл: <b>{total_score}/100</b>\n"
            f"📖 В словаре: {'Да' if is_dict else 'Нет'}\n"
            f"🗣 Читаемость: {read_score}/30\n"
            f"💎 Оценка Fragment: <b>{est_price}</b> (Балл: {frag_score})\n\n"
            f"👑 <b>Данные @UserRate_bot:</b>\n"
            f"🔹 {rank}\n"
            f"⚡ {potential}"
        )

        await asyncio.sleep(1.0)
        for attempt in range(3):
            try:
                await bot.send_message(
                    chat_id, msg,
                    parse_mode="HTML",
                    reply_markup=get_found_keyboard(word, total_score),
                )
                break
            except Exception as e:
                if "timeout" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(5)
                else:
                    raise
    except Exception as e:
        logging.error(f"Ошибка в фоновом процессоре: {e}")


async def search_worker(checker: MultiSessionChecker, engine: Engine, bot: Bot, chat_id: int):
    try:
        while True:
            word                           = engine.generate_word()
            loc_score, is_dict, read_score = engine.local_score(word)
            word_len                       = len(word)

            # -------------------------------------------------------
            # Пороги по длине слова:
            #   5 букв  → минимум MIN_LOCAL_SCORE (30)
            #   6 букв  → минимум 60
            #   7–8 букв → минимум 80
            # -------------------------------------------------------
            if word_len == 5 and loc_score < MIN_LOCAL_SCORE:
                await asyncio.sleep(0.1)
                continue
            elif word_len == 6 and loc_score < 60:
                await asyncio.sleep(0.1)
                continue
            elif word_len >= 7 and (loc_score < 80 or not is_dict):
                await asyncio.sleep(0.1)
                continue

            if await is_in_blacklist(word):
                logging.info(f"   ↳ Пропущен (уже проверяли): @{word}")
                await asyncio.sleep(0.1)
                continue

            logging.info(f"🔍 Проверка (длина {word_len}, балл {loc_score}): @{word}")
            is_free = await checker.is_username_free(word)
            await add_to_blacklist(word)

            if is_free:
                asyncio.create_task(
                    process_free_username(word, loc_score, is_dict, read_score, checker, engine, bot, chat_id)
                )
            else:
                logging.info(f"   ↳ Занят: @{word}")

            await asyncio.sleep(WORKER_SLEEP)

    except asyncio.CancelledError:
        logging.info("Поиск принудительно остановлен.")


# ---------- handlers ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with msg_lock:
        await message.answer(
            "👋 Терминал кибер-снайпера активирован.\nВыберите действие на панели:",
            reply_markup=get_reply_keyboard(),
        )


@router.message(F.text == "▶️ Начать поиск")
async def start_search(message: Message, bot: Bot, checker: MultiSessionChecker, engine: Engine):
    global search_task
    async with msg_lock:
        if search_task and not search_task.done():
            return await message.answer("⏳ Поиск уже запущен!")
        search_task = asyncio.create_task(
            search_worker(checker, engine, bot, message.chat.id)
        )
        await message.answer("▶️ <b>Скрипт активирован!</b>\nСети сканируются...", parse_mode="HTML")


@router.message(F.text == "⏸ Остановить поиск")
async def stop_search(message: Message):
    global search_task
    async with msg_lock:
        if search_task and not search_task.done():
            search_task.cancel()
            await message.answer("⏸ <b>Процесс сканирования приостановлен.</b>", parse_mode="HTML")
        else:
            await message.answer("Поиск в данный момент не активен.")


@router.message(F.text == "📊 Топ-10")
async def show_top(message: Message):
    async with msg_lock:
        top_list = await get_top_10()
        if not top_list:
            return await message.answer("База пока пуста. Запустите поиск.")
        text = "📊 <b>ТОП-10 ЮЗЕРНЕЙМОВ:</b>\n\n"
        for i, (username, score, is_dict) in enumerate(top_list, 1):
            icon  = "📖" if is_dict else "🎲"
            text += f"{i}. @{username} — {score} баллов {icon}\n"
        await message.answer(text, parse_mode="HTML")


@router.message(F.text == "⭐ Избранное")
async def show_favorites(message: Message):
    async with msg_lock:
        favs = await get_favorites()
        if not favs:
            return await message.answer("Список избранного пуст.")
        text = "⭐ <b>ВАШЕ ИЗБРАННОЕ:</b>\n\n"
        for i, (username, score) in enumerate(favs, 1):
            text += f"{i}. @{username} (Балл: {score})\n"
        await message.answer(text, parse_mode="HTML")


@router.message(F.text == "📈 Статистика")
async def show_statistics(message: Message):
    async with msg_lock:
        total_checked, total_found = await get_statistics()
        hit_rate = (total_found / total_checked * 100) if total_checked > 0 else 0.0
        text = (
            "📈 <b>СТАТИСТИКА БОТА:</b>\n\n"
            f"📡 Запросов к Telegram API: <b>{total_checked}</b>\n"
            f"🔥 Успешных находок: <b>{total_found}</b>\n"
            f"🎯 Эффективность сканирования: <b>{hit_rate:.2f}%</b>"
        )
        await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("fav_"))
async def handle_add_favorite(cb: CallbackQuery):
    parts    = cb.data.split("_")
    username = parts[1]
    score    = int(parts[2])
    success  = await add_to_favorites(username, score)
    if success:
        await cb.answer(f"⭐ @{username} добавлен в Избранное!", show_alert=False)
        await cb.message.edit_reply_markup(reply_markup=None)
    else:
        await cb.answer("Ошибка при сохранении.", show_alert=True)


# ==========================================
# 6. ТОЧКА ВХОДА
# ==========================================
async def main():
    global msg_lock
    msg_lock = asyncio.Lock()

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))

    dp = Dispatcher()
    dp.include_router(router)

    checker = MultiSessionChecker(API_ID, API_HASH)
    engine  = Engine()

    dp.workflow_data.update({"checker": checker, "engine": engine})

    await checker.start()
    print("✅ Бот готов к работе. Напиши /start в Telegram.")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, handle_as_tasks=True, relaxation=0.5)


if __name__ == "__main__":
    asyncio.run(main())
