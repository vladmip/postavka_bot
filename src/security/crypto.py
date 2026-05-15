"""Fernet-шифрование пользовательских токенов.

Master-key из env: `TOKEN_ENCRYPTION_KEY` (Fernet base64-encoded 32-byte).
Без ключа — `encrypt()` возвращает строку как есть (plain mode для dev),
но логирует WARN. На проде обязательно задать.

Генерация ключа:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Идемпотентность: `encrypt(already_encrypted)` пропускает повторное шифрование
(детект по префиксу `gAAAAAB`). Это нужно для миграции данных, чтобы случайный
повторный запуск не сломал базу.
"""
from __future__ import annotations

import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from src.config import TOKEN_ENCRYPTION_KEY

logger = logging.getLogger("security.crypto")

# Fernet token всегда начинается с base64('gAAAAAB' + 8 bytes IV...).
# Используем как маркер «уже зашифровано» — для идемпотентности.
_FERNET_PREFIX = "gAAAAAB"

_fernet: Optional[Fernet] = None
_warned_missing = False


def _get_fernet() -> Optional[Fernet]:
    global _fernet, _warned_missing
    if _fernet is not None:
        return _fernet
    if not TOKEN_ENCRYPTION_KEY:
        if not _warned_missing:
            logger.warning(
                "TOKEN_ENCRYPTION_KEY не задан — токены пишутся PLAIN TEXT. "
                "Сгенерируй ключ и положи в .env перед деплоем на сервер!"
            )
            _warned_missing = True
        return None
    try:
        _fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            f"TOKEN_ENCRYPTION_KEY невалиден: {e}. "
            "Сгенерируй заново: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return _fernet


def is_encrypted(value: Optional[str]) -> bool:
    """True если строка похожа на Fernet-токен."""
    return bool(value) and value.startswith(_FERNET_PREFIX)


def encrypt(plain: Optional[str]) -> Optional[str]:
    """Зашифровать строку. Идемпотентно: если уже зашифрована — возвращает как есть.
    None / пустая → как есть. Без ключа → plain (dev mode)."""
    if not plain:
        return plain
    if is_encrypted(plain):
        return plain
    f = _get_fernet()
    if f is None:
        return plain  # dev fallback
    return f.encrypt(plain.encode()).decode()


def decrypt(token: Optional[str]) -> Optional[str]:
    """Расшифровать. Если строка не зашифрована (legacy plain) — вернёт как есть.
    Если зашифрована но ключ невалиден / отсутствует — лог + None."""
    if not token:
        return token
    if not is_encrypted(token):
        return token  # legacy plain
    f = _get_fernet()
    if f is None:
        logger.error(
            "Не могу расшифровать токен — TOKEN_ENCRYPTION_KEY не задан. "
            "Поставь ключ или попроси юзера ввести креды заново."
        )
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("Decryption failed — InvalidToken (ключ изменился?)")
        return None
