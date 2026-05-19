from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from src.config import DB_URL

engine = create_engine(DB_URL, future=True)


# SQLite по умолчанию НЕ enforce'ит FK constraints — в т.ч. ondelete="CASCADE"
# не работает на уровне БД (только ORM-cascade через relationship). Без этого
# bulk `DELETE FROM shipment_requests` оставлял orphan ShipmentItem'ы, которые
# при переиспользовании id (после полной очистки таблицы) «всплывали» в новой
# заявке как лишние позиции с гигантскими qty → Ozon ловил
# TOTAL_VOLUME_IN_LITRES_INVALID. Включаем PRAGMA для каждого нового connect.
if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def db_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
