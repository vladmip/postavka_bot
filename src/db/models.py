from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String, Integer, ForeignKey, DateTime, Boolean, Text, JSON, UniqueConstraint, Date
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


class Sku(Base):
    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    article: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    intake_mode: Mapped[str] = mapped_column(String(16), default="piece")
    intake_box_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    photo_required: Mapped[bool] = mapped_column(Boolean, default=False)
    mark_required: Mapped[bool] = mapped_column(Boolean, default=False)
    ozon_offer_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    ozon_sku: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    wb_nm_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    size_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    photo_paths_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    components: Mapped[List["SkuKitLink"]] = relationship(
        "SkuKitLink", foreign_keys="SkuKitLink.kit_sku_id", back_populates="kit",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Sku {self.article} ({self.barcode})>"


class SkuKitLink(Base):
    __tablename__ = "sku_kits"
    __table_args__ = (UniqueConstraint("kit_sku_id", "component_sku_id", name="uq_kit_component"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kit_sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), index=True)
    component_sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"))
    qty: Mapped[int] = mapped_column(Integer)

    kit: Mapped["Sku"] = relationship("Sku", foreign_keys=[kit_sku_id], back_populates="components")
    component: Mapped["Sku"] = relationship("Sku", foreign_keys=[component_sku_id])


class OzonProduct(Base):
    """Снапшот товара из Ozon Seller API. Заполняется через /refresh_ozon_catalog.
    Это единственный источник правды для Ozon-флоу: matching xlsx → товар,
    pre-check перед draft_create, генерация ТЗ Отгрузка."""
    __tablename__ = "ozon_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    sku: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # числовой Ozon SKU
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    barcode_primary: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    raw_barcodes_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<OzonProduct {self.offer_id} sku={self.sku}>"


class WbProduct(Base):
    """Снапшот товара из WB Content API. Заполняется через /refresh_wb_catalog."""
    __tablename__ = "wb_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    nm_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    article: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    barcode_primary: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    raw_barcodes_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    def __repr__(self) -> str:
        return f"<WbProduct nm={self.nm_id} {self.article}>"


SUPPLY_STATES = (
    "draft", "intake_sent", "intake_done", "shipment_sent",
    "picked", "shipped", "accepted", "closed", "cancelled",
)


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(8))  # 'wb' | 'ozon'
    warehouse: Mapped[str] = mapped_column(String(128))
    mp_supply_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    slot_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    slot_date_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    slot_dates_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="draft")
    comments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    items: Mapped[List["SupplyItem"]] = relationship(
        "SupplyItem", back_populates="supply", cascade="all, delete-orphan",
    )


class SupplyItem(Base):
    __tablename__ = "supply_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id", ondelete="CASCADE"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"))
    qty_planned: Mapped[int] = mapped_column(Integer)
    qty_picked: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    qty_accepted: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    box_label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    expiry_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    expanded_from_kit_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("skus.id"), nullable=True,
    )

    supply: Mapped["Supply"] = relationship("Supply", back_populates="items")
    sku: Mapped["Sku"] = relationship("Sku", foreign_keys=[sku_id])


class InboxFile(Base):
    __tablename__ = "inbox_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    original_name: Mapped[str] = mapped_column(String(256))
    file_kind: Mapped[str] = mapped_column(String(32))  # opis_wb|opis_ozon|prihod|ostatki|unknown
    supply_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("supplies.id", ondelete="SET NULL"), nullable=True,
    )
    parsed_payload_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    file_path: Mapped[str] = mapped_column(String(512))


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    fio: Mapped[str] = mapped_column(String(128))
    vehicle: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    plate: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class StateLog(Base):
    __tablename__ = "state_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    supply_id: Mapped[int] = mapped_column(ForeignKey("supplies.id", ondelete="CASCADE"), index=True)
    from_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    event_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class StockSnapshot(Base):
    __tablename__ = "stock_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now)
    source: Mapped[str] = mapped_column(String(32))  # 'ostatki_xls' | 'calculated'
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer)


# ── Заявки на отгрузку (новая модель, основной flow) ───────────────────────

SHIPMENT_STATES = (
    "draft",            # файлы загружены, идёт wizard
    "planning",         # wizard пройден, выбраны даты/кросс-док
    "slot_searching",   # фоновый слот-хантер бронирует слоты
    "slots_booked",     # все слоты найдены, но поставки ещё не созданы в ЛК
    "supplies_created", # поставки созданы в WB/Ozon ЛК
    "tz_sent",          # ТЗ Отгрузка отправлено в ФФ
    "picked",           # ФФ собрал (опись пришла)
    "shipped",          # выехала
    "accepted",         # МП принял
    "closed",
    "cancelled",
)


class ShipmentRequest(Base):
    __tablename__ = "shipment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    # Параметры отгрузки (заполняются на этапе wizard)
    target_date_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    target_date_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Конкретные выбранные даты (список ISO-строк "YYYY-MM-DD"). Если None —
    # используется диапазон from..to. Введено, чтобы фильтровать слоты Ozon
    # по точным галочкам пользователя (Ozon-API принимает только диапазон).
    target_dates_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    crossdock_warehouses_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Для каждого направления: {'wb_Центральный': 'Внуково', 'ozon_Москва...': 'any'}

    # Тип Ozon-поставки: "direct" (РФЦ) или "cross" (CROSSDOCK через хаб).
    # Фиксируется один раз при создании заявки и больше не меняется — у Ozon
    # под капотом это разные draft API и разные warehouse_id, смешивать нельзя.
    # NULL = legacy-заявки до миграции, юзер выбирает тип при первом открытии карточки.
    ozon_supply_type: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    source_files_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    comments: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    items: Mapped[List["ShipmentItem"]] = relationship(
        "ShipmentItem", back_populates="request", cascade="all, delete-orphan",
    )


class ShipmentItem(Base):
    __tablename__ = "shipment_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("shipment_requests.id", ondelete="CASCADE"), index=True,
    )
    sku_id: Mapped[Optional[int]] = mapped_column(ForeignKey("skus.id"), nullable=True)
    # Прямые ссылки на маркет-каталоги. По одной из двух (в зависимости от marketplace).
    ozon_product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ozon_products.id"), nullable=True, index=True,
    )
    wb_product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("wb_products.id"), nullable=True, index=True,
    )
    raw_article: Mapped[str] = mapped_column(String(128))
    marketplace: Mapped[str] = mapped_column(String(8))      # 'wb' | 'ozon'
    cluster: Mapped[str] = mapped_column(String(64))         # 'Центральный', 'Москва, МО и Дальние регионы'
    qty: Mapped[int] = mapped_column(Integer)

    # После бронирования — конкретный склад (если кластер раскрылся в склад)
    target_warehouse: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    booked_supply_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    booked_slot_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    request: Mapped["ShipmentRequest"] = relationship("ShipmentRequest", back_populates="items")
    sku: Mapped[Optional["Sku"]] = relationship("Sku", foreign_keys=[sku_id])
    ozon_product: Mapped[Optional["OzonProduct"]] = relationship(
        "OzonProduct", foreign_keys=[ozon_product_id],
    )
    wb_product: Mapped[Optional["WbProduct"]] = relationship(
        "WbProduct", foreign_keys=[wb_product_id],
    )


class OzonDraftCache(Base):
    """Кэш созданных Ozon-драфтов. Драфт живёт 30 мин у Ozon; переиспользуем
    в окне 25 мин, чтобы не палить лимит 2/мин на /v1/draft/*/create."""
    __tablename__ = "ozon_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("shipment_requests.id", ondelete="CASCADE"), index=True,
    )
    cluster: Mapped[str] = mapped_column(String(64))
    cluster_id: Mapped[int] = mapped_column(Integer)        # macrolocal_cluster_id
    draft_id: Mapped[int] = mapped_column(Integer)          # Ozon draft_id
    supply_type: Mapped[int] = mapped_column(Integer)       # 1=CROSSDOCK, 2=DIRECT, 3=MULTI
    drop_off_warehouse_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    drop_off_warehouse_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class FavoriteCrossdockPoint(Base):
    """Любимые drop-off точки для кроссдока: ПВЗ/хабы/ФФ — что угодно."""
    __tablename__ = "favorite_crossdock_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))           # display name
    warehouse_id: Mapped[int] = mapped_column(Integer)
    point_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
