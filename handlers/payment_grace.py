from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_IDS
from utils.payment_grace import MOSCOW, load_policy, schedule_policy

router = Router()


@router.message(Command("payment_grace"))
async def payment_grace(message: Message, session: AsyncSession):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.type != "private":
        await message.reply("Используйте эту команду в личном чате с ботом.")
        return
    args = (message.text or "").split()[1:]
    action = args[0].lower() if len(args) == 1 else "status" if not args else None
    if action not in {"on", "off", "status"}:
        await message.reply("Использование: /payment_grace off | on | status")
        return
    if action == "status":
        policy = await load_policy(session)
    else:
        policy = await schedule_policy(session, action == "on", message.from_user.id)
    text = f"Tribute grace сейчас: {'ON (7 дней)' if policy.enabled else 'OFF'}."
    if policy.effective_at:
        when = policy.effective_at.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")
        text += (
            f"\nЗапланировано: {'ON' if policy.pending_enabled else 'OFF'} "
            f"с {when} МСК."
        )
    if action == "off":
        text += (
            "\nПри OFF пользователи с 0 или отрицательными днями теряют доступ "
            "при ночной проверке. Оплаченные дни и lifetime сохраняются."
        )
    text += "\nНастройка действует только для этого бота. Рассылку команда не запускает."
    await message.reply(text)
