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

# startapp code → tier (used in webhook to auto-assign tier)
TRIBUTE_TIER_MAP = {
    "sQSn": "plus",
    "sT5D": "pro",
    "sT5E": "elite",
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

# Crypto prices in USD per tier (used as `amount` for Cryptocloud invoice)
CRYPTO_USDT_PRICES = {
    'plus':  6,
    'pro':   8,
    'elite': 10,
}

# Cryptocloud (https://cryptocloud.plus) — hosted crypto checkout
CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "")
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "")
CRYPTOCLOUD_SECRET  = os.getenv("CRYPTOCLOUD_SECRET", "")  # HS256 secret for postback JWT verification
CRYPTOCLOUD_API_URL = os.getenv("CRYPTOCLOUD_API_URL", "https://api.cryptocloud.plus/v2/invoice/create")
