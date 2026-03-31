import logging
from aiogram import Router, Bot, F
from aiogram.types import Message
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import User, get_user, Artist, get_artist, get_all_artists, create_artist, delete_artist, update_artist_stats, set_artist_tag, Video, create_video, get_all_videos, delete_video
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

    await message.reply(
        f"👤 <b>Пользователь</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Язык: {user.lang}\n"
        f"Дней: <b>{user.units}</b>\n"
        f"Активен: {'✅' if user.is_active else '❌'}",
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
        "/allusers — список всех пользователей\n\n"
        "🚫 <b>Модерация:</b>\n"
        "/kick [user_id] — кикнуть из группы\n"
        "/ban [user_id] — забанить навсегда\n"
        "/nightmode_on — включить ночной режим\n"
        "/nightmode_off — выключить ночной режим\n\n"
        "🎨 <b>Артисты:</b>\n"
        "/set_cont — управление артистами\n\n"
        "🎬 <b>Видео (Last Updates):</b>\n"
        "/add_video [название] [артист] [embed_url] [duration] — добавить видео\n"
        "/del_video [id] — удалить видео по ID\n"
        "/allvideos — показать все видео\n\n"
        "📣 <b>Постинг в каналы:</b>\n"
        "/new_post — создать пост (текст, фото, кнопки, отсрочка)\n"
        "/scheduled_posts — список запланированных постов\n"
        "/cancel_post [id] — отменить запланированный пост\n\n"
        "📝 <b>Пример добавления видео:</b>\n"
        "<code>/add_video \"Название\" \"Артист\" \"https://player.mediadelivery.net/embed/...\" \"12:34\"</code>"
    )


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
        method_labels = {"yoomoney": "СБП", "stars": "⭐", "tribute": "💳"}
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
        "Теги:\n"
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


# ─── Управление видео ────────────────────────────────────────────────────────

@router.message(Command("set_videos"))
async def cmd_set_videos(message: Message):
    """Показать команды для управления видео"""
    if not is_admin(message.from_user.id):
        return
    
    await message.reply(
        "🎬 Управление видео\n\n"
        "Команды:\n"
        "/add_video [название] [артист] [embed_url] [duration] — добавить видео\n"
        "/del_video [id] — удалить видео\n"
        "/allvideos — показать все видео\n\n"
        "Пример:\n"
        "/add_video \"New ASMR Stream\" \"Bunny\" \"https://player.mediadelivery.net/...\" \"41:07\""
    )


# ─── /add_video [название] [артист] [embed_url] [duration] ────────────────────

@router.message(Command("add_video"))
async def cmd_add_video(message: Message, session: AsyncSession):
    """Добавить новое видео"""
    if not is_admin(message.from_user.id):
        return
    
    # Получаем текст после команды и убираем кавычки
    text = message.text[message.text.find(" "):].strip()
    if not text:
        await message.reply(
            "📝 <b>Добавление видео в Last Updates</b>\n\n"
            "Использование:\n"
            "<code>/add_video \"Название\" \"Артист\" \"embed_url\" \"длительность\"</code>\n\n"
            "Пример:\n"
            "<code>/add_video \"New ASMR Stream\" \"Bunny\" \"https://player.mediadelivery.net/embed/621300/9019a393-c9dd-4f8b-9eea-192cab12c819\" \"41:07\"</code>"
        )
        return
    
    # Убираем кавычки из аргументов
    parts = text.split('"')
    # Фильтруем пустые строки
    clean_parts = [p.strip() for p in parts if p.strip()]
    
    if len(clean_parts) < 3:
        await message.reply(
            "❌ Недостаточно аргументов.\n\n"
            "Формат: /add_video \"Название\" \"Артист\" \"embed_url\" \"длительность\"\n\n"
            "Пример:\n"
            "/add_video \"ASMR Stream\" \"Bunny\" \"https://player.mediadelivery.net/embed/...\" \"12:34\""
        )
        return
    
    # Ищем URL (обычно содержит player.mediadelivery.net или similar)
    title = None
    artist_name = None
    embed_url = None
    duration = None
    
    # Проходим по частям и ищем URL
    for i, part in enumerate(clean_parts):
        if "http" in part.lower() or "player" in part.lower():
            embed_url = part.strip()
            # До этого момента - title и artist
            if i > 0:
                # Если есть только одна часть до URL - это title
                # Если две части - title и artist
                if i == 1:
                    title = clean_parts[0]
                    artist_name = "Unknown"
                elif i >= 2:
                    title = " ".join(clean_parts[:i-1])
                    artist_name = clean_parts[i-1]
            # После URL может быть duration
            if i + 1 < len(clean_parts):
                duration = clean_parts[i + 1]
            break
    
    if not embed_url:
        await message.reply(
            "❌ Не найден URL видео.\n\n"
            "URL должен содержать http и ссылку на плеер (player.mediadelivery.net)"
        )
        return
    
    # Если title не определен, используем значение по умолчанию
    if not title:
        title = "Untitled Video"
    if not artist_name:
        artist_name = "Unknown"
    
    # Очищаем duration от лишних символов
    if duration:
        duration = duration.strip().replace('"', '').replace("'", "")
    
    try:
        video = await create_video(
            session,
            title=title,
            url=embed_url,
            embed_url=embed_url,
            artist_name=artist_name,
            duration=duration
        )
        
        # Формируем красивое сообщение
        duration_str = f" ⏱ {duration}" if duration else ""
        await message.reply(
            f"✅ <b>Видео добавлено в Last Updates!</b>\n\n"
            f"📌 <b>ID:</b> <code>{video.id}</code>\n"
            f"🎬 <b>Название:</b> {title}\n"
            f"👤 <b>Артист:</b> {artist_name}{duration_str}\n"
            f"🔗 <b>URL:</b> {embed_url[:50]}..."
        )
    except Exception as e:
        logger.error(f"Error adding video: {e}")
        await message.reply(f"❌ Ошибка при добавлении видео: {e}")


# ─── /del_video [id] ─────────────────────────────────────────────────────────

@router.message(Command("del_video"))
async def cmd_del_video(message: Message, session: AsyncSession):
    """Удалить видео по ID"""
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()[1:]
    if not args or not args[0].isdigit():
        await message.reply("Использование: /del_video [id]")
        return
    
    video_id = int(args[0])
    
    deleted = await delete_video(session, video_id)
    if deleted:
        await message.reply(f"✅ Видео с ID {video_id} удалено.")
    else:
        await message.reply(f"❌ Видео с ID {video_id} не найдено.")


# ─── /allvideos ───────────────────────────────────────────────────────────────

@router.message(Command("allvideos"))
async def cmd_all_videos(message: Message, session: AsyncSession):
    """Показать все видео"""
    if not is_admin(message.from_user.id):
        return
    
    videos = await get_all_videos(session, 50)
    
    if not videos:
        await message.reply("В базе нет видео.")
        return
    
    header = f"🎬 <b>Все видео: {len(videos)}</b>\n{'─' * 30}\n"
    
    lines = []
    for v in videos:
        duration = v.duration or "—"
        lines.append(f"ID:{v.id} | {v.title[:30]} | {v.artist_name} | {duration}")
    
    # Split into parts
    chunk_size = 20
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        text = header + "\n".join(chunk) if i == 0 else "\n".join(chunk)
        await message.answer(text, parse_mode="HTML")