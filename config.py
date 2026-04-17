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

# Telegram Stars — prices in stars
STARS_PRICES = {
    30: 500,
}

# Ночной режим (МСК)
NIGHT_START = 23
NIGHT_END   = 8