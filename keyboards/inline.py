from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import STARS_PRICES, RUB_PRICES, EUR_PRICES, TRIBUTE_BOT_URL
from locales.texts import t


def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        ],
        [
            InlineKeyboardButton(text="🇪🇸 Español (Soon)", callback_data="lang:es_disabled"),
        ]
    ])


def kb_language_change() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        ],
        [
            InlineKeyboardButton(text="🇪🇸 Español (Soon)", callback_data="lang:es_disabled"),
        ]
    ])


def kb_cabinet(lang: str, has_sub: bool, trial_used: bool = False) -> InlineKeyboardMarkup:
    btn_label = t("renew_btn", lang) if has_sub else t("subscribe_btn", lang)
    fav_label = "⭐ Избранное" if lang == "ru" else "⭐ Favorites"
    trial_label = "🎁 Попробовать бесплатно" if lang == "ru" else "🎁 Try for free"
    lang_label = t("change_language", lang)

    rows = [
        [InlineKeyboardButton(text=btn_label, callback_data="subscribe")],
    ]
    if has_sub:
        rows.append([InlineKeyboardButton(text=fav_label, callback_data="favorites")])
    if not trial_used and not has_sub:
        rows.append([InlineKeyboardButton(text=trial_label, callback_data="free_trial")])
    rows.append([InlineKeyboardButton(text=lang_label, callback_data="change_language")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_plans(lang: str) -> InlineKeyboardMarkup:
    if lang == "ru":
        btn_text = f"📅 Месячная подписка — 499₽"
    else:
        btn_text = f"📅 Monthly subscription — €5"

    rows = [
        [InlineKeyboardButton(text=btn_text, callback_data="plan:30")],
        [InlineKeyboardButton(text=t("back", lang), callback_data="cabinet")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_payment(lang: str, days: int) -> InlineKeyboardMarkup:
    stars = STARS_PRICES[days]
    if lang == "ru":
        rows = [
            [InlineKeyboardButton(text=f"🏦 Оплатить по СБП — {RUB_PRICES[days]}₽", callback_data=f"pay:yoomoney:{days}")],
            [InlineKeyboardButton(text=f"⭐ Телеграм Звёзды — {stars}★", callback_data=f"pay:stars:{days}")],
            [InlineKeyboardButton(text="💎 Криптовалюта — Скоро", callback_data="pay:crypto")],
            [InlineKeyboardButton(text=t("back", lang), callback_data="subscribe")],
        ]
    else:
        tribute_url = f"{TRIBUTE_BOT_URL}" if TRIBUTE_BOT_URL else None
        card_btn = (
            InlineKeyboardButton(text=f"💳 Buy with Card — €{EUR_PRICES[days]}", url=tribute_url)
            if tribute_url
            else InlineKeyboardButton(text=f"💳 Buy with Card — €{EUR_PRICES[days]}", callback_data=f"pay:tribute:{days}")
        )
        rows = [
            [card_btn],
            [InlineKeyboardButton(text=f"⭐ Telegram Stars — {stars}★", callback_data=f"pay:stars:{days}")],
            [InlineKeyboardButton(text="💎 Crypto — Soon", callback_data="pay:crypto")],
            [InlineKeyboardButton(text=t("back", lang), callback_data="subscribe")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_after_payment(lang: str, invite_link: str) -> InlineKeyboardMarkup:
    join_text = "🔗 Join the group" if lang == "en" else "🔗 Вступить в группу"
    cabinet_text = t("cabinet_btn", lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=join_text, url=invite_link)],
        [InlineKeyboardButton(text=cabinet_text, callback_data="cabinet")],
    ])


def kb_back_to_cabinet(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("cabinet_btn", lang), callback_data="cabinet")]
    ])
