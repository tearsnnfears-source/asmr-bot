"""Notification failures must never masquerade as failed access grants."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


def delivery_function():
    source = ast.parse((Path(__file__).parents[1] / 'utils/bundles.py').read_text(encoding='utf-8'))
    node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == 'send_bundle_access_message')
    namespace = dict(Bot=object, InlineKeyboardButton=lambda **kw: kw,
                     InlineKeyboardMarkup=lambda **kw: kw, logger=logging.getLogger('delivery-test'))
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'utils/bundles.py', 'exec'), namespace)
    return namespace['send_bundle_access_message']


class BundleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def deliver(self, bot, links=None, lang='en'):
        return await delivery_function()(bot, telegram_id=123, tier='king', days=31,
            local_days_left=31, links=links or [], lang=lang)

    async def test_success_reports_delivery_and_all_links(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        links = [dict(project=p, url=f'https://example.test/{p}')
                 for p in ['asmrleaks', 'privateleaks', 'asianleaks', 'extraleaks']]
        self.assertTrue(await self.deliver(bot, links))
        rows = bot.send_message.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual(len(rows), 4)

    async def test_chat_not_found_does_not_raise_activation_failure(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('chat not found')))
        with self.assertLogs('delivery-test', level='WARNING') as logs:
            self.assertFalse(await self.deliver(bot))
        self.assertIn('links saved in Mini App LINKS', logs.output[0])
        bot.send_message.assert_awaited_once()

    async def test_blocked_or_network_error_does_not_retry_grants(self):
        for error in [PermissionError('blocked'), TimeoutError('network timeout')]:
            bot = SimpleNamespace(send_message=AsyncMock(side_effect=error))
            with self.assertLogs('delivery-test', level='WARNING'):
                self.assertFalse(await self.deliver(bot, lang='ru'))
            bot.send_message.assert_awaited_once()

    def test_both_payment_handlers_distinguish_delivery_failure(self):
        root = Path(__file__).parents[1]
        for file in ['webhook.py', 'handlers/payment.py']:
            source = (root / file).read_text(encoding='utf-8-sig')
            ast.parse(source)
            self.assertIn('delivered = await send_bundle_access_message(', source)
            self.assertIn('Do not re-credit this payment.', source)


if __name__ == '__main__':
    unittest.main()
