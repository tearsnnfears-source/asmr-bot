"""Server-only Plisio client. Provider errors and keyed URLs are never exposed."""
import hashlib
import hmac
import json
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout

METHODS = {
    'usdt_bep20': ('USDT_BSC', 'USDT', 'BEP20'),
    'usdt_trc20': ('USDT_TRX', 'USDT', 'TRC20'),
    'usdt_erc20': ('USDT', 'USDT', 'ERC20'),
    'usdt_ton': ('USDT_TON', 'USDT', 'TON'),
    'btc': ('BTC', 'BTC', 'Bitcoin'),
    'ton': ('TON', 'TON', 'TON'),
    'eth': ('ETH', 'ETH', 'Ethereum'),
    'bnb_bep20': ('BNB', 'BNB', 'BEP20'),
    'ltc': ('LTC', 'LTC', 'Litecoin'),
    'usdc_erc20': ('USDC', 'USDC', 'ERC20'),
}


class CryptoError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def api_key():
    return os.getenv('PLISIO_API_KEY', '').strip()


def payment_url_valid(value):
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.netloc == 'plisio.net'
                and not url.query and not url.fragment
                and re.fullmatch(r'/invoice/[A-Za-z0-9_-]{8,100}/?', url.path) is not None)
    except (ValueError, TypeError):
        return False


def verify_callback(data, key=None):
    key = api_key() if key is None else key
    if not key or not isinstance(data, dict):
        return False
    signature = data.get('verify_hash')
    if not isinstance(signature, str) or not re.fullmatch('[a-fA-F0-9]{40}', signature):
        return False
    signed = {name: value for name, value in data.items() if name != 'verify_hash'}
    try:
        raw = json.dumps(signed, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        return False
    digest = hmac.new(key.encode(), raw.encode(), hashlib.sha1).hexdigest()
    return hmac.compare_digest(digest, signature.lower())


async def request(endpoint, params=None):
    key = api_key()
    if not key:
        raise CryptoError('Plisio is not configured', 503)
    if not re.fullmatch(r'(invoices/new|operations/[A-Za-z0-9_-]{8,100})', endpoint):
        raise CryptoError('Invalid provider operation')
    try:
        async with ClientSession(timeout=ClientTimeout(total=18)) as session:
            async with session.get('https://api.plisio.net/api/v1/' + endpoint,
                                   params={**(params or {}), 'api_key': key},
                                   allow_redirects=False) as response:
                body = await response.json(content_type=None)
                if response.status != 200 or body.get('status') != 'success' or not isinstance(body.get('data'), dict):
                    raise ValueError
                return body['data']
    except Exception:
        raise CryptoError('Plisio is temporarily unavailable. Try again later or contact support.', 503) from None


def assert_operation(data, order):
    if (not isinstance(data, dict) or data.get('type') != 'invoice'
            or str(data.get('id')) != order.invoice_id):
        raise CryptoError('Invoice identity mismatch', 409)
    params = data.get('params') or {}
    if not isinstance(params, dict):
        raise CryptoError('Invalid invoice metadata', 409)
    fields = {**data, **params}
    if 'order_number' in fields and str(fields['order_number']) != order.order_id:
        raise CryptoError('Invoice order mismatch', 409)
    for name, expected in [('source_currency', 'EUR')]:
        if fields.get(name) is not None and str(fields[name]).upper() != expected:
            raise CryptoError('Invoice currency mismatch', 409)
    network = params.get('currency') or data.get('psys_cid') or data.get('currency')
    if network is not None and str(network).upper() != METHODS[order.coin][0]:
        raise CryptoError('Invoice network mismatch', 409)
    if fields.get('source_amount') is not None:
        try:
            amount = Decimal(str(fields['source_amount']))
            valid = amount.is_finite() and amount * 100 == order.price_cents
        except InvalidOperation:
            valid = False
        if not valid:
            raise CryptoError('Invoice price mismatch', 409)
    # Standard non-White-Label operations may omit price/network fields. Only this
    # authenticated lookup of the exact stored invoice can use saved order values.
    return str(data.get('status') or '')
