"""Сервис пользователей. Прокладка между БД и кодом, чтобы потом легко
было воткнуть шифрование токенов (Fernet master-key) — нужно будет поменять
только этот файл, остальной код останется как есть.

Текущая реализация: plain text. При переходе на шифрование подменим
get_ozon_creds / save_ozon_creds на decrypt/encrypt версии.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TypeVar, Type

from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.orm import Session

from src.db.models import User
from src.config import APIKEY_OZON, CLIENT_ID_OZON, APIKEY_WB, ALLOWED_USER_ID

T = TypeVar("T")


@dataclass
class OzonCreds:
    client_id: str
    api_key: str


def get_or_create_user(session: Session, tg_id: int) -> User:
    u = session.get(User, tg_id)
    if u is None:
        u = User(tg_id=tg_id, created_at=datetime.utcnow())
        session.add(u)
        session.flush()
    return u


def is_onboarded(user: Optional[User]) -> bool:
    if user is None:
        return False
    return bool(user.ozon_api_key and user.ozon_client_id)


def save_ozon_creds(session: Session, tg_id: int, client_id: str, api_key: str) -> None:
    """Сохранить Ozon-креды. api_key шифруется через Fernet (если ключ задан).
    client_id остаётся plain — это публичный ID кабинета, не секрет."""
    from src.security.crypto import encrypt
    u = get_or_create_user(session, tg_id)
    u.ozon_client_id = client_id.strip()
    u.ozon_api_key = encrypt(api_key.strip())
    if not u.onboarded_at:
        u.onboarded_at = datetime.utcnow()
    session.flush()


def get_ozon_creds(session: Session, tg_id: int) -> Optional[OzonCreds]:
    """Получить креды Ozon для user'а. Возвращает None если нет.
    Fallback: если tg_id == ALLOWED_USER_ID и в БД пусто — берём из .env
    (для совместимости со старым single-tenant flow).
    api_key расшифровывается через Fernet (если был зашифрован)."""
    from src.security.crypto import decrypt
    u = session.get(User, tg_id)
    if u and u.ozon_client_id and u.ozon_api_key:
        plain_key = decrypt(u.ozon_api_key)
        if plain_key:
            return OzonCreds(client_id=u.ozon_client_id, api_key=plain_key)
        # decrypt вернул None — ключа нет / сменился. Падаем на fallback.
    if tg_id == ALLOWED_USER_ID and APIKEY_OZON and CLIENT_ID_OZON:
        return OzonCreds(client_id=CLIENT_ID_OZON, api_key=APIKEY_OZON)
    return None


def save_wb_creds(session: Session, tg_id: int, api_key: str) -> None:
    from src.security.crypto import encrypt
    u = get_or_create_user(session, tg_id)
    u.wb_api_key = encrypt(api_key.strip())
    session.flush()


def get_wb_api_key(session: Session, tg_id: int) -> Optional[str]:
    from src.security.crypto import decrypt
    u = session.get(User, tg_id)
    if u and u.wb_api_key:
        plain = decrypt(u.wb_api_key)
        if plain:
            return plain
    if tg_id == ALLOWED_USER_ID and APIKEY_WB:
        return APIKEY_WB
    return None


# ── Helpers ───────────────────────────────────────────────────────────────

def current_user_id_from(event: TelegramObject) -> Optional[int]:
    """Извлекает tg_id из любого aiogram-события. Возвращает None если нет.
    Все handlers должны звать этот helper, не лазить в from_user.id напрямую —
    единое место для подмены логики (например при whitelist'е)."""
    if isinstance(event, (Message, CallbackQuery)):
        return event.from_user.id if event.from_user else None
    fu = getattr(event, "from_user", None)
    return fu.id if fu else None


async def validate_ozon_creds(client_id: str, api_key: str) -> tuple[bool, str]:
    """Тестовый запрос к Ozon для проверки валидности кред.
    True/'' если OK; False/error_msg если не работает.

    Используется в onboarding'е перед сохранением — чтобы не записать
    в БД мусорные ключи и сразу сказать юзеру «попробуй ещё раз».
    Самый дешёвый запрос — `product_list(limit=1)` (1 req, не плодит данных).
    """
    from src.integrations import OzonClient
    from src.integrations.ozon_api import OzonAPIError
    from src.config import OZON_PROXY_URL
    if not client_id or not api_key:
        return False, "Пустой client_id или api_key."
    try:
        cli = OzonClient(client_id, api_key, proxy=OZON_PROXY_URL)
        await cli.product_list(limit=1)
        return True, ""
    except OzonAPIError as e:
        s = str(e)
        if "401" in s or "403" in s or "Client-Id" in s:
            return False, "Ozon отверг ключи (401/403). Проверь Client ID и API Key в seller.ozon.ru → Настройки → Seller API."
        if "429" in s:
            return False, "Ozon ограничил запросы. Подожди 1-2 минуты и попробуй снова."
        return False, f"Ozon API: {s[:200]}"
    except Exception as e:
        return False, f"Сеть/прокси: {type(e).__name__}: {str(e)[:200]}"


def get_ozon_client_for(session: Session, tg_id: int):
    """Helper: вернуть готовый OzonClient для tg_id (с прокси из .env).
    None если у юзера нет credentials. Все handlers ДОЛЖНЫ использовать
    его вместо прямого OzonClient(CLIENT_ID_OZON, APIKEY_OZON, ...).
    """
    from src.integrations import OzonClient
    from src.config import OZON_PROXY_URL
    creds = get_ozon_creds(session, tg_id)
    if not creds:
        return None
    return OzonClient(creds.client_id, creds.api_key, proxy=OZON_PROXY_URL)


def get_owned(session: Session, model: Type[T], obj_id: int, user_id: int) -> Optional[T]:
    """Безопасный get: возвращает объект ТОЛЬКО если он принадлежит user_id.
    Если объект не существует или принадлежит другому юзеру — None.
    Защита от cross-tenant утечек, когда handler берёт ID из callback'а юзера B
    и пытается прочитать объект юзера A."""
    obj = session.get(model, obj_id)
    if obj is None:
        return None
    obj_uid = getattr(obj, "user_id", None)
    # NULL user_id = legacy-запись (до multi-tenant миграции). Считаем
    # что она принадлежит ALLOWED_USER_ID (там же и data migration сделана).
    if obj_uid is None and user_id == ALLOWED_USER_ID:
        return obj
    if obj_uid == user_id:
        return obj
    return None
