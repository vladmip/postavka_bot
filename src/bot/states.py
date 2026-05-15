from aiogram.fsm.state import State, StatesGroup


class SkuAdd(StatesGroup):
    barcode = State()
    article = State()
    name = State()
    intake_mode = State()


class SkuKitAdd(StatesGroup):
    pick_component = State()
    qty = State()


class SupplyNew(StatesGroup):
    marketplace = State()
    cluster = State()
    warehouse = State()
    warehouse_custom = State()
    slot_choice = State()
    slot_date = State()


class SupplyAddItem(StatesGroup):
    sku = State()
    qty = State()


class UploadBind(StatesGroup):
    pick_supply = State()


class ProductHints(StatesGroup):
    awaiting_file = State()
    confirm_strategy = State()


class Onboarding(StatesGroup):
    """Wizard первого подключения: Ozon-токен → API key → загрузка каталога."""
    ozon_client_id = State()
    ozon_api_key = State()
    catalog_offer = State()
