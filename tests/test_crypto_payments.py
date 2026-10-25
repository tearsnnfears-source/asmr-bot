"""Isolated SQLite and mocked provider/Telegram checks. Never uses production data."""
import ast
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from aiohttp import web
from sqlalchemy import Boolean, Column, Integer, String, UniqueConstraint, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from utils.plisio import (CryptoError, METHODS, api_key, assert_operation,
                          payment_url_valid, verify_callback)
from utils.tribute_checkout import PRODUCTS, PROJECTS

ROOT = Path(__file__).parents[1]


def load(file, ns):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8-sig'))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, file, 'exec'), ns)


class ProviderTests(unittest.TestCase):
    def test_only_hosted_invoice_urls(self):
        self.assertTrue(payment_url_valid('https://plisio.net/invoice/abcdef0123456789'))
        for value in [None, 'javascript:x', 'http://plisio.net/invoice/abcdef0123456789',
                      'https://plisio.net.evil.test/invoice/abcdef0123456789',
                      'https://u@plisio.net/invoice/abcdef0123456789',
                      'https://plisio.net:444/invoice/abcdef0123456789',
                      'https://plisio.net/invoice/abcdef0123456789?redirect=evil']:
            self.assertFalse(payment_url_valid(value))

    def test_callback_signature_order_tampering_and_missing_key(self):
        data = {'txn_id': 'abcdefgh12345678', 'ipn_type': 'invoice', 'status': 'completed', 'source_amount': '6.00'}
        digest = hmac.new(b'fixture', json.dumps(data, separators=(',', ':')).encode(), hashlib.sha1).hexdigest()
        signed = {**data, 'verify_hash': digest}
        self.assertTrue(verify_callback(signed, 'fixture'))
        self.assertFalse(verify_callback({**signed, 'source_amount': '1.00'}, 'fixture'))
        self.assertFalse(verify_callback(dict(reversed(list(signed.items()))), 'fixture'))
        self.assertFalse(verify_callback(signed, ''))
        for value in [None, [], {}, {'verify_hash': 'f' * 39}]:
            self.assertFalse(verify_callback(value, 'fixture'))

    def test_operation_identity_and_conflicting_metadata(self):
        order = SimpleNamespace(invoice_id='abcdefgh12345678', order_id='fixture-order',
                                price_cents=600, coin='usdt_trc20')
        # Standard operations may omit White Label fields, but must match stored ID.
        self.assertEqual(assert_operation({'id': order.invoice_id, 'type': 'invoice', 'status': 'completed'}, order), 'completed')
        for changes in [{'id': 'different'}, {'type': 'cash_out'}, {'source_amount': '1.00'},
                        {'source_amount': 'NaN'}, {'source_currency': 'USD'}, {'currency': 'BTC'},
                        {'params': {'order_number': 'another'}}]:
            with self.assertRaises(CryptoError):
                assert_operation({'id': order.invoice_id, 'type': 'invoice', 'status': 'completed', **changes}, order)


class CryptoPayments(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Base(DeclarativeBase):
            pass
        class User(Base):
            __tablename__ = 'users'
            telegram_id = Column(Integer, primary_key=True)
            units = Column(Integer, default=0)
            tier = Column(String, default='free')
            username = Column(String)
            full_name = Column(String)
            lang = Column(String, default='en')
            badge = Column(String)
            last_payment_method = Column(String)
            is_active = Column(Boolean, default=False)
        class Grant(Base):
            __tablename__ = 'external_grants'
            __table_args__ = (UniqueConstraint('source_project', 'order_uuid'),)
            id = Column(Integer, primary_key=True)
            source_project = Column(String)
            order_uuid = Column(String)
            telegram_id = Column(Integer)
            days = Column(Integer)
            tier = Column(String)
        self.User, self.Grant, self.base = User, Grant, Base
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + str(Path(self.temp.name) / 'crypto.db'))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async def user(session, telegram_id, **kwargs):
            row = await session.get(User, telegram_id)
            if row is None:
                row = User(telegram_id=telegram_id, units=7, tier='free', lang='en')
                session.add(row)
                await session.commit()
            return row
        self.bot = SimpleNamespace(send_message=AsyncMock(), create_chat_invite_link=AsyncMock(
            return_value=SimpleNamespace(invite_link='https://example.test/invite')),
            session=SimpleNamespace(close=AsyncMock()))
        self.request = AsyncMock(return_value={'txn_id': 'abcdef0123456789',
                                              'invoice_url': 'https://plisio.net/invoice/abcdef0123456789'})
        self.ns = dict(Base=Base, User=User, ExternalGrant=Grant, async_session=self.sessions,
            Mapped=Mapped, mapped_column=mapped_column, BigInteger=__import__('sqlalchemy').BigInteger,
            DateTime=__import__('sqlalchemy').DateTime, Integer=Integer, String=String, UniqueConstraint=UniqueConstraint,
            select=select, func=func, IntegrityError=IntegrityError, datetime=datetime,
            timedelta=__import__('datetime').timedelta, timezone=__import__('datetime').timezone,
            uuid=uuid, re=__import__('re'), logging=logging, os=os,
            PROJECT_KEY='privateleaks', PROJECTS=PROJECTS, PRODUCTS=PRODUCTS, METHODS=METHODS,
            BOT_TOKEN='fixture', GROUP_ID=-1001, INVITE_LINK='', ADMIN_IDS=[1], MINIAPP_URL='https://example.test/app',
            get_or_create_user=user, assign_tier_badge=lambda _: None, AsyncSession=object,
            Bot=lambda **_: self.bot, InlineKeyboardButton=lambda **kw: kw, InlineKeyboardMarkup=lambda **kw: kw,
            complete_bundle_checkout=AsyncMock(), create_pending_invite=AsyncMock(),
            get_latest_invite=AsyncMock(return_value='https://example.test/saved'),
            activate_bundle_access=AsyncMock(return_value=([{'project': 'privateleaks', 'url': 'https://example.test/invite'}], [])),
            send_bundle_access_message=AsyncMock(), CryptoError=CryptoError, api_key=api_key,
            payment_url_valid=payment_url_valid, request=self.request, assert_operation=assert_operation)
        grant_tree = ast.parse((ROOT / 'database.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in grant_tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'apply_external_grant')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'database.py', 'exec'), self.ns)
        load('utils/crypto_payments.py', self.ns)
        self.Order, self.Setting = self.ns['CryptoOrder'], self.ns['CryptoSetting']
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.env = patch.dict(os.environ, {'PLISIO_API_KEY': 'OFFLINE_FIXTURE_ONLY',
            'PLISIO_TEST_MODE': 'false', 'PLISIO_TEST_TELEGRAM_ID': ''})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def enable(self, value='plisio'):
        async with self.sessions() as session:
            return await self.ns['set_mode'](session, value, 1)

    async def create(self, **changes):
        body = {'tier': 'plus', 'coin': 'usdt_trc20', 'request_id': str(uuid.uuid4()), **changes}
        return await self.ns['create_order'](77, body), body

    def completed(self, order, **changes):
        self.request.return_value = {'id': 'abcdef0123456789', 'type': 'invoice', 'status': 'completed',
            'params': {'order_number': order['order_id'], 'source_currency': 'EUR', 'source_amount': '5.50'},
            'psys_cid': 'USDT_TRX', **changes}

    async def balance(self):
        async with self.sessions() as session:
            user = await session.get(self.User, 77)
            return user.units, user.tier, await session.scalar(select(func.count()).select_from(self.Grant))

    async def test_manual_default_does_not_contact_provider_or_create_user(self):
        result, _ = await self.create()
        self.assertEqual(result, {'provider': 'manual'})
        self.request.assert_not_awaited()
        async with self.sessions() as session:
            self.assertIsNone(await session.get(self.User, 77))

    async def test_project_local_persistent_switch_and_key_requirement(self):
        await self.enable()
        self.ns['PROJECT_KEY'] = 'asianleaks'
        async with self.sessions() as session:
            self.assertEqual(await self.ns['mode'](session), 'manual')
        with patch.dict(os.environ, {'PLISIO_API_KEY': ''}):
            with self.assertRaises(CryptoError): await self.enable()
        self.assertEqual(await self.enable('manual'), 'manual')

    async def test_all_seven_server_prices_and_bundle_validation(self):
        for project, cents in [('asmrleaks', 600), ('privateleaks', 550), ('asianleaks', 525), ('extraleaks', 300)]:
            self.ns['PROJECT_KEY'] = project
            self.assertEqual(self.ns['plan']('plus', None), (cents, None))
            self.assertEqual(self.ns['plan']('king', None), (1300, 'all'))
            target = next(iter(PROJECTS - {project}))
            self.assertEqual(self.ns['plan']('elite', target), (1000, target))
            with self.assertRaises(CryptoError): self.ns['plan']('elite', project)
            with self.assertRaises(CryptoError): self.ns['plan']('plus', target)
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        self.assertEqual(self.ns['plan']('pro', None), (800, None))
        self.ns['PROJECT_KEY'] = 'privateleaks'
        with self.assertRaises(CryptoError): self.ns['plan']('pro', None)

    async def test_creating_invoice_never_grants_and_ignores_client_price_or_user(self):
        await self.enable()
        result, body = await self.create(price_cents=1, telegram_id=999, days=9999)
        self.assertEqual(result['price_cents'], 550)
        args = self.request.await_args.args
        self.assertEqual(args[1]['source_amount'], '5.50')
        self.assertEqual(args[1]['currency'], 'USDT_TRX')
        self.assertTrue(args[1]['callback_url'].endswith('/payments/plisio/webhook?json=true'))
        self.assertNotIn('77', args[1]['order_name'])
        self.assertEqual(await self.balance(), (7, 'free', 0))
        self.ns['activate_bundle_access'].assert_not_awaited()

    async def test_request_retry_reuses_original_invoice_even_after_mode_switch(self):
        await self.enable()
        first, body = await self.create()
        await self.enable('manual')
        second = await self.ns['create_order'](77, body)
        self.assertEqual(first, second)
        self.assertEqual(self.request.await_count, 1)
        with self.assertRaises(CryptoError): await self.ns['create_order'](78, body)
        with self.assertRaises(CryptoError): await self.ns['create_order'](77, {**body, 'coin': 'btc'})

    async def test_ambiguous_provider_timeout_is_not_created_again(self):
        await self.enable()
        self.request.side_effect = TimeoutError('api_key=MUST_NOT_ESCAPE')
        body = {'tier': 'plus', 'coin': 'btc', 'request_id': str(uuid.uuid4())}
        with self.assertRaises(CryptoError) as raised: await self.ns['create_order'](77, body)
        self.assertNotIn('MUST_NOT_ESCAPE', str(raised.exception))
        result = await self.ns['create_order'](77, body)
        self.assertEqual(result['status'], 'creation_unknown')
        self.assertEqual(self.request.await_count, 1)

    async def test_owner_is_checked_before_provider_lookup(self):
        await self.enable()
        order, _ = await self.create()
        self.request.reset_mock()
        with self.assertRaises(CryptoError) as raised: await self.ns['reconcile'](order['order_id'], 78)
        self.assertEqual(raised.exception.status, 404)
        self.request.assert_not_awaited()

    async def test_completed_invoice_and_duplicate_add_days_once_even_after_manual_switch(self):
        await self.enable()
        order, _ = await self.create()
        await self.enable('manual')
        self.completed(order)
        result = await self.ns['reconcile'](order['order_id'], 77)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(await self.balance(), (38, 'plus', 1))
        self.assertEqual((await self.ns['reconcile'](order['order_id'], 77, force=True))['status'], 'confirmed')
        self.assertEqual(await self.balance(), (38, 'plus', 1))

    async def test_unpaid_expired_and_underpayment_never_grant(self):
        await self.enable()
        order, _ = await self.create()
        for status in ['pending', 'expired', 'mismatch']:
            self.completed(order, status=status)
            await self.ns['reconcile'](order['order_id'], 77, force=True)
            self.assertEqual(await self.balance(), (7, 'free', 0))
        self.completed(order, params={'source_currency': 'EUR', 'source_amount': '1.00'})
        with self.assertRaises(CryptoError): await self.ns['reconcile'](order['order_id'], 77, force=True)
        self.assertEqual(await self.balance(), (7, 'free', 0))

    async def test_partial_bundle_retry_preserves_target_and_does_not_credit_twice(self):
        await self.enable()
        order, _ = await self.create(tier='elite', bundle_target='asianleaks')
        self.completed(order, params={'source_amount': '10.00'})
        self.ns['activate_bundle_access'].return_value = ([], [{'project': 'asianleaks'}])
        with self.assertRaises(CryptoError): await self.ns['reconcile'](order['order_id'], 77, force=True)
        self.assertEqual(await self.balance(), (38, 'elite', 1))
        self.ns['activate_bundle_access'].return_value = ([{'url': 'https://example.test/saved'}], [])
        self.assertEqual((await self.ns['reconcile'](order['order_id'], 77, force=True))['status'], 'confirmed')
        self.assertEqual(await self.balance(), (38, 'elite', 1))
        self.assertEqual(self.ns['activate_bundle_access'].await_args.kwargs['target_project'], 'asianleaks')

    async def test_blocked_dm_does_not_turn_confirmed_payment_into_retry(self):
        await self.enable()
        order, _ = await self.create()
        self.completed(order)
        self.bot.send_message.side_effect = RuntimeError('bot blocked')
        self.assertEqual((await self.ns['reconcile'](order['order_id'], 77))['status'], 'confirmed')
        self.assertEqual(await self.balance(), (38, 'plus', 1))

    async def test_lifetime_is_never_downgraded(self):
        await self.enable()
        order, _ = await self.create()
        async with self.sessions() as session:
            user = await session.get(self.User, 77)
            user.tier = 'lifetime'
            await session.commit()
        self.completed(order)
        await self.ns['reconcile'](order['order_id'], 77)
        self.assertEqual((await self.balance())[1], 'lifetime')

    async def test_status_checks_are_throttled(self):
        await self.enable()
        order, _ = await self.create()
        self.completed(order, status='pending')
        self.request.reset_mock()
        await self.ns['reconcile'](order['order_id'], 77)
        await self.ns['reconcile'](order['order_id'], 77)
        self.assertEqual(self.request.await_count, 1)

    async def test_asmr_promo_is_used_only_on_confirmed_payment_once(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        order, _ = await self.create()
        promo = AsyncMock(return_value=(45, 'FIXTURE'))
        self.completed(order, params={'source_amount': '6.00'})
        with patch.dict(sys.modules, {'database': SimpleNamespace(consume_active_promo_days=promo)}):
            await self.ns['reconcile'](order['order_id'], 77, force=True)
            await self.ns['reconcile'](order['order_id'], 77, force=True)
        promo.assert_awaited_once()
        self.assertEqual(await self.balance(), (52, 'plus', 1))

    async def test_distinct_paid_invoices_are_not_mistaken_for_duplicate_callbacks(self):
        await self.enable()
        first, _ = await self.create()
        self.request.return_value = {'txn_id': 'fedcba9876543210',
                                     'invoice_url': 'https://plisio.net/invoice/fedcba9876543210'}
        second, _ = await self.create()
        for order, invoice_id in [(first, 'abcdef0123456789'), (second, 'fedcba9876543210')]:
            self.completed(order, id=invoice_id)
            await self.ns['reconcile'](order['order_id'], 77, force=True)
        self.assertEqual(await self.balance(), (69, 'plus', 2))

    async def test_invoice_creation_rate_limit_is_server_enforced(self):
        await self.enable()
        for number in range(5):
            invoice_id = f'fixtureinvoice{number}'
            self.request.return_value = {'txn_id': invoice_id,
                'invoice_url': 'https://plisio.net/invoice/' + invoice_id}
            await self.create()
        with self.assertRaises(CryptoError) as raised:
            await self.create()
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(self.request.await_count, 5)

    async def test_closed_scope_returns_plisio_only_to_one_account_and_plus(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        with patch.dict(os.environ, {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '77'}):
            allowed = await self.ns['settings'](telegram_id=77, tier='plus')
            self.assertEqual((allowed['provider'], allowed['test_only'], allowed['price_cents']), ('plisio', True, 600))
            for uid, tier in [(78, 'plus'), (None, 'plus'), (0, 'plus'), (77, 'pro'),
                              (77, 'elite'), (77, 'king'), (77, None)]:
                self.assertEqual((await self.ns['settings'](telegram_id=uid, tier=tier))['provider'], 'manual')
            self.ns['PROJECT_KEY'] = 'asianleaks'
            await self.enable()
            self.assertEqual((await self.ns['settings'](telegram_id=77, tier='plus'))['provider'], 'manual')

    async def test_closed_scope_rejects_client_spoofing_without_any_invoice_or_user(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        with patch.dict(os.environ, {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '77'}):
            for uid, tier, target in [(78, 'plus', ''), (77, 'pro', ''),
                                      (77, 'elite', 'asianleaks'), (77, 'king', 'all')]:
                result = await self.ns['create_order'](uid, {'tier': tier, 'coin': 'btc',
                    'request_id': str(uuid.uuid4()), 'bundle_target': target,
                    'telegram_id': 77, 'provider': 'plisio', 'price_cents': 1})
                self.assertEqual(result, {'provider': 'manual'})
            self.request.assert_not_awaited()
            async with self.sessions() as session:
                self.assertEqual(await session.scalar(select(func.count()).select_from(self.User)), 0)
                self.assertEqual(await session.scalar(select(func.count()).select_from(self.Order)), 0)

    async def test_closed_scope_allowed_invoice_keeps_normal_six_euro_price(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        with patch.dict(os.environ, {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '77'}):
            result, _ = await self.create(price_cents=1, days=9999)
        self.assertEqual((result['provider'], result['price_cents'], result['days']), ('plisio', 600, 31))
        self.assertEqual(self.request.await_args.args[1]['source_amount'], '6.00')
        self.assertEqual(await self.balance(), (7, 'free', 0))

    async def test_closed_scope_invalid_configuration_fails_closed(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        for values in [{'PLISIO_TEST_MODE': 'typo'}, {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': ''},
                       {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '77,78'},
                       {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '0'}]:
            with patch.dict(os.environ, values):
                with self.assertRaises(CryptoError): await self.ns['settings'](telegram_id=77, tier='plus')
        self.request.assert_not_awaited()

    async def test_manual_command_stops_new_test_invoices_but_not_an_existing_receipt(self):
        self.ns['PROJECT_KEY'] = 'asmrleaks'
        await self.enable()
        with patch.dict(os.environ, {'PLISIO_TEST_MODE': 'true', 'PLISIO_TEST_TELEGRAM_ID': '77'}):
            original, body = await self.create()
            await self.enable('manual')
            self.assertEqual((await self.ns['settings'](telegram_id=77, tier='plus'))['provider'], 'manual')
            self.assertEqual((await self.create())[0], {'provider': 'manual'})
            self.assertEqual(await self.ns['create_order'](77, body), original)
            self.assertEqual(self.request.await_count, 1)


class RoutesAndCommand(unittest.IsolatedAsyncioTestCase):
    def routes(self, authenticated=True):
        ns = dict(web=web, json=json, logging=logging, CryptoError=CryptoError,
            settings=AsyncMock(return_value={'provider': 'manual'}), create_order=AsyncMock(),
            reconcile=AsyncMock(), verify_callback=lambda _: False)
        load('utils/crypto_routes.py', ns)
        app = web.Application()
        ns['register_crypto_routes'](app, AsyncMock(return_value={'user_id': 77} if authenticated else None))
        return {route.resource.canonical: route.handler for route in app.router.routes()}, ns

    async def test_unauthenticated_routes_do_not_read_settings_or_create_invoice(self):
        routes, ns = self.routes(False)
        request = SimpleNamespace(json=AsyncMock(return_value={}))
        for name in ['config', 'checkout', 'status']:
            self.assertEqual((await routes['/miniapp/crypto/' + name](request)).status, 401)
        ns['settings'].assert_not_awaited()
        ns['create_order'].assert_not_awaited()
        ns['reconcile'].assert_not_awaited()

    async def test_invalid_callback_never_reconciles(self):
        routes, ns = self.routes()
        request = SimpleNamespace(content_length=20, json=AsyncMock(return_value={}))
        self.assertEqual((await routes['/payments/plisio/webhook'](request)).status, 401)
        ns['reconcile'].assert_not_awaited()

    async def test_authenticated_checkout_uses_session_user_not_client_user(self):
        routes, ns = self.routes()
        ns['create_order'].return_value = {'provider': 'manual'}
        payload = {'tier': 'plus', 'telegram_id': 999, 'price_cents': 1}
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        result = await routes['/miniapp/crypto/checkout'](request)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers['Cache-Control'], 'private, no-store')
        ns['create_order'].assert_awaited_once_with(77, payload)

    async def test_config_scope_uses_authenticated_user_and_selected_tier(self):
        routes, ns = self.routes()
        request = SimpleNamespace(json=AsyncMock(return_value={'tier': 'PRO', 'telegram_id': 999}))
        self.assertEqual((await routes['/miniapp/crypto/config'](request)).status, 200)
        ns['settings'].assert_awaited_once_with(telegram_id=77, tier='pro')

    async def test_signed_callback_still_requires_stored_invoice_and_provider_lookup(self):
        routes, ns = self.routes()
        @asynccontextmanager
        async def sessions():
            yield object()
        ns['async_session'] = sessions
        ns['verify_callback'] = lambda _: True
        ns['owned_order'] = AsyncMock(return_value=SimpleNamespace(order_id='fixture-order', invoice_id='fixture-invoice'))
        request = SimpleNamespace(content_length=100, json=AsyncMock(return_value={
            'ipn_type': 'invoice', 'order_number': 'fixture-order', 'txn_id': 'wrong-invoice'}))
        self.assertEqual((await routes['/payments/plisio/webhook'](request)).status, 409)
        ns['reconcile'].assert_not_awaited()
        request.json.return_value['txn_id'] = 'fixture-invoice'
        self.assertEqual((await routes['/payments/plisio/webhook'](request)).status, 200)
        ns['reconcile'].assert_awaited_once_with('fixture-order', force=True)

    async def test_admin_command_requires_admin_private_chat_and_valid_action(self):
        ns = dict(ADMIN_IDS=[1], mode=AsyncMock(return_value='manual'), set_mode=AsyncMock(return_value='plisio'),
                  CryptoError=CryptoError, api_key=lambda: 'fixture', test_scope=lambda: None,
                  Message=object, AsyncSession=object)
        tree = ast.parse((ROOT / 'handlers/crypto_pay.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef))
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'command', 'exec'), ns)
        message = SimpleNamespace(from_user=SimpleNamespace(id=2), chat=SimpleNamespace(type='private'),
                                  text='/crypto_pay Plisio', reply=AsyncMock())
        await ns['crypto_pay'](message, object())
        message.reply.assert_not_awaited()
        ns['set_mode'].assert_not_awaited()
        message.from_user.id = 1
        message.chat.type = 'group'
        await ns['crypto_pay'](message, object())
        ns['set_mode'].assert_not_awaited()
        message.chat.type = 'private'
        await ns['crypto_pay'](message, object())
        ns['set_mode'].assert_awaited_once_with(unittest.mock.ANY, 'plisio', 1)
