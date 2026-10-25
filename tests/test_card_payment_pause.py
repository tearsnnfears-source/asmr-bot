"""Offline checks: no network, payment events, or real account data."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1]


def load_function(file, name, namespace):
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), namespace)
    return namespace[name]


def translation():
    namespace = {}
    exec((ROOT / "locales/texts.py").read_text(encoding="utf-8-sig"), namespace)
    return namespace["t"]


class CardPaymentPauseTests(unittest.IsolatedAsyncioTestCase):
    def test_closed_checkout_does_not_use_legacy_links(self):
        namespace = dict(STARS_PRICES={30: 500}, TRIBUTE_BOT_URL="https://example.test/old-checkout",
                         InlineKeyboardMarkup=lambda **kw: kw, InlineKeyboardButton=lambda **kw: kw,
                         t=translation(), TRIBUTE_PLUS_URL="")
        keyboard = load_function("keyboards/inline.py", "kb_payment", namespace)
        for language in ("en", "ru", "es"):
            rows = keyboard(language, 30)["inline_keyboard"]
            self.assertEqual(rows[0][0]["callback_data"], "pay:tribute:30")
            self.assertNotIn("url", rows[0][0])
            self.assertEqual(rows[1][0]["callback_data"], "pay:stars:30")

    def test_notice_fits_telegram_alert_and_falls_back_to_english(self):
        t = translation()
        for language in ("en", "ru", "es"):
            text = t("card_payment_unavailable", language)
            self.assertTrue(text)
            self.assertLessEqual(len(text), 200)
        self.assertEqual(t("card_payment_unavailable", "unknown"), t("card_payment_unavailable", "en"))

    def test_top_up_copy_uses_approved_sentence_without_store_names(self):
        t = translation()
        self.assertTrue(t("card_payment_unavailable", "ru").endswith(
            "Stars можно удобно купить банковской картой прямо в Telegram."))
        for language in ("en", "ru", "es"):
            text = t("card_payment_unavailable", language)
            for name in ("App Store", "Google Play", "Apple Pay", "Google Pay", "@PremiumBot"):
                self.assertNotIn(name, text)

    async def test_legacy_callback_only_answers_and_never_creates_invoice(self):
        for language in ("en", "ru", "es"):
            user_lookup = AsyncMock(return_value=SimpleNamespace(lang=language))
            session = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
            call = SimpleNamespace(from_user=SimpleNamespace(id=123), data="pay:tribute:30",
                                   answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()))
            handler = load_function("handlers/payment.py", "cb_tribute",
                                    dict(CallbackQuery=object, AsyncSession=object,
                                         get_or_create_user=user_lookup, t=translation(), TRIBUTE_PLUS_URL=""))
            await handler(call, session)
            call.answer.assert_awaited_once_with(translation()("card_payment_unavailable", language), show_alert=True)
            call.message.answer.assert_not_awaited()
            session.commit.assert_not_awaited()
            session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

