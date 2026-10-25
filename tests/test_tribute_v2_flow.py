"""Real handlers, isolated SQLite ledger and mocked Telegram only."""
import ast
from datetime import datetime, timedelta
import hashlib
import hmac
import html
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from sqlalchemy import Boolean, Column, Integer, JSON, String, Text, select, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from utils.tribute_v2 import Settings, TributeV2Error, checkout_product, parse_paid_event
from test_tribute_v2 import BINDINGS, PAYER, KEY, event, signed

ROOT = Path(__file__).parents[1]
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    telegram_id = Column(Integer, primary_key=True)
    units = Column(Integer, default=0)
    tier = Column(String, default='free')
    is_active = Column(Boolean, default=False)
    username = Column(String)
    full_name = Column(String)
    lang = Column(String, default='en')
    last_payment_method = Column(String)


class Receipt(Base):
    __tablename__ = 'tribute_webhook_events'
    id = Column(Integer, primary_key=True)
    event_key = Column(Text, unique=True)
    name = Column(String)
    order_uuid = Column(Text)
    payload = Column(JSON)


def load(file, name, namespace):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, 'exec'), namespace)
    return namespace[name]


class TributeV2FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(telegram_id=PAYER, units=7, tier='free', lang='en'))
            await session.commit()
        self.env = {'TRIBUTE_V2_API_KEY': KEY, 'TRIBUTE_V2_MODE': 'test',
                    'TRIBUTE_V2_TEST_TELEGRAM_IDS': str(PAYER),
                    'TRIBUTE_V2_SUBSCRIPTION_IDS_JSON': json.dumps(BINDINGS),
                    'TRIBUTE_V2_TEST_ASMR_PLUS_EUR1': 'true'}
        self.bot = SimpleNamespace(
            create_chat_invite_link=AsyncMock(return_value=SimpleNamespace(invite_link='https://example.test/fixture-invite')),
            send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        self.ns = dict(web=web, os=SimpleNamespace(environ=self.env), json=json, hmac=hmac,
            hashlib=hashlib, html=html, _dt=datetime, timedelta=timedelta,
            logger=logging.getLogger('tribute-flow-test'),
            TributeV2Settings=Settings, TributeV2Error=TributeV2Error, parse_paid_event=parse_paid_event,
            TRIBUTE_API_KEY='fixture-legacy-key', TRIBUTE_SITE_WEBHOOK_URL='https://example.test/site',
            TRIBUTE_PLUS_PROJECT_BY_CENTS={300:'extraleaks',525:'asianleaks',549:'privateleaks',550:'privateleaks'},
            PROJECT_KEY='asmrleaks', BOT_TOKEN='fixture-unused', GROUP_ID=-100123, INVITE_LINK='',
            User=User, TributeWebhookEvent=Receipt, IntegrityError=IntegrityError, AsyncSession=object,
            async_session=self.sessions, select=select,
            Bot=lambda **_kwargs: self.bot, assign_tier_badge=lambda _user: None,
            consume_active_promo_days=AsyncMock(return_value=(31, None)),
            create_pending_invite=AsyncMock(), _notify_admins=AsyncMock(),
            notify_admin_error=AsyncMock(), forward_tribute_webhook=Mock(return_value=None),
            _schedule_background=Mock())
        load('database.py', 'claim_tribute_webhook_event', self.ns)
        load('webhook.py', '_tribute_event_key', self.ns)
        self.legacy = load('webhook.py', 'tribute_webhook', self.ns)
        self.new = load('webhook.py', 'tribute_v2_webhook', self.ns)
        module = ModuleType('aiogram.types')
        module.InlineKeyboardButton = lambda **kwargs: kwargs
        module.InlineKeyboardMarkup = lambda **kwargs: kwargs
        self.modules = patch.dict(sys.modules, {'aiogram.types': module})
        self.modules.start()

    async def asyncTearDown(self):
        self.modules.stop()
        await self.engine.dispose()

    async def request(self, data=None, key=KEY, legacy=False):
        body, signature = signed(data or event(price=100, amount=70), key)
        request = SimpleNamespace(read=AsyncMock(return_value=body), headers={'trbt-signature': signature})
        response = await (self.legacy if legacy else self.new)(request)
        return response.status, json.loads(response.text) if response.text else {}

    async def state(self):
        async with self.sessions() as session:
            user = await session.get(User, PAYER)
            count = await session.scalar(select(func.count(Receipt.id)))
            return user.units, user.tier, user.is_active, count

    async def test_one_euro_payment_grants_full_31_days_plus_and_one_saved_link(self):
        status, _ = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(await self.state(), (38, 'plus', True, 1))
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.ns['create_pending_invite'].assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        self.ns['consume_active_promo_days'].assert_not_awaited()
        self.ns['forward_tribute_webhook'].assert_not_called()

    async def test_webhook_retry_never_recredits_or_creates_a_second_link(self):
        await self.request()
        duplicate = event(price=100, amount=70)
        duplicate.update(name='renewed_subscription', created_at='2026-10-25T12:01:00Z', sent_at='2026-10-25T12:01:01Z')
        status, body = await self.request(duplicate)
        self.assertEqual((status, body.get('duplicate')), (200, True))
        self.assertEqual(await self.state(), (38, 'plus', True, 1))
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_new_renewal_period_grants_once_again(self):
        await self.request()
        renewal = event(price=100, amount=70, expires_at='2026-12-25T12:00:00Z')
        renewal['name'] = 'renewed_subscription'
        await self.request(renewal)
        self.assertEqual(await self.state(), (69, 'plus', True, 2))

    async def test_foreign_test_user_bad_id_and_wrong_price_do_not_touch_ledger(self):
        for data, expected in [(event(price=100,amount=70,telegram_user_id=PAYER+1),403),
                               (event(price=100,amount=70,subscription_id=9999),422),
                               (event(price=600,amount=550),422)]:
            with self.subTest(expected=expected):
                status, _ = await self.request(data)
                self.assertEqual(status, expected)
        self.assertEqual(await self.state(), (7, 'free', False, 0))
        self.bot.create_chat_invite_link.assert_not_awaited()

    async def test_all_other_products_stay_closed_for_the_first_real_payment_test(self):
        for code in BINDINGS:
            if code == 's1ajK':
                continue
            status, body = await self.request(event(code))
            self.assertEqual((status, body['error']), (503, 'product_not_enabled_for_first_test'))
        self.assertEqual(await self.state(), (7, 'free', False, 0))

    async def test_closed_mode_and_wrong_signatures_never_grant(self):
        self.env['TRIBUTE_V2_MODE'] = 'closed'
        self.assertEqual((await self.request())[0], 503)
        self.env['TRIBUTE_V2_MODE'] = 'test'
        self.assertEqual((await self.request(key='fixture-legacy-key'))[0], 401)
        self.assertEqual((await self.request(legacy=True))[0], 410)
        self.assertEqual(await self.state(), (7, 'free', False, 0))

    async def test_retired_legacy_account_never_grants_or_forwards_with_bad_new_config(self):
        self.env.update(TRIBUTE_V2_MODE='invalid', TRIBUTE_V2_SUBSCRIPTION_IDS_JSON='[]')
        data = event(price=800, amount=800)
        status, _ = await self.request(data, key='fixture-legacy-key', legacy=True)
        self.assertEqual(status, 410)
        self.assertEqual(await self.state(), (7, 'free', False, 0))
        self.ns['consume_active_promo_days'].assert_not_awaited()
        self.ns['forward_tribute_webhook'].assert_not_called()
        self.bot.create_chat_invite_link.assert_not_awaited()

    async def test_private_notification_failure_does_not_undo_granted_access(self):
        self.bot.send_message.side_effect = PermissionError('fixture blocked bot')
        self.assertEqual((await self.request())[0], 200)
        self.assertEqual(await self.state(), (38, 'plus', True, 1))
        self.ns['notify_admin_error'].assert_awaited_once()
        self.assertEqual((await self.request())[1].get('duplicate'), True)

    async def test_new_payment_is_marked_separately_for_correct_renewal_notices(self):
        self.assertEqual((await self.request())[0], 200)
        async with self.sessions() as session:
            user = await session.get(User, PAYER)
            self.assertEqual(user.last_payment_method, 'tribute_v2')

    def test_postgres_plan_serializes_old_and_new_tribute_account_updates(self):
        query = select(User).where(User.telegram_id == PAYER).with_for_update()
        self.assertIn('FOR UPDATE', str(query.compile(dialect=postgresql.dialect())))
        source = (ROOT/'webhook.py').read_text(encoding='utf-8')
        self.assertIn('select(User).where(User.telegram_id == telegram_id).with_for_update()', source)


class PrivateTestCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_is_only_sent_in_a_private_chat_to_the_test_user(self):
        env = {'TRIBUTE_V2_API_KEY':KEY, 'TRIBUTE_V2_MODE':'test',
               'TRIBUTE_V2_TEST_TELEGRAM_IDS':str(PAYER),
               'TRIBUTE_V2_SUBSCRIPTION_IDS_JSON':json.dumps(BINDINGS),
               'TRIBUTE_V2_TEST_ASMR_PLUS_EUR1':'true'}
        namespace = dict(Message=object, os=SimpleNamespace(environ=env), Settings=Settings,
                         TributeV2Error=TributeV2Error, checkout_product=checkout_product,
                         InlineKeyboardButton=lambda **kwargs: kwargs,
                         InlineKeyboardMarkup=lambda **kwargs: kwargs)
        handler = load('handlers/tribute_v2.py', 'tribute_test', namespace)
        for user, chat, opens in [(PAYER,'private',True), (PAYER+1,'private',False), (PAYER,'group',False)]:
            message = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(type=chat), answer=AsyncMock())
            await handler(message)
            if opens:
                self.assertIn('EUR 1.00', message.answer.call_args.args[0])
                self.assertEqual(message.answer.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['url'],
                                 'https://t.me/tribute/app?startapp=s1ajK')
            elif chat == 'private':
                self.assertNotIn('reply_markup', message.answer.call_args.kwargs)
            else:
                message.answer.assert_not_awaited()
