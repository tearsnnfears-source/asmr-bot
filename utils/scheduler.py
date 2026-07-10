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


def _kick_reason(user) -> tuple[str, str]:
    """Return (short_label, long_reason) describing why this user was kicked.

    Distinguishes paid subscribers from free-trial users so the admin can
    tell at a glance whether a renewal is expected or the slot just opened
    up after a trial expired.
    """
    pm = (user.last_payment_method or '').lower()
    if pm == 'tribute':
        return ("Tribute", "💳 Tribute grace истёк")
    if pm == 'stars':
        return ("Stars",   "⭐ Stars-подписка закончилась")
    if pm == 'crypto':
        return ("Crypto",  "💎 Crypto-подписка закончилась")
    if user.trial_used:
        return ("Trial",   "🎁 Бесплатный триал закончился")
    # Edge: admin вручную начислил дни без указания способа оплаты.
    return ("Admin", "📅 Дни закончились (без способа оплаты)")


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
            select(User).where(User.is_active == True, User.units > -7)
        )
        users = result.scalars().all()

        GRACE_LIMIT = -7

        for user in users:
            is_tribute = (user.last_payment_method or '') == 'tribute'
            user.units -= 1

            # ── Non-tribute (Stars / Crypto / Trial): kick immediately at 0
            if not is_tribute and user.units <= 0:
                user.units = 0  # don't let go negative
                user.tier = 'plus'
                try:
                    await bot.ban_chat_member(GROUP_ID, user.telegram_id)
                    await bot.unban_chat_member(GROUP_ID, user.telegram_id)
                    user.is_active = False
                    kicked += 1
                    short_label, long_reason = _kick_reason(user)
                    logger.info(f"Auto-kicked ({short_label}) {user.telegram_id}")
                    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🚫 <b>Юзер удалён из группы</b>\n"
                                f"👤 {nick} | <code>{user.telegram_id}</code>\n"
                                f"{long_reason}",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"Kick error {user.telegram_id}: {e}")
                continue

            # ── Tribute only: grace period logic
            if user.units == 0:
                # Day 0 — warn, start grace
                try:
                    lang = user.lang or "en"
                    msg = ("⏰ <b>Подписка закончилась!</b>\n\nУ тебя есть <b>7 дней</b> чтобы продлить и не потерять доступ." if lang == "ru"
                           else "⏰ <b>Your subscription has ended!</b>\n\nYou have <b>7 grace days</b> to renew before losing access.")
                    await bot.send_message(user.telegram_id, msg, parse_mode="HTML")
                except Exception:
                    pass

            elif 0 > user.units > GRACE_LIMIT:
                # Grace period days -1..-6
                remaining = 7 - abs(user.units)
                try:
                    lang = user.lang or "en"
                    msg = (f"⚠️ <b>Льготный период: {remaining} дн. осталось</b>\nПродли подписку, иначе потеряешь доступ." if lang == "ru"
                           else f"⚠️ <b>Grace period: {remaining} day(s) left</b>\nRenew now to keep your access.")
                    await bot.send_message(user.telegram_id, msg, parse_mode="HTML")
                except Exception:
                    pass

            elif user.units <= GRACE_LIMIT:
                user.tier = 'plus'
                try:
                    await bot.ban_chat_member(GROUP_ID, user.telegram_id)
                    await bot.unban_chat_member(GROUP_ID, user.telegram_id)
                    user.is_active = False
                    kicked += 1
                    short_label, long_reason = _kick_reason(user)
                    logger.info(f"Auto-kicked ({short_label}, grace expired) {user.telegram_id}")

                    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(
                                admin_id,
                                f"🚫 <b>Юзер удалён из группы</b>\n"
                                f"👤 {nick} | <code>{user.telegram_id}</code>\n"
                                f"{long_reason}",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"Cannot kick {user.telegram_id}: {e}")

                try:
                    lang = user.lang or "en"
                    await bot.send_message(user.telegram_id, t("kicked_message", lang), parse_mode="HTML")
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
