from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_user, Favorite
from locales.texts import t

router = Router()

FAVORITES_LIMIT = 100


class AddFavorite(StatesGroup):
    waiting_url   = State()
    waiting_title = State()


FAVORITES_IMAGE = "AgACAgIAAxkBAAIBFWm7b8SXUptC0kgabVPWgIISIgvwAAKKEWsb5azZSdWfQZCMl1SGAQADAgADeQADOgQ"

async def _show_favorites(bot: Bot, telegram_id: int, session: AsyncSession, lang: str):
    result = await session.execute(
        select(Favorite)
        .where(Favorite.telegram_id == telegram_id)
        .order_by(Favorite.created_at.desc())
    )
    favs = result.scalars().all()

    if lang == "ru":
        header   = f"⭐ <b>Избранное</b> ({len(favs)}/{FAVORITES_LIMIT})\n"
        empty    = "Пока пусто. Нажми «Добавить» чтобы сохранить первое видео!"
        add_btn  = "➕ Добавить"
        back_btn = "← Назад"
    else:
        header   = f"⭐ <b>Favorites</b> ({len(favs)}/{FAVORITES_LIMIT})\n"
        empty    = "Nothing here yet. Tap 'Add' to save your first video!"
        add_btn  = "➕ Add"
        back_btn = "← Back"

    text = header + ("\n" + empty if not favs else "")

    rows = []
    for fav in favs:
        rows.append([
            InlineKeyboardButton(text=f"▶ {fav.title}", url=fav.url),
            InlineKeyboardButton(text="🗑", callback_data=f"fav_del:{fav.id}"),
        ])

    if len(favs) < FAVORITES_LIMIT:
        rows.append([InlineKeyboardButton(text=add_btn, callback_data="fav_add")])
    rows.append([InlineKeyboardButton(text=back_btn, callback_data="cabinet")])

    await bot.send_photo(
        chat_id=telegram_id,
        photo=FAVORITES_IMAGE,
        caption=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "favorites")
async def cb_favorites(call: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await get_user(session, call.from_user.id)

    if not user or user.units <= 0:
        lang = user.lang if user else "en"
        msg = "❌ Favorites are available for active subscribers only." if lang == "en" \
              else "❌ Избранное доступно только активным подписчикам."
        await call.answer(msg, show_alert=True)
        return

    lang = user.lang or "en"
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_favorites(bot, call.from_user.id, session, lang)


@router.callback_query(F.data.startswith("fav_del:"))
async def cb_fav_delete(call: CallbackQuery, session: AsyncSession, bot: Bot):
    fav_id = int(call.data.split(":")[1])

    result = await session.execute(
        select(Favorite).where(Favorite.id == fav_id, Favorite.telegram_id == call.from_user.id)
    )
    fav = result.scalar_one_or_none()

    if fav:
        await session.delete(fav)
        await session.commit()

    user = await get_user(session, call.from_user.id)
    lang = user.lang if user else "en"
    await call.answer("🗑 Удалено" if lang == "ru" else "🗑 Removed")
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_favorites(bot, call.from_user.id, session, lang)


@router.callback_query(F.data == "fav_add")
async def cb_fav_add(call: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    user = await get_user(session, call.from_user.id)
    lang = user.lang if user else "en"

    await state.set_state(AddFavorite.waiting_url)
    await state.update_data(lang=lang)

    if lang == "ru":
        text = (
            "📎 <b>Шаг 1 из 2</b>\n\n"
            "Отправь ссылку на видео из группы.\n\n"
            "<i>Например: https://t.me/c/2078277088/13639</i>"
        )
        cancel_text = "❌ Отмена"
    else:
        text = (
            "📎 <b>Step 1 of 2</b>\n\n"
            "Send the link to the video from the group.\n\n"
            "<i>Example: https://t.me/c/2078277088/13639</i>"
        )
        cancel_text = "❌ Cancel"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cancel_text, callback_data="fav_cancel")]
    ])

    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        chat_id=call.from_user.id,
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(AddFavorite.waiting_url)
async def fsm_got_url(message: Message, state: FSMContext, session: AsyncSession):
    url = message.text.strip()
    data = await state.get_data()
    lang = data.get("lang", "en")

    if not url.startswith("http"):
        await message.answer(
            "⚠️ Это не похоже на ссылку. Попробуй ещё раз:" if lang == "ru"
            else "⚠️ Doesn't look like a link. Try again:"
        )
        return

    await state.update_data(url=url)
    await state.set_state(AddFavorite.waiting_title)

    if lang == "ru":
        text = "✏️ <b>Шаг 2 из 2</b>\n\nТеперь введи название для этого видео:"
        cancel_text = "❌ Отмена"
    else:
        text = "✏️ <b>Step 2 of 2</b>\n\nNow enter a name for this video:"
        cancel_text = "❌ Cancel"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=cancel_text, callback_data="fav_cancel")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(AddFavorite.waiting_title)
async def fsm_got_title(message: Message, state: FSMContext, session: AsyncSession, bot: Bot):
    data = await state.get_data()
    lang = data.get("lang", "en")
    url  = data.get("url", "")
    title = message.text.strip()[:128]

    if not title:
        await message.answer(
            "⚠️ Название не может быть пустым." if lang == "ru"
            else "⚠️ Title can't be empty."
        )
        return

    fav = Favorite(telegram_id=message.from_user.id, title=title, url=url)
    session.add(fav)
    await session.commit()
    await state.clear()

    await message.answer(
        f"✅ <b>«{title}»</b> сохранено в избранное!" if lang == "ru"
        else f"✅ <b>«{title}»</b> saved to favorites!",
        parse_mode="HTML"
    )
    await _show_favorites(bot, message.from_user.id, session, lang)


@router.callback_query(F.data == "fav_cancel")
async def cb_fav_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot):
    await state.clear()
    user = await get_user(session, call.from_user.id)
    lang = user.lang if user else "en"
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _show_favorites(bot, call.from_user.id, session, lang)