import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
GROUP_ID        = int(os.getenv("GROUP_ID", "0"))
INVITE_LINK     = os.getenv("INVITE_LINK", "")
ADMIN_IDS       = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
DATABASE_URL    = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./asmr.db")
WEBHOOK_HOST    = os.getenv("WEBHOOK_HOST", "")

# Tribute (SBP / Card)
TRIBUTE_BOT_URL     = os.getenv("TRIBUTE_BOT_URL", "")
TRIBUTE_API_KEY     = os.getenv("TRIBUTE_API_KEY", "")
TRIBUTE_DONATE_LINK = os.getenv("TRIBUTE_DONATE_LINK", "")
TRIBUTE_SITE_WEBHOOK_URL = os.getenv(
    "TRIBUTE_SITE_WEBHOOK_URL",
    "https://play.asmrleaks.tv/api/payments/tribute/webhook",
)

# Tribute per-tier links  (startapp code → tier)
TRIBUTE_PLUS_URL    = os.getenv("TRIBUTE_PLUS_URL",  "https://t.me/tribute/app?startapp=sQSn")
TRIBUTE_PRO_URL     = os.getenv("TRIBUTE_PRO_URL",   "https://t.me/tribute/app?startapp=sT5D")
TRIBUTE_ELITE_URL   = os.getenv("TRIBUTE_ELITE_URL", "https://t.me/tribute/app?startapp=sT5E")
TRIBUTE_KING_URL    = os.getenv("TRIBUTE_KING_URL",  "https://t.me/tribute/app?startapp=s10Vq")
TRIBUTE_ELITE_MIN_EUR = float(os.getenv("TRIBUTE_ELITE_MIN_EUR", "9") or "9")

# startapp code → tier (used in webhook to auto-assign tier)
TRIBUTE_TIER_MAP = {
    "sQSn": "plus",
    "sT5D": "pro",
    "sT5E": "elite",
    "s10Vq": "king",
}

# Telegram Stars — prices in stars
STARS_PRICES = {
    30: 500,
}

# Stars price per tier
STARS_TIER_PRICES = {
    'plus':  500,
    'pro':   650,
    'elite': 800,
    'king':  1050,
    'free':  500,
}

# Bot links
MINIAPP_URL   = os.getenv("MINIAPP_URL", "https://t.me/asmrleaksbot/app")
FREE_PAGES    = os.getenv("FREE_PAGES",  "")   # comma-separated: Name|URL,Name|URL
SUPPORT_URL   = os.getenv("SUPPORT_URL", "https://t.me/sonnnnnua")

# Ночной режим (МСК)
NIGHT_START = 23
NIGHT_END   = 8

# Crypto payment wallets (legacy manual flow — kept only for backward-compat env)
CRYPTO_WALLET_USDT_TRC20 = os.getenv("CRYPTO_WALLET_USDT_TRC20", "")
CRYPTO_WALLET_USDT_TON   = os.getenv("CRYPTO_WALLET_USDT_TON",   "")
CRYPTO_WALLET_ETH         = os.getenv("CRYPTO_WALLET_ETH",        "")


# Cross-bot ELITE sync. The receiving bot checks INTERNAL_GRANT_SECRET.
# The sending bot posts to PEER_GRANT_URL using PEER_GRANT_SECRET.
PROJECT_KEY = os.getenv("PROJECT_KEY", "asmrleaks")
INTERNAL_GRANT_SECRET = os.getenv("INTERNAL_GRANT_SECRET", "")
PEER_GRANT_URL = os.getenv("PEER_GRANT_URL", "")
PEER_GRANT_SECRET = os.getenv("PEER_GRANT_SECRET", "")
PEER_PROJECT_KEY = os.getenv("PEER_PROJECT_KEY", "privateleaks")
ELITE_SYNC_DAYS = int(os.getenv("ELITE_SYNC_DAYS", "31") or "31")

# Multi-project bundles sold by the ASMR bot. PrivateLeaks reuses the old
# one-peer variables so the existing ELITE bridge keeps working while Asian
# and Extra are configured independently.
BUNDLE_PEERS = {
    "privateleaks": {
        "label": "PrivateLeaks",
        "grant_url": os.getenv("PRIVATELEAKS_GRANT_URL", PEER_GRANT_URL),
        "grant_secret": os.getenv("PRIVATELEAKS_GRANT_SECRET", PEER_GRANT_SECRET),
    },
    "asianleaks": {
        "label": "AsianLeaks",
        "grant_url": os.getenv("ASIANLEAKS_GRANT_URL", ""),
        "grant_secret": os.getenv("ASIANLEAKS_GRANT_SECRET", ""),
    },
    "extraleaks": {
        "label": "ExtraLeaks",
        "grant_url": os.getenv("EXTRALEAKS_GRANT_URL", ""),
        "grant_secret": os.getenv("EXTRALEAKS_GRANT_SECRET", ""),
    },
}
BUNDLE_TARGETS = tuple(BUNDLE_PEERS)
