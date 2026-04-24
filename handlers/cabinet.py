from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_or_create_user
from keyboards.inline import kb_cabinet, kb_plans
from locales.texts import t
from handlers.payment import PAYMENT_IMAGE

router = Router()

CABINET_IMAGE = "AgACAgIAAxkBAAIBHGm7cIqXo0APoaonaqGKih11w2S7AAJwEmsbcLjgSfSON7D-LevlAQADAgADeQADOgQ"


@router.callback_query(F.data == "cabinet")
async def cb_cabinet(call: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    username = user.username or "—"

    if user.units > 0:
        days_text = t("days_left", lang, n=user.units)
    else:
        days_text = t("no_subscription", lang)

    text = t("cabinet", lang,
             telegram_id=user.telegram_id,
             username=username,
             days=days_text)

    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass

    await bot.send_photo(
        chat_id=call.from_user.id,
        photo=CABINET_IMAGE,
        caption=text,
        reply_markup=kb_cabinet(lang, has_sub=user.units > 0, trial_used=user.trial_used),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "subscribe")
async def cb_subscribe(call: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"

    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_photo(
        chat_id=call.from_user.id,
        photo=PAYMENT_IMAGE,
        caption=t("choose_plan", lang),
        reply_markup=kb_plans(lang),
        parse_mode="HTML",
    )

from config import INVITE_LINK, ADMIN_IDS

FREE_TRIAL_IMAGE = "AgACAgIAAxkBAAIBF2m7b9FqEtFgjznUGrwH30VGpcDnAAJrEmsbcLjgSdNs4lv2kAkPAQADAgADeQADOgQ"

async def _notify_admins(bot, text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

@router.callback_query(F.data == "free_trial")
async def cb_free_trial(call: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"

    # Повторная попытка
    if user.trial_used:
        msg = "❌ Вы уже использовали бесплатный период." if lang == "ru" \
              else "❌ You have already used the free trial."
        await call.answer(msg, show_alert=True)
        return

    # Начисляем 5 дней
    user.units += 5
    user.trial_used = True
    user.is_active = True
    await session.commit()

    await call.answer()

    # Уведомление админу
    nick = f"@{user.username}" if user.username else f"id{user.telegram_id}"
    await _notify_admins(bot,
        f"🎁 <b>Новый триал</b>\n"
        f"👤 {nick} | <code>{user.telegram_id}</code>\n"
        f"📅 Активировал 5 бесплатных дней"
    )

    if lang == "ru":
        text = (
            "🎁 <b>Вам активировано 5 бесплатных дней!</b>\n\n"
            "📅 Когда дни закончатся — доступ будет закрыт.\n\n"
            f"🔗 Вступить в группу:\n{INVITE_LINK}"
        )
    else:
        text = (
            "🎁 <b>5 free days activated!</b>\n\n"
            "📅 When the days run out — access will be closed.\n\n"
            f"🔗 Join the group:\n{INVITE_LINK}"
        )

    from keyboards.inline import kb_after_payment
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_photo(
        chat_id=call.from_user.id,
        photo=FREE_TRIAL_IMAGE,
        caption=text,
        reply_markup=kb_after_payment(lang, INVITE_LINK),
        parse_mode="HTML",
    )