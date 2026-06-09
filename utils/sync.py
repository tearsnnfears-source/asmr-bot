import logging
from aiohttp import ClientSession, ClientTimeout

from config import ELITE_SYNC_DAYS, PEER_GRANT_SECRET, PEER_GRANT_URL, PROJECT_KEY

logger = logging.getLogger(__name__)


async def send_peer_elite_grant(
    *,
    telegram_id: int,
    order_uuid: str,
    username: str | None = None,
    full_name: str | None = None,
    days: int | None = None,
    tier: str = "elite",
) -> bool:
    """Notify the paired bot that this user bought ELITE here."""
    if tier != "elite":
        return False
    if not PEER_GRANT_URL or not PEER_GRANT_SECRET:
        logger.info("Peer ELITE sync skipped: PEER_GRANT_URL or PEER_GRANT_SECRET is empty")
        return False

    payload = {
        "source_project": PROJECT_KEY,
        "order_uuid": order_uuid,
        "telegram_id": int(telegram_id),
        "days": int(days or ELITE_SYNC_DAYS),
        "tier": "elite",
        "username": username or "",
        "full_name": full_name or "",
    }
    headers = {
        "Authorization": f"Bearer {PEER_GRANT_SECRET}",
        "Content-Type": "application/json",
    }

    try:
        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(PEER_GRANT_URL, json=payload, headers=headers) as response:
                text = await response.text()
                if response.status >= 400:
                    logger.warning(
                        "Peer ELITE sync failed: status=%s body=%s",
                        response.status,
                        text[:300],
                    )
                    return False
                logger.info("Peer ELITE sync accepted for user %s order %s", telegram_id, order_uuid)
                return True
    except Exception as exc:
        logger.warning("Peer ELITE sync error for user %s: %s", telegram_id, exc)
        return False
