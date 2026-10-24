"""All Bot, database and payment interactions below are isolated test doubles."""
import ast
import asyncio
import copy
from functools import partial
import logging
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).parents[1]
REPORTING = runpy.run_path(str(ROOT / 'utils/error_reporting.py'))
describe_error = REPORTING['describe_error']
format_admin_error = REPORTING['format_admin_error']
notify_admin_error = REPORTING['notify_admin_error']


def exception(name, message, **attributes):
    error = type(name, (Exception,), {})(message)
    for key, value in attributes.items():
        setattr(error, key, value)
    return error


def load_function(file, name, namespace):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8-sig'))
    node = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name))
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, 'exec'), namespace)
    return namespace[name]


class ErrorDescriptionTests(unittest.TestCase):
    def test_known_causes_are_distinct(self):
        cases = [
            ('bot was blocked by the user', 'BOT_BLOCKED'),
            ('Forbidden: user is deactivated', 'USER_DEACTIVATED'),
            ("bot can't initiate conversation with a user", 'CHAT_NOT_STARTED'),
            ('Bad Request: chat not found', 'CHAT_NOT_FOUND'),
            ('Forbidden: bot was kicked from the supergroup chat', 'BOT_REMOVED'),
            ('not enough rights to invite users', 'INSUFFICIENT_RIGHTS'),
            ("Bad Request: can't parse entities", 'INVALID_MESSAGE'),
            ('not configured', 'PEER_NOT_CONFIGURED'),
            ('HTTP 401', 'PEER_AUTH_FAILED'),
            ('HTTP 503', 'PEER_UNAVAILABLE'),
            ('invite unavailable', 'INVITE_UNAVAILABLE'),
        ]
        for message, code in cases:
            with self.subTest(code=code):
                self.assertEqual(describe_error(RuntimeError(message))['code'], code)

    def test_forbidden_alone_is_not_labelled_user_blocking(self):
        info = describe_error(exception('TelegramForbiddenError', 'Forbidden'))
        self.assertEqual(info['code'], 'FORBIDDEN')

    def test_rate_limit_uses_structured_retry_after(self):
        info = describe_error(exception('TelegramRetryAfter', 'Flood control', retry_after=37))
        self.assertEqual(info['code'], 'RATE_LIMIT')
        self.assertIn('37 сек.', info['reason'])

    def test_network_failure_is_unconfirmed_not_a_definite_failed_grant(self):
        info = describe_error(TimeoutError('private response details'))
        self.assertEqual(info['code'], 'NETWORK_UNCONFIRMED')
        self.assertIn('мог выполниться', info['action'])

    def test_database_error_takes_priority_over_connection_text(self):
        self.assertEqual(describe_error(exception('OperationalError', 'connection timeout'))['code'], 'DATABASE_ERROR')

    def test_telegram_auth_and_server_errors(self):
        for name, code in [('TelegramUnauthorizedError', 'BOT_AUTH_FAILED'), ('TelegramServerError', 'TELEGRAM_UNAVAILABLE')]:
            self.assertEqual(describe_error(exception(name, 'details'))['code'], code)

    def test_unknown_exception_payload_is_not_exposed(self):
        secret = 'DO_NOT_RENDER_PRIVATE_PAYLOAD'
        text = format_admin_error(title='Title <unsafe>', stage='Step <unsafe>',
            error=RuntimeError('https://example.invalid/private?token=' + secret),
            telegram_id=123, order_uuid=secret)
        self.assertNotIn(secret, text)
        self.assertNotIn('https://', text)
        self.assertIn('Title &lt;unsafe&gt;', text)
        self.assertIn('Step &lt;unsafe&gt;', text)
        self.assertIn('RuntimeError', text)
        self.assertNotIn('Дни ASMR уже начислены', text)

    def test_saved_links_notice_is_explicit_and_does_not_promise_all_peer_grants(self):
        text = format_admin_error(title='KING', stage='DM', telegram_id=123,
            error=RuntimeError('bot was blocked by the user'), access_state='links_saved')
        self.assertIn('пользователь заблокировал бота', text)
        self.assertIn('Mini App LINKS', text)
        self.assertIn('Повторно начислять дни не нужно', text)

    def test_peer_failures_show_safe_causes_without_raw_payloads(self):
        text = REPORTING['format_bundle_failures']([
            {'project': 'privateleaks', 'error': 'HTTP 403'},
            {'project': 'asianleaks', 'error': 'connection timeout'},
            {'project': 'extraleaks', 'error': 'https://secret.invalid/private token=SECRET'},
        ])
        self.assertIn('PrivateLeaks', text)
        self.assertIn('PEER_AUTH_FAILED', text)
        self.assertIn('NETWORK_UNCONFIRMED', text)
        self.assertNotIn('secret.invalid', text)
        self.assertNotIn('SECRET', text)


class AdminNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_hanging_admin_request_is_bounded_and_next_admin_is_not_skipped(self):
        async def send(chat_id, *_args, **_kwargs):
            if chat_id == 11:
                await asyncio.Event().wait()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        with patch.dict(notify_admin_error.__globals__, ADMIN_NOTICE_TIMEOUT_SECONDS=0.01), self.assertLogs(level='WARNING'):
            result = await notify_admin_error(bot, admin_ids=[11, 22], title='KING', stage='DM',
                error=RuntimeError('chat not found'), telegram_id=123)
        self.assertTrue(result)
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_a_failed_admin_dm_does_not_block_other_admins_or_raise(self):
        async def send(chat_id, *_args, **_kwargs):
            if chat_id == 11:
                raise RuntimeError('bot was blocked by the user SECRET')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        with self.assertLogs(level='WARNING') as logs:
            result = await notify_admin_error(bot, admin_ids=[11, 22], title='KING', stage='DM',
                error=RuntimeError('bot was blocked by the user SECRET'), telegram_id=123)
        self.assertTrue(result)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    async def test_no_recursive_notification_when_all_admins_are_unreachable(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError('private')))
        with self.assertLogs(level='WARNING'):
            result = await notify_admin_error(bot, admin_ids=[11, 22], title='KING', stage='DM',
                error=RuntimeError('chat not found'), telegram_id=123)
        self.assertFalse(result)
        self.assertEqual(bot.send_message.await_count, 2)


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class PaymentFailureFlowTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, blocked=False, tier='king'):
        async def send(chat_id, *_args, **_kwargs):
            if blocked and chat_id == 123:
                raise RuntimeError('Forbidden: bot was blocked by the user')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
            session=SimpleNamespace(close=AsyncMock()), create_chat_invite_link=AsyncMock())
        user = SimpleNamespace(units=31 if tier == 'king' else 0, tier=tier, lang='en', username='', full_name='Fixture')
        session = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
        notify = partial(notify_admin_error, admin_ids=[11, 22])
        namespace = dict(Bot=object, Message=object, AsyncSession=object,
            logger=logging.getLogger('flow-test'),
            InlineKeyboardButton=lambda **kw: kw, InlineKeyboardMarkup=lambda **kw: kw,
            notify_admin_error=notify, describe_error=describe_error,
            format_bundle_failures=REPORTING['format_bundle_failures'])
        delivery = load_function('utils/bundles.py', 'send_bundle_access_message', namespace)
        links = [{'project': 'asmrleaks', 'url': 'https://example.test/fixture'}]
        namespace.update(
            _resolve_stars_payload=lambda _payload: (31, tier, 5000, 'all' if tier == 'king' else None),
            get_or_create_user=AsyncMock(return_value=user),
            consume_active_promo_days=AsyncMock(return_value=(31, None)),
            apply_external_grant=AsyncMock(return_value=(True, user)),
            activate_bundle_access=AsyncMock(return_value=(links, [])),
            complete_bundle_checkout=AsyncMock(), create_pending_invite=AsyncMock(),
            _notify_admins=AsyncMock(), assign_tier_badge=Mock(),
            select=lambda *_args: SimpleNamespace(where=lambda *_args: None),
            ExternalGrant=SimpleNamespace(id=0, source_project='', order_uuid=''),
            INVITE_LINK='https://example.test/fallback', GROUP_ID=None,
            async_session=lambda: SessionContext(session),
        )
        message = SimpleNamespace(from_user=SimpleNamespace(id=123),
            successful_payment=SimpleNamespace(invoice_payload='fixture', total_amount=5000,
                telegram_payment_charge_id='fixture-charge'))
        return namespace, bot, user, session, message

    def admin_texts(self, bot):
        return [call.args[1] for call in bot.send_message.await_args_list if call.args[0] in {11, 22}]

    async def test_stars_blocked_dm_reports_exact_reason_without_recredit(self):
        ns, bot, _user, session, message = self.runtime(blocked=True)
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        ns['apply_external_grant'].assert_awaited_once()
        ns['activate_bundle_access'].assert_awaited_once()
        ns['complete_bundle_checkout'].assert_awaited_once()
        texts = self.admin_texts(bot)
        self.assertEqual(len(texts), 2)
        self.assertIn('пользователь заблокировал бота', texts[0])
        self.assertIn('Mini App LINKS', texts[0])
        self.assertNotIn('подготовка пакета не завершена', texts[0])

    async def test_bundle_save_failure_identifies_stage_and_keeps_peer_status_unconfirmed(self):
        ns, bot, _user, session, message = self.runtime()
        ns['complete_bundle_checkout'].side_effect = exception('IntegrityError', 'private database details')
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        text = self.admin_texts(bot)[0]
        self.assertIn('Сохранение ссылок пакета', text)
        self.assertIn('DATABASE_ERROR', text)
        self.assertNotIn('ссылки сохранены', text)
        self.assertNotIn('private database details', text)
        ns['apply_external_grant'].assert_awaited_once()
        self.assertEqual(sum(call.args[0] == 123 for call in bot.send_message.await_args_list), 0)

    async def test_bundle_peer_timeout_never_claims_access_was_not_granted(self):
        ns, bot, _user, session, message = self.runtime()
        ns['activate_bundle_access'].side_effect = TimeoutError('private network details')
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        text = self.admin_texts(bot)[0]
        self.assertIn('NETWORK_UNCONFIRMED', text)
        self.assertIn('Дни ASMR начислены', text)
        self.assertIn('требуют сверки', text)
        ns['complete_bundle_checkout'].assert_not_awaited()
        ns['apply_external_grant'].assert_awaited_once()

    async def test_successful_bundle_does_not_generate_error_alerts(self):
        ns, bot, _user, session, message = self.runtime()
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        await handler(message, session, bot)
        self.assertEqual(self.admin_texts(bot), [])
        bot.send_message.assert_awaited_once()

    async def test_message_preparation_failure_preserves_confirmed_saved_links_status(self):
        ns, bot, _user, session, message = self.runtime()
        ns['send_bundle_access_message'] = AsyncMock(side_effect=RuntimeError('private payload'))
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        text = self.admin_texts(bot)[0]
        self.assertIn('Отправка сообщения со ссылками', text)
        self.assertIn('ссылки сохранены', text)
        self.assertNotIn('private payload', text)
        ns['apply_external_grant'].assert_awaited_once()

    async def test_nonbundle_invite_rights_failure_does_not_change_credited_days(self):
        ns, bot, user, session, message = self.runtime(tier='plus')
        ns['GROUP_ID'] = -100123
        bot.create_chat_invite_link.side_effect = RuntimeError('not enough rights to invite users')
        module = ModuleType('keyboards.inline')
        module.kb_after_payment = lambda *_args: None
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with patch.dict(sys.modules, {'keyboards.inline': module}), self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        self.assertEqual(user.units, 31)
        session.commit.assert_awaited_once()
        text = self.admin_texts(bot)[0]
        self.assertIn('INSUFFICIENT_RIGHTS', text)
        self.assertIn('Создание одноразового приглашения после Stars', text)
        self.assertIn('Резервная ссылка доступна', text)
        self.assertEqual(sum(call.args[0] == 123 for call in bot.send_message.await_args_list), 1)

    async def test_manual_credit_notification_failure_reports_reason_without_an_extra_commit(self):
        ns, bot, user, session, _message = self.runtime(blocked=True)
        user.units = 10
        ns.update(is_admin=lambda _value: True, get_user=AsyncMock(return_value=user))
        message = SimpleNamespace(from_user=SimpleNamespace(id=11), text='/add_units 123 5', reply=AsyncMock())
        handler = load_function('handlers/admin.py', 'cmd_add_units', ns)
        with self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        self.assertEqual(user.units, 15)
        session.commit.assert_awaited_once()
        self.assertIn('BOT_BLOCKED', self.admin_texts(bot)[0])
        self.assertIn('Дни ASMR уже начислены', self.admin_texts(bot)[0])

    async def test_nonbundle_stars_dm_failure_does_not_masquerade_as_payment_failure(self):
        ns, bot, user, session, message = self.runtime(blocked=True, tier='plus')
        module = ModuleType('keyboards.inline')
        module.kb_after_payment = lambda *_args: None
        handler = load_function('handlers/payment.py', 'process_successful_payment', ns)
        with patch.dict(sys.modules, {'keyboards.inline': module}), self.assertLogs(level='WARNING'):
            await handler(message, session, bot)
        self.assertEqual(user.units, 31)
        session.commit.assert_awaited_once()
        text = self.admin_texts(bot)[0]
        self.assertIn('Stars: не удалось отправить сообщение после оплаты', text)
        self.assertIn('Дни ASMR уже начислены', text)

    async def test_tribute_bundle_branch_reports_blocking_without_a_second_grant(self):
        ns, bot, user, session, _message = self.runtime(blocked=True)
        tree = ast.parse((ROOT / 'webhook.py').read_text(encoding='utf-8-sig'))
        webhook = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'tribute_webhook')
        branch = next(node for node in ast.walk(webhook) if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name) and node.test.id == 'is_bundle'
            and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                and child.func.id == 'send_bundle_access_message' for child in ast.walk(node)))
        wrapper = ast.parse('async def run_bundle():\n    pass').body[0]
        wrapper.body = copy.deepcopy(branch.body)
        ns.update(tribute_order_uuid='fixture-order', BOT_TOKEN='fixture-unused', Bot=lambda **_kw: bot,
            user=user, payload={}, TRIBUTE_DAYS=31, total=31, purchased_tier='king',
            bundle_target='all', bundle_checkout_id=None, telegram_id=123, lang='en',
            promo_code=None, admin_event_label='Fixture payment', admin_payment_details='',
            web=SimpleNamespace(json_response=lambda value: value))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), 'webhook.py', 'exec'), ns)
        with self.assertLogs(level='WARNING'):
            result = await ns['run_bundle']()
        self.assertEqual(result['status'], 'ok')
        ns['activate_bundle_access'].assert_awaited_once()
        ns['complete_bundle_checkout'].assert_awaited_once()
        ns['apply_external_grant'].assert_not_awaited()
        text = self.admin_texts(bot)[0]
        self.assertIn('BOT_BLOCKED', text)
        self.assertIn('ссылки сохранены', text)
        bot.session.close.assert_awaited_once()

    async def test_bundle_invite_error_and_unreachable_admins_do_not_skip_peer_grant(self):
        ns, bot, _user, session, _message = self.runtime()
        bot.create_chat_invite_link.side_effect = RuntimeError('not enough rights to invite users')
        bot.send_message.side_effect = TimeoutError('private')
        peer = AsyncMock(return_value={'ok': True, 'url': 'https://example.test/peer',
            'project': 'privateleaks', 'label': 'PrivateLeaks', 'days_left': 31})
        ns.update(GROUP_ID=-100123, datetime=datetime, timedelta=timedelta, asyncio=asyncio,
            bundle_projects_for_tier=lambda *_args: ['privateleaks'], SOURCE_PROJECT='asmrleaks',
            SOURCE_LABEL='ASMR.LEAKS', ClientTimeout=lambda **_kwargs: None,
            ClientSession=lambda **_kwargs: SessionContext(session), _send_peer_grant=peer)
        activate = load_function('utils/bundles.py', 'activate_bundle_access', ns)
        with self.assertLogs(level='WARNING'):
            links, failures = await activate(bot, telegram_id=123, order_uuid='fixture',
                username='', full_name='Fixture', days=31, local_days_left=31,
                tier='king', target_project='all')
        peer.assert_awaited_once()
        self.assertEqual(len(links), 2)
        self.assertEqual(failures, [])

    async def test_shared_invite_failure_reports_link_stage_and_closes_bot(self):
        ns, bot, _user, _session, _message = self.runtime()
        bot.create_chat_invite_link.side_effect = RuntimeError('not enough rights to invite users')
        ns.update(Bot=lambda **_kwargs: bot, BOT_TOKEN='fixture-unused', GROUP_ID=-100123,
            _dt=datetime, timedelta=timedelta)
        create = load_function('webhook.py', '_create_group_invite_for_user', ns)
        with self.assertLogs(level='WARNING'):
            link = await create(123)
        self.assertEqual(link, 'https://example.test/fallback')
        self.assertIn('INSUFFICIENT_RIGHTS', self.admin_texts(bot)[0])
        self.assertNotIn('Дни ASMR уже начислены', self.admin_texts(bot)[0])
        bot.session.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
