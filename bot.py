"""
Baseline Telegram-бот рекомендаций событий
Стек: aiogram 3, JSON-база событий, OpenAI API (опционально)
Запуск: python bot.py
"""

import json
import logging
import os
from datetime import datetime, timedelta
from openai import AsyncOpenAI

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


# ─── Конфиг ────────────────────────────────────────────────────────────────────
load_dotenv()  # читает .env файл

BOT_TOKEN   = os.getenv("BOT_TOKEN")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY")        # Если пусто — LLM отключён

EVENTS_FILE = "events.json"
WELCOME_IMAGE  = "images/welcome.PNG"
RESEARCH_IMAGE = "images/research.PNG"
FIND_IMAGE     = "images/find.PNG"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ─── Загрузка событий ──────────────────────────────────────────────────────────

def load_events() -> list[dict]:
    with open(EVENTS_FILE, encoding="utf-8") as f:
        return json.load(f)

EVENTS = load_events()

# ─── Все доступные теги ────────────────────────────────────────────────────────

ALL_TAGS = {
    "education":     "📚 Образование",
    "tech":          "💻 Технологии",
    "business":      "💼 Бизнес",
    "startup":       "🚀 Стартапы",
    "networking":    "🤝 Нетворкинг",
    "career":        "🎯 Карьера",
    "art":           "🎨 Искусство",
    "music":         "🎵 Музыка",
    "science":       "🔬 Наука",
    "entertainment": "🎭 Развлечения",
    "sport":         "⚽ Спорт",
    "food":          "🍕 Еда",
    "free":          "🆓 Бесплатно",
}

# ─── FSM: состояния диалога ────────────────────────────────────────────────────

class Form(StatesGroup):
    choosing_tags = State()   # Пользователь выбирает теги
    choosing_date = State()   # Пользователь выбирает дату

# ─── Хранилище выбранных тегов (в памяти, для baseline достаточно) ─────────────

user_tags: dict[int, set[str]] = {}

# ─── Фильтрация событий ────────────────────────────────────────────────────────

def filter_events(tags: set[str], date_from: str, date_to: str) -> list[dict]:
    """
    Фильтрует события по тегам (хотя бы один совпадает) и диапазону дат.
    """
    result = []
    for event in EVENTS:
        # Проверка даты
        if not (date_from <= event["date"] <= date_to):
            continue
        # Проверка тегов — хотя бы одно пересечение
        if tags and not tags.intersection(set(event["tags"])):
            continue
        result.append(event)
    # Сортируем по дате
    result.sort(key=lambda e: e["date"])
    return result[:5]  # Максимум 5 событий за раз

# ─── LLM: генерация органичного текста ────────────────────────────────────────

async def llm_recommend(events: list[dict], user_tags: set[str]) -> str:
    """
    Передаём отфильтрованные события в OpenAI и просим написать рекомендацию.
    Если ключа нет — возвращаем форматированный текст напрямую.
    """
    if not OPENAI_KEY or not events:
        return format_events_plain(events)

    client = AsyncOpenAI(api_key=OPENAI_KEY)

    events_text = "\n".join([
        f"- {e['title']} | {e['date']} {e['time']} | {e['location']} | {e['link']}"
        for e in events
    ])
    tags_text = ", ".join(user_tags)

    prompt = f"""Ты дружелюбный помощник по досугу в Telegram-боте.
Пользователь интересуется: {tags_text}.

Вот подходящие события:
{events_text}

Напиши короткую, живую рекомендацию (3-5 предложений) — как будто советуешь другу.
Упомяни названия событий, дату и время. В конце добавь ссылки списком.
Не используй markdown-разметку — только обычный текст."""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log.warning(f"LLM error: {e}")
        return format_events_plain(events)

def format_events_plain(events: list[dict]) -> str:
    if not events:
        return "По вашим критериям ничего не нашлось. Попробуйте изменить теги или дату."

    lines = ["Вот что нашлось для вас:\n"]
    for i, e in enumerate(events, 1):
        lines.append(
            f"{i}. {e['title']}\n"
            f"   📅 {e['date']} в {e['time']}\n"
            f"   📍 {e['location']}\n"
            f"   💸 {e.get('price', 'цена не указана')}\n"
            f"   🔗 {e['link']}\n"
        )
    return "\n".join(lines)

# ─── Клавиатуры ────────────────────────────────────────────────────────────────

def tags_keyboard(selected: set[str]) -> InlineKeyboardBuilder:
    """Клавиатура с тегами. Выбранные помечаются галочкой."""
    builder = InlineKeyboardBuilder()
    for tag, label in ALL_TAGS.items():
        mark = "✅ " if tag in selected else ""
        builder.button(text=f"{mark}{label}", callback_data=f"tag:{tag}")
    builder.button(text="➡️ Готово", callback_data="tags_done")
    builder.adjust(2)  # 2 кнопки в ряд
    return builder

def date_keyboard() -> InlineKeyboardBuilder:
    """Клавиатура выбора периода."""
    today = datetime.today()
    builder = InlineKeyboardBuilder()
    builder.button(text="Сегодня",        callback_data="date:today")
    builder.button(text="Завтра",         callback_data="date:tomorrow")
    builder.button(text="Эта неделя",     callback_data="date:week")
    builder.button(text="Следующие 2 нед", callback_data="date:2weeks")
    builder.button(text="Весь апрель",    callback_data="date:april")
    builder.button(text="Любая дата",     callback_data="date:any")
    builder.adjust(2)
    return builder

# ─── Bot & Dispatcher ──────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ─── Хэндлеры ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_tags[message.from_user.id] = set()
    photo = FSInputFile(WELCOME_IMAGE)
    await message.answer_photo(photo)

    await message.answer(
        "Я помогу тебе найти интересные события в Москве ٩(◕‿◕)۶\n\n"
        "Давай начнем с выбора тем, которые тебя интересуют:",
        reply_markup=tags_keyboard(set()).as_markup()
    )
    await state.set_state(Form.choosing_tags)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ Как пользоваться ботом:\n\n"
        "/start — начать заново и выбрать интересы\n"
        "/find  — найти события (если интересы уже выбраны)\n"
        "/tags  — посмотреть выбранные теги\n\n"
        "Просто выбирай темы → дату → получай рекомендации!"
    )


@dp.message(Command("tags"))
async def cmd_tags(message: Message):
    selected = user_tags.get(message.from_user.id, set())
    if not selected:
        await message.answer("Ты ещё не выбрал теги. Нажми /start чтобы начать.")
        return
    labels = [ALL_TAGS[t] for t in selected if t in ALL_TAGS]
    await message.answer(f"Твои интересы: {', '.join(labels)}")


@dp.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext):
    selected = user_tags.get(message.from_user.id, set())
    if not selected:
        await message.answer("Сначала выбери интересы через /start")
        return
    await message.answer(
        "Отлично! На какой период ищем события?",
        reply_markup=date_keyboard().as_markup()
    )
    await state.set_state(Form.choosing_date)


# ─── Выбор тегов ──────────────────────────────────────────────────────────────

@dp.callback_query(Form.choosing_tags, F.data.startswith("tag:"))
async def toggle_tag(callback: CallbackQuery, state: FSMContext):
    tag = callback.data.split(":")[1]
    uid = callback.from_user.id

    if uid not in user_tags:
        user_tags[uid] = set()

    # Тоггл — добавляем или убираем тег
    if tag in user_tags[uid]:
        user_tags[uid].discard(tag)
    else:
        user_tags[uid].add(tag)

    selected = user_tags[uid]
    count = len(selected)
    hint = f"Выбрано: {count}" if count > 0 else "Выбери хотя бы один интерес"

    await callback.message.edit_text(
        f"Выбери темы, которые тебя интересуют:\n\n{hint}",
        reply_markup=tags_keyboard(selected).as_markup()
    )
    await callback.answer()


@dp.callback_query(Form.choosing_tags, F.data == "tags_done")
async def tags_done(callback: CallbackQuery, state: FSMContext):
    selected = user_tags.get(callback.from_user.id, set())

    if not selected:
        await callback.answer("Выбери хотя бы один тег!", show_alert=True)
        return

    labels = [ALL_TAGS[t] for t in selected]
    await callback.message.edit_text(
        f"Запомнил твои интересы ᕦ(ಠ_ಠ)ᕤ\n{', '.join(labels)}\n\n"
        f"На какой период ищем события?",
        reply_markup=date_keyboard().as_markup()
    )
    await state.set_state(Form.choosing_date)
    await callback.answer()


# ─── Выбор даты и показ результата ───────────────────────────────────────────

@dp.callback_query(Form.choosing_date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":")[1]
    today  = datetime.today()

    # Определяем диапазон дат по выбранному периоду
    if period == "today":
        date_from = date_to = today.strftime("%Y-%m-%d")
    elif period == "tomorrow":
        d = today + timedelta(days=1)
        date_from = date_to = d.strftime("%Y-%m-%d")
    elif period == "week":
        date_from = today.strftime("%Y-%m-%d")
        date_to   = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "2weeks":
        date_from = today.strftime("%Y-%m-%d")
        date_to   = (today + timedelta(days=14)).strftime("%Y-%m-%d")
    elif period == "april":
        date_from = "2026-04-01"
        date_to   = "2026-04-30"
    else:  # any
        date_from = "2026-01-01"
        date_to   = "2026-12-31"

    uid      = callback.from_user.id
    selected = user_tags.get(uid, set())

    await callback.message.edit_text("🔍 Ищу события...")

    found = filter_events(selected, date_from, date_to)

    if not found:
        await callback.message.edit_text(
            "По твоим критериям ничего не нашлось (ಥ﹏ಥ)\n\n"
            "Попробуй:\n"
            "• выбрать другой период\n"
            "• добавить больше тегов\n\n"
            "Нажми /start чтобы начать заново."
        )
        await state.clear()
        await callback.answer()
        return

    # Генерируем рекомендацию (с LLM или без)
    await callback.message.edit_text("✨ Формирую рекомендации...")
    text = await llm_recommend(found, selected)

    await callback.message.edit_text(text)

    # Предлагаем найти ещё
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Изменить теги",  callback_data="restart_tags")
    builder.button(text="📅 Другой период",  callback_data="restart_date")
    builder.adjust(2)

    await callback.message.answer(
        "Хочешь найти что-то ещё? (° ͜ʖ ͡°)",
        reply_markup=builder.as_markup()
    )

    await state.clear()
    await callback.answer()


# ─── Быстрый рестарт ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "restart_tags")
async def restart_tags(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    user_tags[uid] = set()
    await callback.message.edit_text(
        "Выбери темы, которые тебя интересуют:",
        reply_markup=tags_keyboard(set()).as_markup()
    )
    await state.set_state(Form.choosing_tags)
    await callback.answer()


@dp.callback_query(F.data == "restart_date")
async def restart_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "На какой период ищем события?",
        reply_markup=date_keyboard().as_markup()
    )
    await state.set_state(Form.choosing_date)
    await callback.answer()


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
