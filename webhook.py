import hashlib
import hmac
import logging
import json
import urllib.parse
from aiohttp import web
from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy import select

from database import async_session, User, PendingPayment, Artist, get_all_artists, Video, get_all_videos
from config import TRIBUTE_API_KEY, BOT_TOKEN, INVITE_LINK, STARS_PRICES, RUB_PRICES
from utils.yoomoney import make_payment_url, generate_label

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

            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=payload.get("telegram_username"),
                    units=30,
                    is_active=True,
                    last_payment_method="tribute",
                )
                session.add(user)
            else:
                user.units += 30
                user.is_active = True
                user.last_payment_method = "tribute"

            await session.commit()
            total = user.units
            lang = user.lang or "en"

        # Уведомление админу о Tribute оплате
        username = payload.get("telegram_username", "")
        nick = f"@{username}" if username else f"id{telegram_id}"
        await _notify_admins(
            f"💳 <b>Новая оплата — Tribute</b>\n"
            f"👤 {nick} | <code>{telegram_id}</code>\n"
            f"📅 +30 дней | Итого: {total} дн."
        )

        bot = Bot(token=BOT_TOKEN)
        try:
            if lang == "ru":
                text = (f"✅ <b>Оплата через Tribute прошла успешно!</b>\n\n"
                        f"📅 Добавлено: <b>30 дней</b>\n"
                        f"📅 Итого: <b>{total} дней</b>\n\n"
                        f"🔗 Ссылка для входа:\n{INVITE_LINK}")
            else:
                text = (f"✅ <b>Tribute payment successful!</b>\n\n"
                        f"📅 Added: <b>30 days</b>\n"
                        f"📅 Total: <b>{total} days</b>\n\n"
                        f"🔗 Join the group:\n{INVITE_LINK}")
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
    except:
        days = 30
    
    stars = STARS_PRICES.get(days, 400)
    
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


async def api_create_yoomoney_payment(request: web.Request) -> web.Response:
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data = await request.json()
        days = data.get("days", 30)
    except:
        days = 30
    
    amount = RUB_PRICES.get(days, 499)
    label = generate_label()
    pay_url = make_payment_url(amount, label, f"asmrleaks.tv {days} дней")
    
    return web.json_response({
        "payment_url": pay_url,
        "amount": amount,
        "days": days,
        "label": label
    })

async def api_get_artists(request: web.Request) -> web.Response:
    async with async_session() as session:
        result = await session.execute(select(Artist).order_by(Artist.name))
        artists = result.scalars().all()
        artists_data = [{
            "name": a.name, "photo_url": a.photo_url, "profile_photo_url": a.profile_photo_url,
            "topic_url": getattr(a, 'topic_url', None),
            "photos": a.photos, "videos": a.videos, "tag_hot": getattr(a, 'tag_hot', False),
            "tag_new": getattr(a, 'tag_new', False), "tag_prom": getattr(a, 'tag_prom', False)
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
                "notify_expiry": getattr(user, 'notify_expiry', True)
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
                user = User(telegram_id=user_id, units=3, trial_used=True, is_active=True)
                session.add(user)
                await session.commit()
                await session.refresh(user)
            else:
                if user.trial_used:
                    return web.json_response({"error": "Trial already used"}, status=409)
                user.units += 3
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
        limit = int(request.query.get('limit', 20))
        async with async_session() as session:
            videos = await get_all_videos(session, limit)
            videos_data = [{"id": v.id, "title": v.title, "url": v.url, "embed_url": v.embed_url, "thumbnail_url": v.thumbnail_url, "artist_name": v.artist_name, "duration": v.duration} for v in videos]
            return web.json_response({"videos": videos_data, "total": len(videos_data)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

def create_app() -> web.Application:
    app = web.Application()
    app.middlewares.append(cors_middleware)
    app.router.add_post("/tribute-webhook", tribute_webhook)
    app.router.add_post("/miniapp/create_stars_invoice", api_create_stars_invoice)
    app.router.add_post("/miniapp/create_yoomoney_payment", api_create_yoomoney_payment)
    app.router.add_get("/miniapp/artists", api_get_artists)
    app.router.add_get("/miniapp/videos", api_get_videos)
    app.router.add_post("/miniapp/profile", api_get_profile)
    app.router.add_post("/miniapp/set_language", api_set_language)
    app.router.add_post("/miniapp/set_notify_expiry", api_set_notify_expiry)
    app.router.add_post("/miniapp/favorites", api_get_favorites)
    app.router.add_post("/miniapp/favorites/add", api_add_favorite)
    app.router.add_post("/miniapp/favorites/delete", api_delete_favorite)
    app.router.add_post("/miniapp/free_trial", api_free_trial)
    return app