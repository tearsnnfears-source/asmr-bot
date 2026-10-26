"""Receipt rendering and Stars handlers with synthetic data; no network or DB."""
import ast
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

from utils.payment_notifications import payment_admin_notice

ROOT = Path(__file__).parents[1]


class ReceiptFormatTests(unittest.TestCase):
    def notice(self, **changes):
        data = dict(provider='Tribute V2', telegram_id=12345, username='fixture',
                    amount='€6.00', tier='plus', project='asmrleaks', days=31,
                    total_days=38, receipt_id=1000, channel='fixture_channel')
        return payment_admin_notice(**(data | changes))

    def test_tribute_uses_the_previous_field_order(self):
        self.assertEqual(self.notice(), '\n'.join([
            '💳 <b>Новая подписка — Tribute V2</b>',
            '👤 @fixture | <code>12345</code>',
            '💶 Сумма: <b>€6.00</b>',
            '💎 Tier: <b>PLUS</b>',
            '📣 Канал: <code>fixture_channel</code>',
            '🧾 Subscription ID: <code>1000</code>',
            '📅 +31 дней | Итого: 38 дн.',
        ]))

    def test_renewal_remains_distinguishable(self):
        self.assertIn('Продление подписки — Tribute V2', self.notice(renewal=True))

    def test_stars_uses_actual_xtr_amount_and_payment_not_subscription_id(self):
        notice = self.notice(provider='TG Stars', amount='925 ⭐ (XTR)', receipt_id='fixture-charge')
        self.assertIn('Новая оплата — TG Stars', notice)
        self.assertIn('925 ⭐ (XTR)', notice)
        self.assertIn('Payment ID: <code>fixture-charge</code>', notice)
        self.assertNotIn('Subscription ID', notice)
        self.assertNotIn('€', notice)

    def test_untrusted_text_is_escaped_and_missing_fields_not_fabricated(self):
        notice = self.notice(username='<x&', channel='<channel&', receipt_id='<id&', promo_code='<promo&')
        for value in ['@&lt;x&amp;', '&lt;channel&amp;', '&lt;id&amp;', '&lt;promo&amp;']:
            self.assertIn(value, notice)
        self.assertIn('id12345', self.notice(username=None))
        self.assertIn('Subscription ID: <code>Не указан</code>', self.notice(receipt_id=None))
        self.assertIn('Итого: Не указан', self.notice(total_days=None))

    def test_each_project_has_its_own_label(self):
        for project, label in [('asmrleaks','ASMRLeaks'),('privateleaks','PrivateLeaks'),
                               ('asianleaks','AsianLeaks'),('extraleaks','ExtraLeaks')]:
            self.assertIn(f'Канал: <code>{label}</code>', self.notice(project=project, channel=None))

    def test_bundle_and_promo_details_remain(self):
        notice = self.notice(tier='king', links_count=4, local_tier='pro', promo_code='FIXTURE')
        for text in ['Tier: <b>KING</b>', 'ASMR tier: <b>PRO</b>', 'Ссылок: 4', 'Промокод: <code>FIXTURE</code>']:
            self.assertIn(text, notice)

    def test_lifetime_never_looks_like_a_monthly_payment(self):
        notice = self.notice(provider='TG Stars', tier='lifetime', days=10000, total_days=10000)
        self.assertIn('Бессрочный доступ | Итого: Без ограничений', notice)


class StarsReceiptFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_payment(self, tier='plus', failures=False):
        user = SimpleNamespace(units=38 if tier in {'elite','king'} else 7, tier='plus',
                               username='fixture',full_name='Fixture',lang='en')
        project = 'asmrleaks' if ROOT.name == 'asmr-bot-shorts' else 'asianleaks' if ROOT.name == 'asianleaks-bot' else 'extraleaks' if ROOT.name == 'extraleaks-bot' else 'privateleaks'
        session = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda:None)))
        ns = dict(Message=object,AsyncSession=object,Bot=object,logger=logging.getLogger('receipt-fixture'),
            _resolve_stars_payload=lambda _: (10000 if tier=='lifetime' else 31,tier,925,'all' if tier=='king' else 'asianleaks' if tier=='elite' else None),
            get_or_create_user=AsyncMock(return_value=user),consume_active_promo_days=AsyncMock(return_value=(31,'FIXTURE')),
            apply_external_grant=AsyncMock(return_value=(True,user)),assign_tier_badge=lambda _:None,
            select=lambda *_:SimpleNamespace(where=lambda *_:None),ExternalGrant=SimpleNamespace(id=0,source_project='',order_uuid=''),
            activate_bundle_access=AsyncMock(return_value=([{'project':project,'url':'https://example.test/fixture'}],[{'project':'asianleaks'}] if failures else [])),
            complete_bundle_checkout=AsyncMock(),send_bundle_access_message=AsyncMock(),
            create_pending_invite=AsyncMock(),_notify_admins=AsyncMock(),notify_admin_error=AsyncMock(),
            payment_admin_notice=payment_admin_notice,format_bundle_failures=lambda items:'\nFIXTURE_LINK_FAILURE' if items else '',
            PROJECT_KEY=project,INVITE_LINK='https://example.test/fixture',GROUP_ID=None)
        source=ast.parse((ROOT/'handlers/payment.py').read_text(encoding='utf-8-sig'))
        handler=next(n for n in source.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='process_successful_payment')
        handler.decorator_list=[]
        exec(compile(ast.Module(body=[handler],type_ignores=[]),'handlers/payment.py','exec'),ns)
        message=SimpleNamespace(from_user=SimpleNamespace(id=12345,username='fixture',full_name='Fixture'),
            successful_payment=SimpleNamespace(invoice_payload='fixture',total_amount=925,telegram_payment_charge_id='fixture-charge'))
        bot=SimpleNamespace(send_message=AsyncMock(),create_chat_invite_link=AsyncMock())
        inline=ModuleType('keyboards.inline')
        inline.kb_after_payment=lambda *_:None
        with patch.dict(sys.modules,{'keyboards.inline':inline}):
            await ns['process_successful_payment'](message,session,bot)
        ns['_notify_admins'].assert_awaited_once()
        ns['notify_admin_error'].assert_not_awaited()
        notice=ns['_notify_admins'].await_args.args[1]
        for text in ['925 ⭐ (XTR)',f'Tier: <b>{tier.upper()}</b>','Payment ID: <code>fixture-charge</code>']:
            self.assertIn(text,notice)
        if tier=='lifetime':
            label={'privateleaks':'PrivateLeaks','asianleaks':'AsianLeaks','extraleaks':'ExtraLeaks'}[project]
            self.assertIn(f'{label} is unlocked forever',bot.send_message.await_args.args[1])
        return ns,session,user,notice

    async def test_regular_receipt_keeps_one_balance_commit(self):
        ns,session,user,notice=await self.run_payment()
        self.assertEqual(user.units,38)
        session.commit.assert_awaited_once()
        ns['apply_external_grant'].assert_not_awaited()
        self.assertIn('Итого: 38 дн.',notice)

    async def test_bundle_receipt_preserves_link_count_and_single_grant(self):
        ns,session,user,notice=await self.run_payment('king')
        ns['apply_external_grant'].assert_awaited_once()
        ns['activate_bundle_access'].assert_awaited_once()
        ns['complete_bundle_checkout'].assert_awaited_once()
        self.assertIn('Ссылок: 1',notice)

    async def test_partial_bundle_keeps_failure_details(self):
        ns,session,user,notice=await self.run_payment('elite',failures=True)
        self.assertTrue('FIXTURE_LINK_FAILURE' in notice or 'Links failed:' in notice)
        self.assertTrue(ns['complete_bundle_checkout'].await_args.kwargs['partial'])

    async def test_lifetime_receipt_and_user_message_keep_the_project_brand(self):
        if ROOT.name == 'asmr-bot-shorts':
            self.skipTest('ASMR does not sell a Stars lifetime plan')
        ns,session,user,notice=await self.run_payment('lifetime')
        self.assertEqual(user.units,10000)
        self.assertIn('Бессрочный доступ',notice)
