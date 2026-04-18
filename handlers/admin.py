import logging
from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import User, get_user, Artist, get_artist, get_all_artists, create_artist, delete_artist, update_artist_stats, set_artist_tag, ArtistContent, add_artist_content, get_artist_content, clear_artist_content, get_artist_content_counts, Tag, get_all_tags, get_tag, create_tag, delete_tag, CustomBadge, create_custom_badge, get_custom_badges, delete_custom_badge, assign_tier_badge
from handlers.group import enable_night_mode, disable_night_mode
from config import ADMIN_IDS, GROUP_ID, INVITE_LINK

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ─── /add_units [user_id] [дней] ─────────────────────────────────────────────

@router.message(Command("add_units"))
async def cmd_add_units(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await message.reply("Использование: /add_units [user_id] [дней]")
        return

    user_id, days = int(args[0]), int(args[1])
    user = await get_user(session, user_id)

    if not user:
        await message.reply(f"❌ Пользователь {user_id} не найден в базе.")
        return

    user.units += days
    user.is_active = True
    await session.commit()

    await message.reply(f"✅ Пользователю {user_id} добавлено {days} дней. Итого: {user.units}")

    # Уведомляем пользователя
    try:
        lang = user.lang or "en"
        if lang == "ru":
            text = (f"🎁 Вам начислено <b>{days} дней</b> подписки!\n\n"
                    f"📅 Итого: <b>{user.units} дней</b>\n\n"
                    f"🔗 Ссылка для входа:\n{INVITE_LINK}")
        else:
            text = (f"🎁 <b>{days} days</b> added to your subscription!\n\n"
                    f"📅 Total: <b>{user.units} days</b>\n\n"
                    f"🔗 Join link:\n{INVITE_LINK}")
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Cannot notify user {user_id}: {e}")


# ─── /check_user [user_id] ────────────────────────────────────────────────────

@router.message(Command("check_user"))
async def cmd_check_user(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.reply("Использование: /check_user [user_id]")
        return

    user = await get_user(session, int(args[0]))
    if not user:
        await message.reply("❌ Пользователь не найден.")
        return

    badges_list = _parse_badges(getattr(user, 'badge', None))
    badge_line = f"\nБейджи: <b>{' + '.join(badges_list)}</b>" if badges_list else ""
    await message.reply(
        f"👤 <b>Пользователь</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Язык: {user.lang}\n"
        f"Дней: <b>{user.units}</b>\n"
        f"Активен: {'✅' if user.is_active else '❌'}"
        f"{badge_line}",
        parse_mode="HTML"
    )


# ─── /kick — кик (по reply или ID) ───────────────────────────────────────────

@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    target_id = _get_target(message)
    if not target_id:
        await message.reply("Ответьте на сообщение или укажите /kick [user_id]")
        return

    try:
        await bot.ban_chat_member(GROUP_ID, target_id)
        await bot.unban_chat_member(GROUP_ID, target_id)

        user = await get_user(session, target_id)
        if user:
            user.is_active = False
            await session.commit()

        await message.reply(f"✅ Пользователь {target_id} кикнут.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ─── /ban — перманентный бан ──────────────────────────────────────────────────

@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    target_id = _get_target(message)
    if not target_id:
        await message.reply("Ответьте на сообщение или укажите /ban [user_id]")
        return

    try:
        await bot.ban_chat_member(GROUP_ID, target_id)

        user = await get_user(session, target_id)
        if user:
            user.units = 0
            user.is_active = False
            await session.commit()

        await message.reply(f"🚫 Пользователь {target_id} забанен навсегда.")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")


# ─── /nightmode_on / /nightmode_off ──────────────────────────────────────────

@router.message(Command("nightmode_on"))
async def cmd_night_on(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await enable_night_mode(bot)
    await message.reply("🌙 Ночной режим включён.")


@router.message(Command("nightmode_off"))
async def cmd_night_off(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await disable_night_mode(bot)
    await message.reply("☀️ Ночной режим выключен.")


# ─── /admin_help ─────────────────────────────────────────────────────────────

@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply(
        "🛠 <b>Команды администратора</b>\n\n"
        "📊 <b>Пользователи:</b>\n"
        "/add_units [user_id] [дней] — начислить дни\n"
        "/remove_units [user_id] [дней] — забрать дни\n"
        "/set_units [user_id] [дней] — установить дни\n"
        "/check_user [user_id] — проверить пользователя\n"
        "/allusers — список всех пользователей\n"
        "/activeusers — только с активной подпиской (units > 0)\n\n"
        "🏅 <b>Бейджи (накапливаются!):</b>\n"
        "/badges — список всех бейджей\n"
        "/create_badge \"NAME\" | #HEX — создать кастомный бейдж\n"
        "/set_badge [user_id] [BADGE] — выдать бейдж\n"
        "/remove_badge [user_id] [BADGE] — убрать конкретный\n"
        "/remove_badge [user_id] all — убрать все\n\n"
        "🚫 <b>Модерация:</b>\n"
        "/kick [user_id] — кикнуть из группы\n"
        "/ban [user_id] — забанить навсегда\n"
        "/nightmode_on — включить ночной режим\n"
        "/nightmode_off — выключить ночной режим\n\n"
        "💎 <b>Тиры подписки:</b>\n"
        "/set_tier [user_id] [plus|pro|elite|free] — установить тир\n"
        "/users_tiers — активные юзеры с тирами и днями\n"
        "/sync_tier_badges — выдать бейджи PLUS/PRO/ELITE всем по тиру\n"
        "   plus — стандарт (€6), pro — расширенный (€8), elite — скоро\n\n"
        "🎨 <b>Артисты и контент:</b>\n"
        "/set_cont — управление артистами\n"
        "/add_ready_on [имя] — показать READY бейдж (выше всех)\n"
        "/add_ready_off [имя] — убрать READY бейдж\n"
        "/add_cont [имя] — добавить видео/фото артисту\n"
        "/list_cont [имя] — просмотреть контент артиста\n"
        "/clear_cont [имя] videos|photos|all — очистить контент\n\n"
        "📣 <b>Постинг в каналы:</b>\n"
        "/new_post — создать пост (текст, фото, кнопки, отсрочка)\n"
        "/scheduled_posts — список запланированных постов\n"
        "/cancel_post [id] — отменить запланированный пост"
    )


# ─── /broadcast — рассылка пользователям ──────────────────────────────────────

from aiogram.fsm.state import State, StatesGroup as _SG

class BroadcastForm(_SG):
    content  = State()
    buttons  = State()
    confirm  = State()


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(BroadcastForm.content)
    await message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Шаг 1/3 — Отправь сообщение для рассылки.\n\n"
        "Поддерживается:\n"
        "• Текст (HTML)\n"
        "• Фото с подписью\n"
        "• Custom emoji: <code>&lt;tg-emoji emoji-id=\"ID\"&gt;👍&lt;/tg-emoji&gt;</code>",
        parse_mode="HTML",
    )


@router.message(BroadcastForm.content)
async def broadcast_got_content(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = {}
    if message.photo:
        data["photo"] = message.photo[-1].file_id
        data["text"] = message.caption or ""
    else:
        data["text"] = message.text or ""
    await state.update_data(**data)
    await state.set_state(BroadcastForm.buttons)
    await message.answer(
        "Шаг 2/3 — Кнопки (необязательно).\n\n"
        "Формат: <code>Текст | URL</code>\n"
        "Несколько в ряд: <code>Кнопка1 | url1 ;; Кнопка2 | url2</code>\n"
        "Новая строка = новый ряд.\n\n"
        "Или отправь <code>-</code> без кнопок.",
        parse_mode="HTML",
    )


@router.message(BroadcastForm.buttons)
async def broadcast_got_buttons(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    kb = None
    if raw not in ("-", "нет", "no", "skip"):
        rows = []
        for line in raw.splitlines():
            row = []
            for part in line.split(";;"):
                part = part.strip()
                if "|" not in part:
                    continue
                pieces = [p.strip() for p in part.split("|", 1)]
                if len(pieces) == 2 and pieces[0] and pieces[1]:
                    row.append(InlineKeyboardButton(text=pieces[0], url=pieces[1]))
            if row:
                rows.append(row)
        if rows:
            kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await state.update_data(keyboard=kb)
    await state.set_state(BroadcastForm.confirm)

    data = await state.get_data()
    text = data.get("text", "")
    photo = data.get("photo")

    # Preview
    try:
        if photo:
            await message.answer_photo(
                photo=photo, caption="👀 <b>Предпросмотр:</b>\n\n" + text,
                reply_markup=kb, parse_mode="HTML",
            )
        else:
            await message.answer(
                "👀 <b>Предпросмотр:</b>\n\n" + text,
                reply_markup=kb, parse_mode="HTML",
            )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка предпросмотра: {e}")
        return

    await message.answer(
        "Отправить рассылку?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить всем", callback_data="broadcast_send")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
        ])
    )


@router.callback_query(F.data == "broadcast_send", BroadcastForm.confirm)
async def broadcast_confirm(call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    await state.clear()

    text = data.get("text", "")
    photo = data.get("photo")
    kb = data.get("keyboard")

    # Get all users
    from sqlalchemy import select as sa_select
    result = await session.execute(sa_select(User.telegram_id))
    user_ids = [r[0] for r in result.all()]

    await call.message.edit_text(f"📤 Рассылка начата... 0/{len(user_ids)}")
    await call.answer()

    sent, failed = 0, 0
    for i, uid in enumerate(user_ids):
        try:
            if photo:
                await bot.send_photo(uid, photo=photo, caption=text,
                                     reply_markup=kb, parse_mode="HTML")
            else:
                await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 25 == 0:
            try:
                await call.message.edit_text(f"📤 Рассылка... {i+1}/{len(user_ids)} (✅ {sent} / ❌ {failed})")
            except Exception:
                pass
            import asyncio
            await asyncio.sleep(0.5)  # Rate limit

    await call.message.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📤 Всего: {len(user_ids)}\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "broadcast_cancel", BroadcastForm.confirm)
async def broadcast_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Рассылка отменена.")
    await call.answer()


# ─── Вспомогательная функция ──────────────────────────────────────────────────

def _get_target(message: Message) -> int | None:
    if message.reply_to_message:
        return message.reply_to_message.from_user.id
    args = message.text.split()[1:]
    if args and args[0].isdigit():
        return int(args[0])
    return None

@router.message(Command("allusers"))
async def cmd_all_users(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    result = await session.execute(
        select(User).order_by(User.units.desc())
    )
    users = result.scalars().all()

    if not users:
        await message.reply("База пользователей пуста.")
        return

    lines_active = []
    lines_inactive = []

    for u in users:
        nick = f"@{u.username}" if u.username else f"id{u.telegram_id}"
        method_labels = {"stars": "⭐", "tribute": "💳"}
        method = method_labels.get(u.last_payment_method, u.last_payment_method or "—")
        trial_icon = "🎁" if u.trial_used else ""
        line = f"{'✅' if u.units > 0 else '❌'} {nick} | <code>{u.telegram_id}</code> | {u.units}д | {method} {trial_icon}"
        if u.units > 0:
            lines_active.append(line)
        else:
            lines_inactive.append(line)

    total = len(users)
    active = len(lines_active)

    header = (
        f"👥 <b>Все пользователи: {total}</b>\n"
        f"✅ Активных: {active} | ❌ Без подписки: {total - active}\n"
        f"{'─' * 30}\n"
    )

    all_lines = lines_active + lines_inactive

    chunk_size = 30
    for i in range(0, len(all_lines), chunk_size):
        chunk = all_lines[i:i + chunk_size]
        text = (header if i == 0 else "") + "\n".join(chunk)
        await message.answer(text, parse_mode="HTML")

@router.message(Command("activeusers"))
async def cmd_active_users(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    result = await session.execute(
        select(User).where(User.units > 0).order_by(User.units.desc())
    )
    users = result.scalars().all()

    if not users:
        await message.reply("❌ Нет пользователей с активной подпиской.")
        return

    lines = []
    for u in users:
        nick = f"@{u.username}" if u.username else f"id{u.telegram_id}"
        method_labels = {"stars": "⭐", "tribute": "💳"}
        method = method_labels.get(u.last_payment_method, u.last_payment_method or "—")
        trial_icon = "🎁" if u.trial_used else ""
        raw_badges = _parse_badges(getattr(u, 'badge', None))
        badge_icon = " [" + ",".join(raw_badges) + "]" if raw_badges else ""
        lines.append(f"✅ {nick} | <code>{u.telegram_id}</code> | {u.units}д | {method}{badge_icon} {trial_icon}")

    header = (
        f"✅ <b>Активные пользователи: {len(users)}</b>\n"
        f"{'─' * 30}\n"
    )

    chunk_size = 30
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        text = (header if i == 0 else "") + "\n".join(chunk)
        await message.answer(text, parse_mode="HTML")


    # ─── /remove_units <user_id> <дней> ──────────────────────────────────────────

@router.message(Command("remove_units"))
async def cmd_remove_units(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await message.reply("Использование: /remove_units [user_id] [дней]")
        return

    user_id, days = int(args[0]), int(args[1])
    user = await get_user(session, user_id)

    if not user:
        await message.reply(f"❌ Пользователь {user_id} не найден.")
        return

    user.units = max(0, user.units - days)
    if user.units == 0:
        user.is_active = False
        user.tier = 'plus'  # reset tier on expiry
    await session.commit()

    await message.reply(f"✅ У пользователя {user_id} забрано {days} дней. Итого: {user.units}")


# ─── /set_units <user_id> <дней> ─────────────────────────────────────────────

@router.message(Command("set_units"))
async def cmd_set_units(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 2 or not all(a.isdigit() for a in args):
        await message.reply("Использование: /set_units [user_id] [дней]")
        return

    user_id, days = int(args[0]), int(args[1])
    user = await get_user(session, user_id)

    if not user:
        await message.reply(f"❌ Пользователь {user_id} не найден.")
        return

    user.units = days
    user.is_active = days > 0
    await session.commit()

    await message.reply(f"✅ Пользователю {user_id} установлено {days} дней.")


# ─── /set_badge, /remove_badge ───────────────────────────────────────────────

VALID_BADGES = {"VIP", "TESTER", "OLD", "FOUNDER", "EARLY", "DONOR"}

BADGE_INFO = {
    "VIP":     ("💚", "Особый статус / VIP доступ",    "#CCFF00"),
    "TESTER":  ("🔵", "Бета-тестер проекта",           "#00E5FF"),
    "OLD":     ("🟡", "Ранний пользователь (old sub)", "#FFD740"),
    "FOUNDER": ("🟣", "Основатель / поддержал в нуле", "#E040FB"),
    "EARLY":   ("🟠", "Пришёл одним из первых",        "#FF6D00"),
    "DONOR":   ("🔴", "Донатер / спонсор проекта",     "#FF4081"),
}


def _parse_badges(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [b.strip() for b in raw.split(",") if b.strip()]


def _serialize_badges(badges: list[str]) -> str | None:
    return ",".join(badges) if badges else None


@router.message(Command("badges"))
async def cmd_badges(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    lines = ["🏅 <b>Встроенные бейджи:</b>\n"]
    for name, (icon, desc, color) in BADGE_INFO.items():
        lines.append(f"{icon} <b>{name}</b> — {desc}\n   Цвет: <code>{color}</code>")
    custom = await get_custom_badges(session)
    if custom:
        lines.append("\n🎨 <b>Кастомные бейджи:</b>\n")
        for b in custom:
            lines.append(f"● <b>{b.name}</b> — <code>{b.color}</code>")
    lines.append("\n<i>Создать:</i> /create_badge \"NAME\" | #HEX")
    lines.append("<i>Выдать:</i> /set_badge [user_id] [BADGE]")
    lines.append("<i>Убрать конкретный:</i> /remove_badge [user_id] [BADGE]")
    lines.append("<i>Убрать все:</i> /remove_badge [user_id] all")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("create_badge"))
async def cmd_create_badge(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    text = message.text[len("/create_badge"):].strip()
    if "|" not in text:
        await message.reply(
            "Использование: /create_badge \"NAME\" | #HEX\n\n"
            "Пример: /create_badge \"WHALE\" | #FF00AA"
        )
        return
    parts = text.split("|", 1)
    name = parts[0].strip().strip('"').strip("'").upper()
    color = parts[1].strip()
    if not name or len(name) > 32:
        await message.reply("❌ Имя бейджа должно быть от 1 до 32 символов.")
        return
    if not color.startswith("#") or len(color) not in (4, 7):
        await message.reply("❌ Цвет должен быть в формате #RGB или #RRGGBB")
        return
    if name in VALID_BADGES:
        await message.reply(f"❌ Бейдж <b>{name}</b> уже существует как встроенный.", parse_mode="HTML")
        return
    existing = await get_custom_badges(session)
    if any(b.name == name for b in existing):
        await message.reply(f"❌ Кастомный бейдж <b>{name}</b> уже существует.", parse_mode="HTML")
        return
    badge = await create_custom_badge(session, name, color)
    await message.reply(
        f"✅ Бейдж создан!\n\n<b>{badge.name}</b> — <code>{badge.color}</code>\n\n"
        "Выдать: /set_badge [user_id] " + badge.name,
        parse_mode="HTML"
    )


@router.message(Command("set_badge"))
async def cmd_set_badge(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) != 2 or not args[0].isdigit():
        await message.reply(
            "Использование: /set_badge [user_id] [BADGE]\n\n"
            f"Доступные бейджи: {', '.join(sorted(VALID_BADGES))}\n\n"
            "Бейджи накапливаются — выдай несколько командой по очереди."
        )
        return

    user_id = int(args[0])
    badge = args[1].upper()
    custom_names = {b.name for b in await get_custom_badges(session)}
    all_valid = VALID_BADGES | custom_names
    if badge not in all_valid:
        await message.reply(f"❌ Неверный бейдж. Доступные: {', '.join(sorted(all_valid))}")
        return

    user = await get_user(session, user_id)
    if not user:
        await message.reply(f"❌ Пользователь {user_id} не найден.")
        return

    existing = _parse_badges(user.badge)
    if badge in existing:
        await message.reply(f"ℹ️ У пользователя уже есть бейдж <b>{badge}</b>.", parse_mode="HTML")
        return

    existing.append(badge)
    user.badge = _serialize_badges(existing)
    await session.commit()

    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
    all_badges = " + ".join(existing)
    await message.reply(
        f"✅ Бейдж <b>{badge}</b> добавлен пользователю {nick}\n"
        f"Все бейджи: <b>{all_badges}</b>",
        parse_mode="HTML"
    )


@router.message(Command("remove_badge"))
async def cmd_remove_badge(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.reply(
            "Использование:\n"
            "/remove_badge [user_id] [BADGE] — убрать конкретный бейдж\n"
            "/remove_badge [user_id] all — убрать все бейджи"
        )
        return

    user = await get_user(session, int(args[0]))
    if not user:
        await message.reply(f"❌ Пользователь {args[0]} не найден.")
        return

    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"

    if len(args) >= 2 and args[1].upper() != "ALL":
        badge_to_remove = args[1].upper()
        existing = _parse_badges(user.badge)
        if badge_to_remove not in existing:
            await message.reply(f"ℹ️ У пользователя нет бейджа <b>{badge_to_remove}</b>.", parse_mode="HTML")
            return
        existing.remove(badge_to_remove)
        user.badge = _serialize_badges(existing)
        await session.commit()
        remaining = " + ".join(existing) if existing else "нет"
        await message.reply(
            f"✅ Бейдж <b>{badge_to_remove}</b> убран у {nick}\n"
            f"Остались: <b>{remaining}</b>",
            parse_mode="HTML"
        )
    else:
        user.badge = None
        await session.commit()
        await message.reply(f"✅ Все бейджи убраны у пользователя {nick}", parse_mode="HTML")


# ─── /set_tier ───────────────────────────────────────────────────────────────

VALID_TIERS = {"plus", "pro", "elite", "free"}

@router.message(Command("set_tier"))
async def cmd_set_tier(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if len(args) != 2 or not args[0].isdigit():
        await message.reply("Использование: /set_tier [user_id] [plus|pro|elite|free]")
        return
    tier = args[1].lower()
    if tier not in VALID_TIERS:
        await message.reply(f"❌ Неверный тир. Доступные: {', '.join(VALID_TIERS)}")
        return
    user = await get_user(session, int(args[0]))
    if not user:
        await message.reply(f"❌ Пользователь {args[0]} не найден.")
        return
    user.tier = tier
    assign_tier_badge(user)
    await session.commit()
    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
    await message.reply(f"✅ Тир пользователя {nick} установлен: <b>{tier.upper()}</b>", parse_mode="HTML")


# ─── /sync_tier_badges ───────────────────────────────────────────────────────

@router.message(Command("sync_tier_badges"))
async def cmd_sync_tier_badges(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    result = await session.execute(select(User))
    users = result.scalars().all()
    count = 0
    for u in users:
        assign_tier_badge(u)
        count += 1
    await session.commit()
    await message.reply(f"✅ Тир-бейджи синхронизированы у <b>{count}</b> пользователей.", parse_mode="HTML")


# ─── /users_tiers ────────────────────────────────────────────────────────────

@router.message(Command("users_tiers"))
async def cmd_users_tiers(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    result = await session.execute(
        select(User).where(User.units > 0).order_by(User.tier, User.units.desc())
    )
    users = result.scalars().all()
    if not users:
        await message.reply("Нет активных пользователей.")
        return

    tier_icon = {'pro': '💎', 'elite': '👑', 'plus': '⭐', 'free': '🆓'}
    lines = ["💎 <b>Активные подписчики по тирам:</b>\n"]
    current_tier = None
    for u in users:
        t = (u.tier or 'plus').lower()
        if t != current_tier:
            current_tier = t
            lines.append(f"\n{tier_icon.get(t,'•')} <b>{t.upper()}</b>")
        nick = f"@{u.username}" if u.username else f"id{u.telegram_id}"
        lines.append(f"  {nick} — {u.units} дн.")

    # Split into chunks of ≤4096 chars
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.reply(text[i:i+4000], parse_mode="HTML")


# ─── /add_tag, /list_tags, /del_tag ──────────────────────────────────────────

@router.message(Command("add_tag"))
async def cmd_add_tag(message: Message, session: AsyncSession):
    """Usage: /add_tag Ear Licking | #FF5050"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or "|" not in args[1]:
        await message.reply(
            "Использование: /add_tag Название | #HEX\n"
            "Пример: <code>/add_tag Ear Licking | #FF5050</code>",
            parse_mode="HTML"
        )
        return
    parts = [p.strip() for p in args[1].split("|", 1)]
    name, color = parts[0], parts[1]
    if not color.startswith("#") or len(color) not in (4, 7):
        await message.reply("⚠️ Цвет должен быть в формате #RGB или #RRGGBB, например <code>#FF5050</code>", parse_mode="HTML")
        return
    existing = await get_tag(session, name)
    if existing:
        await message.reply(f"❌ Тег <b>{name}</b> уже существует (цвет: <code>{existing.color}</code>)", parse_mode="HTML")
        return
    tag = await create_tag(session, name, color)
    await message.reply(
        f"✅ Тег создан!\n"
        f"Имя: <b>{tag.name}</b>\n"
        f"Цвет: <code>{tag.color}</code>",
        parse_mode="HTML"
    )


@router.message(Command("list_tags"))
async def cmd_list_tags(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    tags = await get_all_tags(session)
    if not tags:
        await message.reply("📭 Теги не найдены. Создай: /add_tag Название | #HEX")
        return
    lines = ["🏷 <b>Все теги:</b>\n"]
    for t in tags:
        lines.append(f"  • <b>{t.name}</b> — <code>{t.color}</code>")
    lines.append(f"\nВсего: {len(tags)}")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("del_tag"))
async def cmd_del_tag(message: Message, session: AsyncSession):
    """Usage: /del_tag Ear Licking"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /del_tag Название")
        return
    name = args[1].strip()
    deleted = await delete_tag(session, name)
    if deleted:
        await message.reply(f"🗑 Тег <b>{name}</b> удалён.", parse_mode="HTML")
    else:
        await message.reply(f"❌ Тег <b>{name}</b> не найден.", parse_mode="HTML")


# ─── /add_ready_on / /add_ready_off ─────────────────────────────────────────

@router.message(Command("add_ready_on"))
async def cmd_add_ready_on(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /add_ready_on [имя артиста]")
        return
    artist = await set_artist_tag(session, args[1].strip(), 'ready', True)
    if not artist:
        await message.reply(f"❌ Артист «{args[1].strip()}» не найден.")
        return
    await message.reply(f"✅ <b>READY</b> включён для <b>{artist.name}</b>", parse_mode="HTML")


@router.message(Command("add_ready_off"))
async def cmd_add_ready_off(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("Использование: /add_ready_off [имя артиста]")
        return
    artist = await set_artist_tag(session, args[1].strip(), 'ready', False)
    if not artist:
        await message.reply(f"❌ Артист «{args[1].strip()}» не найден.")
        return
    await message.reply(f"✅ <b>READY</b> выключён для <b>{artist.name}</b>", parse_mode="HTML")


# ─── /set_cont — Управление артистами ─────────────────────────────────────────

@router.message(Command("set_cont"))
async def cmd_set_cont(message: Message):
    if not is_admin(message.from_user.id):
        return
    
    await message.reply(
        "🎨 Управление артистами\n\n"
        "Команды:\n"
        "/add_artist [имя] — добавить артиста\n"
        "/del_artist [имя] — удалить артиста\n"
        "/set_artist_photos [имя] [кол-во] — установить фото\n"
        "/set_artist_videos [имя] [кол-во] — установить видео\n"
        "/set_artist_photo [имя] [url] — установить фото (карточка)\n"
        "/set_artist_profile_photo [имя] [url] — установить фото профиля\n"
        "/set_artist_link [имя] [url] — ссылка на топик в группе\n"
        "/allartists — показать всех артистов\n\n"
        "Контент профиля:\n"
        "/add_cont [имя] — загрузить видео/фото артисту\n"
        "/list_cont [имя] — показать контент артиста\n"
        "/clear_cont [имя] videos|photos|all — очистить контент\n\n"
        "🏷 Теги (для видео):\n"
        "/add_tag Название | #HEX — создать тег с цветом\n"
        "/list_tags — все теги\n"
        "/del_tag Название — удалить тег\n\n"
        "Теги артиста:\n"
        "/artist_hot_on [имя] — добавить тег HOT\n"
        "/artist_hot_off [имя] — убрать тег HOT\n"
        "/artist_new_on [имя] — добавить тег NEW\n"
        "/artist_new_off [имя] — убрать тег NEW\n"
        "/artist_prom_on [имя] — добавить на главную\n"
        "/artist_prom_off [имя] — убрать с главной"
    )


# ─── /add_artist [имя] ────────────────────────────────────────────────────────

@router.message(Command("add_artist"))
async def cmd_add_artist(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /add_artist [имя]")
        return
    
    name = " ".join(args)
    
    existing = await get_artist(session, name)
    if existing:
        await message.reply(f"❌ Артист '{name}' уже существует.")
        return
    
    artist = await create_artist(session, name)
    await message.reply(f"✅ Артист '{artist.name}' добавлен (ID: {artist.id})")


# ─── /del_artist [имя] ────────────────────────────────────────────────────────

@router.message(Command("del_artist"))
async def cmd_del_artist(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /del_artist [имя]")
        return
    
    name = " ".join(args)
    
    deleted = await delete_artist(session, name)
    if deleted:
        await message.reply(f"✅ Артист '{name}' удалён.")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


# ─── /set_artist_photos [имя] [кол-во] ───────────────────────────────────────

@router.message(Command("set_artist_photos"))
async def cmd_set_artist_photos(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if len(args) < 2 or not args[-1].isdigit():
        await message.reply("Использование: /set_artist_photos [имя] [кол-во]")
        return
    
    photos = int(args[-1])
    name = " ".join(args[:-1])
    
    artist = await update_artist_stats(session, name, photos=photos)
    if artist:
        await message.reply(f"✅ У артиста '{artist.name}' установлено {photos} фото.")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


# ─── /set_artist_videos [имя] [кол-во] ───────────────────────────────────────

@router.message(Command("set_artist_videos"))
async def cmd_set_artist_videos(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if len(args) < 2 or not args[-1].isdigit():
        await message.reply("Использование: /set_artist_videos [имя] [кол-во]")
        return
    
    videos = int(args[-1])
    name = " ".join(args[:-1])
    
    artist = await update_artist_stats(session, name, videos=videos)
    if artist:
        await message.reply(f"✅ У артиста '{artist.name}' установлено {videos} видео.")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


# ─── /set_artist_photo [имя] [url] ───────────────────────────────────────────

@router.message(Command("set_artist_photo"))
async def cmd_set_artist_photo(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("Использование: /set_artist_photo [имя] [url]")
        return
    
    url = args[-1]
    name = " ".join(args[:-1])
    
    artist = await get_artist(session, name)
    if not artist:
        await message.reply(f"❌ Артист '{name}' не найден.")
        return
    
    artist.photo_url = url
    await session.commit()
    
    await message.reply(f"✅ Фото карточки установлено для '{artist.name}'")


# ─── /set_artist_profile_photo [имя] [url] ─────────────────────────────────

@router.message(Command("set_artist_profile_photo"))
async def cmd_set_artist_profile_photo(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply("Использование: /set_artist_profile_photo [имя] [url]")
        return
    
    url = args[-1]
    name = " ".join(args[:-1])
    
    artist = await get_artist(session, name)
    if not artist:
        await message.reply(f"❌ Артист '{name}' не найден.")
        return
    
    artist.profile_photo_url = url
    await session.commit()
    
    await message.reply(f"✅ Фото профиля установлено для '{artist.name}'")


# ─── /set_artist_link [имя] [url] ────────────────────────────────────────────

@router.message(Command("set_artist_link"))
async def cmd_set_artist_link(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if len(args) < 2:
        await message.reply(
            "Использование: /set_artist_link [имя] [url]\n\n"
            "Пример: /set_artist_link Moona ASMR https://t.me/c/2078277088/12345"
        )
        return
    
    url = args[-1]
    name = " ".join(args[:-1])
    
    from database import set_artist_topic_url
    artist = await set_artist_topic_url(session, name, url)
    if not artist:
        await message.reply(f"❌ Артист '{name}' не найден.")
        return
    
    await message.reply(f"✅ Ссылка на топик установлена для <b>{artist.name}</b>\n{url}", parse_mode="HTML")


# ─── /allartists ───────────────────────────────────────────────────────────────

@router.message(Command("allartists"))
async def cmd_all_artists(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    
    artists = await get_all_artists(session)
    
    if not artists:
        await message.reply("В базе нет артистов.")
        return
    
    total_photos = sum(a.photos for a in artists)
    total_videos = sum(a.videos for a in artists)
    
    header = (
        f"🎤 <b>Все артисты: {len(artists)}</b>\n"
        f"📷 Всего фото: {total_photos} | 🎬 Всего видео: {total_videos}\n"
        f"{'─' * 35}\n"
    )
    
    lines = []
    for a in artists:
        photo_info = f"📷{a.photos}" if a.photos else "📷0"
        video_info = f"🎬{a.videos}" if a.videos else "🎬0"
        has_photo = "✅" if a.photo_url else "❌"
        has_profile = "✅" if a.profile_photo_url else "❌"
        lines.append(f"{has_photo}{has_profile} {a.name} | {photo_info} | {video_info}")
    
    # Разбиваем на части по 30 строк
    chunk_size = 30
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        text = header + "\n".join(chunk) if i == 0 else "\n".join(chunk)
        await message.answer(text, parse_mode="HTML")


# ─── Теги для артистов: Hot, New, Prom ────────────────────────────────────────

@router.message(Command("artist_hot_on"))
async def cmd_artist_hot_on(message: Message, session: AsyncSession):
    """Включить тег Hot для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_hot_on [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "hot", True)
    if artist:
        await message.reply(f"✅ Тег 🔥 HOT добавлен для '{artist.name}'")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


@router.message(Command("artist_hot_off"))
async def cmd_artist_hot_off(message: Message, session: AsyncSession):
    """Выключить тег Hot для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_hot_off [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "hot", False)
    if artist:
        await message.reply(f"✅ Тег 🔥 HOT убран для '{artist.name}'")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


@router.message(Command("artist_new_on"))
async def cmd_artist_new_on(message: Message, session: AsyncSession):
    """Включить тег New для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_new_on [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "new", True)
    if artist:
        await message.reply(f"✅ Тег ✨ NEW добавлен для '{artist.name}'")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


@router.message(Command("artist_new_off"))
async def cmd_artist_new_off(message: Message, session: AsyncSession):
    """Выключить тег New для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_new_off [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "new", False)
    if artist:
        await message.reply(f"✅ Тег ✨ NEW убран для '{artist.name}'")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


@router.message(Command("artist_prom_on"))
async def cmd_artist_prom_on(message: Message, session: AsyncSession):
    """Включить тег Prom (показывать на главной) для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_prom_on [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "prom", True)
    if artist:
        await message.reply(f"✅ Тег ⭐ PROMOTED добавлен для '{artist.name}' (будет показываться на главной)")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")


@router.message(Command("artist_prom_off"))
async def cmd_artist_prom_off(message: Message, session: AsyncSession):
    """Выключить тег Prom для артиста"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.reply("Использование: /artist_prom_off [имя]")
        return
    name = " ".join(args)
    artist = await set_artist_tag(session, name, "prom", False)
    if artist:
        await message.reply(f"✅ Тег ⭐ PROMOTED убран для '{artist.name}'")
    else:
        await message.reply(f"❌ Артист '{name}' не найден.")



# ═══════════════════════════════════════════════════════════════════════════════
# ARTIST PROFILE CONTENT — /set_cont, /list_cont, /clear_cont
# ═══════════════════════════════════════════════════════════════════════════════
#
# /set_cont
#   Бот спрашивает: имя артиста → тип (videos/photos) → режим (replace/add)
#   Затем одним сообщением принимаешь массив:
#
#   VIDEOS (одна строка = одно видео):
#     Название видео | https://url | HOT,NEW
#     Другое видео   | https://url2 | EXCLUSIVE
#     Третье видео   | https://url3
#
#   PHOTOS (одна строка = один URL):
#     https://photo1.jpg
#     https://photo2.jpg
#
# /list_cont Имя Артиста
# /clear_cont Имя Артиста videos|photos|all
# ─────────────────────────────────────────────────────────────────────────────

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


class SetContForm(StatesGroup):
    artist  = State()
    ctype   = State()
    mode    = State()
    content = State()


def _cancel_kb_c():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="setcont_cancel")
    ]])


@router.message(Command("add_cont"))
async def cmd_add_cont(message: Message, state: FSMContext, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    await state.clear()

    # Поддержка передачи имени артиста сразу: /add_cont Moona ASMR
    args = message.text.split(maxsplit=1)
    artist_name = args[1].strip() if len(args) > 1 else None

    if artist_name:
        artist = await get_artist(session, artist_name)
        if not artist:
            await message.answer(
                f"❌ Артист <b>{artist_name}</b> не найден. Проверь имя командой /allartists",
                parse_mode="HTML"
            )
            return
        counts = await get_artist_content_counts(session, artist_name)
        await state.update_data(artist=artist_name)
        await state.set_state(SetContForm.ctype)
        await message.answer(
            f"✅ Артист: <b>{artist_name}</b>\n"
            f"📹 Видео в профиле: {counts['video']} · 🖼 Фото: {counts['photo']}\n\n"
            "Шаг 2/4 — Что загружаем?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📹 Видео", callback_data="setcont_videos"),
                 InlineKeyboardButton(text="🖼 Фото", callback_data="setcont_photos")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="setcont_cancel")],
            ])
        )
    else:
        await state.set_state(SetContForm.artist)
        await message.answer(
            "🎨 <b>Загрузка контента для профиля артиста</b>\n\n"
            "Шаг 1/4 — Введи имя артиста (точно как в базе)\n"
            "или используй: <code>/add_cont Имя Артиста</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb_c()
        )


@router.message(SetContForm.artist)
async def setcont_got_artist(message: Message, state: FSMContext, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    name = message.text.strip()
    artist = await get_artist(session, name)
    if not artist:
        await message.answer(
            f"❌ Артист <b>{name}</b> не найден. Проверь имя и попробуй снова.",
            parse_mode="HTML"
        )
        return
    counts = await get_artist_content_counts(session, name)
    await state.update_data(artist=name)
    await state.set_state(SetContForm.ctype)
    await message.answer(
        f"✅ Артист: <b>{name}</b>\n"
        f"📹 Видео в профиле: {counts['video']} · 🖼 Фото: {counts['photo']}\n\n"
        "Шаг 2/4 — Что загружаем?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📹 Видео", callback_data="setcont_videos"),
             InlineKeyboardButton(text="🖼 Фото", callback_data="setcont_photos")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="setcont_cancel")],
        ])
    )


@router.callback_query(F.data.in_({"setcont_videos", "setcont_photos"}), SetContForm.ctype)
async def setcont_got_type(call, state: FSMContext, session: AsyncSession):
    ctype = "video" if call.data == "setcont_videos" else "photo"
    data = await state.get_data()
    counts = await get_artist_content_counts(session, data["artist"])
    existing = counts[ctype]
    await state.update_data(ctype=ctype)
    await state.set_state(SetContForm.mode)
    await call.message.edit_text(
        f"Шаг 3/4 — В базе уже <b>{existing}</b> {'видео' if ctype == 'video' else 'фото'} для <b>{data['artist']}</b>.\n\n"
        "Что делаем?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="♻️ Заменить всё", callback_data="setcont_replace"),
             InlineKeyboardButton(text="➕ Добавить к существующим", callback_data="setcont_add")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="setcont_cancel")],
        ])
    )
    await call.answer()


@router.callback_query(F.data.in_({"setcont_replace", "setcont_add"}), SetContForm.mode)
async def setcont_got_mode(call, state: FSMContext):
    mode = "replace" if call.data == "setcont_replace" else "add"
    data = await state.get_data()
    await state.update_data(mode=mode)
    await state.set_state(SetContForm.content)

    if data["ctype"] == "video":
        hint = (
            "Шаг 4/4 — Отправь список видео, <b>одно на строку</b>:\n\n"
            "<code>Название | URL | ТЕГИ | THUMBNAIL</code>\n\n"
            "• ТЕГИ — через запятую (необязательно)\n"
            "• THUMBNAIL — ссылка на превью-картинку (необязательно)\n\n"
            "Пример:\n"
            "<code>Night ASMR Rain | https://files.asmrleaks.tv/v1.m3u8 | Licking,HOT | https://img.asmrleaks.tv/v1.jpg\n"
            "Whisper Triggers | https://files.asmrleaks.tv/v2.m3u8 | ASMR\n"
            "Soft Tapping | https://files.asmrleaks.tv/v3.m3u8</code>"
        )
    else:
        hint = (
            "Шаг 4/4 — Отправь URLs фотографий, <b>одна на строку</b>:\n\n"
            "<code>https://cdn.example.com/photo1.jpg\n"
            "https://cdn.example.com/photo2.jpg\n"
            "https://cdn.example.com/photo3.jpg</code>"
        )

    await call.message.edit_text(hint, parse_mode="HTML", reply_markup=_cancel_kb_c())
    await call.answer()


@router.message(SetContForm.content)
async def setcont_got_content(message: Message, state: FSMContext, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    artist_name = data["artist"]
    ctype = data["ctype"]
    mode = data["mode"]

    lines = [l.strip() for l in message.text.strip().splitlines() if l.strip()]
    if not lines:
        await message.answer("⚠️ Пустой список. Попробуй ещё раз.")
        return

    # Replace mode — clear first
    if mode == "replace":
        deleted = await clear_artist_content(session, artist_name, ctype)
    else:
        deleted = 0

    added = 0
    errors = []

    for i, line in enumerate(lines):
        try:
            if ctype == "video":
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2:
                    errors.append(f"Строка {i+1}: нет разделителя |")
                    continue
                title = parts[0]
                url = parts[1]
                tags = parts[2] if len(parts) >= 3 and parts[2] else None
                thumb = parts[3] if len(parts) >= 4 and parts[3] else None
                if not url.startswith("http"):
                    errors.append(f"Строка {i+1}: невалидный URL")
                    continue
                if thumb and not thumb.startswith("http"):
                    errors.append(f"Строка {i+1}: невалидный thumbnail URL")
                    continue
                await add_artist_content(session, artist_name, "video",
                                          url=url, title=title, tags=tags,
                                          sort_order=i, thumbnail_url=thumb)
            else:
                url = line
                if not url.startswith("http"):
                    errors.append(f"Строка {i+1}: невалидный URL")
                    continue
                await add_artist_content(session, artist_name, "photo",
                                          url=url, sort_order=i)
            added += 1
        except Exception as e:
            errors.append(f"Строка {i+1}: {e}")

    await state.clear()

    summary = (
        f"✅ <b>Готово!</b> Артист: <b>{artist_name}</b>\n"
        f"{'♻️ Удалено: ' + str(deleted) + ' | ' if deleted else ''}"
        f"➕ Добавлено: <b>{added}</b> {'видео' if ctype == 'video' else 'фото'}\n"
    )
    if errors:
        summary += f"\n⚠️ Ошибок: {len(errors)}\n" + "\n".join(errors[:5])

    await message.answer(summary, parse_mode="HTML")


@router.callback_query(F.data == "setcont_cancel")
async def setcont_cancel(call, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено.")
    await call.answer()


# ─── /list_cont ───────────────────────────────────────────────────────────────

@router.message(Command("list_cont"))
async def cmd_list_cont(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /list_cont [имя артиста]")
        return
    name = args[1].strip()
    videos = await get_artist_content(session, name, "video")
    photos = await get_artist_content(session, name, "photo")

    if not videos and not photos:
        await message.answer(f"📭 Контент для <b>{name}</b> не найден.", parse_mode="HTML")
        return

    lines = [f"📋 <b>Контент: {name}</b>\n"]
    if videos:
        lines.append(f"📹 <b>Видео ({len(videos)}):</b>")
        for v in videos[:15]:
            tags_str = f" [{v.tags}]" if v.tags else ""
            lines.append(f"  {v.sort_order+1}. {v.title or '—'}{tags_str}\n     <code>{v.url[:60]}...</code>" if len(v.url) > 60 else f"  {v.sort_order+1}. {v.title or '—'}{tags_str}\n     <code>{v.url}</code>")
        if len(videos) > 15:
            lines.append(f"  ... и ещё {len(videos)-15}")

    if photos:
        lines.append(f"\n🖼 <b>Фото ({len(photos)}):</b>")
        for p in photos[:10]:
            lines.append(f"  {p.sort_order+1}. <code>{p.url[:60]}{'...' if len(p.url)>60 else ''}</code>")
        if len(photos) > 10:
            lines.append(f"  ... и ещё {len(photos)-10}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /clear_cont ─────────────────────────────────────────────────────────────

@router.message(Command("clear_cont"))
async def cmd_clear_cont(message: Message, session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование: /clear_cont [имя] videos|photos|all\n"
            "Пример: /clear_cont Moona ASMR videos"
        )
        return
    mode = parts[-1].lower()
    name = " ".join(parts[1:-1])
    if mode not in ("videos", "photos", "all"):
        await message.answer("⚠️ Тип должен быть: videos, photos или all")
        return
    ctype = None if mode == "all" else mode.rstrip("s")  # "videos"→"video", "photos"→"photo"
    deleted = await clear_artist_content(session, name, ctype)
    label = {"videos": "видео", "photos": "фото", "all": "всего контента"}.get(mode, mode)
    await message.answer(
        f"🗑 Удалено <b>{deleted}</b> записей {label} для <b>{name}</b>.",
        parse_mode="HTML"
    )