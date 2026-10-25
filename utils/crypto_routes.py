import json
import logging

from aiohttp import web

from database import async_session
from utils.crypto_payments import settings, create_order, owned_order, reconcile
from utils.plisio import CryptoError, verify_callback

logger = logging.getLogger(__name__)


def register_crypto_routes(app, parse_init_data):
    async def authenticated(request):
        user = await parse_init_data(request)
        if not user:
            raise CryptoError('Invalid Telegram session', 401)
        return user['user_id']

    async def config(request):
        telegram_id = await authenticated(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise CryptoError('Invalid configuration request')
        return web.json_response(await settings(telegram_id=telegram_id, tier=str(body.get('tier') or '').lower()))

    async def checkout(request):
        telegram_id = await authenticated(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise CryptoError('Invalid checkout request')
        return web.json_response(await create_order(telegram_id, body))

    async def status(request):
        telegram_id = await authenticated(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise CryptoError('Invalid invoice request')
        return web.json_response(await reconcile(body.get('order_id'), telegram_id))

    async def callback(request):
        if request.content_length and request.content_length > 65536:
            raise CryptoError('Invalid callback', 413)
        data = await request.json()
        if not verify_callback(data):
            raise CryptoError('Invalid callback signature', 401)
        if data.get('ipn_type') != 'invoice':
            raise CryptoError('Invalid invoice update')
        async with async_session() as session:
            order = await owned_order(session, data.get('order_number'))
            if not order.invoice_id or order.invoice_id not in {str(data.get('txn_id')), str(data.get('switch_id'))}:
                raise CryptoError('Invoice identity mismatch', 409)
        # A callback is only a wake-up signal. Access requires an authenticated
        # provider lookup of the exact stored invoice, never a success redirect.
        await reconcile(order.order_id, force=True)
        return web.json_response({'ok': True})

    def safe(handler):
        async def wrapped(request):
            try:
                response = await handler(request)
            except CryptoError as error:
                response = web.json_response({'error': error.code}, status=error.status)
            except (json.JSONDecodeError, UnicodeError, ValueError):
                response = web.json_response({'error': 'Invalid payment request'}, status=400)
            except Exception:
                logger.warning('Crypto operation failed; provider details withheld')
                response = web.json_response({'error': 'Payment service is temporarily unavailable'}, status=503)
            response.headers['Cache-Control'] = 'private, no-store'
            return response
        return wrapped

    app.router.add_post('/miniapp/crypto/config', safe(config))
    app.router.add_post('/miniapp/crypto/checkout', safe(checkout))
    app.router.add_post('/miniapp/crypto/status', safe(status))
    app.router.add_post('/payments/plisio/webhook', safe(callback))
