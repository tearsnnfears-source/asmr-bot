import hashlib
import hmac
import logging
import json
import urllib.parse
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy import select

from database import async_session, User, PendingPayment, Artist, get_all_artists, ArtistContent, get_artist_content, Tag, get_all_tags, get_reactions, get_user_reactions, get_user_reaction, set_reaction, get_comments, add_comment, ALLOWED_REACTIONS, Favorite, Playlist, PlaylistItem, ArtistSuggestion, CustomBadge, get_custom_badges, PendingInvite, create_pending_invite, consume_pending_invite, assign_tier_badge, _auto_thumbnail
from config import TRIBUTE_API_KEY, TRIBUTE_SITE_WEBHOOK_URL, BOT_TOKEN, INVITE_LINK, STARS_PRICES, STARS_TIER_PRICES, GROUP_ID, ADMIN_IDS, TRIBUTE_TIER_MAP, TRIBUTE_PLUS_URL, TRIBUTE_PRO_URL, CRYPTO_WALLET_USDT_TRC20, CRYPTO_WALLET_USDT_TON, CRYPTO_WALLET_ETH, CRYPTO_USDT_PRICES

logger = logging.getLogger(__name__)

async def _notify_admins(text: str):
    from config import ADMIN_IDS
    bot = Bot(token=BOT_TOKEN)
    try:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass
    finally:
        await bot.session.close()

async def forward_tribute_webhook(body: bytes, signature: str) -> None:
    if not TRIBUTE_SITE_WEBHOOK_URL:
        return

    headers = {"Content-Type": "application/json"}
    if signature:
        headers["trbt-signature"] = signature

    try:
        timeout = ClientTimeout(total=8)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(TRIBUTE_SITE_WEBHOOK_URL, data=body, headers=headers) as response:
                if response.status >= 400:
                    response_text = await response.text()
                    logger.warning(
                        "Failed to forward Tribute webhook to site: status=%s body=%s",
                        response.status,
                        response_text[:300],
                    )
    except Exception as e:
        logger.warning("Failed to forward Tribute webhook to site: %s", e)

@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        response = web.Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

async def tribute_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    signature = request.headers.get("trbt-signature", "")
    if TRIBUTE_API_KEY and signature:
        expected = hmac.new(TRIBUTE_API_KEY.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Tribute signature")
            return web.Response(status=403)

    await forward_tribute_webhook(body, signature)

    try:
        data = json.loads(body)
    except Exception:
        return web.Response(status=400)

    event = data.get("name")
    payload = data.get("payload", {})
    logger.info(f"Tribute webhook event: {event}")

    if event in ("new_subscription", "renewed_subscription"):
        telegram_id = int(payload.get("telegram_user_id", 0))
        if not telegram_id:
            return web.json_response({"status": "ok"})

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()

            # Determine tier from startapp code (explicit), fallback to amount
            raw = json.dumps(payload)
            new_tier = 'plus'
            for code, tier in TRIBUTE_TIER_MAP.items():
                if code in raw:
                    new_tier = tier
                    break
            else:
                try:
                    amount_eur = float(payload.get("amount", 0) or payload.get("price", 0) or 0)
                except Exception:
                    amount_eur = 0
                if amount_eur >= 8:
                    new_tier = 'pro'

            TRIBUTE_DAYS = 31
            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=payload.get("telegram_username"),
                    units=TRIBUTE_DAYS,
                    is_active=True,
                    last_payment_method="tribute",
                    tier=new_tier,
                )
                session.add(user)
                grace_debt = 0
            else:
                # Grace period: preserve negative balance (anti-abuse)
                grace_debt = min(0, user.units)  # e.g. -3 if in grace period
                user.units += TRIBUTE_DAYS  # -3 + 31 = 28 effective days
                user.is_active = True
                user.last_payment_method = "tribute"
                if new_tier == 'pro' or getattr(user, 'tier', 'plus') != 'pro':
                    user.tier = new_tier
            assign_tier_badge(user)

            await session.commit()
            total = user.units
            lang = user.lang or "en"

        # Уведомление админу о Tribute оплате
        username = payload.get("telegram_username", "")
        nick = f"@{username}" if username else f"id{telegram_id}"
        grace_note = f" (grace debt: {grace_debt}d)" if grace_debt < 0 else ""
        await _notify_admins(
            f"💳 <b>Новая оплата — Tribute</b>\n"
            f"👤 {nick} | <code>{telegram_id}</code>\n"
            f"📅 +{TRIBUTE_DAYS}{grace_note} | Итого: {total} дн."
        )

        bot = Bot(token=BOT_TOKEN)
        invite_link = INVITE_LINK
        try:
            from datetime import timedelta
            if GROUP_ID:
                link_obj = await bot.create_chat_invite_link(
                    GROUP_ID,
                    member_limit=1,
                    expire_date=int((__import__('datetime').datetime.utcnow() + timedelta(hours=72)).timestamp()),
                )
                invite_link = link_obj.invite_link
                async with async_session() as inv_session:
                    await create_pending_invite(inv_session, telegram_id, invite_link)
        except Exception as e:
            logger.error(f"Failed to create invite link for {telegram_id}: {e}")

        try:
            if lang == "ru":
                text = (f"✅ <b>Оплата через Tribute прошла успешно!</b>\n\n"
                        f"📅 Добавлено: <b>31 день</b>\n"
                        f"📅 Итого: <b>{total} дней</b>\n\n"
                        f"🔗 Вернитесь в мини-апп — там вас ждёт ссылка для вступления.")
            else:
                text = (f"✅ <b>Tribute payment successful!</b>\n\n"
                        f"📅 Added: <b>31 days</b>\n"
                        f"📅 Total: <b>{total} days</b>\n\n"
                        f"🔗 Return to the mini-app — your join link is waiting there.")
            await bot.send_message(telegram_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Cannot notify user {telegram_id}: {e}")
        finally:
            await bot.session.close()

    return web.json_response({"status": "ok"})


async def parse_init_data(request: web.Request) -> dict:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data:
            return None
        return {"init_data": init_data}
    except:
        return None

# Тот самый эндпоинт, который мы починили!
async def api_create_stars_invoice(request: web.Request) -> web.Response:
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    
    try:
        data = await request.json()
        days = data.get("days", 30)
        tier = data.get("tier", "plus")
    except:
        days = 30
        tier = "plus"

    stars = STARS_TIER_PRICES.get(tier, STARS_PRICES.get(days, 500))
    
    # Достаем ID юзера из initData, чтобы положить его в payload
    params = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_info = json.loads(params['user'])
        user_id = user_info.get('id')
    
    if not user_id:
        return web.json_response({"error": "Cannot parse user"}, status=400)

    # Строка, которая вернется тебе в successful_payment
    payload = f"stars_{days}_{user_id}"
    
    bot = Bot(token=BOT_TOKEN)
    try:
        # Генерируем реальную строку инвойса от Telegram
        invoice_link = await bot.create_invoice_link(
            title=f"Подписка на {days} дней",
            description="Оплата премиум доступа к ASMR.LEAKS",
            payload=payload,
            provider_token="", # Для Telegram Звезд это поле ОБЯЗАТЕЛЬНО должно быть пустым
            currency="XTR",
            prices=[LabeledPrice(label=f"{days} Days VIP", amount=stars)]
        )
        return web.json_response({
            "invoice_link": invoice_link,
            "stars": stars,
            "days": days
        })
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
        return web.json_response({"error": str(e)}, status=500)
    finally:
        await bot.session.close()


async def api_check_invite(request: web.Request) -> web.Response:
    """POST /miniapp/check_invite — returns and consumes pending invite link for the user."""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data:
            return web.json_response({"invite_link": None})
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = None
        if 'user' in params:
            user_info = json.loads(params['user'])
            user_id = user_info.get('id')
        if not user_id:
            return web.json_response({"invite_link": None})
        async with async_session() as session:
            link = await consume_pending_invite(session, int(user_id))
        return web.json_response({"invite_link": link})
    except Exception as e:
        logger.error(f"check_invite error: {e}")
        return web.json_response({"invite_link": None})


async def api_get_artists(request: web.Request) -> web.Response:
    async with async_session() as session:
        result = await session.execute(select(Artist).order_by(Artist.name))
        artists = result.scalars().all()
        # Check which artists have profile content
        from sqlalchemy import func
        content_result = await session.execute(
            select(ArtistContent.artist_name, func.count(ArtistContent.id))
            .group_by(ArtistContent.artist_name)
        )
        artists_with_content = {row[0] for row in content_result.all() if row[1] > 0}
        artists_data = [{
            "name": a.name, "photo_url": a.photo_url, "profile_photo_url": a.profile_photo_url,
            "topic_url": getattr(a, 'topic_url', None),
            "has_profile": a.name in artists_with_content,
            "photos": a.photos, "videos": a.videos, "tag_hot": getattr(a, 'tag_hot', False),
            "tag_new": getattr(a, 'tag_new', False), "tag_prom": getattr(a, 'tag_prom', False),
            "tag_ready": getattr(a, 'tag_ready', False)
        } for a in artists]
        return web.json_response({"artists": artists_data, "total": len(artists_data)})

async def api_get_profile(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data: return web.json_response({"error": "No initData"}, status=400)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = None
        if 'user' in params:
            user_id = json.loads(params['user']).get('id')
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=400)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            
            if not user:
                return web.json_response({"days_left": 0, "show_subscribe_button": True, "trial_used": False, "lang": ""})
            
            days_left = user.units or 0
            show_subscribe = True if days_left <= 3 else False
            
            return web.json_response({
                "days_left": days_left, "show_subscribe_button": show_subscribe,
                "trial_used": user.trial_used, "full_name": user.full_name or "",
                "username": user.username or "", "telegram_id": user.telegram_id,
                "is_active": user.is_active, "units": user.units, "lang": user.lang or "",
                "notify_expiry": getattr(user, 'notify_expiry', True),
                "badge": getattr(user, 'badge', None),
                "badges": [b.strip() for b in (getattr(user, 'badge', None) or "").split(",") if b.strip()],
                "tier": getattr(user, 'tier', 'plus') or 'plus',
                "tribute_plus_url": TRIBUTE_PLUS_URL,
                "tribute_pro_url": TRIBUTE_PRO_URL,
            })
    except Exception as e:
        logger.error(f"Error in api_get_profile: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_set_language(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, lang = data.get("initData", ""), data.get("lang", "en")
        if not init_data: return web.json_response({"error": "No initData"}, status=400)
        if lang not in ("en", "ru", "es"): return web.json_response({"error": "Invalid language"}, status=400)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=user_id, lang=lang)
                session.add(user)
            else:
                user.lang = lang
            await session.commit()
            return web.json_response({"ok": True, "lang": lang})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_set_notify_expiry(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, notify = data.get("initData", ""), data.get("notify_expiry", True)
        if not init_data: return web.json_response({"error": "No initData"}, status=400)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=user_id, notify_expiry=bool(notify))
                session.add(user)
            else:
                user.notify_expiry = bool(notify)
            await session.commit()
            return web.json_response({"ok": True, "notify_expiry": bool(notify)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_get_favorites(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data: return web.json_response({"error": "No initData"}, status=403)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0: return web.json_response({"error": "No subscription"}, status=403)
            
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.telegram_id == user_id))
            favorites = result.scalars().all()
            items = [{"id": f.id, "title": f.title, "url": f.url} for f in favorites]
            return web.json_response({"items": items, "count": len(items), "limit": 100})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_add_favorite(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, url, title = data.get("initData", ""), data.get("url", ""), data.get("title", "")
        if not init_data or not url or not title: return web.json_response({"error": "Missing data"}, status=400)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0: return web.json_response({"error": "No subscription"}, status=403)
            
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.telegram_id == user_id))
            if len(result.scalars().all()) >= 100: return web.json_response({"error": "Limit reached"}, status=409)
            
            fav = Favorite(telegram_id=user_id, title=title, url=url)
            session.add(fav)
            await session.commit()
            return web.json_response({"ok": True, "id": fav.id})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_delete_favorite(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, fav_id = data.get("initData", ""), data.get("id", 0)
        if not init_data or not fav_id: return web.json_response({"error": "Missing data"}, status=400)
        
        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.id == fav_id, Favorite.telegram_id == user_id))
            fav = result.scalar_one_or_none()
            if fav:
                await session.delete(fav)
                await session.commit()
                return web.json_response({"ok": True})
            return web.json_response({"error": "Not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_free_trial(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data:
            return web.json_response({"error": "No initData"}, status=400)

        params = dict(urllib.parse.parse_qsl(init_data))
        user_id = json.loads(params['user']).get('id') if 'user' in params else None
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()

            if not user:
                user = User(telegram_id=user_id, units=5, trial_used=True, is_active=True)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            else:
                if user.trial_used:
                    return web.json_response({"error": "Trial already used"}, status=409)
                user.units += 5
                user.trial_used = True
                user.is_active = True
                await session.commit()

            # Уведомление админу
            nick = f"@{user.username}" if getattr(user, 'username', None) else f"id{user_id}"
            await _notify_admins(
                f"🎁 <b>Новый триал (Mini App)</b>\n"
                f"👤 {nick} | <code>{user_id}</code>\n"
                f"📅 Активировал 3 бесплатных дня"
            )

            return web.json_response({"ok": True, "days_left": user.units})
    except Exception as e:
        logger.error(f"Error in api_free_trial: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_get_videos(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get('limit', 500))
        async with async_session() as session:
            result = await session.execute(
                select(ArtistContent)
                .where(ArtistContent.content_type == "video")
                .order_by(ArtistContent.created_at.desc())
                .limit(limit)
            )
            videos = result.scalars().all()
            videos_data = [{
                "id": v.id, "title": v.title or "", "url": v.url,
                "embed_url": v.url, "thumbnail_url": v.thumbnail_url or _auto_thumbnail(v.url) or "",
                "artist_name": v.artist_name, "tags": v.tags or "",
                "created_at": v.created_at.isoformat() if v.created_at else ""
            } for v in videos]
            return web.json_response({"videos": videos_data, "total": len(videos_data)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_get_custom_badges(request: web.Request) -> web.Response:
    try:
        async with async_session() as session:
            badges = await get_custom_badges(session)
            return web.json_response({"badges": [{"name": b.name, "color": b.color} for b in badges]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_get_artist_content(request: web.Request) -> web.Response:
    """GET /miniapp/artist_content?name=ArtistName"""
    artist_name = request.rel_url.query.get("name", "").strip()
    if not artist_name:
        return web.json_response({"error": "name required"}, status=400)
    async with async_session() as session:
        videos = await get_artist_content(session, artist_name, "video")
        photos = await get_artist_content(session, artist_name, "photo")
        shorts = await get_artist_content(session, artist_name, "short")
    return web.json_response({
        "artist": artist_name,
        "videos": [
            {"id": v.id, "title": v.title or "", "url": v.url,
             "thumbnail_url": v.thumbnail_url or _auto_thumbnail(v.url) or "",
             "tags": v.tags or "", "sort_order": v.sort_order}
            for v in videos
        ],
        "photos": [
            {"id": p.id, "url": p.url, "sort_order": p.sort_order}
            for p in photos
        ],
        "shorts": [
            {"id": s.id, "title": s.title or "", "url": s.url,
             "thumbnail_url": s.thumbnail_url or _auto_thumbnail(s.url) or "",
             "tags": s.tags or "", "sort_order": s.sort_order}
            for s in shorts
        ],
    })


async def api_crypto_checkout(request: web.Request) -> web.Response:
    """POST /miniapp/crypto/checkout — create a crypto payment order"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data = await request.json()
        tier     = data.get("tier", "plus")
        currency = data.get("currency", "USDT_TRC20")  # USDT_TRC20 | USDT_TON | ETH
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)

    params = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"error": "Cannot parse user"}, status=400)

    wallets = {
        "USDT_TRC20": (CRYPTO_WALLET_USDT_TRC20, "TRC20",    "USDT"),
        "USDT_TON":   (CRYPTO_WALLET_USDT_TON,   "TON",      "USDT"),
        "ETH":        (CRYPTO_WALLET_ETH,         "Ethereum", "ETH"),
    }
    if currency not in wallets or not wallets[currency][0]:
        return web.json_response({"error": "Currency not configured"}, status=400)

    wallet_addr, network, symbol = wallets[currency]
    amount = CRYPTO_USDT_PRICES.get(tier, 6)

    import uuid as _uuid
    from datetime import timedelta, datetime as _dt
    order_uuid = f"crypto_{_uuid.uuid4().hex[:16]}"
    expires_at = _dt.now(__import__('datetime').timezone.utc).replace(tzinfo=None) + timedelta(hours=24)

    async with async_session() as session:
        await session.execute(
            __import__('sqlalchemy').text(
                "INSERT INTO crypto_checkouts (order_uuid, telegram_id, tier, crypto_amount, crypto_currency, network, wallet_address, expires_at) "
                "VALUES (:uuid, :tid, :tier, :amount, :currency, :network, :wallet, :exp)"
            ),
            {"uuid": order_uuid, "tid": user_id, "tier": tier,
             "amount": amount, "currency": symbol, "network": network,
             "wallet": wallet_addr, "exp": expires_at}
        )
        await session.commit()

    for admin_id in ADMIN_IDS:
        try:
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(admin_id,
                f"💎 <b>Новый крипто-заказ</b>\n"
                f"👤 <code>{user_id}</code> | Тир: <b>{tier.upper()}</b>\n"
                f"💰 {amount} {symbol} ({network})\n"
                f"🔑 Заказ: <code>{order_uuid}</code>\n"
                f"Подтвердить: /confirm_crypto {order_uuid}",
                parse_mode="HTML")
        except Exception:
            pass

    return web.json_response({
        "order_uuid": order_uuid,
        "wallet_address": wallet_addr,
        "network": network,
        "symbol": symbol,
        "amount": amount,
        "expires_at": expires_at.isoformat(),
    })


async def api_crypto_claim(request: web.Request) -> web.Response:
    """POST /miniapp/crypto/claim — user submits tx hash"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data = await request.json()
        order_uuid = data.get("order_uuid", "")
        tx_hash    = data.get("tx_hash", "").strip()
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)

    if not order_uuid or not tx_hash:
        return web.json_response({"error": "order_uuid and tx_hash required"}, status=400)

    from sqlalchemy import text as _text
    async with async_session() as session:
        result = await session.execute(
            _text("SELECT * FROM crypto_checkouts WHERE order_uuid = :uuid"),
            {"uuid": order_uuid}
        )
        row = result.mappings().one_or_none()
        if not row:
            return web.json_response({"error": "Order not found"}, status=404)
        if row["status"] not in ("pending",):
            return web.json_response({"error": "Order already processed"}, status=409)
        await session.execute(
            _text("UPDATE crypto_checkouts SET tx_hash = :tx, status = 'claimed' WHERE order_uuid = :uuid"),
            {"tx": tx_hash, "uuid": order_uuid}
        )
        await session.commit()

    for admin_id in ADMIN_IDS:
        try:
            bot = Bot(token=BOT_TOKEN)
            await bot.send_message(admin_id,
                f"💎 <b>Крипто TX получен!</b>\n"
                f"👤 <code>{row['telegram_id']}</code> | <b>{row['tier'].upper()}</b>\n"
                f"💰 {row['crypto_amount']} {row['crypto_currency']} ({row['network']})\n"
                f"🔗 TX: <code>{tx_hash}</code>\n"
                f"✅ Подтвердить: /confirm_crypto {order_uuid}",
                parse_mode="HTML")
        except Exception:
            pass

    return web.json_response({"status": "claimed"})


async def api_get_shorts(request: web.Request) -> web.Response:
    """GET /miniapp/shorts?limit=20 — latest shorts for home page scroll"""
    try:
        limit = int(request.query.get('limit', 20))
        async with async_session() as session:
            result = await session.execute(
                select(ArtistContent)
                .where(ArtistContent.content_type == "short")
                .order_by(__import__('sqlalchemy').func.random())
                .limit(limit)
            )
            shorts = result.scalars().all()
        return web.json_response({"shorts": [
            {"id": s.id, "title": s.title or "", "url": s.url,
             "thumbnail_url": s.thumbnail_url or _auto_thumbnail(s.url) or "",
             "artist_name": s.artist_name, "tags": s.tags or "",
             "created_at": s.created_at.isoformat() if s.created_at else ""}
            for s in shorts
        ]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_watch_progress(request: web.Request) -> web.Response:
    """POST /miniapp/watch_progress — save/update watch position"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data      = await request.json()
        content_id = int(data.get("content_id", 0))
        progress   = int(data.get("progress_seconds", 0))
        duration   = int(data.get("duration_seconds", 0))
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)

    params = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id or not content_id:
        return web.json_response({"ok": False})

    from sqlalchemy import text as _t
    async with async_session() as session:
        await session.execute(_t("""
            INSERT INTO watch_history (telegram_id, content_id, progress_seconds, duration_seconds, updated_at)
            VALUES (:uid, :cid, :prog, :dur, NOW())
            ON CONFLICT (telegram_id, content_id)
            DO UPDATE SET progress_seconds=:prog, duration_seconds=:dur, updated_at=NOW()
        """), {"uid": user_id, "cid": content_id, "prog": progress, "dur": duration})
        await session.commit()
    return web.json_response({"ok": True})


async def api_continue_watching(request: web.Request) -> web.Response:
    """POST /miniapp/continue_watching — get user's in-progress videos"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)

    params = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"items": []})

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(_t("""
            SELECT wh.content_id, wh.progress_seconds, wh.duration_seconds,
                   ac.title, ac.url, ac.thumbnail_url, ac.artist_name, ac.tags
            FROM watch_history wh
            JOIN artist_content ac ON ac.id = wh.content_id
            WHERE wh.telegram_id = :uid
              AND wh.progress_seconds > 0
              AND wh.duration_seconds > 30
              AND CAST(wh.progress_seconds AS FLOAT) / NULLIF(wh.duration_seconds,0) BETWEEN 0.03 AND 0.92
            ORDER BY wh.updated_at DESC
            LIMIT 10
        """), {"uid": user_id})
        rows = result.mappings().all()

    return web.json_response({"items": [
        {"id": r["content_id"], "title": r["title"] or "",
         "url": r["url"], "artist_name": r["artist_name"] or "",
         "thumbnail_url": r["thumbnail_url"] or _auto_thumbnail(r["url"]) or "",
         "tags": r["tags"] or "",
         "progress": r["progress_seconds"], "duration": r["duration_seconds"]}
        for r in rows
    ]})


async def api_search(request: web.Request) -> web.Response:
    """GET /miniapp/search?q=TEXT&limit=20"""
    q     = request.query.get("q", "").strip()
    limit = min(int(request.query.get("limit", 20)), 50)
    if len(q) < 2:
        return web.json_response({"results": []})

    from sqlalchemy import or_
    async with async_session() as session:
        result = await session.execute(
            select(ArtistContent)
            .where(ArtistContent.content_type == "video")
            .where(or_(
                ArtistContent.title.ilike(f"%{q}%"),
                ArtistContent.artist_name.ilike(f"%{q}%"),
                ArtistContent.tags.ilike(f"%{q}%"),
            ))
            .order_by(ArtistContent.created_at.desc())
            .limit(limit)
        )
        items = result.scalars().all()

    return web.json_response({"results": [
        {"id": v.id, "title": v.title or "", "url": v.url,
         "thumbnail_url": v.thumbnail_url or _auto_thumbnail(v.url) or "",
         "artist_name": v.artist_name or "", "tags": v.tags or "",
         "created_at": v.created_at.isoformat() if v.created_at else ""}
        for v in items
    ]})


async def api_toggle_follow(request: web.Request) -> web.Response:
    """POST /miniapp/follow — toggle artist follow, returns {following: bool, count: int}"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data        = await request.json()
        artist_name = data.get("artist_name", "").strip()
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)
    if not artist_name:
        return web.json_response({"error": "artist_name required"}, status=400)

    params  = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"error": "Cannot parse user"}, status=400)

    from sqlalchemy import text as _t
    async with async_session() as session:
        existing = await session.execute(
            _t("SELECT id FROM artist_follows WHERE telegram_id=:uid AND artist_name=:name"),
            {"uid": user_id, "name": artist_name}
        )
        row = existing.scalar_one_or_none()
        if row:
            await session.execute(
                _t("DELETE FROM artist_follows WHERE telegram_id=:uid AND artist_name=:name"),
                {"uid": user_id, "name": artist_name}
            )
            following = False
        else:
            await session.execute(
                _t("INSERT INTO artist_follows (telegram_id, artist_name) VALUES (:uid, :name) ON CONFLICT DO NOTHING"),
                {"uid": user_id, "name": artist_name}
            )
            following = True
        await session.commit()
        count_row = await session.execute(
            _t("SELECT COUNT(*) FROM artist_follows WHERE telegram_id=:uid"), {"uid": user_id}
        )
        count = count_row.scalar() or 0
    return web.json_response({"following": following, "count": int(count)})


async def api_get_follows(request: web.Request) -> web.Response:
    """POST /miniapp/follows — list of followed artist names + their recent video"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    params  = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"artists": []})

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(
            _t("SELECT af.artist_name, a.photo_url, a.profile_photo_url FROM artist_follows af "
               "LEFT JOIN artists a ON a.name = af.artist_name "
               "WHERE af.telegram_id=:uid ORDER BY af.created_at DESC"),
            {"uid": user_id}
        )
        rows = result.mappings().all()
    return web.json_response({"artists": [
        {"name": r["artist_name"], "photo_url": r["photo_url"] or "", "profile_photo_url": r["profile_photo_url"] or ""}
        for r in rows
    ]})


async def api_followed_feed(request: web.Request) -> web.Response:
    """POST /miniapp/followed_feed — recent videos from followed artists"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    params  = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"videos": []})

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(_t("""
            SELECT ac.id, ac.title, ac.url, ac.thumbnail_url, ac.artist_name, ac.tags, ac.created_at
            FROM artist_content ac
            WHERE ac.content_type = 'video'
              AND ac.artist_name IN (
                SELECT artist_name FROM artist_follows WHERE telegram_id = :uid
              )
            ORDER BY ac.created_at DESC
            LIMIT 20
        """), {"uid": user_id})
        rows = result.mappings().all()
    return web.json_response({"videos": [
        {"id": r["id"], "title": r["title"] or "", "url": r["url"],
         "thumbnail_url": r["thumbnail_url"] or _auto_thumbnail(r["url"]) or "",
         "artist_name": r["artist_name"] or "", "tags": r["tags"] or "",
         "created_at": r["created_at"].isoformat() if r["created_at"] else ""}
        for r in rows
    ]})


async def api_user_stats(request: web.Request) -> web.Response:
    """POST /miniapp/user_stats — user activity stats"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    params  = dict(urllib.parse.parse_qsl(user_data["init_data"]))
    user_id = None
    if 'user' in params:
        user_id = json.loads(params['user']).get('id')
    if not user_id:
        return web.json_response({"stats": {}})

    from sqlalchemy import text as _t
    async with async_session() as session:
        follows   = (await session.execute(_t("SELECT COUNT(*) FROM artist_follows WHERE telegram_id=:uid"),   {"uid": user_id})).scalar() or 0
        favorites = (await session.execute(_t("SELECT COUNT(*) FROM favorites WHERE telegram_id=:uid"),         {"uid": user_id})).scalar() or 0
        reactions = (await session.execute(_t("SELECT COUNT(*) FROM video_reactions WHERE telegram_id=:uid"),   {"uid": user_id})).scalar() or 0
        comments  = (await session.execute(_t("SELECT COUNT(*) FROM video_comments WHERE telegram_id=:uid"),    {"uid": user_id})).scalar() or 0
        user_row  = (await session.execute(_t("SELECT created_at FROM users WHERE telegram_id=:uid"),           {"uid": user_id})).mappings().one_or_none()
        # Top artist from follows
        top_artist_row = (await session.execute(_t(
            "SELECT artist_name FROM artist_follows WHERE telegram_id=:uid ORDER BY created_at ASC LIMIT 1"
        ), {"uid": user_id})).scalar_one_or_none()
    member_since = user_row["created_at"].strftime("%b %Y") if user_row and user_row["created_at"] else ""
    return web.json_response({"stats": {
        "follows": int(follows), "favorites": int(favorites),
        "reactions": int(reactions), "comments": int(comments),
        "top_artist": top_artist_row or "",
        "member_since": member_since,
    }})


async def api_get_tags(request: web.Request) -> web.Response:
    """GET /miniapp/tags — все теги с цветами"""
    async with async_session() as session:
        tags = await get_all_tags(session)
        return web.json_response({
            "tags": [{"name": t.name, "color": t.color} for t in tags]
        })


def _parse_user_id(init_data: str) -> int | None:
    params = dict(urllib.parse.parse_qsl(init_data))
    try:
        return json.loads(params['user']).get('id') if 'user' in params else None
    except Exception:
        return None


async def api_get_video_reactions(request: web.Request) -> web.Response:
    """GET /miniapp/video/{id}/reactions?initData=..."""
    content_id = int(request.match_info.get("id", 0))
    init_data = request.rel_url.query.get("initData", "")
    user_id = _parse_user_id(init_data) if init_data else None

    async with async_session() as session:
        counts = await get_reactions(session, content_id)
        user_reactions = []
        if user_id:
            user_reactions = await get_user_reactions(session, content_id, user_id)
        return web.json_response({
            "content_id": content_id,
            "counts": counts,
            "user_reactions": user_reactions,   # list of emojis
            "allowed": ALLOWED_REACTIONS,
            "max_per_user": 3,
        })


async def api_post_reaction(request: web.Request) -> web.Response:
    """POST /miniapp/video/{id}/react  body: {initData, emoji}"""
    content_id = int(request.match_info.get("id", 0))
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        emoji = data.get("emoji", "")
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if emoji not in ALLOWED_REACTIONS:
            return web.json_response({"error": "Invalid emoji"}, status=400)

        async with async_session() as session:
            # verify subscription
            from sqlalchemy import select as sa_select
            result = await session.execute(sa_select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)
            counts = await set_reaction(session, content_id, user_id, emoji)
            user_reactions = await get_user_reactions(session, content_id, user_id)
            return web.json_response({"counts": counts, "user_reactions": user_reactions})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_get_comments(request: web.Request) -> web.Response:
    """GET /miniapp/video/{id}/comments"""
    content_id = int(request.match_info.get("id", 0))
    async with async_session() as session:
        comments = await get_comments(session, content_id)

        # Batch-load badges for all commenters
        tg_ids = list({c.telegram_id for c in comments})
        badge_map: dict[int, list[str]] = {}
        if tg_ids:
            result = await session.execute(
                select(User.telegram_id, User.badge).where(User.telegram_id.in_(tg_ids))
            )
            for tid, raw in result.all():
                badge_map[tid] = [b.strip() for b in (raw or "").split(",") if b.strip()]

        return web.json_response({
            "comments": [
                {
                    "id": c.id,
                    "username": c.username or "Anonymous",
                    "photo_url": c.photo_url or "",
                    "text": c.text,
                    "created_at": c.created_at.strftime("%d.%m %H:%M"),
                    "badges": badge_map.get(c.telegram_id, [])[:3],
                }
                for c in reversed(comments)
            ]
        })


async def api_post_comment(request: web.Request) -> web.Response:
    """POST /miniapp/video/{id}/comment  body: {initData, text}"""
    content_id = int(request.match_info.get("id", 0))
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        text = data.get("text", "").strip()
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if not text:
            return web.json_response({"error": "Empty comment"}, status=400)

        # Extract photo_url from initData user object
        photo_url = None
        try:
            params = dict(urllib.parse.parse_qsl(init_data))
            user_obj = json.loads(params.get('user', '{}'))
            photo_url = user_obj.get('photo_url') or None
        except Exception:
            pass

        async with async_session() as session:
            from sqlalchemy import select as sa_select
            result = await session.execute(sa_select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)
            comment = await add_comment(session, content_id, user_id, user.username, text, photo_url)
            return web.json_response({
                "ok": True,
                "comment": {
                    "id": comment.id,
                    "username": comment.username or "Anonymous",
                    "photo_url": comment.photo_url or "",
                    "text": comment.text,
                    "created_at": comment.created_at.strftime("%d.%m %H:%M"),
                }
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_suggest_artist(request: web.Request) -> web.Response:
    """POST /miniapp/suggest_artist {initData, artist_name}"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        artist_name = data.get("artist_name", "").strip()
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if not artist_name or len(artist_name) > 256:
            return web.json_response({"error": "Invalid artist name"}, status=400)

        # Get username
        params = dict(urllib.parse.parse_qsl(init_data))
        username = None
        try:
            username = json.loads(params.get('user', '{}')).get('username')
        except Exception:
            pass

        async with async_session() as session:
            sug = ArtistSuggestion(telegram_id=user_id, username=username, artist_name=artist_name)
            session.add(sug)
            await session.commit()

        # Notify admins via bot
        nick = f"@{username}" if username else f"id{user_id}"
        await _notify_admins(
            f"💡 <b>Предложение артиста</b>\n"
            f"👤 {nick} | <code>{user_id}</code>\n"
            f"🎤 <b>{artist_name}</b>"
        )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_toggle_favorite(request: web.Request) -> web.Response:
    """POST /miniapp/favorites/toggle {initData, content_id, title, url}"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        content_id = data.get("content_id")
        user_id = _parse_user_id(init_data)
        if not user_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)

            # Check if already favorited
            result = await session.execute(
                select(Favorite).where(Favorite.telegram_id == user_id, Favorite.content_id == content_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                await session.delete(existing)
                await session.commit()
                return web.json_response({"ok": True, "favorited": False})
            else:
                # Check limit
                from sqlalchemy import func as sa_func
                count_result = await session.execute(
                    select(sa_func.count(Favorite.id)).where(Favorite.telegram_id == user_id)
                )
                if (count_result.scalar() or 0) >= 200:
                    return web.json_response({"error": "Limit reached"}, status=409)
                fav = Favorite(
                    telegram_id=user_id,
                    content_id=content_id,
                    title=data.get("title", "Video")[:128],
                    url=data.get("url", ""),
                )
                session.add(fav)
                await session.commit()
                return web.json_response({"ok": True, "favorited": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_check_favorite(request: web.Request) -> web.Response:
    """GET /miniapp/favorites/check?initData=...&content_id=..."""
    init_data = request.rel_url.query.get("initData", "")
    content_id = request.rel_url.query.get("content_id", "")
    user_id = _parse_user_id(init_data) if init_data else None
    if not user_id or not content_id:
        return web.json_response({"favorited": False})
    async with async_session() as session:
        result = await session.execute(
            select(Favorite).where(Favorite.telegram_id == user_id, Favorite.content_id == int(content_id))
        )
        return web.json_response({"favorited": result.scalar_one_or_none() is not None})


async def api_get_favorites_v2(request: web.Request) -> web.Response:
    """POST /miniapp/favorites  — returns favorites with content details"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)

            result = await session.execute(
                select(Favorite).where(Favorite.telegram_id == user_id).order_by(Favorite.created_at.desc())
            )
            favorites = result.scalars().all()

            # Enrich with content details
            items = []
            content_ids = [f.content_id for f in favorites if f.content_id]
            content_map = {}
            if content_ids:
                cr = await session.execute(
                    select(ArtistContent).where(ArtistContent.id.in_(content_ids))
                )
                for c in cr.scalars().all():
                    content_map[c.id] = c

            for f in favorites:
                item = {"id": f.id, "title": f.title, "url": f.url, "content_id": f.content_id}
                if f.content_id and f.content_id in content_map:
                    c = content_map[f.content_id]
                    item.update({
                        "artist_name": c.artist_name,
                        "thumbnail_url": c.thumbnail_url or "",
                        "tags": c.tags or "",
                    })
                items.append(item)
            return web.json_response({"items": items, "count": len(items), "limit": 200})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Playlists API ────────────────────────────────────────────────────────────

async def api_get_playlists(request: web.Request) -> web.Response:
    """POST /miniapp/playlists {initData}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.telegram_id == user_id).order_by(Playlist.created_at.desc())
            )
            playlists = result.scalars().all()
            # Count items per playlist
            items_out = []
            for pl in playlists:
                from sqlalchemy import func as sa_func
                count_r = await session.execute(
                    select(sa_func.count(PlaylistItem.id)).where(PlaylistItem.playlist_id == pl.id)
                )
                items_out.append({
                    "id": pl.id, "name": pl.name,
                    "item_count": count_r.scalar() or 0,
                    "created_at": pl.created_at.strftime("%d.%m.%Y"),
                })
            return web.json_response({"playlists": items_out})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_create_playlist(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/create {initData, name}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        name = data.get("name", "").strip()
        if not user_id or not name:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            # Limit 20 playlists
            from sqlalchemy import func as sa_func
            count_r = await session.execute(
                select(sa_func.count(Playlist.id)).where(Playlist.telegram_id == user_id)
            )
            if (count_r.scalar() or 0) >= 20:
                return web.json_response({"error": "Max 20 playlists"}, status=409)
            pl = Playlist(telegram_id=user_id, name=name[:128])
            session.add(pl)
            await session.commit()
            await session.refresh(pl)
            return web.json_response({"ok": True, "id": pl.id, "name": pl.name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_delete_playlist(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/delete {initData, playlist_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        if not user_id or not playlist_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            pl = result.scalar_one_or_none()
            if not pl:
                return web.json_response({"error": "Not found"}, status=404)
            from sqlalchemy import delete as sa_delete
            await session.execute(sa_delete(PlaylistItem).where(PlaylistItem.playlist_id == pl.id))
            await session.delete(pl)
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_add_item(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/add_item {initData, playlist_id, content_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        content_id = data.get("content_id")
        if not user_id or not playlist_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            if not result.scalar_one_or_none():
                return web.json_response({"error": "Not found"}, status=404)
            # Check duplicate
            result = await session.execute(
                select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.content_id == content_id)
            )
            if result.scalar_one_or_none():
                return web.json_response({"error": "Already in playlist"}, status=409)
            item = PlaylistItem(playlist_id=playlist_id, content_id=content_id)
            session.add(item)
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_remove_item(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/remove_item {initData, playlist_id, content_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        content_id = data.get("content_id")
        if not user_id or not playlist_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            if not result.scalar_one_or_none():
                return web.json_response({"error": "Not found"}, status=404)
            from sqlalchemy import delete as sa_delete
            await session.execute(
                sa_delete(PlaylistItem).where(
                    PlaylistItem.playlist_id == playlist_id,
                    PlaylistItem.content_id == content_id,
                )
            )
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_items(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/items {initData, playlist_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        if not user_id or not playlist_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            pl = result.scalar_one_or_none()
            if not pl:
                return web.json_response({"error": "Not found"}, status=404)
            result = await session.execute(
                select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.created_at.desc())
            )
            p_items = result.scalars().all()
            content_ids = [i.content_id for i in p_items]
            content_map = {}
            if content_ids:
                cr = await session.execute(select(ArtistContent).where(ArtistContent.id.in_(content_ids)))
                for c in cr.scalars().all():
                    content_map[c.id] = c
            items_out = []
            for pi in p_items:
                c = content_map.get(pi.content_id)
                if c:
                    items_out.append({
                        "id": c.id, "title": c.title or "", "url": c.url,
                        "artist_name": c.artist_name, "tags": c.tags or "",
                        "thumbnail_url": c.thumbnail_url or "",
                    })
            return web.json_response({"playlist": {"id": pl.id, "name": pl.name}, "items": items_out})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def create_app() -> web.Application:
    app = web.Application()
    app.middlewares.append(cors_middleware)
    app.router.add_post("/tribute-webhook", tribute_webhook)
    app.router.add_post("/miniapp/create_stars_invoice", api_create_stars_invoice)
    app.router.add_post("/miniapp/check_invite", api_check_invite)
    app.router.add_get("/miniapp/artists", api_get_artists)
    app.router.add_get("/miniapp/videos", api_get_videos)
    app.router.add_get("/miniapp/custom_badges", api_get_custom_badges)
    app.router.add_post("/miniapp/profile", api_get_profile)
    app.router.add_post("/miniapp/set_language", api_set_language)
    app.router.add_post("/miniapp/set_notify_expiry", api_set_notify_expiry)
    app.router.add_post("/miniapp/favorites", api_get_favorites_v2)
    app.router.add_post("/miniapp/favorites/add", api_add_favorite)
    app.router.add_post("/miniapp/favorites/delete", api_delete_favorite)
    app.router.add_post("/miniapp/favorites/toggle", api_toggle_favorite)
    app.router.add_get("/miniapp/favorites/check", api_check_favorite)
    app.router.add_post("/miniapp/free_trial", api_free_trial)
    app.router.add_post("/miniapp/suggest_artist", api_suggest_artist)
    app.router.add_post("/miniapp/playlists", api_get_playlists)
    app.router.add_post("/miniapp/playlists/create", api_create_playlist)
    app.router.add_post("/miniapp/playlists/delete", api_delete_playlist)
    app.router.add_post("/miniapp/playlists/add_item", api_playlist_add_item)
    app.router.add_post("/miniapp/playlists/remove_item", api_playlist_remove_item)
    app.router.add_post("/miniapp/playlists/items", api_playlist_items)
    app.router.add_get("/miniapp/artist_content", api_get_artist_content)
    app.router.add_post("/miniapp/crypto/checkout", api_crypto_checkout)
    app.router.add_post("/miniapp/crypto/claim", api_crypto_claim)
    app.router.add_post("/miniapp/watch_progress", api_watch_progress)
    app.router.add_post("/miniapp/continue_watching", api_continue_watching)
    app.router.add_get("/miniapp/search", api_search)
    app.router.add_post("/miniapp/follow", api_toggle_follow)
    app.router.add_post("/miniapp/follows", api_get_follows)
    app.router.add_post("/miniapp/followed_feed", api_followed_feed)
    app.router.add_post("/miniapp/user_stats", api_user_stats)
    app.router.add_get("/miniapp/shorts", api_get_shorts)
    app.router.add_get("/miniapp/tags", api_get_tags)
    app.router.add_get("/miniapp/video/{id}/reactions", api_get_video_reactions)
    app.router.add_post("/miniapp/video/{id}/react", api_post_reaction)
    app.router.add_get("/miniapp/video/{id}/comments", api_get_comments)
    app.router.add_post("/miniapp/video/{id}/comment", api_post_comment)
    return app
