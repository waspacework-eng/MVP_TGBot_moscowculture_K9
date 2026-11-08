"""
Telegram-бот «Разбуди будни» v1.0 (MVP)
Стек: aiogram 3, JSON-база событий, OpenAI API
Режимы: кнопочный, свободный текст с памятью диалога, удиви меня

Изменения относительно прошлой версии (см. комментарии «FIX»):
  1. Время считается по Москве (Railway работает в UTC).
  2. Нет жёстко прошитых дат: период «любая дата» и «ближайший месяц» считаются от сегодня.
  3. Модель берётся из переменной окружения OPENAI_MODEL (по умолчанию gpt-5.4-mini).
  4. Имя пользователя больше не отправляется в OpenAI (решение протокола № 2).
  5. В режиме свободного поиска в модель реально передаются последние 8 сообщений диалога.
  6. Названия и тексты экранируются для HTML-режима Telegram.
"""

import html
import json
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI, BadRequestError

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN   = os.getenv("BOT_TOKEN")
OPENAI_KEY  = os.getenv("OPENAI_API_KEY")
MODEL       = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")   # FIX: модель в настройке, не в коде
EVENTS_FILE   = "events.json"
WELCOME_IMAGE = "welcome.PNG"

MSK = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def now_msk() -> datetime:
    """FIX: текущее московское время (наивное, для сравнения с датами из JSON)."""
    return datetime.now(MSK).replace(tzinfo=None)


def ymd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def esc(s) -> str:
    """FIX: экранирование для parse_mode=HTML (названия вроде «A & B» ломали разметку)."""
    return html.escape(str(s if s is not None else ""), quote=False)


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
    """True, если событие ещё не началось (по московскому времени)."""
    try:
        event_dt = datetime.strptime(
            f"{event['date']} {event.get('time', '23:59')}", "%Y-%m-%d %H:%M"
        )
        return event_dt > now_msk()
    except Exception:
        return True  # если не можем распарсить — не скрываем


def price_level(price) -> int:
    """FIX: устойчивый разбор цены: 0 — бесплатно, 1 — до 500, 2 — до 1000, 3 — дороже/неизвестно."""
    p = str(price or "").lower()
    if "бесплат" in p or p.strip() in ("0", "free"):
        return 0
    if "свыше" in p or "более" in p:
        return 3
    digits = "".join(ch if ch.isdigit() else " " for ch in p).split()
    if digits:
        n = int(digits[-1]) if "до" in p else int(digits[0])
        if n <= 500:
            return 1
        if n <= 1000:
            return 2
    return 3


def filter_events(tags, date_from, date_to, exclude_seen=None):
    result = []
    for i, event in enumerate(EVENTS):
        if exclude_seen and i in exclude_seen:
            continue
        if not (date_from <= event["date"] <= date_to):
            continue
        if tags and not tags.intersection(set(event.get("tags", []))):
            continue
        if not is_event_upcoming(event):
            continue
        result.append((i, event))
    result.sort(key=lambda x: (x[1]["date"], x[1].get("time", "")))
    return result[:5]


def format_events_plain(indexed_events):
    if not indexed_events:
        return "По вашим критериям ничего не нашлось. Попробуйте изменить запрос."
    lines = ["Вот что нашлось:\n"]
    for _, e in indexed_events:
        lines.append(
            f"• <b>{esc(e['title'])}</b>\n"
            f"  📅 {esc(e['date'])} в {esc(e.get('time', ''))}\n"
            f"  📍 {esc(e.get('location', ''))}\n"
            f"  💸 {esc(e.get('price', 'цена не указана'))}\n"
            f"  🔗 {esc(e.get('link', ''))}\n"
        )
    return "\n".join(lines)


def render_cards(indexed_events, hooks, intro, understood=""):
    lines = []
    if understood:
        lines.append(f"🎯 <i>{esc(understood)}</i>\n")
    if intro:
        lines.append(f"✨ {esc(intro)}\n")
    for i, (_, e) in enumerate(indexed_events):
        hook = hooks[i] if i < len(hooks) else ""
        card = (
            "─" * 28 + "\n"
            + f"<b>{i+1}. {esc(e['title'])}</b>\n"
            + f"📅 {esc(e['date'])} в {esc(e.get('time', ''))}\n"
            + f"📍 {esc(e.get('location', ''))}\n"
            + f"💸 {esc(e.get('price', 'цена не указана'))}\n"
        )
        if hook:
            card += f"💬 <i>{esc(hook)}</i>\n"
        card += f"🔗 {esc(e.get('link', ''))}"
        lines.append(card)
    return "\n".join(lines)


def strip_json(raw: str) -> str:
    raw = (raw or "").strip()
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


async def llm_call(client: AsyncOpenAI, messages: list, max_tokens: int, temperature=None) -> str:
    """
    FIX: единая обёртка вызова модели.
    Новые модели принимают max_completion_tokens и могут не поддерживать
    нестандартную temperature — в этом случае повторяем запрос без неё.
    """
    kwargs = dict(model=MODEL, messages=messages, max_completion_tokens=max_tokens)
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        resp = await client.chat.completions.create(**kwargs)
    except BadRequestError as e:
        log.warning(f"LLM bad request ({e}); повтор без temperature")
        kwargs.pop("temperature", None)
        resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def llm_recommend(indexed_events, selected_tags):
    if not OPENAI_KEY or not indexed_events:
        return format_events_plain(indexed_events)
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    # FIX: имя пользователя в модель не передаётся
    events_data = "\n".join([
        f"{i+1}. {e['title']} | {e['date']} {e.get('time', '')} | Цена: {e.get('price','?')} | Теги: {', '.join(e.get('tags',[]))}"
        for i, (_, e) in enumerate(indexed_events)
    ])
    tags_text = ", ".join([ALL_TAGS.get(t, t) for t in selected_tags])
    prompt = (
        f"Пользователь интересуется: {tags_text}.\n"
        f"События ({len(indexed_events)} шт):\n{events_data}\n\n"
        f"Верни ТОЛЬКО JSON без обёртки:\n"
        f'{{"intro": "1-2 живых предложения — совет другу","hooks": ["крючок 1","крючок 2"]}}\n'
        f"hooks — ровно {len(indexed_events)} штук."
    )
    try:
        raw = await llm_call(client, [{"role": "user", "content": prompt}], 1500, 0.75)
        ai = json.loads(strip_json(raw))
        return render_cards(indexed_events, ai.get("hooks", []), ai.get("intro", ""))
    except Exception as e:
        log.warning(f"llm_recommend error: {e}")
        return format_events_plain(indexed_events)


async def llm_free_search(uid: int, user_query: str) -> str:
    """
    Двухэтапный подход:
    1. ИИ парсит запрос → критерии (теги, ключевые слова, даты, бюджет)
    2. Python фильтрует события по этим критериям
    3. ИИ пишет текст по уже отфильтрованным результатам
    """
    if not OPENAI_KEY:
        return "⚠️ Режим живого поиска недоступен: не настроен OpenAI API ключ."

    client = AsyncOpenAI(api_key=OPENAI_KEY)
    today = now_msk()
    today_str = ymd(today)
    far_str = ymd(today + timedelta(days=365))   # FIX: вместо «2026-12-31»
    u = get_user(uid)

    parse_prompt = (
        f"Сегодня {today_str} ({today.strftime('%A')}). Пользователь написал запрос о событиях в Москве.\n"
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
        f"- 'завтра' → дата {ymd(today + timedelta(days=1))}\n"
        f"- 'эта неделя', 'на неделе' → date_from={today_str}, date_to={ymd(today + timedelta(days=7))}\n"
        "- 'выходные' → ближайшие суббота-воскресенье, считай от сегодняшней даты\n"
        "- если назван месяц ('в декабре') → весь этот месяц ближайшего подходящего года, считай от сегодняшней даты\n"
        "- если дата не упомянута → date_from и date_to = null (ищем по всей базе)\n"
        "- keywords должны быть конкретными словами из запроса, НЕ общими тегами"
    )

    u["history"].append({"role": "user", "content": user_query})
    u["history"] = u["history"][-16:]          # ограничиваем рост истории

    try:
        # FIX: в модель реально передаём последние 8 сообщений диалога (память контекста, IMP-05)
        raw = await llm_call(
            client,
            [{"role": "system", "content": parse_prompt}, *u["history"][-8:]],
            1000, 0.1,
        )
        params = json.loads(strip_json(raw))
    except Exception as e:
        log.warning(f"llm parse error: {e}")
        params = {"keywords": [], "tags": [], "price_max": "любая",
                  "date_from": None, "date_to": None, "understood": user_query}

    keywords   = [str(k).lower() for k in params.get("keywords", [])]
    req_tags   = set(params.get("tags", []))
    price_max  = params.get("price_max", "любая")
    date_from  = params.get("date_from") or today_str
    date_to    = params.get("date_to") or far_str
    if date_from < today_str:                  # прошлое не ищем
        date_from = today_str
    understood = params.get("understood", "")

    PRICE_LIMIT = {"бесплатно": 0, "до500": 1, "до1000": 2, "любая": 99}
    limit = PRICE_LIMIT.get(price_max, 99)

    candidates = []
    for i, event in enumerate(EVENTS):
        if i in u["seen"]:
            continue
        if not is_event_upcoming(event):
            continue
        if not (date_from <= event["date"] <= date_to):
            continue
        if price_level(event.get("price")) > limit:
            continue

        event_tags  = set(event.get("tags", []))
        title_lower = event["title"].lower()
        desc_lower  = event.get("description", "").lower()

        tag_matches = len(req_tags & event_tags)
        kw_in_title = sum(1 for kw in keywords if kw in title_lower)
        kw_in_desc  = sum(1 for kw in keywords if kw in desc_lower)
        score = tag_matches * 10 + kw_in_title * 15 + kw_in_desc * 5

        if req_tags and tag_matches == 0 and kw_in_title == 0:
            continue
        if keywords and kw_in_title == 0 and kw_in_desc == 0 and tag_matches == 0:
            continue
        # если критериев нет совсем (модель не разобрала запрос) — ничего не показываем
        if not req_tags and not keywords and limit == 99:
            continue

        if score > 0 or (not req_tags and not keywords):
            candidates.append((score, i, event))

    candidates.sort(key=lambda x: (-x[0], x[2]["date"]))
    found = [(i, e) for _, i, e in candidates[:5]]

    if not found:
        date_hint = ""
        if date_from != today_str or date_to != far_str:
            date_hint = f" на период с {date_from} по {date_to}"

        any_time = [(i, e) for i, e in enumerate(EVENTS)
                    if is_event_upcoming(e) and
                    (req_tags & set(e.get("tags", [])) or
                     any(kw in e["title"].lower() for kw in keywords))]
        if any_time:
            nearest = min(any_time, key=lambda x: x[1]["date"])
            return (
                f"🔍 <i>{esc(understood)}</i>\n\n"
                f"😔 На запрашиваемый период{date_hint} таких событий нет.\n\n"
                f"Ближайшее похожее — <b>{esc(nearest[1]['title'])}</b> ({esc(nearest[1]['date'])}).\n\n"
                "Попробуй расширить период или выбери другую тему 👇"
            )
        return (
            f"🔍 <i>{esc(understood)}</i>\n\n"
            "😔 По такому запросу событий в базе нет совсем.\n\n"
            "Попробуй другие интересы:\n"
            "• <i>«мастер-класс по рисованию»</i>\n"
            "• <i>«бесплатный спорт в эти выходные»</i>\n"
            "• <i>«концерт или выставка»</i>"
        )

    u["seen"].update(i for i, _ in found)

    events_for_ai = "\n".join([
        f"{n+1}. {e['title']} | {e['date']} {e.get('time', '')} | {e.get('location', '')} | Цена: {e.get('price','?')}"
        for n, (_, e) in enumerate(found)
    ])
    text_prompt = (
        f"Пользователь искал: {user_query}\n"
        f"Найдено {len(found)} подходящих событий:\n{events_for_ai}\n\n"
        "Верни ТОЛЬКО JSON без обёртки:\n"
        '{"intro": "1-2 живых предложения — совет другу под этот конкретный запрос",'
        '"hooks": ["почему событие подходит (1 предложение)", ...]}'
        f"\nhooks — ровно {len(found)} штук."
    )
    try:
        raw = await llm_call(client, [{"role": "user", "content": text_prompt}], 1500, 0.75)
        ai = json.loads(strip_json(raw))
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
    available = [
        (i, e) for i, e in enumerate(EVENTS)
        if i not in u["seen"] and is_event_upcoming(e)
    ]
    if not available:
        u["seen"].clear()
        available = [(i, e) for i, e in enumerate(EVENTS) if is_event_upcoming(e)]
    if not available:
        return "😔 Похоже, ближайших событий в базе нет. Загляни позже!"
    if not OPENAI_KEY:
        pick = random.choice(available)
        u["seen"].add(pick[0])
        return render_cards([pick], ["Просто попробуй — может понравится!"], "Вот случайное событие 🎲")

    # FIX: в запрос уходит не вся база, а случайные 40 предстоящих событий (экономия токенов)
    sample = random.sample(available, min(40, len(available)))
    client = AsyncOpenAI(api_key=OPENAI_KEY)
    now_str = now_msk().strftime("%Y-%m-%d %H:%M")
    events_json = json.dumps([{"index": i, **e} for i, e in sample], ensure_ascii=False)
    prompt = (
        f"Сейчас {now_str}. ВСЕ события в списке уже проверены — они ещё не начались.\n"
        f"Выбери 1-2 самых неожиданных, нестандартных события.\n"
        f"Не самые очевидные — то о чём человек не подумал бы сам, но был бы рад.\n\n"
        f"База (только предстоящие события):\n{events_json}\n\n"
        f"Верни ТОЛЬКО JSON:\n"
        f'{{"events":[индексы из поля index, 1-2 штуки],"intro":"1-2 предложения с интригой","hooks":["почему именно это стоит попробовать"]}}'
    )
    try:
        raw = await llm_call(client, [{"role": "user", "content": prompt}], 1500, 1.0)
        ai = json.loads(strip_json(raw))
        allowed = {i for i, _ in sample}
        found = [(i, EVENTS[i]) for i in ai.get("events", [])
                 if isinstance(i, int) and i in allowed]
        if not found:
            found = [sample[0]]
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
    b.button(text="Сегодня",            callback_data="date:today")
    b.button(text="Завтра",             callback_data="date:tomorrow")
    b.button(text="Эта неделя",         callback_data="date:week")
    b.button(text="Следующие 2 нед",    callback_data="date:2weeks")
    b.button(text="Ближайший месяц",    callback_data="date:month")   # FIX: вместо «Весь апрель»
    b.button(text="Любая дата",         callback_data="date:any")
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
            f"С возвращением, {esc(u['name'])}! 👋\n\nКак будем искать?",
            reply_markup=main_menu_keyboard().as_markup(), parse_mode="HTML"
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
    name = (message.text or "").strip()
    if name and name.lower() not in ("пропустить", "skip", "-", "нет"):
        u["name"] = name.split()[0][:30]
    await state.clear()
    greeting = f"Отлично, {esc(u['name'])}! " if u["name"] else "Отлично! "
    await message.answer(greeting + "Как будем искать события?",
                         reply_markup=main_menu_keyboard().as_markup(), parse_mode="HTML")


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
    name_str = f", {esc(u['name'])}" if u["name"] else ""
    await callback.message.answer(f"Главное меню{name_str} 👇",
                                   reply_markup=main_menu_keyboard().as_markup(), parse_mode="HTML")
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


def period_range(period: str):
    """FIX: границы периода считаются от текущей московской даты, без прошитых дат."""
    today = now_msk()
    if period == "today":
        return ymd(today), ymd(today)
    if period == "tomorrow":
        d = ymd(today + timedelta(days=1)); return d, d
    if period == "week":
        return ymd(today), ymd(today + timedelta(days=7))
    if period == "2weeks":
        return ymd(today), ymd(today + timedelta(days=14))
    if period == "month":
        return ymd(today), ymd(today + timedelta(days=30))
    return ymd(today), ymd(today + timedelta(days=365))   # «любая дата»


@dp.callback_query(Form.choosing_date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split(":")[1]
    date_from, date_to = period_range(period)
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
    text = await llm_recommend(found, u["tags"])
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
    if not message.text:
        await message.answer("Напиши запрос текстом 🙂")
        return
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
    upcoming = sum(1 for e in EVENTS if is_event_upcoming(e))
    # Эта строка в логах Railway — удобный контроль K1 и признак, что бот жив (K6)
    log.info(f"Bot started | model={MODEL} | events total={len(EVENTS)} | upcoming={upcoming} | now_msk={now_msk():%Y-%m-%d %H:%M}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
