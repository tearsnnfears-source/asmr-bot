from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_or_create_user, get_user
from keyboards.inline import kb_language, kb_language_change, kb_cabinet
from locales.texts import t

router = Router()

CABINET_IMAGE = "AgACAgIAAxkBAAIBHGm7cIqXo0APoaonaqGKih11w2S7AAJwEmsbcLjgSfSON7D-LevlAQADAgADeQADOgQ"

# ID изображения для страницы выбора языка (то же что и cabinet)
LANGUAGE_IMAGE = "AgACAgIAAxkBAAIBHGm7cIqXo0APoaonaqGKih11w2S7AAJwEmsbcLjgSfSON7D-LevlAQADAgADeQADOgQ"


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession):
    user = await get_or_create_user(
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )

    if user.lang in ("en", "ru"):
        await _show_cabinet(message, user)
        return

    await _show_language_selection(message)


async def _show_language_selection(message: Message):
    """Показать красивую страницу выбора языка"""
    text = (
        "🌍 <b>Добро пожаловать!</b>\n\n"
        "Выберите удобный язык интерфейса:\n\n"
        "🌍 <b>Welcome!</b>\n"
        "Choose your preferred language:"
    )
    
    await message.answer_photo(
        photo=LANGUAGE_IMAGE,
        caption=text,
        reply_markup=kb_language(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("lang:"))
async def cb_language(call: CallbackQuery, session: AsyncSession):
    lang = call.data.split(":")[1]
    
    # Обработка испанского (пока недоступно)
    if lang == "es_disabled":
        await call.answer("🇪🇸 Spanish coming soon! / Испанский появится скоро!", show_alert=True)
        return

    user = await get_or_create_user(session, call.from_user.id)
    user.lang = lang
    await session.commit()

    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_cabinet(call.message, user, edit=False)


@router.callback_query(F.data == "change_language")
async def cb_change_language(call: CallbackQuery, session: AsyncSession):
    """Обработка изменения языка из профиля"""
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.lang or "en"
    
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    
    # Показываем страницу смены языка
    text = t("welcome_title", lang) + "\n\n" + t("welcome", lang)
    await call.message.answer_photo(
        photo=LANGUAGE_IMAGE,
        caption=text,
        reply_markup=kb_language_change(),
        parse_mode="HTML",
    )


async def _show_cabinet(message, user, edit=False):
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

    kb = kb_cabinet(lang, has_sub=user.units > 0, trial_used=user.trial_used)

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