from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_or_create_user
from keyboards.inline import kb_language, kb_language_change
from locales.texts import t
from config import MINIAPP_URL, SUPPORT_URL, FREE_PAGES

router = Router()

CABINET_IMAGE  = "AgACAgIAAxkBAAIBHGm7cIqXo0APoaonaqGKih11w2S7AAJwEmsbcLjgSfSON7D-LevlAQADAgADeQADOgQ"
LANGUAGE_IMAGE = "AgACAgIAAxkBAAIBHGm7cIqXo0APoaonaqGKih11w2S7AAJwEmsbcLjgSfSON7D-LevlAQADAgADeQADOgQ"


# ── Keyboards ──────────────────────────────────────────────────────────────────

def kb_start(lang: str, has_sub: bool, trial_used: bool) -> InlineKeyboardMarkup:
    rows = []

    # 1. Open MiniApp — green
    if MINIAPP_URL:
        rows.append([InlineKeyboardButton(
            text="🟢 Open App" if lang == "en" else "🟢 Открыть приложение",
            web_app=WebAppInfo(url=MINIAPP_URL),
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="🟢 Open App" if lang == "en" else "🟢 Открыть приложение",
            url="https://t.me/asmrleaksbot",
        )])

    # 2. Free Pages — orange
    rows.append([InlineKeyboardButton(
        text="🟠 Free Pages" if lang == "en" else "🟠 Бесплатные страницы",
        callback_data="free_pages"
    )])

    # 3. Support — blue
    rows.append([InlineKeyboardButton(
        text="🔵 Support" if lang == "en" else "🔵 Поддержка",
        url=SUPPORT_URL
    )])

    # 4. Trial if not used
    if not has_sub and not trial_used:
        rows.append([InlineKeyboardButton(
            text="🎁 Try 5 days free" if lang == "en" else "🎁 Попробовать 5 дней бесплатно",
            callback_data="free_trial"
        )])

    # 5. Language
    rows.append([InlineKeyboardButton(
        text="🌐 Language" if lang == "en" else "🌐 Язык",
        callback_data="change_language"
    )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_free_pages(lang: str) -> InlineKeyboardMarkup:
    """Build free pages keyboard from FREE_PAGES config (Name|URL,Name|URL)."""
    rows = []
    if FREE_PAGES:
        for entry in FREE_PAGES.split(","):
            entry = entry.strip()
            if "|" in entry:
                name, url = entry.split("|", 1)
                rows.append([InlineKeyboardButton(text=name.strip(), url=url.strip())])
    if not rows:
        # Fallback placeholders — replace with real links in env
        rows = [
            [InlineKeyboardButton(text="📢 ASMR Leaks (free)", url="https://t.me/asmrleaks")],
        ]
    rows.append([InlineKeyboardButton(
        text="‹ Back" if lang == "en" else "‹ Назад",
        callback_data="cabinet"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Handlers ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    user = await get_or_create_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    if user.lang in ("en", "ru"):
        await _show_profile(message, user)
    else:
        await _show_language_selection(message)


async def _show_language_selection(message: Message):
    text = (
        "🌍 <b>Welcome!</b> Choose your language:\n\n"
        "🌍 <b>Добро пожаловать!</b> Выберите язык:"
    )
    await message.answer_photo(
        photo=LANGUAGE_IMAGE,
        caption=text,
        reply_markup=kb_language(),
        parse_mode="HTML",
    )


async def _show_profile(message: Message, user, edit: bool = False):
    lang = user.lang or "en"
    username = f"@{user.username}" if user.username else "—"

    if user.units > 9000:
        sub_line = "∞ Unlimited" if lang == "en" else "∞ Безлимит"
    elif user.units > 0:
        sub_line = (f"📅 {user.units} days remaining" if lang == "en"
                    else f"📅 Осталось {user.units} дней")
    elif user.units < 0:
        grace = 7 - abs(user.units)
        sub_line = (f"⏳ Grace period — {grace} day(s) left" if lang == "en"
                    else f"⏳ Льготный период — {grace} дн.")
    else:
        sub_line = "❌ No active subscription" if lang == "en" else "❌ Нет активной подписки"

    tier = (user.tier or "plus").upper()

    if lang == "en":
        text = (
            f"👤 <b>Your Profile</b>\n\n"
            f"🆔 ID: <code>{user.telegram_id}</code>\n"
            f"🏷 Username: {username}\n"
            f"💎 Tier: <b>{tier}</b>\n"
            f"📋 {sub_line}"
        )
    else:
        text = (
            f"👤 <b>Ваш профиль</b>\n\n"
            f"🆔 ID: <code>{user.telegram_id}</code>\n"
            f"🏷 Username: {username}\n"
            f"💎 Тир: <b>{tier}</b>\n"
            f"📋 {sub_line}"
        )

    kb = kb_start(lang, has_sub=user.units > 0, trial_used=user.trial_used or False)

    if edit:
        try:
            await message.delete()
        except Exception:
            pass

    await message.answer_photo(
        photo=CABINET_IMAGE,
        caption=text,
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "free_pages")
async def cb_free_pages(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    await call.answer()
    text = ("📢 <b>Free pages & channels:</b>" if lang == "en"
            else "📢 <b>Бесплатные страницы и каналы:</b>")
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb_free_pages(lang), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb_free_pages(lang), parse_mode="HTML")


@router.callback_query(F.data.startswith("lang:"))
async def cb_language(call: CallbackQuery, session: AsyncSession):
    lang = call.data.split(":")[1]
    if lang == "es_disabled":
        await call.answer("🇪🇸 Spanish coming soon!", show_alert=True)
        return
    user = await get_or_create_user(session, call.from_user.id)
    user.lang = lang
    await session.commit()
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_profile(call.message, user)


@router.callback_query(F.data == "change_language")
async def cb_change_language(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    text = "🌍 Choose your language / Выберите язык:"
    await call.message.answer_photo(
        photo=LANGUAGE_IMAGE,
        caption=text,
        reply_markup=kb_language_change(),
        parse_mode="HTML",
    )
