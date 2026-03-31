TEXTS = {
    "welcome": {
        "en": (
            "👋 Hello! Welcome to <b>asmrleaks.tv</b> community bot.\n\n"
            "Please choose your language:"
        ),
        "ru": (
            "👋 Привет! Добро пожаловать в бот сообщества <b>asmrleaks.tv</b>.\n\n"
            "Пожалуйста, выберите язык:"
        ),
        "es": (
            "👋 ¡Hola! Bienvenido al bot de la comunidad <b>asmrleaks.tv</b>.\n\n"
            "Por favor, elige tu idioma:"
        ),
    },
    
    "welcome_title": {
        "en": "🌐 Choose Your Language",
        "ru": "🌐 Выберите язык",
        "es": "🌐 Elige tu idioma",
    },
    
    "language_changed": {
        "en": "✅ Language changed to English",
        "ru": "✅ Язык изменён на Русский",
        "es": "✅ Idioma cambiado a Español",
    },
    
    "change_language": {
        "en": "🌐 Change Language",
        "ru": "🌐 Изменить язык",
        "es": "🌐 Cambiar idioma",
    },

    "cabinet": {
        "en": (
            "👤 <b>Your Profile</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "📎 Username: @{username}\n\n"
            "📅 Subscription: {days}"
        ),
        "ru": (
            "👤 <b>Ваш профиль</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "📎 Никнейм: @{username}\n\n"
            "📅 Подписка: {days}"
        ),
    },

    "days_left": {
        "en": "{n} days remaining",
        "ru": "осталось {n} дней",
    },

    "no_subscription": {
        "en": "❌ No active subscription",
        "ru": "❌ Нет активной подписки",
    },

    "choose_plan": {
        "en": "💳 Choose a subscription plan:",
        "ru": "💳 Выберите тариф подписки:",
    },

    "choose_payment": {
        "en": "Select payment method for <b>{plan}</b>:",
        "ru": "Выберите способ оплаты для <b>{plan}</b>:",
    },

    "plan_label": {
    "en": {30: "30 days"},
    "ru": {30: "30 дней"},
},

    "payment_pending": {
        "en": "⏳ Waiting for payment confirmation...",
        "ru": "⏳ Ожидаем подтверждения оплаты...",
    },

    "payment_success": {
        "en": (
            "✅ <b>Your subscription is active: {days} Days</b>\n\n"
            "🔗 Join the community:\n{link}"
        ),
        "ru": (
            "✅ <b>Ваша подписка активна: {days} дней</b>\n\n"
            "🔗 Вступить в группу:\n{link}"
        ),
    },

    "payment_cancelled": {
        "en": "❌ Payment was cancelled. You can try again anytime.",
        "ru": "❌ Оплата отменена. Вы можете попробовать снова в любое время.",
    },

    "remind_3days": {
        "en": (
            "⚠️ <b>Reminder</b>\n\n"
            "Your subscription expires in <b>3 days</b>.\n"
            "Renew to keep access to the community!"
        ),
        "ru": (
            "⚠️ <b>Напоминание</b>\n\n"
            "Ваша подписка истекает через <b>3 дня</b>.\n"
            "Продлите её, чтобы сохранить доступ к сообществу!"
        ),
    },

    "kicked_message": {
        "en": (
            "😔 Your subscription has expired and you've been removed from the group.\n\n"
            "To rejoin — purchase a new subscription."
        ),
        "ru": (
            "😔 Ваша подписка закончилась, и вы были исключены из группы.\n\n"
            "Чтобы вернуться — приобретите новую подписку."
        ),
    },

    "stars_invoice_title": {
        "en": "asmrleaks.tv — {days} days",
        "ru": "asmrleaks.tv — {days} дней",
    },

    "stars_invoice_desc": {
        "en": "Access to the closed ASMR community for {days} days.",
        "ru": "Доступ к закрытому ASMR-сообществу на {days} дней.",
    },

    "back": {"en": "← Back", "ru": "← Назад"},
    "cabinet_btn": {"en": "👤 My Profile", "ru": "👤 Личный кабинет"},
    "subscribe_btn": {"en": "⭐ Subscribe", "ru": "⭐ Подписаться"},
    "renew_btn": {"en": "🔄 Renew", "ru": "🔄 Продлить"},
}


def t(key: str, lang: str, **kwargs) -> str:
    """Получить текст по ключу и языку."""
    val = TEXTS.get(key, {}).get(lang) or TEXTS.get(key, {}).get("en", "")
    if kwargs:
        return val.format(**kwargs)
    return val
