"""New creator-account policy. Legacy Tribute settings are intentionally unused."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Mapping


class TributeV2Error(ValueError):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Product:
    code: str
    project: str
    tier: str
    price_cents: int
    days: int = 31

    @property
    def url(self) -> str:
        return f'https://t.me/tribute/app?startapp={self.code}'


PRODUCTS = (
    Product('s1ajK', 'asmrleaks', 'plus', 600),
    Product('s1ajL', 'privateleaks', 'plus', 550),
    Product('s1ajN', 'asianleaks', 'plus', 525),
    Product('s1ajP', 'extraleaks', 'plus', 300),
    Product('s1ajR', 'asmrleaks', 'pro', 800),
    Product('s1ajS', 'asmrleaks', 'elite', 1000),
    Product('s1ajU', 'asmrleaks', 'king', 1300),
)
PRODUCT_BY_CODE = {product.code: product for product in PRODUCTS}
BUNDLE_PROJECTS = frozenset({'privateleaks', 'asianleaks', 'extraleaks'})


def positive_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise TributeV2Error('invalid_identifier', 400)
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    mode: str
    test_ids: frozenset[int]
    bindings: Mapping[str, tuple[int, int]]
    asmr_plus_eur1: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        key = (environ.get('TRIBUTE_V2_API_KEY') or '').strip()
        mode = (environ.get('TRIBUTE_V2_MODE') or 'closed').strip().lower()
        test_price = (environ.get('TRIBUTE_V2_TEST_ASMR_PLUS_EUR1') or 'false').strip().lower()
        if test_price not in {'true', 'false'}:
            raise TributeV2Error('invalid_test_price_flag')
        # Public checkout requires a separate reviewed release after the paid test.
        if mode not in {'closed', 'test'}:
            raise TributeV2Error('invalid_checkout_mode')
        try:
            raw_ids = (environ.get('TRIBUTE_V2_TEST_TELEGRAM_IDS') or '').split(',')
            test_ids = frozenset(positive_id(int(value.strip())) for value in raw_ids if value.strip())
        except (ValueError, TypeError):
            raise TributeV2Error('invalid_test_allowlist') from None
        try:
            raw = json.loads(environ.get('TRIBUTE_V2_SUBSCRIPTION_IDS_JSON') or '{}')
            if not isinstance(raw, dict) or set(raw) - set(PRODUCT_BY_CODE):
                raise ValueError
            bindings = {}
            for code, entry in raw.items():
                if not isinstance(entry, dict) or set(entry) != {'subscription_id', 'period_id'}:
                    raise ValueError
                bindings[code] = (positive_id(entry['subscription_id']), positive_id(entry['period_id']))
            if len({pair[0] for pair in bindings.values()}) != len(bindings):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise TributeV2Error('invalid_product_bindings') from None
        return cls(key, mode, test_ids, bindings, test_price == 'true')

    def price_for_test(self, product: Product) -> int:
        if self.mode == 'test' and self.asmr_plus_eur1 and product.code == 's1ajK':
            return 100
        return product.price_cents

    def require_test_access(self, telegram_id: int) -> None:
        if self.mode != 'test':
            raise TributeV2Error('checkout_closed')
        if positive_id(telegram_id) not in self.test_ids:
            raise TributeV2Error('test_account_required', 403)
        if not self.api_key:
            raise TributeV2Error('new_account_key_missing')
        if set(self.bindings) != set(PRODUCT_BY_CODE):
            raise TributeV2Error('product_bindings_incomplete')

    def product_for_ids(self, subscription_id: int, period_id: int) -> Product:
        pair = (positive_id(subscription_id), positive_id(period_id))
        for code, binding in self.bindings.items():
            if binding == pair:
                return PRODUCT_BY_CODE[code]
        raise TributeV2Error('unknown_subscription_period', 422)


def checkout_product(settings: Settings, telegram_id: int, code: str,
                     target_project: str | None = None) -> tuple[Product, str | None]:
    settings.require_test_access(telegram_id)
    product = PRODUCT_BY_CODE.get(code)
    if not product:
        raise TributeV2Error('unknown_product', 400)
    target = (target_project or '').strip().lower() or None
    if product.tier == 'elite':
        if target not in BUNDLE_PROJECTS:
            raise TributeV2Error('elite_target_required', 400)
    elif product.tier == 'king':
        if target not in {None, 'all'}:
            raise TributeV2Error('invalid_king_target', 400)
        target = 'all'
    elif target is not None:
        raise TributeV2Error('unexpected_bundle_target', 400)
    return product, target


def verify_signature(body: bytes, signature: str, api_key: str) -> None:
    if not api_key:
        raise TributeV2Error('new_account_key_missing')
    if (not isinstance(signature, str) or len(signature) != 64
            or any(char not in '0123456789abcdef' for char in signature)):
        raise TributeV2Error('invalid_signature', 401)
    expected = hmac.new(api_key.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise TributeV2Error('invalid_signature', 401)


def utc_timestamp(value) -> str:
    if not isinstance(value, str):
        raise TributeV2Error('invalid_event_timestamp', 400)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except ValueError:
        raise TributeV2Error('invalid_event_timestamp', 400) from None


@dataclass(frozen=True)
class PaidEvent:
    product: Product
    telegram_id: int
    event_key: str
    renewal: bool
    expires_at: str

    @property
    def order_uuid(self) -> str:
        return self.event_key


def parse_paid_event(body: bytes, signature: str, settings: Settings) -> PaidEvent:
    if len(body) > 65536:
        raise TributeV2Error('event_too_large', 413)
    verify_signature(body, signature, settings.api_key)
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get('payload'), dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise TributeV2Error('invalid_json_event', 400) from None
    if data.get('name') not in {'new_subscription', 'renewed_subscription'}:
        raise TributeV2Error('unsupported_event', 200)
    payload = data['payload']
    telegram_id = positive_id(payload.get('telegram_user_id'))
    settings.require_test_access(telegram_id)
    product = settings.product_for_ids(payload.get('subscription_id'), payload.get('period_id'))
    if payload.get('type', 'regular') != 'regular':
        raise TributeV2Error('nonpaid_subscription_type', 200)
    if str(payload.get('currency') or '').lower() != 'eur' or payload.get('period') != 'monthly':
        raise TributeV2Error('currency_or_period_mismatch', 422)
    # Tribute amount is NET of fees; price is the customer-facing minor-unit price.
    price, amount = payload.get('price'), payload.get('amount')
    if isinstance(price, bool) or not isinstance(price, int) or price != settings.price_for_test(product):
        raise TributeV2Error('price_mismatch', 422)
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= price:
        raise TributeV2Error('invalid_net_amount', 422)
    created_at = utc_timestamp(data.get('created_at'))
    expires_at = utc_timestamp(payload.get('expires_at'))
    if expires_at <= created_at:
        raise TributeV2Error('invalid_subscription_expiry', 422)
    # Retry sent_at/created_at and event-name changes must not re-credit a paid period.
    identity = json.dumps([payload['subscription_id'], payload['period_id'], telegram_id, expires_at],
                          separators=(',', ':')).encode()
    event_key = 'tribute-v2:' + hashlib.sha256(identity).hexdigest()
    return PaidEvent(product, telegram_id, event_key, data['name'] == 'renewed_subscription', expires_at)
