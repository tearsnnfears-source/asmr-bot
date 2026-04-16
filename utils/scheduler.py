import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot
from sqlalchemy import select

from database import async_session, User
from handlers.group import enable_night_mode, disable_night_mode
from locales.texts import t
from config import GROUP_ID, NIGHT_START, NIGHT_END, INVITE_LINK, ADMIN_IDS

logger = logging.getLogger(__name__)


async def daily_unit_check(bot: Bot):
    """
    Каждую ночь в 00:01 МСК:
    - Списываем 1 день у всех активных
    - При 3 днях — предупреждение
    - При 0 — кикаем
    """
    logger.info("Running daily unit check...")
    kicked = 0
    warned = 0

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.is_active == True)
        )
        users = result.scalars().all()

        for user in users:
            user.units = max(0, user.units - 1)

            if user.units == 0:
                # Кик
                try:
                    await bot.ban_chat_member(GROUP_ID, user.telegram_id)
                    await bot.unban_chat_member(GROUP_ID, user.telegram_id)
                    user.is_active = False
                    kicked += 1
                    logger.info(f"Auto-kicked {user.telegram_id}")

                    # Уведомление админу
                    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🚫 <b>Юзер удалён из группы</b>\n"
                                f"👤 {nick} | <code>{user.telegram_id}</code>\n"
                                f"📅 Подписка закончилась",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"Cannot kick {user.telegram_id}: {e}")

                # Уведомление
                try:
                    lang = user.lang or "en"
                    await bot.send_message(
                        user.telegram_id,
                        t("kicked_message", lang),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            elif user.units == 3:
                # Напоминание за 3 дня
                try:
                    lang = user.lang or "en"
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    from locales.texts import t as _t
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text=_t("renew_btn", lang),
                            callback_data="subscribe"
                        )
                    ]])
                    await bot.send_message(
                        user.telegram_id,
                        t("remind_3days", lang),
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    warned += 1
                except Exception:
                    pass

        await session.commit()

    logger.info(f"Daily check done — kicked: {kicked}, warned: {warned}, total: {len(users)}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Ежедневное списание дней — 00:01 МСК
    scheduler.add_job(
        daily_unit_check,
        CronTrigger(hour=0, minute=1, timezone="Europe/Moscow"),
        args=[bot],
        id="daily_check",
    )

    # Включение ночного режима (раскомментировать при необходимости)
    # scheduler.add_job(
    #     enable_night_mode,
    #     CronTrigger(hour=NIGHT_START, minute=0, timezone="Europe/Moscow"),
    #     args=[bot], id="night_on",
    # )
    # scheduler.add_job(
    #     disable_night_mode,
    #     CronTrigger(hour=NIGHT_END, minute=0, timezone="Europe/Moscow"),
    #     args=[bot], id="night_off",
    # )

    return scheduler