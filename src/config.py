from pathlib import Path
from dotenv import load_dotenv
import os

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0") or "0")

APIKEY_OZON = os.getenv("APIKEY_OZON", "")
CLIENT_ID_OZON = os.getenv("CLIEN_TID", "")
APIKEY_WB = os.getenv("APIKEY_WB", "")
APIKEY_CLAUDE = os.getenv("APIKEY_CLAUDE", "")

# Прокси для Ozon (на случай VPN/региональных блоков).
# Формат: 'http://USER:PASS@HOST:PORT' или 'socks5://USER:PASS@HOST:PORT'
# Если пусто — без прокси, прямое соединение.
OZON_PROXY_URL = os.getenv("OZON_PROXY_URL", "").strip() or None

# Прокси для Telegram Bot API (если провайдер VPS блокирует api.telegram.org).
# Формат: 'socks5://USER:PASS@HOST:PORT' (либо http://...). Пусто — прямое соединение.
# Нужен пакет aiohttp-socks (см. pyproject.toml).
TELEGRAM_PROXY_URL = os.getenv("TELEGRAM_PROXY_URL", "").strip() or None

# Fernet master-key для шифрования ozon_api_key / wb_api_key в БД.
# Генерация: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Если пусто — на проде fail-fast (см. src/security/crypto.py).
# В dev-режиме допустимо отсутствие — тогда токены пишутся как plain text.
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()

DATA_DIR = PROJECT_ROOT / "data"
STORAGE_DIR = DATA_DIR / "storage"
DB_PATH = DATA_DIR / "bot.db"
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

REFERENCE_FILES_DIR = PROJECT_ROOT / "переписки и файлы менеджер, фулфилмент" / "files"

DATA_DIR.mkdir(exist_ok=True)
STORAGE_DIR.mkdir(exist_ok=True)
