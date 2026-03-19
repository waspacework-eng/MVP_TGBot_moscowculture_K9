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

def is_event_upcoming(event: dict) -> bool:
    """
    Возвращает True если событие ещё не началось (с учётом текущего времени).
    Событие сегодня в 10:00 в 23:00 — уже прошло, не показываем.
    """
    try:
        now = datetime.now()
        event_dt = datetime.strptime(
            f"{event['date']} {event.get('time', '23:59')}", "%Y-%m-%d %H:%M"
        )
        return event_dt > now
    except Exception:
        return True  # если не можем распарсить — не скрываем

def filter_events(tags, date_from, date_to, exclude_seen=None):
    now = datetime.now()
    result = []
    for i, event in enumerate(EVENTS):
        if exclude_seen and i in exclude_seen:
            continue
        if not (date_from <= event["date"] <= date_to):
            continue
        if tags and not tags.intersection(set(event["tags"])):
            continue
        # Скрываем события которые уже прошли (дата+время < сейчас)
        if not is_event_upcoming(event):
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
    """
    Двухэтапный подход:
    1. ИИ парсит запрос → возвращает критерии (теги, ключевые слова, даты, бюджет)
    2. Python фильтрует события программно по этим критериям
    3. ИИ пишет красивый текст по уже отфильтрованным результатам
    """
    if not OPENAI_KEY:
        return "⚠️ Режим живого поиска недоступен: не настроен OpenAI API ключ."

    client = AsyncOpenAI(api_key=OPENAI_KEY)
    today  = datetime.today()
    today_str = today.strftime("%Y-%m-%d")
    u = get_user(uid)
    name_str = f"Имя пользователя: {u['name']}. " if u["name"] else ""

    # ── Шаг 1: ИИ только парсит запрос, никаких событий не видит ─────────────
    # Уже здесь исключаем прошедшие события из контекста пользователя
    stale = {i for i in u["seen"] if not is_event_upcoming(EVENTS[i]) if 0 <= i < len(EVENTS)}
    u["seen"].update(stale)  # помечаем прошедшие как "виденные" чтобы не показывать

    parse_prompt = (
        f"Сегодня {today_str}. Пользователь написал запрос о событиях в Москве.\n"
        "Извлеки параметры поиска и верни ТОЛЬКО JSON без обёртки:\n"
        "{"
        '"keywords": ["ключевые слова из запроса для поиска в title/description, например: гончарный, керамика, лепка"],'
        '"tags": ["теги из списка: education, tech, business, startup, networking, career, art, music, science, entertainment, sport, food, free, masterclass"],'
        '"price_max": "бесплатно|до500|до1000|любая — на основе слов типа бесплатно/дёшево/до500р",'
        '"date_from": "YYYY-MM-DD или null",'
        '"date_to": "YYYY-MM-DD или null",'
        '"understood": "одно предложение — как понял запрос"'
        "}\n\n"
        "Правила:\n"
        "- 'мастер-класс', 'мастерклас', 'workshop', 'воркшоп' → тег masterclass + keywords\n"
        "- 'лекция' → тег education\n"
        "- 'концерт', 'музыка' → тег music\n"
        "- 'спорт', 'тренировка', 'пробежка' → тег sport\n"
        "- 'еда', 'кулинар', 'гастро' → тег food\n"
        "- 'бесплатно', 'free', 'даром' → price_max=бесплатно\n"
        "- 'до 500', 'недорого', 'дёшево' → price_max=до500\n"
        "- 'сегодня' → date_from и date_to = сегодня\n"
        f"- 'завтра' → дата {(today + timedelta(days=1)).strftime('%Y-%m-%d')}\n"
        f"- 'эта неделя', 'на неделе' → date_from={today_str}, date_to={(today + timedelta(days=7)).strftime('%Y-%m-%d')}\n"
        f"- 'выходные' → ближайшие суббота-воскресенье\n"
        "- 'в апреле' → date_from=2026-04-01, date_to=2026-04-30\n"
        "- если дата не упомянута → date_from и date_to = null (ищем по всей базе)\n"
        "- keywords должны быть конкретными словами из запроса, НЕ общими тегами"
    )

    u["history"].append({"role": "user", "content": user_query})

    try:
        # Шаг 1: парсим запрос
        parse_resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": parse_prompt},
                {"role": "user", "content": user_query},
            ],
            max_tokens=300, temperature=0.1,  # низкая температура — нужна точность
        )
        params = json.loads(strip_json(parse_resp.choices[0].message.content.strip()))
    except Exception as e:
        log.warning(f"llm parse error: {e}")
        params = {"keywords": [], "tags": [], "price_max": "любая",
                  "date_from": None, "date_to": None, "understood": user_query}

    # ── Шаг 2: Python фильтрует события программно ────────────────────────────
    keywords  = [k.lower() for k in params.get("keywords", [])]
    req_tags  = set(params.get("tags", []))
    price_max = params.get("price_max", "любая")
    date_from = params.get("date_from") or today_str  # если дата не указана — от сегодня
    date_to   = params.get("date_to")   or "2026-12-31"
    understood = params.get("understood", "")

    PRICE_ORDER = {"бесплатно": 0, "до 500 руб.": 1, "до 1000 руб.": 2, "свыше 1000 руб.": 3}
    PRICE_LIMIT = {"бесплатно": 0, "до500": 1, "до1000": 2, "любая": 99}
    limit = PRICE_LIMIT.get(price_max, 99)

    candidates = []
    for i, event in enumerate(EVENTS):
        if i in u["seen"]:
            continue
        # Скрываем события которые уже прошли (дата+время < сейчас)
        if not is_event_upcoming(event):
            continue
        # Фильтр по дате
        if not (date_from <= event["date"] <= date_to):
            continue
        # Фильтр по цене
        event_price_level = PRICE_ORDER.get(event.get("price", "свыше 1000 руб."), 3)
        if event_price_level > limit:
            continue

        score = 0
        event_tags = set(event.get("tags", []))
        title_lower = event["title"].lower()
        desc_lower  = event.get("description", "").lower()

        # Совпадение по тегам (основной критерий)
        tag_matches = len(req_tags & event_tags)
        score += tag_matches * 10

        # Совпадение по ключевым словам в названии (приоритет выше)
        kw_in_title = sum(1 for kw in keywords if kw in title_lower)
        score += kw_in_title * 15

        # Совпадение по ключевым словам в описании
        kw_in_desc = sum(1 for kw in keywords if kw in desc_lower)
        score += kw_in_desc * 5

        # Если есть теги но нет совпадений — пропускаем полностью
        if req_tags and tag_matches == 0 and kw_in_title == 0:
            continue

        # Если есть ключевые слова но нет совпадений нигде — пропускаем
        if keywords and kw_in_title == 0 and kw_in_desc == 0 and tag_matches == 0:
            continue

        if score > 0:
            candidates.append((score, i, event))

    # Сортируем по релевантности, берём топ-5
    candidates.sort(key=lambda x: (-x[0], x[2]["date"]))
    found = [(i, e) for _, i, e in candidates[:5]]

    # ── Если ничего не нашлось — честный ответ ────────────────────────────────
    if not found:
        date_hint = ""
        if date_from != today_str or date_to != "2026-12-31":
            date_hint = f" на период с {date_from} по {date_to}"

        # Проверим — может есть такие события в другой период?
        any_time = [(i, e) for i, e in enumerate(EVENTS)
                    if (req_tags & set(e.get("tags", [])) or
                        any(kw in e["title"].lower() for kw in keywords))]

        if any_time:
            nearest = min(any_time, key=lambda x: x[1]["date"])
            nearest_date = nearest[1]["date"]
            return (
                f"🔍 <i>{understood}</i>\n\n"
                f"😔 На запрашиваемый период{date_hint} таких событий нет.\n\n"
                f"Ближайшее похожее — <b>{nearest[1]['title']}</b> ({nearest_date}).\n\n"
                "Попробуй расширить период или выбери другую тему 👇"
            )
        else:
            return (
                f"🔍 <i>{understood}</i>\n\n"
                "😔 По такому запросу событий в базе нет совсем.\n\n"
                "Попробуй другие интересы:\n"
                "• <i>«мастер-класс по рисованию»</i>\n"
                "• <i>«бесплатный спорт в эти выходные»</i>\n"
                "• <i>«концерт или выставка»</i>"
            )

    # ── Шаг 3: ИИ пишет красивый текст по уже отфильтрованным результатам ─────
    u["seen"].update(i for i, _ in found)

    events_for_ai = "\n".join([
        f"{n+1}. {e['title']} | {e['date']} {e['time']} | {e['location']} | Цена: {e.get('price','?')}"
        for n, (_, e) in enumerate(found)
    ])

    text_prompt = (
        f"{name_str}Пользователь искал: {user_query}\n"
        f"Найдено {len(found)} подходящих событий:\n{events_for_ai}\n\n"
        "Верни ТОЛЬКО JSON без обёртки:\n"
        '{"intro": "1-2 живых предложения — совет другу под этот конкретный запрос",'
        '"hooks": ["почему событие подходит (1 предложение)", ...]}'
        f"\nhooks — ровно {len(found)} штук."
    )

    try:
        text_resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": text_prompt}],
            max_tokens=600, temperature=0.75,
        )
        ai = json.loads(strip_json(text_resp.choices[0].message.content))
        u["history"].append({
            "role": "assistant",
            "content": f"Показал: {[e['title'] for _, e in found]}"
        })
        return render_cards(found, ai.get("hooks", []), ai.get("intro", ""), understood)
    except Exception as e:
        log.warning(f"llm text error: {e}")
        return render_cards(found, [], "", understood)


async def llm_surprise(uid: int) -> str:
    u = get_user(uid)
    # Только предстоящие события, которые ещё не показывали
    available = [
        (i, e) for i, e in enumerate(EVENTS)
        if i not in u["seen"] and is_event_upcoming(e)
    ]
    if not available:
        # Если всё показано — сбрасываем seen, но всё равно только будущие
        u["seen"].clear()
        available = [(i, e) for i, e in enumerate(EVENTS) if is_event_upcoming(e)]
    if not available:
        return "😔 Похоже, ближайших событий в базе нет. Загляни позже!"
    if not OPENAI_KEY:
        pick = random.choice(available)
        u["seen"].add(pick[0])
        return render_cards([pick], ["Просто попробуй — может понравится!"], "Вот случайное событие 🎲")
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    events_json = json.dumps([{"index": i, **e} for i, e in available], ensure_ascii=False)
    prompt = (
        f"Сейчас {now_str}. ВСЕ события в списке уже проверены — они ещё не начались.\n"
        f"Выбери 1-2 самых неожиданных, нестандартных события.\n"
        f"Не самые очевидные — то о чём человек не подумал бы сам, но был бы рад.\n\n"
        f"База (только предстоящие события):\n{events_json}\n\n"
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
