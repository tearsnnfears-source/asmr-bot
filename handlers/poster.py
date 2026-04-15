"""
Poster — модуль для создания постов в каналы с пресетами.

Команды:
  /new_post              — создать новый пост (FSM)
  /new_post <preset>     — создать пост из пресета
  /save_preset           — сохранить черновик как пресет (из FSM)
  /list_presets          — показать все пресеты
  /del_preset <name>     — удалить пресет
  /scheduled_posts       — список запланированных постов
  /cancel_post <id>      — отменить запланированный пост

Форматы кнопок:
  Текст | https://url                  — обычная
  Текст | https://url | success        — зелёная 🟢
  Текст | https://url | danger         — красная 🔴
  Текст | https://url | primary        — синяя 🔵
  Несколько в ряд: Кнопка1 | url1 | primary ;; Кнопка2 | url2
  Каждая новая строка = новый ряд кнопок

Премиум эмодзи (нужна TG Premium у владельца бота):
  <tg-emoji emoji-id="5285430309720966085">👍</tg-emoji>
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

# ── Хранилище ─────────────────────────────────────────────────────────────────
_scheduled: dict[int, dict] = {}
_post_counter = 0
PRESETS_FILE = "presets.json"


def _load_presets() -> dict:
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_presets(presets: dict):
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def _next_id() -> int:
    global _post_counter
    _post_counter += 1
    return _post_counter


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ── FSM ───────────────────────────────────────────────────────────────────────

class PostForm(StatesGroup):
    channel   = State()
    text      = State()
    photo     = State()
    buttons   = State()
    schedule  = State()
    confirm   = State()
    save_name = State()


# ── Парсинг кнопок ────────────────────────────────────────────────────────────

BUTTON_STYLES = {"danger", "success", "primary"}
CUSTOM_EMOJI_TAG_RE = re.compile(
    r"<tg-emoji\b[^>]*emoji-id=['\"]\d+['\"][^>]*>.*?</tg-emoji>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _parse_buttons(raw: str) -> list[list[dict]] | None:
    if not raw or raw.strip().lower() in ("нет", "no", "-", "skip"):
        return None
    rows = []
    for line in raw.strip().splitlines():
        row = []
        for part in line.split(";;"):
            part = part.strip()
            if "|" not in part:
                continue
            pieces = [p.strip() for p in part.split("|")]
            if len(pieces) < 2:
                continue
            label = pieces[0]
            url_or_cb = pieces[1]
            style = pieces[2].lower() if len(pieces) >= 3 and pieces[2].lower() in BUTTON_STYLES else None
            if label:
                row.append({"text": label, "url": url_or_cb, "style": style})
        if row:
            rows.append(row)
    return rows if rows else None


def _build_keyboard(button_rows: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not button_rows:
        return None
    kb_rows = []
    for row in button_rows:
        kb_row = []
        for btn in row:
            kwargs = {"text": btn["text"]}
            url = btn.get("url", "")
            if url.startswith("http") or url.startswith("tg://"):
                kwargs["url"] = url
            else:
                kwargs["callback_data"] = url
            if btn.get("style"):
                kwargs["style"] = btn["style"]
            kb_row.append(InlineKeyboardButton(**kwargs))
        kb_rows.append(kb_row)
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


def _contains_custom_emoji_markup(text: str) -> bool:
    return bool(text and CUSTOM_EMOJI_TAG_RE.search(text))


async def _send_custom_emoji_channel_notice(message: Message, bot: Bot, channel: str, text: str):
    if not _contains_custom_emoji_markup(text):
        return

    try:
        chat = await bot.get_chat(channel)
    except Exception as e:
        logger.warning(f"Cannot inspect chat {channel!r} for custom emoji support: {e}")
        return

    if getattr(chat, "type", None) != "channel":
        return

    await message.answer(
        "⚠️ <b>Premium emoji и канал</b>\n\n"
        "По Telegram Bot API 9.4+ custom emoji для ботов с Telegram Premium у владельца "
        "работают только в <b>private/group/supergroup</b>.\n"
        "Для <b>channel</b>-постов боту нужен дополнительный collectible username, "
        "назначенный через Fragment.\n\n"
        "Иначе Telegram публикует обычный fallback emoji вместо <code>&lt;tg-emoji&gt;</code>.",
        parse_mode="HTML",
    )


def _parse_schedule(raw: str) -> datetime | None:
    raw = raw.strip().lower()
    if raw in ("now", "сейчас"):
        return datetime.now(tz=MSK)
    if raw.startswith("+"):
        val = raw[1:]
        try:
            if val.endswith("h"):
                return datetime.now(tz=MSK) + timedelta(hours=int(val[:-1]))
            elif val.endswith("d"):
                return datetime.now(tz=MSK) + timedelta(days=int(val[:-1]))
            else:
                return datetime.now(tz=MSK) + timedelta(minutes=int(val))
        except ValueError:
            return None
    for fmt in ("%d.%m %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now().year)
            return dt.replace(tzinfo=MSK)
        except ValueError:
            continue
    return None


async def _send_post(bot: Bot, channel: str, text: str,
                     photo: str | None, button_rows: list | None):
    kb = _build_keyboard(button_rows)
    if photo:
        await bot.send_photo(
            chat_id=channel, photo=photo,
            caption=text, reply_markup=kb, parse_mode="HTML",
        )
    else:
        await bot.send_message(
            chat_id=channel, text=text,
            reply_markup=kb, parse_mode="HTML",
        )


def _cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")
    ]])


def _buttons_help() -> str:
    return (
        "🔘 <b>Шаг 4/5 — Кнопки</b>\n\n"
        "Формат: <code>Текст | URL | стиль</code>\n"
        "Стили: <code>primary</code> 🔵 · <code>success</code> 🟢 · <code>danger</code> 🔴\n"
        "В ряд: через <code>;;</code>  |  Новая строка = новый ряд\n\n"
        "<b>Пример:</b>\n"
        "<code>🔗 Наша страница | https://t.me/bot/app | primary\n"
        "💳 Card | https://tribute.tg | success ;; ⭐ Stars | https://t.me/bot | primary\n"
        "🎁 Try Free | https://t.me/bot | success</code>\n\n"
        "Или <code>-</code> чтобы без кнопок."
    )


# ── /list_presets ─────────────────────────────────────────────────────────────

@router.message(Command("list_presets"))
async def cmd_list_presets(message: Message):
    if not is_admin(message.from_user.id):
        return
    presets = _load_presets()
    if not presets:
        await message.answer(
            "📭 Пресетов нет.\n\nСоздай пост через /new_post, "
            "нажми «💾 Сохранить пресет» и дай ему имя."
        )
        return
    lines = ["📋 <b>Сохранённые пресеты:</b>\n"]
    for name, data in presets.items():
        channel = data.get("channel", "—")
        has_photo = "🖼" if data.get("photo") else "📝"
        btn_count = sum(len(r) for r in (data.get("buttons") or []))
        lines.append(
            f"{has_photo} <b>{name}</b> → <code>{channel}</code>"
            + (f" · {btn_count} кнопок" if btn_count else "")
            + f"\n   👉 /new_post {name}"
        )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ── /del_preset ───────────────────────────────────────────────────────────────

@router.message(Command("del_preset"))
async def cmd_del_preset(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /del_preset <название>")
        return
    name = args[1].strip()
    presets = _load_presets()
    if name not in presets:
        await message.answer(f"❌ Пресет «{name}» не найден.")
        return
    del presets[name]
    _save_presets(presets)
    await message.answer(f"🗑 Пресет «<b>{name}</b>» удалён.", parse_mode="HTML")


# ── /new_post [preset] ────────────────────────────────────────────────────────

@router.message(Command("new_post"))
async def cmd_new_post(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        preset_name = args[1].strip()
        presets = _load_presets()
        if preset_name in presets:
            preset = presets[preset_name]
            await state.clear()
            await state.update_data(**preset)
            await state.set_state(PostForm.confirm)
            await _show_preset_preview(message, preset, preset_name, bot)
            return
        else:
            await message.answer(f"⚠️ Пресет «{preset_name}» не найден. Начинаем с нуля.\n")

    await state.clear()
    await state.set_state(PostForm.channel)

    presets = _load_presets()
    preset_hint = ""
    if presets:
        names = " · ".join(f"/new_post {n}" for n in list(presets.keys())[:5])
        preset_hint = f"\n\n💡 Быстрый старт из пресета:\n{names}"

    await message.answer(
        "📢 <b>Создание нового поста</b>\n\n"
        "<b>Шаг 1/5 — Канал</b>\n"
        "Укажи <code>@username</code> или числовой ID канала."
        f"{preset_hint}",
        parse_mode="HTML",
        reply_markup=_cancel_kb()
    )


async def _show_preset_preview(message: Message, preset: dict, preset_name: str, bot: Bot):
    channel = preset.get("channel", "—")
    text = preset.get("text", "")
    photo = preset.get("photo")
    buttons = preset.get("buttons")
    kb = _build_keyboard(buttons)

    header = (
        f"📋 <b>Пресет: {preset_name}</b>\n"
        f"📢 Канал: <code>{channel}</code>\n"
        f"{'─' * 28}\n\n"
    )

    await _send_custom_emoji_channel_notice(message, bot, channel, text)

    try:
        if photo:
            await message.answer_photo(photo=photo, caption=header + text,
                                        reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(header + text, reply_markup=kb,
                                  parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка предпросмотра: {e}")
        return

    await message.answer(
        "Что делаем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data="post_now_preset"),
             InlineKeyboardButton(text="📅 Запланировать", callback_data="preset_schedule")],
            [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="preset_edit_text"),
             InlineKeyboardButton(text="🏷 Изменить канал", callback_data="preset_edit_channel")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
        ])
    )


@router.callback_query(F.data == "post_now_preset", PostForm.confirm)
async def send_preset_now(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    channel  = data.get("channel", "")
    text     = data.get("text", "")
    photo    = data.get("photo")
    buttons  = data.get("buttons")
    post_id  = _next_id()
    try:
        await _send_post(bot, channel, text, photo, buttons)
        await call.message.edit_text(
            f"✅ Пост <b>#{post_id}</b> отправлен в <code>{channel}</code>!",
            parse_mode="HTML"
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await call.answer()


@router.callback_query(F.data == "preset_schedule", PostForm.confirm)
async def preset_go_schedule(call: CallbackQuery, state: FSMContext):
    await state.set_state(PostForm.schedule)
    await _ask_schedule(call.message, edit=True)
    await call.answer()


@router.callback_query(F.data == "preset_edit_text", PostForm.confirm)
async def preset_edit_text(call: CallbackQuery, state: FSMContext):
    await state.set_state(PostForm.text)
    await call.message.edit_text(
        "✏️ Введи новый текст поста (HTML).\n\n"
        "Премиум эмодзи:\n"
        "<code>&lt;tg-emoji emoji-id=\"...\"&gt;🔥&lt;/tg-emoji&gt;</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb()
    )
    await call.answer()


@router.callback_query(F.data == "preset_edit_channel", PostForm.confirm)
async def preset_edit_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(PostForm.channel)
    await call.message.edit_text(
        "🏷 Введи новый @username или ID канала:",
        reply_markup=_cancel_kb()
    )
    await call.answer()


# ── FSM шаги ─────────────────────────────────────────────────────────────────

@router.message(PostForm.channel)
async def got_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(channel=message.text.strip())
    await state.set_state(PostForm.text)
    await message.answer(
        "✏️ <b>Шаг 2/5 — Текст поста</b>\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href='url'&gt;</code>\n\n"
        "Премиум эмодзи:\n"
        "<code>&lt;tg-emoji emoji-id=\"5285430309720966085\"&gt;👍&lt;/tg-emoji&gt;</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb()
    )


@router.message(PostForm.text)
async def got_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(text=message.text or "")
    await state.set_state(PostForm.photo)
    await message.answer(
        "🖼 <b>Шаг 3/5 — Фото</b>\n\nОтправь фото или нажми «Пропустить».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить →", callback_data="post_skip_photo")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
        ])
    )


@router.callback_query(F.data == "post_skip_photo", PostForm.photo)
async def skip_photo(call: CallbackQuery, state: FSMContext):
    await state.update_data(photo=None)
    await state.set_state(PostForm.buttons)
    await call.message.edit_text(_buttons_help(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить →", callback_data="post_skip_buttons")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
        ]))
    await call.answer()


@router.message(PostForm.photo)
async def got_photo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(photo=message.photo[-1].file_id if message.photo else None)
    await state.set_state(PostForm.buttons)
    await message.answer(_buttons_help(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить →", callback_data="post_skip_buttons")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
        ]))


@router.callback_query(F.data == "post_skip_buttons", PostForm.buttons)
async def skip_buttons(call: CallbackQuery, state: FSMContext):
    await state.update_data(buttons=None)
    await state.set_state(PostForm.schedule)
    await _ask_schedule(call.message, edit=True)
    await call.answer()


@router.message(PostForm.buttons)
async def got_buttons(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    buttons = _parse_buttons(raw) if raw != "-" else None
    await state.update_data(buttons=buttons)
    await state.set_state(PostForm.schedule)
    await _ask_schedule(message)


async def _ask_schedule(message: Message, edit: bool = False):
    text = (
        "🕐 <b>Шаг 5/5 — Время отправки</b>\n\n"
        "• <code>now</code> — сразу\n"
        "• <code>+30</code> — через 30 мин\n"
        "• <code>+2h</code> — через 2 часа\n"
        "• <code>+1d</code> — через 1 день\n"
        "• <code>25.03 14:30</code> — точная дата (МСК)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Сейчас", callback_data="post_now")],
        [InlineKeyboardButton(text="+1ч", callback_data="post_plus1h"),
         InlineKeyboardButton(text="+3ч", callback_data="post_plus3h"),
         InlineKeyboardButton(text="+1д", callback_data="post_plus1d")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(
    F.data.in_({"post_now", "post_plus1h", "post_plus3h", "post_plus1d"}),
    PostForm.schedule
)
async def quick_schedule(call: CallbackQuery, state: FSMContext, bot: Bot):
    mapping = {"post_now": "now", "post_plus1h": "+1h",
               "post_plus3h": "+3h", "post_plus1d": "+1d"}
    await _process_schedule(call.message, state, mapping[call.data], bot)
    await call.answer()


@router.message(PostForm.schedule)
async def got_schedule(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await _process_schedule(message, state, message.text.strip(), bot)


async def _process_schedule(message: Message, state: FSMContext, raw: str, bot: Bot):
    send_at = _parse_schedule(raw)
    if not send_at:
        await message.answer("⚠️ Не понял формат. Попробуй: now, +30, +2h, 25.03 14:30")
        return

    data = await state.get_data()
    await state.update_data(send_at=send_at.isoformat())
    await state.set_state(PostForm.confirm)

    channel = data.get("channel", "—")
    text = data.get("text", "")
    photo = data.get("photo")
    buttons = data.get("buttons")
    kb = _build_keyboard(buttons)
    time_str = send_at.strftime("%d.%m.%Y %H:%M МСК")

    header = (
        f"👀 <b>Предпросмотр</b>\n"
        f"📢 <code>{channel}</code> · 🕐 <b>{time_str}</b>\n"
        f"{'─' * 28}\n\n"
    )

    await _send_custom_emoji_channel_notice(message, bot, channel, text)

    try:
        if photo:
            await message.answer_photo(photo=photo, caption=header + text,
                                        reply_markup=kb, parse_mode="HTML")
        else:
            await message.answer(header + text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка предпросмотра: {e}")
        return

    await message.answer(
        "Всё верно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="post_confirm"),
             InlineKeyboardButton(text="💾 Сохранить пресет", callback_data="post_save_preset")],
            [InlineKeyboardButton(text="✏️ Заново", callback_data="post_restart"),
             InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel")],
        ])
    )


# ── Сохранение пресета ────────────────────────────────────────────────────────

@router.callback_query(F.data == "post_save_preset", PostForm.confirm)
async def ask_preset_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(PostForm.save_name)
    presets = _load_presets()
    hint = f"\nСуществующие: {', '.join(f'<code>{n}</code>' for n in presets)}" if presets else ""
    await call.message.answer(
        f"💾 Введи название пресета (без пробелов):{hint}\n\n"
        f"Пример: <code>espe</code>, <code>main</code>, <code>en_post</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb()
    )
    await call.answer()


@router.message(PostForm.save_name)
async def save_preset_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip().replace(" ", "_")
    data = await state.get_data()
    await state.set_state(PostForm.confirm)

    presets = _load_presets()
    presets[name] = {
        "channel": data.get("channel", ""),
        "text":    data.get("text", ""),
        "photo":   data.get("photo"),
        "buttons": data.get("buttons"),
    }
    _save_presets(presets)

    await message.answer(
        f"✅ Пресет <b>{name}</b> сохранён!\n\n"
        f"Следующий раз просто:\n<code>/new_post {name}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить пост", callback_data="post_confirm")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="post_cancel")],
        ])
    )


# ── Подтверждение и отправка ──────────────────────────────────────────────────

@router.callback_query(F.data == "post_confirm", PostForm.confirm)
async def confirm_post(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()

    channel     = data.get("channel", "")
    text        = data.get("text", "")
    photo       = data.get("photo")
    buttons     = data.get("buttons")
    send_at_raw = data.get("send_at")

    send_at = datetime.fromisoformat(send_at_raw) if send_at_raw else datetime.now(tz=MSK)
    delay   = (send_at - datetime.now(tz=MSK)).total_seconds()
    post_id = _next_id()

    if delay <= 5:
        try:
            await _send_post(bot, channel, text, photo, buttons)
            await call.message.edit_text(
                f"✅ Пост <b>#{post_id}</b> отправлен в <code>{channel}</code>!",
                parse_mode="HTML"
            )
        except Exception as e:
            await call.message.edit_text(f"❌ Ошибка: {e}")
    else:
        async def _delayed():
            await asyncio.sleep(delay)
            try:
                await _send_post(bot, channel, text, photo, buttons)
                _scheduled.pop(post_id, None)
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"✅ Отложенный пост <b>#{post_id}</b> отправлен в <code>{channel}</code>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    break
            except Exception as e:
                logger.error(f"Post #{post_id} failed: {e}")

        task = asyncio.create_task(_delayed())
        _scheduled[post_id] = {
            "channel": channel,
            "text": text[:50] + "…" if len(text) > 50 else text,
            "send_at": send_at,
            "task": task,
        }
        time_str = send_at.strftime("%d.%m.%Y %H:%M МСК")
        await call.message.edit_text(
            f"⏳ Пост <b>#{post_id}</b> запланирован на <b>{time_str}</b>\n"
            f"📢 <code>{channel}</code>\n\n"
            f"Отменить: /cancel_post {post_id}",
            parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data == "post_restart")
async def restart_post(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Создание поста отменено. Начни заново: /new_post")
    await call.answer()


@router.callback_query(F.data == "post_cancel")
async def cancel_post_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено.")
    await call.answer()


# ── /scheduled_posts и /cancel_post ──────────────────────────────────────────

@router.message(Command("scheduled_posts"))
async def cmd_scheduled_posts(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not _scheduled:
        await message.answer("📭 Нет запланированных постов.")
        return
    lines = ["📋 <b>Запланированные посты:</b>\n"]
    for pid, info in _scheduled.items():
        time_str = info["send_at"].strftime("%d.%m %H:%M МСК")
        lines.append(
            f"<b>#{pid}</b> → <code>{info['channel']}</code> · {time_str}\n"
            f"   {info['text']}\n"
            f"   /cancel_post {pid}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("cancel_post"))
async def cmd_cancel_post(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /cancel_post <id>")
        return
    try:
        pid = int(args[1])
    except ValueError:
        await message.answer("⚠️ ID должен быть числом.")
        return
    info = _scheduled.pop(pid, None)
    if not info:
        await message.answer(f"❌ Пост #{pid} не найден.")
        return
    task = info.get("task")
    if task and not task.done():
        task.cancel()
    await message.answer(f"🗑 Пост <b>#{pid}</b> отменён.", parse_mode="HTML")
