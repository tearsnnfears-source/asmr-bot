from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_IDS
from utils.crypto_payments import mode, set_mode, test_scope
from utils.plisio import CryptoError, api_key

router = Router()


@router.message(Command('crypto_pay'))
async def crypto_pay(message: Message, session: AsyncSession):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.type != 'private':
        await message.reply('Используйте команду в личном чате с ботом.')
        return
    args = (message.text or '').split()[1:]
    value = args[0].lower() if len(args) == 1 else 'status' if not args else ''
    if value not in {'plisio', 'manual', 'status'}:
        await message.reply('Использование: /crypto_pay Plisio | Manual | status')
        return
    try:
        current = await mode(session) if value == 'status' else await set_mode(session, value, message.from_user.id)
        tester = test_scope()
    except CryptoError as error:
        await message.reply(error.code)
        return
    scope_text = ''
    if tester is not None:
        state = 'включён' if current == 'plisio' else 'остановлен'
        scope_text = f'Закрытый тест {state}: только PLUS €6 для ID {tester}. Остальным доступен Manual.\n'
    await message.reply(
        f'Криптооплата: {current.upper()}.\n'
        f'Ключ Plisio: {"настроен" if api_key() else "не настроен"}.\n'
        f'{scope_text}'
        'Настройка только для этого бота и Mini App. Уже созданные счета продолжат обрабатываться.'
    )
