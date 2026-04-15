import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery, Message,
    LabeledPrice, PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from aiogram import Router, F, Bot
from aiogram.types import PreCheckoutQuery, Message
from aiogram.types.message import ContentType
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_or_create_user
from database import get_or_create_user, PendingPayment
from keyboards.inline import kb_payment, kb_plans, kb_after_payment, kb_back_to_cabinet
from locales.texts import t
from utils.yoomoney import make_payment_url, generate_label, check_payment
from config import STARS_PRICES, RUB_PRICES, INVITE_LINK

router = Router()
logger = logging.getLogger(__name__)

PAYMENT_IMAGE = "AgACAgIAAxkBAAIBQGm7cq7ZO6fKjw7XrFrMiyI4X3h3AALXEWsbpuDhSd4hIjY-KzpiAQADAgADeQADOgQ"

async def _notify_admins(bot: Bot, text: str):
    from config import ADMIN_IDS
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

# ─── Выбор тарифа ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery, session: AsyncSession, bot: Bot):
    days = int(call.data.split(":")[1])
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"

    plan_name = t("plan_label", lang)[days]
    text = t("choose_payment", lang, plan=plan_name)

    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_photo(
        chat_id=call.from_user.id,
        photo=PAYMENT_IMAGE,
        caption=text,
        reply_markup=kb_payment(lang, days),
        parse_mode="HTML",
    )


# ─── Крипто — заглушка ───────────────────────────────────────────────────────

@router.callback_query(F.data == "pay:crypto")
async def cb_crypto(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    msg = "💎 Crypto payments coming soon!" if lang == "en" else "💎 Оплата криптовалютой — скоро!"
    await call.answer(msg, show_alert=True)


# ─── Tribute — редирект на внешнюю ссылку ────────────────────────────────────

@router.callback_query(F.data.startswith("pay:tribute:"))
async def cb_tribute(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    # Если TRIBUTE_BOT_URL не задан — покажем сообщение
    msg = "💳 Card payment via Tribute — link will appear here after setup."
    await call.answer(msg, show_alert=True)

# ─── Telegram Stars ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pay:stars:"))
async def cb_stars(call: CallbackQuery, session: AsyncSession, bot: Bot):
    days = int(call.data.split(":")[2])
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    stars = STARS_PRICES[days]

    await call.answer()

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=t("stars_invoice_title", lang, days=days),
        description=t("stars_invoice_desc", lang, days=days),
        payload=f"stars_{days}_{call.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=t("plan_label", lang)[days], amount=stars)],
    )


# 1. Разрешаем транзакцию, если валюта XTR (Звезды)
@router.pre_checkout_query(F.currency == "XTR")
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    logger.info(f"Pre-checkout approved for user {pre_checkout_query.from_user.id}")


# 2. Ловим успешную оплату и выдаем дни
@router.message(F.successful_payment)
async def process_successful_payment(message: Message, session: AsyncSession, bot: Bot):
    try:
        payload = message.successful_payment.invoice_payload
        # Берём ID плательщика напрямую из message — это надёжнее чем парсить payload
        user_id = message.from_user.id

        logger.info(f"Successful payment from user {user_id}, payload: {payload}")

        # Поддерживаем оба формата: "stars_30_userid" (mini app) и "stars:30:userid" (bot)
        days = None
        if payload.startswith("stars_"):
            parts = payload.split("_")
            days = int(parts[1])
        elif payload.startswith("stars:"):
            parts = payload.split(":")
            days = int(parts[1])

        if not days:
            logger.warning(f"Unknown Stars payload format: {payload}")
            return

        # Начисляем подписку
        user = await get_or_create_user(session, user_id)
        lang = user.lang or "en"
        user.units += days
        user.is_active = True
        user.last_payment_method = "stars"
        await session.commit()

        logger.info(f"Stars credited: user {user_id} +{days} days → total {user.units}")

        # Уведомление админу
        nick = f"@{user.username}" if user.username else f"id{user_id}"
        await _notify_admins(bot,
            f"⭐ <b>Новая оплата — TG Stars</b>\n"
            f"👤 {nick} | <code>{user_id}</code>\n"
            f"📅 +{days} дней | Итого: {user.units} дн."
        )

        # Уведомление с инвайт-ссылкой
        if lang == "ru":
            text = (
                f"🌟 <b>Оплата Звёздами прошла успешно!</b>\n\n"
                f"📅 Добавлено: <b>{days} дней</b>\n"
                f"📅 Итого: <b>{user.units} дней</b>\n\n"
                f"🔗 Вступить в группу:\n{INVITE_LINK}"
            )
        else:
            text = (
                f"🌟 <b>Stars payment successful!</b>\n\n"
                f"📅 Added: <b>{days} days</b>\n"
                f"📅 Total: <b>{user.units} days</b>\n\n"
                f"🔗 Join the group:\n{INVITE_LINK}"
            )

        from keyboards.inline import kb_after_payment
        await bot.send_message(
            user_id, text,
            reply_markup=kb_after_payment(lang, INVITE_LINK),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Stars payment error for user {message.from_user.id}: {e}", exc_info=True)


# ─── Активация подписки ───────────────────────────────────────────────────────

async def _activate_subscription(
    session,
    bot: Bot,
    user,
    days: int,
    lang: str,
    message,
    payment_method: str | None = None,
):
    """Начисляет юниты, отмечает активным, отправляет сообщение с ссылкой."""
    user.units += days
    user.is_active = True
    if payment_method:
        user.last_payment_method = payment_method
    await session.commit()

    text = t("payment_success", lang, days=days, link=INVITE_LINK)
    kb   = kb_after_payment(lang, INVITE_LINK)

    try:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await bot.send_message(user.telegram_id, text, reply_markup=kb, parse_mode="HTML")

    logger.info(f"User {user.telegram_id} activated {days} units. Total: {user.units}")