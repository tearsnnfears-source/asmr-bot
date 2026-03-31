import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN")
GROUP_ID        = int(os.getenv("GROUP_ID", "0"))
INVITE_LINK     = os.getenv("INVITE_LINK", "")
ADMIN_IDS       = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
DATABASE_URL    = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./asmr.db")
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "")
TRIBUTE_DONATE_LINK = os.getenv("TRIBUTE_DONATE_LINK", "")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")  # твой Railway URL

# YooMoney
YOOMONEY_TOKEN      = os.getenv("YOOMONEY_TOKEN", "")
YOOMONEY_WALLET     = os.getenv("YOOMONEY_WALLET", "")   # номер кошелька получателя

# Tribute (для карты)
TRIBUTE_BOT_URL = os.getenv("TRIBUTE_BOT_URL", "")   # например https://tribute.tg/to/your_page
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "")
TRIBUTE_DONATE_LINK = os.getenv("TRIBUTE_DONATE_LINK", "")

# Telegram Stars — цены в звёздах
STARS_PRICES = {
    30: 400,
}

# Цены в рублях (для СБП / YooMoney)
RUB_PRICES = {
    30: 499,
}

# Цены для Tribute (карта, в рублях)
EUR_PRICES = {
    30: 5,
}

# Ночной режим (МСК)
NIGHT_START = 23
NIGHT_END   = 8