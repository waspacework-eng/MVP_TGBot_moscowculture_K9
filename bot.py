"""
Telegram-бот рекомендаций событий v2
Стек: aiogram 3, JSON-база событий, OpenAI API
Режимы: кнопочный, свободный текст с памятью диалога, удиви меня
"""

import json
import logging
import os
import random
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

load_dotenv()
BOT_TOKEN  = os.getenv("BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
EVENTS_FILE   = "events.json"
WELCOME_IMAGE = "welcome.PNG"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def load_events():
    with open(EVENTS_FILE, encoding="utf-8") as f:
        return json.load(f)

EVENTS = load_events()

ALL_TAGS = {
    "education": "📚 Образование", "tech": "💻 Технологии",
    "business": "💼 Бизнес", "startup": "🚀 Стартапы",
    "networking": "🤝 Нетворкинг", "career": "🎯 Карьера",
    "art": "🎨 Искусство", "music": "🎵 Музыка",
    "science": "🔬 Наука", "entertainment": "🎭 Развлечения",
    "sport": "⚽ Спорт", "food": "🍕 Еда", "free": "🆓 Бесплатно",
}

class Form(StatesGroup):
    choosing_tags = State()
    choosing_date = State()
    free_query    = State()
    asking_name   = State()

user_data: dict = {}

def get_user(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {"name": "", "tags": set(), "history": [], "seen": set()}
    return user_data[uid]

def filter_events(tags, date_from, date_to, exclude_seen=None):
    result = []
    for i, event in enumerate(EVENTS):
        if exclude_seen and i in exclude_seen:
            continue
        if not (date_from <= event["date"] <= date_to):
            continue
        if tags and not tags.intersection(set(event["tags"])):
            continue
        result.append((i, event))
    result.sort(key=lambda x: x[1]["date"])
    return result[:5]

def format_events_plain(indexed_events):
    if not indexed_events:
        return "По вашим критериям ничего не нашлось. Попробуйте изменить запрос."
    lines = ["Вот что нашлось:\n"]
    for _, e in indexed_events:
        lines.append(
            f"• <b>{e['title']}</b>\n"
            f"  📅 {e['date']} в {e['time']}\n"
            f"  📍 {e['location']}\n"
            f"  💸 {e.get('price', 'цена не указана')}\n"
            f"  🔗 {e['link']}\n"
        )
    return "\n".join(lines)

def render_cards(indexed_events, hooks, intro, understood=""):
    lines = []
    if understood:
        lines.append(f"🎯 <i>{understood}</i>\n")
    if intro:
        lines.append(f"✨ {intro}\n")
    for i, (_, e) in enumerate(indexed_events):
        hook = hooks[i] if i < len(hooks) else ""
        card = (
            "─" * 28 + "\n"
            + f"<b>{i+1}. {e['title']}</b>\n"
            + f"📅 {e['date']} в {e['time']}\n"
            + f"📍 {e['location']}\n"
            + f"💸 {e.get('price', 'цена не указана')}\n"
        )
        if hook:
            card += f"💬 <i>{hook}</i>\n"
        card += f"🔗 {e['link']}"
        lines.append(card)
    return "\n".join(lines)

def strip_json(raw: str) -> str:
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

async def llm_recommend(indexed_events, selected_tags, user_name=""):
    if not OPENAI_KEY or not indexed_events:
        return format_events_plain(indexed_events)
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    name_str = f"Имя пользователя: {user_name}. " if user_name else ""
    events_data = "\n".join([
        f"{i+1}. {e['title']} | {e['date']} {e['time']} | Цена: {e.get('price','?')} | Теги: {', '.join(e.get('tags',[]))}"
        for i, (_, e) in enumerate(indexed_events)
    ])
    tags_text = ", ".join([ALL_TAGS.get(t, t) for t in selected_tags])
    prompt = (
        f"{name_str}Пользователь интересуется: {tags_text}.\n"
        f"События ({len(indexed_events)} шт):\n{events_data}\n\n"
        f"Верни ТОЛЬКО JSON без обёртки:\n"
        f'{{"intro": "1-2 живых предложения — совет другу","hooks": ["крючок 1","крючок 2"]}}\n'
        f"hooks — ровно {len(indexed_events)} штук."
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700, temperature=0.75,
        )
        ai = json.loads(strip_json(resp.choices[0].message.content))
        return render_cards(indexed_events, ai.get("hooks", []), ai.get("intro", ""))
    except Exception as e:
        log.warning(f"llm_recommend error: {e}")
        return format_events_plain(indexed_events)

async def llm_free_search(uid: int, user_query: str) -> str:
    if not OPENAI_KEY:
        return "⚠️ Режим живого поиска недоступен: не настроен OpenAI API ключ."
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    today = datetime.today().strftime("%Y-%m-%d")
    u = get_user(uid)
    name_str = f"Имя пользователя: {u['name']}. " if u["name"] else ""
    available = [(i, e) for i, e in enumerate(EVENTS) if i not in u["seen"]]
    if not available:
        u["seen"].clear()
        available = list(enumerate(EVENTS))
    events_json = json.dumps([{"index": i, **e} for i, e in available], ensure_ascii=False)
    system_prompt = (
        f"{name_str}Ты — умный помощник по досугу в Telegram-боте. Сегодня: {today}.\n"
        "ВАЖНЕЙШЕЕ ПРАВИЛО: ты выбираешь ТОЛЬКО события из базы, которые РЕАЛЬНО подходят под запрос.\n"
        "Если подходящих нет — верни пустой список events:[]. НЕ предлагай нерелевантные события.\n"
        "Например: запрос 'мастер-класс' → только события с тегом masterclass. Запрос 'бесплатно' → только price=='бесплатно'.\n\n"
        f"База событий Москвы (доступные, не показанные):\n{events_json}\n\n"
        "Выбери до 5 строго подходящих событий. Верни ТОЛЬКО JSON без обёртки:\n"
        '{"understood":"как понял запрос одним предложением","events":[значения поля index, макс 5],"intro":"1-2 живых предложения если есть результаты","hooks":["почему подходит (1 предл)"]}\n'
        "Если НИЧЕГО не подходит: {\"understood\":\"...\",\"events\":[],\"intro\":\"\",\"hooks\":[]}\n\n"
        f"Строгие правила:\n"
        "- 'мастер-класс', 'воркшоп', 'workshop' → тег masterclass или слово в title\n"
        "- 'бесплатно', 'free' → price=='бесплатно'\n"
        "- 'до 500', 'недорого', 'дёшево' → price в ['бесплатно','до 500 руб.']\n"
        f"- 'сегодня' → дата {today}, 'завтра' → следующий день, 'эта неделя' → 7 дней от {today}\n"
        "- НЕ подменяй хакатон мастер-классом, митап — концертом. Смысл должен совпадать."
    )
    u["history"].append({"role": "user", "content": user_query})
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, *u["history"][-8:]],
            max_tokens=900, temperature=0.7,
        )
        ai = json.loads(strip_json(resp.choices[0].message.content.strip()))
        indices = ai.get("events", [])
        understood = ai.get("understood", "")
        u["history"].append({
            "role": "assistant",
            "content": f"Показал: {[EVENTS[i]['title'] for i in indices if 0 <= i < len(EVENTS)]}"
        })
        if not indices:
            return (
                f"🤔 <i>{understood}</i>\n\n"
                "Ничего подходящего не нашлось.\n\nПопробуй:\n"
                "• <i>«что-нибудь бесплатное на выходных»</i>\n"
                "• <i>«митап по технологиям»</i>"
            )
        found = [(i, EVENTS[i]) for i in indices if 0 <= i < len(EVENTS)]
        u["seen"].update(i for i, _ in found)
        return render_cards(found, ai.get("hooks", []), ai.get("intro", ""), understood)
    except Exception as e:
        log.warning(f"llm_free_search error: {e}")
        return "😔 Что-то пошло не так. Попробуй ещё раз."

async def llm_surprise(uid: int) -> str:
    u = get_user(uid)
    available = [(i, e) for i, e in enumerate(EVENTS) if i not in u["seen"]]
    if not available:
        u["seen"].clear()
        available = list(enumerate(EVENTS))
    if not OPENAI_KEY:
        pick = random.choice(available)
        u["seen"].add(pick[0])
        return render_cards([pick], ["Просто попробуй — может понравится!"], "Вот случайное событие 🎲")
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    today = datetime.today().strftime("%Y-%m-%d")
    events_json = json.dumps([{"index": i, **e} for i, e in available], ensure_ascii=False)
    prompt = (
        f"Сегодня {today}. Выбери 1-2 самых неожиданных, нестандартных события.\n"
        f"Не самые очевидные — то о чём человек не подумал бы сам, но был бы рад.\n\n"
        f"База:\n{events_json}\n\n"
        f"Верни ТОЛЬКО JSON:\n"
        f'{{"events":[индексы из поля index, 1-2 штуки],"intro":"1-2 предложения с интригой","hooks":["почему именно это стоит попробовать"]}}'
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500, temperature=1.0,
        )
        ai = json.loads(strip_json(resp.choices[0].message.content))
        found = [(i, EVENTS[i]) for i in ai.get("events", []) if 0 <= i < len(EVENTS)]
        if not found:
            found = [available[0]]
        u["seen"].update(i for i, _ in found)
        return render_cards(found, ai.get("hooks", []), ai.get("intro", ""), "Выбрал специально для тебя 🎲")
    except Exception as e:
        log.warning(f"llm_surprise error: {e}")
        pick = random.choice(available)
        u["seen"].add(pick[0])
        return render_cards([pick], ["Просто попробуй!"], "Удивляю 🎲")

def main_menu_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Выбрать по темам",  callback_data="mode_tags")
    b.button(text="🗣 Написать свободно",  callback_data="mode_ask")
    b.button(text="🎲 Удиви меня",         callback_data="mode_surprise")
    b.adjust(1)
    return b

def tags_keyboard(selected):
    b = InlineKeyboardBuilder()
    for tag, label in ALL_TAGS.items():
        mark = "✅ " if tag in selected else ""
        b.button(text=f"{mark}{label}", callback_data=f"tag:{tag}")
    b.button(text="➡️ Готово", callback_data="tags_done")
    b.adjust(2)
    return b

def date_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="Сегодня",         callback_data="date:today")
    b.button(text="Завтра",          callback_data="date:tomorrow")
    b.button(text="Эта неделя",      callback_data="date:week")
    b.button(text="Следующие 2 нед", callback_data="date:2weeks")
    b.button(text="Весь апрель",     callback_data="date:april")
    b.button(text="Любая дата",      callback_data="date:any")
    b.adjust(2)
    return b

def after_results_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🗣 Уточнить запрос",   callback_data="go_ask")
    b.button(text="🔄 Изменить теги",     callback_data="restart_tags")
    b.button(text="🎲 Удиви меня",        callback_data="mode_surprise")
    b.button(text="🏠 В начало",          callback_data="go_home")
    b.adjust(2)
    return b

def after_ask_keyboard():
    b = InlineKeyboardBuilder()
    b.button(text="🗣 Уточнить / спросить ещё", callback_data="go_ask")
    b.button(text="🎲 Удиви меня",              callback_data="mode_surprise")
    b.button(text="🔄 Сбросить историю",        callback_data="reset_history")
    b.button(text="🏠 В начало",                callback_data="go_home")
    b.adjust(2)
    return b

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    u = get_user(uid)
    u["tags"] = set(); u["history"] = []; u["seen"] = set()
    try:
        await message.answer_photo(FSInputFile(WELCOME_IMAGE))
    except Exception:
        pass
    if u["name"]:
        await message.answer(
            f"С возвращением, {u['name']}! 👋\n\nКак будем искать?",
            reply_markup=main_menu_keyboard().as_markup()
        )
    else:
        await state.set_state(Form.asking_name)
        await message.answer(
            "Привет! Я помогу найти интересные события в Москве ٩(◕‿◕)۶\n\n"
            "Как тебя зовут? (или напиши «пропустить»)"
        )

@dp.message(Form.asking_name)
async def handle_name(message: Message, state: FSMContext):
    u = get_user(message.from_user.id)
    name = message.text.strip()
    if name.lower() not in ("пропустить", "skip", "-", "нет"):
        u["name"] = name.split()[0]
    await state.clear()
    greeting = f"Отлично, {u['name']}! " if u["name"] else "Отлично! "
    await message.answer(greeting + "Как будем искать события?",
                         reply_markup=main_menu_keyboard().as_markup())

@dp.callback_query(F.data == "mode_tags")
async def mode_tags(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["tags"] = set()
    await callback.message.edit_text("Выбери темы, которые тебя интересуют:",
                                      reply_markup=tags_keyboard(set()).as_markup())
    await state.set_state(Form.choosing_tags)
    await callback.answer()

@dp.callback_query(F.data == "mode_ask")
async def mode_ask_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.free_query)
    u = get_user(callback.from_user.id)
    note = "\n\n💡 <i>Помню наш разговор — можешь уточнять прямо в диалоге.</i>" if u["history"] else ""
    await callback.message.edit_text(
        "🗣 <b>Режим живого поиска</b>\n\nНапиши что ищешь своими словами:\n\n"
        "• <i>«хочу что-нибудь бесплатное на выходных»</i>\n"
        "• <i>«ищу тусовку для стартаперов»</i>\n"
        "• <i>«а подешевле что-нибудь есть?»</i>" + note,
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "go_ask")
async def go_ask_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Form.free_query)
    await callback.message.answer("🗣 Уточни запрос или спроси что-то новое:")
    await callback.answer()

@dp.callback_query(F.data == "mode_surprise")
async def mode_surprise_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("🎲 Подбираю...")
    thinking = await callback.message.answer("🎲 Выбираю что-нибудь неожиданное...")
    result = await llm_surprise(callback.from_user.id)
    await thinking.delete()
    await callback.message.answer(result, parse_mode="HTML")
    await callback.message.answer("Что скажешь?", reply_markup=after_ask_keyboard().as_markup())

@dp.callback_query(F.data == "reset_history")
async def reset_history(callback: CallbackQuery, state: FSMContext):
    u = get_user(callback.from_user.id)
    u["history"] = []; u["seen"] = set()
    await callback.answer("История сброшена ✅")
    await callback.message.answer("Начинаем с чистого листа! Как будем искать?",
                                   reply_markup=main_menu_keyboard().as_markup())

@dp.callback_query(F.data == "go_home")
async def go_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    u = get_user(callback.from_user.id)
    u["tags"] = set()
    name_str = f", {u['name']}" if u["name"] else ""
    await callback.message.answer(f"Главное меню{name_str} 👇",
                                   reply_markup=main_menu_keyboard().as_markup())
    await callback.answer()

@dp.message(Command("ask"))
async def cmd_ask(message: Message, state: FSMContext):
    await state.set_state(Form.free_query)
    await message.answer(
        "🗣 <b>Режим живого поиска</b>\n\nНапиши что ищешь — я разберусь!\n"
        "Можно уточнять: «а подешевле?», «покажи ещё».", parse_mode="HTML"
    )

@dp.message(Command("surprise"))
async def cmd_surprise(message: Message, state: FSMContext):
    await state.clear()
    thinking = await message.answer("🎲 Выбираю что-нибудь неожиданное...")
    result = await llm_surprise(message.from_user.id)
    await thinking.delete()
    await message.answer(result, parse_mode="HTML")
    await message.answer("Что скажешь?", reply_markup=after_ask_keyboard().as_markup())

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>Команды:</b>\n\n"
        "/start    — начать заново\n/ask      — живой поиск\n"
        "/surprise — удиви меня 🎲\n/find     — поиск по тегам\n/tags     — мои интересы\n\n"
        "<b>Режимы:</b>\n"
        "🎯 По темам — теги → период → подборка\n"
        "🗣 Свободный — пишешь как другу, ИИ понимает\n"
        "🎲 Удиви меня — ИИ выбирает неожиданное\n\n"
        "<b>Фишки:</b>\n• Помнит контекст разговора\n"
        "• Не повторяет уже показанные события\n"
        "• Понимает уточнения: «а подешевле?», «покажи ещё»",
        parse_mode="HTML"
    )

@dp.message(Command("tags"))
async def cmd_tags(message: Message):
    u = get_user(message.from_user.id)
    if not u["tags"]:
        await message.answer("Ты ещё не выбирал теги. Нажми /start.")
        return
    labels = [ALL_TAGS[t] for t in u["tags"] if t in ALL_TAGS]
    await message.answer(f"Твои интересы: {', '.join(labels)}")

@dp.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext):
    u = get_user(message.from_user.id)
    if not u["tags"]:
        await message.answer("Сначала выбери интересы через /start")
        return
    await message.answer("На какой период ищем?", reply_markup=date_keyboard().as_markup())
    await state.set_state(Form.choosing_date)

@dp.callback_query(Form.choosing_tags, F.data.startswith("tag:"))
async def toggle_tag(callback: CallbackQuery, state: FSMContext):
    tag = callback.data.split(":")[1]
    u = get_user(callback.from_user.id)
    if tag in u["tags"]: u["tags"].discard(tag)
    else: u["tags"].add(tag)
    count = len(u["tags"])
    hint = f"Выбрано: {count}" if count > 0 else "Выбери хотя бы один интерес"
    await callback.message.edit_text(f"Выбери темы:\n\n{hint}",
                                      reply_markup=tags_keyboard(u["tags"]).as_markup())
    await callback.answer()

@dp.callback_query(Form.choosing_tags, F.data == "tags_done")
async def tags_done(callback: CallbackQuery, state: FSMContext):
    u = get_user(callback.from_user.id)
    if not u["tags"]:
        await callback.answer("Выбери хотя бы один тег!", show_alert=True)
        return
    labels = [ALL_TAGS[t] for t in u["tags"]]
    await callback.message.edit_text(
        f"Запомнил ᕦ(ಠ_ಠ)ᕤ\n{', '.join(labels)}\n\nНа какой период?",
        reply_markup=date_keyboard().as_markup()
    )
    await state.set_state(Form.choosing_date)
    await callback.answer()

@dp.callback_query(Form.choosing_date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":")[1]
    today = datetime.today()
    if period == "today":
        date_from = date_to = today.strftime("%Y-%m-%d")
    elif period == "tomorrow":
        d = today + timedelta(days=1); date_from = date_to = d.strftime("%Y-%m-%d")
    elif period == "week":
        date_from = today.strftime("%Y-%m-%d"); date_to = (today + timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "2weeks":
        date_from = today.strftime("%Y-%m-%d"); date_to = (today + timedelta(days=14)).strftime("%Y-%m-%d")
    elif period == "april":
        date_from, date_to = "2026-04-01", "2026-04-30"
    else:
        date_from, date_to = "2026-01-01", "2026-12-31"
    u = get_user(callback.from_user.id)
    await callback.message.edit_text("🔍 Ищу события...")
    found = filter_events(u["tags"], date_from, date_to, u["seen"])
    if not found:
        found = filter_events(u["tags"], date_from, date_to)
        if found:
            u["seen"].clear()
        else:
            await callback.message.edit_text(
                "По твоим критериям ничего не нашлось (ಥ﹏ಥ)\n\n"
                "Попробуй другой период, больше тегов или /ask для свободного поиска."
            )
            await state.clear(); await callback.answer(); return
    await callback.message.edit_text("✨ Формирую рекомендации...")
    text = await llm_recommend(found, u["tags"], u["name"])
    u["seen"].update(i for i, _ in found)
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.message.answer("Хочешь найти что-то ещё?",
                                   reply_markup=after_results_keyboard().as_markup())
    await state.clear(); await callback.answer()

@dp.callback_query(F.data == "restart_tags")
async def restart_tags(callback: CallbackQuery, state: FSMContext):
    get_user(callback.from_user.id)["tags"] = set()
    await callback.message.edit_text("Выбери темы:", reply_markup=tags_keyboard(set()).as_markup())
    await state.set_state(Form.choosing_tags); await callback.answer()

@dp.callback_query(F.data == "restart_date")
async def restart_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("На какой период?", reply_markup=date_keyboard().as_markup())
    await state.set_state(Form.choosing_date); await callback.answer()

@dp.message(Form.choosing_tags)
async def unexpected_in_tags(message: Message):
    await message.answer("👆 Выбери теги из кнопок выше или нажми «Готово».")

@dp.message(Form.choosing_date)
async def unexpected_in_date(message: Message):
    await message.answer("👆 Выбери период из кнопок выше.")

@dp.message(Form.free_query)
async def handle_free_query(message: Message, state: FSMContext):
    thinking = await message.answer("🤔 Анализирую...")
    result = await llm_free_search(message.from_user.id, message.text.strip())
    await thinking.delete()
    await message.answer(result, parse_mode="HTML")
    await message.answer("Могу уточнить, найти ещё или удивить 👇",
                         reply_markup=after_ask_keyboard().as_markup())

@dp.message()
async def fallback_handler(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer(
            "Не понял тебя 🤔\n\nИспользуй /start или /ask для свободного поиска.",
            reply_markup=main_menu_keyboard().as_markup()
        )

async def main():
    log.info("Bot v2 started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
