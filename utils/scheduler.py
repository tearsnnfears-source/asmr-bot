import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot
from sqlalchemy import select

from database import async_session, User
from utils.payment_grace import load_policy
from utils.payment_renewal import send_renewal_notice
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
    if pm in {'tribute', 'tribute_v2'}:
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
    Каждую ночь в 00:00 МСК:
    - Списываем 1 день у всех активных
    - При 3 днях — предупреждение
    - При 0 — кикаем
    """
    logger.info("Running daily unit check...")
    kicked = 0
    warned = 0

    async with async_session() as session:
        grace_enabled = (await load_policy(session)).enabled
        result = await session.execute(
            select(User).where(User.is_active == True)
        )
        users = result.scalars().all()

        GRACE_LIMIT = -7

        for user in users:
            if (user.tier or '').lower() == 'lifetime':
                continue
            payment_method = (user.last_payment_method or '').lower()
            is_tribute = payment_method in {'tribute', 'tribute_v2'}
            is_bundle_sync = payment_method.startswith('bundle_')
            cutoff = GRACE_LIMIT if is_tribute and grace_enabled else 0
            if user.units > cutoff:
                user.units -= 1

            # ── Non-tribute (Stars / Crypto / Trial): kick immediately at 0
            if (not is_tribute or not grace_enabled) and user.units <= 0:
                if not is_tribute:
                    user.units = 0
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
                if is_tribute and not user.is_active:
                    try:
                        await send_renewal_notice(bot, user)
                    except Exception:
                        logger.warning("Could not deliver Tribute expiry notice")
                continue

            # ── Tribute only: grace period logic
            if user.units == 0:
                # Day 0 — warn, start grace
                try:
                    await send_renewal_notice(bot, user, grace_remaining=7)
                except Exception:
                    pass

            elif 0 > user.units > GRACE_LIMIT:
                # Grace period days -1..-6
                remaining = 7 - abs(user.units)
                try:
                    await send_renewal_notice(bot, user, grace_remaining=remaining)
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
                    if not user.is_active:
                        await send_renewal_notice(bot, user)
                except Exception:
                    pass

            elif is_tribute and user.units in {3, 1}:
                try:
                    await send_renewal_notice(bot, user)
                    warned += 1
                except Exception:
                    logger.warning("Could not deliver Tribute renewal notice")

            elif user.units == 3 and not is_bundle_sync:
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

    # Ежедневное списание дней — 00:00 МСК
    scheduler.add_job(
        daily_unit_check,
        CronTrigger(hour=0, minute=0, timezone="Europe/Moscow"),
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
