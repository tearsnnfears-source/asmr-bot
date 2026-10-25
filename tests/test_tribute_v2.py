"""Offline new-account signature, product identity and private test policy."""
from dataclasses import replace
import hashlib
import hmac
import json
import unittest

from utils.tribute_v2 import (PRODUCTS, PRODUCT_BY_CODE, Settings, TributeV2Error,
                             checkout_product, parse_paid_event, verify_signature)

PAYER = 1234567890
KEY = 'fixture-new-account-key'
BINDINGS = {product.code: {'subscription_id': 1000 + index, 'period_id': 2000 + index}
            for index, product in enumerate(PRODUCTS)}


def settings(**overrides):
    env = {'TRIBUTE_V2_API_KEY': KEY, 'TRIBUTE_V2_MODE': 'test',
           'TRIBUTE_V2_TEST_TELEGRAM_IDS': str(PAYER),
           'TRIBUTE_V2_SUBSCRIPTION_IDS_JSON': json.dumps(BINDINGS)}
    env.update(overrides)
    return Settings.from_env(env)


def event(code='s1ajK', **payload):
    product = PRODUCT_BY_CODE[code]
    return {'name': 'new_subscription', 'created_at': '2026-10-25T12:00:00Z',
            'sent_at': '2026-10-25T12:00:01Z', 'payload': {
                **BINDINGS[code], 'telegram_user_id': PAYER, 'trb_user_id': 'T-fixture',
                'period': 'monthly', 'type': 'regular', 'currency': 'eur',
                'price': product.price_cents, 'amount': product.price_cents - 50,
                'expires_at': '2026-11-25T12:00:00Z', **payload}}


def signed(data, key=KEY):
    body = json.dumps(data, separators=(',', ':')).encode()
    return body, hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def parse(data=None, config=None):
    body, signature = signed(data or event())
    return parse_paid_event(body, signature, config or settings())


class TributeV2PolicyTests(unittest.TestCase):
    def test_public_mode_accepts_normal_products_for_other_users_but_never_the_euro_one_override(self):
        config = settings(TRIBUTE_V2_MODE='public')
        for product in PRODUCTS:
            payment = parse(event(product.code, telegram_user_id=PAYER+1), config)
            self.assertEqual(payment.product, product)
        self.assert_code('price_mismatch', lambda: parse(event(price=100, amount=70), config))
        self.assert_code('test_price_must_be_disabled', lambda:
                         settings(TRIBUTE_V2_MODE='public', TRIBUTE_V2_TEST_ASMR_PLUS_EUR1='true'))

    def test_non_hex_signatures_are_rejected_without_a_server_error(self):
        for signature in ['я' * 64, 'z' * 64, None]:
            with self.assertRaises(TributeV2Error) as caught:
                verify_signature(b'{}', signature, KEY)
            self.assertEqual(caught.exception.status, 401)

    def assert_code(self, code, fn):
        with self.assertRaises(TributeV2Error) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def test_all_seven_product_routes_and_prices_are_explicit(self):
        self.assertEqual([(p.project, p.tier, p.price_cents) for p in PRODUCTS], [
            ('asmrleaks', 'plus', 600), ('privateleaks', 'plus', 550),
            ('asianleaks', 'plus', 525), ('extraleaks', 'plus', 300),
            ('asmrleaks', 'pro', 800), ('asmrleaks', 'elite', 1000), ('asmrleaks', 'king', 1300)])
        for product in PRODUCTS:
            with self.subTest(code=product.code):
                paid = parse(event(product.code))
                self.assertEqual(paid.product, product)
                self.assertEqual(paid.product.days, 31)

    def test_defaults_are_closed_and_legacy_key_is_not_inherited(self):
        config = Settings.from_env({'TRIBUTE_API_KEY': 'fixture-legacy'})
        self.assertEqual(config.mode, 'closed')
        self.assertEqual(config.api_key, '')
        self.assert_code('checkout_closed', lambda: checkout_product(config, PAYER, 's1ajK'))
        self.assert_code('invalid_checkout_mode', lambda: settings(TRIBUTE_V2_MODE='unknown'))

    def test_only_explicit_test_user_can_receive_checkout_links(self):
        self.assert_code('test_account_required', lambda: checkout_product(settings(), PAYER + 1, 's1ajK'))
        product, target = checkout_product(settings(), PAYER, 's1ajK')
        self.assertEqual(product.url, 'https://t.me/tribute/app?startapp=s1ajK')
        self.assertIsNone(target)
        self.assert_code('test_account_required', lambda: parse(event(telegram_user_id=PAYER + 1)))

    def test_missing_key_or_partial_binding_never_opens_checkout(self):
        self.assert_code('new_account_key_missing', lambda: checkout_product(settings(TRIBUTE_V2_API_KEY=''), PAYER, 's1ajK'))
        self.assert_code('product_bindings_incomplete', lambda: checkout_product(settings(TRIBUTE_V2_SUBSCRIPTION_IDS_JSON='{}'), PAYER, 's1ajK'))
        self.assertNotIn(KEY, repr(settings()))

    def test_signature_uses_new_key_and_original_bytes_only(self):
        body, signature = signed(event())
        verify_signature(body, signature, KEY)
        for candidate, sig, key in [(body, signature, 'fixture-old-key'), (body+b' ', signature, KEY),
                                     (body, '', KEY), (body, 'x'*64, KEY)]:
            with self.subTest(signature=sig[:4]):
                self.assert_code('invalid_signature', lambda: verify_signature(candidate, sig, key))
        self.assert_code('new_account_key_missing', lambda: verify_signature(body, signature, ''))

    def test_product_identity_never_comes_from_price(self):
        self.assert_code('unknown_subscription_period', lambda: parse(event(subscription_id=99999)))
        self.assert_code('unknown_subscription_period', lambda: parse(event(period_id=99999)))
        self.assert_code('price_mismatch', lambda: parse(event('s1ajL', price=600)))
        self.assertEqual(parse(event('s1ajL', amount=300)).product.project, 'privateleaks')

    def test_price_not_net_amount_is_used_for_plan_validation(self):
        for amount in [0, 300, 549, 600]:
            self.assertEqual(parse(event(amount=amount)).product.tier, 'plus')
        self.assert_code('invalid_net_amount', lambda: parse(event(amount=601)))

    def test_one_euro_override_is_only_asmr_plus_and_explicit_test_account(self):
        config = settings(TRIBUTE_V2_TEST_ASMR_PLUS_EUR1='true')
        paid = parse(event(price=100, amount=70), config)
        self.assertEqual((paid.product.code, paid.product.tier, paid.product.days), ('s1ajK', 'plus', 31))
        self.assertEqual(paid.product.price_cents, 600)
        self.assert_code('price_mismatch', lambda: parse(event(price=100, amount=70)))
        self.assert_code('price_mismatch', lambda: parse(event('s1ajU', price=100, amount=70), config))
        self.assert_code('test_account_required', lambda: parse(event(price=100, amount=70, telegram_user_id=PAYER+1), config))
        self.assert_code('checkout_closed', lambda: parse(event(price=100, amount=70), replace(config, mode='closed')))

    def test_retries_and_duplicate_event_names_share_one_billing_period_key(self):
        original = event()
        duplicate = event()
        duplicate.update(name='renewed_subscription', sent_at='2026-10-25T12:10:00Z', created_at='2026-10-25T12:01:00Z')
        self.assertEqual(parse(original).event_key, parse(duplicate).event_key)
        self.assertNotEqual(parse(original).event_key, parse(event(expires_at='2026-12-25T12:00:00Z')).event_key)
        self.assertTrue(parse(original).event_key.startswith('tribute-v2:'))
        self.assertLessEqual(len(parse(original).order_uuid), 96)

    def test_expiration_timezone_is_canonical(self):
        self.assertEqual(parse(event()).event_key, parse(event(expires_at='2026-11-25T15:00:00+03:00')).event_key)
        self.assert_code('invalid_event_timestamp', lambda: parse(event(expires_at='2026-11-25T12:00:00')))
        self.assert_code('invalid_subscription_expiry', lambda: parse(event(expires_at='2026-10-24T12:00:00Z')))

    def test_currency_period_gifts_and_trial_cannot_grant_regular_paid_access(self):
        for payload, code in [({'currency':'usd'},'currency_or_period_mismatch'),
                              ({'period':'yearly'},'currency_or_period_mismatch'),
                              ({'type':'trial'},'nonpaid_subscription_type'),
                              ({'type':'gift'},'nonpaid_subscription_type')]:
            self.assert_code(code, lambda: parse(event(**payload)))

    def test_bundle_target_is_mandatory_and_cannot_be_forged(self):
        config = settings()
        self.assert_code('elite_target_required', lambda: checkout_product(config, PAYER, 's1ajS'))
        self.assert_code('elite_target_required', lambda: checkout_product(config, PAYER, 's1ajS', 'asmrarchive'))
        self.assertEqual(checkout_product(config, PAYER, 's1ajS', 'asianleaks')[1], 'asianleaks')
        self.assertEqual(checkout_product(config, PAYER, 's1ajU')[1], 'all')
        self.assert_code('unexpected_bundle_target', lambda: checkout_product(config, PAYER, 's1ajK', 'all'))

    def test_invalid_configuration_and_boolean_identifiers_fail_closed(self):
        self.assert_code('invalid_test_allowlist', lambda: settings(TRIBUTE_V2_TEST_TELEGRAM_IDS='not-an-id'))
        self.assert_code('invalid_test_price_flag', lambda: settings(TRIBUTE_V2_TEST_ASMR_PLUS_EUR1='yes'))
        self.assert_code('invalid_identifier', lambda: parse(event(telegram_user_id=True)))
        self.assert_code('price_mismatch', lambda: parse(event(price=True)))
        self.assert_code('invalid_product_bindings', lambda: settings(TRIBUTE_V2_SUBSCRIPTION_IDS_JSON='[]'))
        duplicate = dict(BINDINGS)
        duplicate['s1ajL'] = BINDINGS['s1ajK']
        self.assert_code('invalid_product_bindings', lambda: settings(TRIBUTE_V2_SUBSCRIPTION_IDS_JSON=json.dumps(duplicate)))


if __name__ == '__main__':
    unittest.main()
