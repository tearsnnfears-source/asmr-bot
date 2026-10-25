"""Renewal notices for the retired Tribute account. No checkout is opened here."""
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from config import MINIAPP_URL


def miniapp_keyboard(lang):
    label = "Открыть Mini App" if lang == "ru" else "Open Mini App"
    if urlparse(MINIAPP_URL).hostname in {"t.me", "telegram.me"}:
        button = InlineKeyboardButton(text=label, url=MINIAPP_URL)
    else:
        button = InlineKeyboardButton(text=label, web_app=WebAppInfo(url=MINIAPP_URL))
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def renewal_notice(lang, days, grace_remaining=None, legacy=True):
    if lang == "ru":
        timing = (f"До окончания подписки: <b>{days} дн.</b>" if days > 0
                  else "Оплаченные дни закончились.")
        if grace_remaining is not None:
            timing += f" Льготный период: <b>{grace_remaining} дн.</b>"
        if not legacy:
            return f"{timing}\n\nПродлить подписку можно в нашем Mini App."
        return (
            "Привет! У нас изменились платёжные ссылки. "
            "<b>Старое автопродление больше не работает.</b>\n\n"
            f"{timing}\n\n"
            "Чтобы продолжить пользоваться доступом, оформите подписку заново "
            "в нашем Mini App. Выберите доступный способ оплаты."
        )
    timing = (f"Your subscription ends in <b>{days} day(s).</b>" if days > 0
              else "Your paid subscription days have ended.")
    if grace_remaining is not None:
        timing += f" Grace period: <b>{grace_remaining} day(s)</b>."
    if not legacy:
        return f"{timing}\n\nYou can renew your subscription in our Mini App."
    return (
        "Hi! Our payment links have changed. "
        "<b>The old subscription will no longer renew automatically.</b>\n\n"
        f"{timing}\n\n"
        "To continue your access, subscribe again in our Mini App "
        "using an available payment method."
    )


async def send_renewal_notice(bot, user, grace_remaining=None):
    if not getattr(user, "notify_expiry", True):
        return
    lang = user.lang or "en"
    await bot.send_message(
        user.telegram_id, renewal_notice(
            lang, user.units, grace_remaining,
            legacy=(user.last_payment_method or "").lower() == "tribute",
        ),
        reply_markup=miniapp_keyboard(lang), parse_mode="HTML",
    )
