"""Private operator checkout. Public Card/SBP menus remain paused."""
import os

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

from utils.tribute_v2 import Settings, TributeV2Error, checkout_product

router = Router()


@router.message(Command('tribute_test'))
async def tribute_test(message: Message):
    if message.chat.type != 'private':
        return
    try:
        settings = Settings.from_env(os.environ)
        product, _ = checkout_product(settings, message.from_user.id, 's1ajK')
    except TributeV2Error:
        await message.answer('Tribute test checkout is closed or unavailable for this account.')
        return
    cents = settings.price_for_test(product)
    await message.answer(
        f'ASMR PLUS: {product.days} days, EUR {cents / 100:.2f}.\n'
        'Private migration test. The button opens Tribute; payment is a separate action.\n'
        'Use the same Telegram account when paying. No payment has been made yet.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f'Open ASMR PLUS test - EUR {cents / 100:.2f}', url=product.url)
        ]]),
    )
