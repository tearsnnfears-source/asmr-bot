import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, ChatMemberUpdated, ChatPermissions
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_user, User
from config import GROUP_ID, ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

# Права: полный молчок (найтмод)
NIGHT_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

# Права: обычный режим
DAY_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


# ─── Удаление сервисных сообщений ────────────────────────────────────────────

@router.message(
    F.chat.id == GROUP_ID,
    F.content_type.in_({"new_chat_members", "left_chat_member",
                        "pinned_message", "new_chat_title", "new_chat_photo"})
)
async def delete_service_messages(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Cannot delete service message: {e}")


# ─── Удаление ссылок от не-админов ───────────────────────────────────────────

@router.message(F.chat.id == GROUP_ID)
async def anti_link(message: Message, bot: Bot):
    if not message.from_user or message.from_user.id in ADMIN_IDS:
        return

    # Проверяем статус в группе
    try:
        member = await bot.get_chat_member(GROUP_ID, message.from_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        return

    entities = message.entities or message.caption_entities or []
    has_link = any(e.type in ("url", "text_link") for e in entities)
    if has_link:
        try:
            await message.delete()
            logger.info(f"Deleted link from {message.from_user.id}")
        except Exception as e:
            logger.warning(f"Cannot delete link: {e}")


# ─── Проверка подписки при входе ─────────────────────────────────────────────

@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated, bot: Bot, session: AsyncSession):
    if event.chat.id != GROUP_ID:
        return

    new_member = event.new_chat_member.user
    if new_member.is_bot:
        return

    if new_member.id in ADMIN_IDS:
        return

    user = await get_user(session, new_member.id)
    has_paid_access = bool(
        user
        and user.is_active
        and (
            user.units > 0
            or ((user.last_payment_method or "") == "tribute" and user.units > -7)
        )
    )

    if not has_paid_access:
        try:
            await bot.ban_chat_member(GROUP_ID, new_member.id)
            await bot.unban_chat_member(GROUP_ID, new_member.id)
            logger.info(f"Kicked {new_member.id} — no active subscription")
        except Exception as e:
            logger.error(f"Cannot kick {new_member.id}: {e}")
        return

    user.is_active = True
    await session.commit()
    logger.info(f"User {new_member.id} joined with {user.units} units")


# ─── Ночной режим ─────────────────────────────────────────────────────────────

CHAT_THREAD_ID = 18891

async def enable_night_mode(bot: Bot):
    try:
        await bot.set_chat_permissions(GROUP_ID, NIGHT_PERMISSIONS)
        await bot.send_message(
            GROUP_ID,
            "🌙 Night mode activated. The chat is silent until morning.",
            message_thread_id=CHAT_THREAD_ID
        )
        logger.info("Night mode ON")
    except Exception as e:
        logger.error(f"Night mode ON failed: {e}")


async def disable_night_mode(bot: Bot):
    try:
        await bot.set_chat_permissions(GROUP_ID, DAY_PERMISSIONS)
        await bot.send_message(
            GROUP_ID,
            "☀️ Good morning! Night mode is off, you can chat again.",
            message_thread_id=CHAT_THREAD_ID
        )
        logger.info("Night mode OFF")
    except Exception as e:
        logger.error(f"Night mode OFF failed: {e}")
