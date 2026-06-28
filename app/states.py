# app/states.py

from aiogram.fsm.state import State, StatesGroup

class MarketSearch(StatesGroup):
    waiting_for_query = State()
    waiting_for_detail = State()

# -----------------------------------------------------------------------------
# ServiceSearch
# -----------------------------------------------------------------------------
# Аналог состояния поиска для раздела «Услуги». Используется в services_view.py.
class ServiceSearch(StatesGroup):
    """
    FSM states for searching services. Works similarly to MarketSearch but scoped
    to the services section. See services_view.py for handlers.
    """
    waiting_for_query = State()   # awaiting user search query input
    waiting_for_detail = State()  # awaiting user selection of a search result

class AdminCategoryStates(StatesGroup):
    waiting_for_new_category_name = State()
    waiting_for_new_category_slug = State()
    renaming_category_name = State()
    renaming_category_slug = State()

class FeedbackStates(StatesGroup):
    waiting_for_feedback_message = State()

class AdminFieldStates(StatesGroup):
    choosing_type = State()
    waiting_label = State()
    waiting_key = State()
    waiting_required = State()
    waiting_options = State()  # только для select/multiselect
    editing_label = State()
    editing_key = State()
    editing_options = State()

class EditListing(StatesGroup):
    waiting_price = State()
    waiting_descr = State()


# ─────────────────────────────────────────────────────────────────────────────
# Состояния для создания объявления (у вас уже есть)
# ─────────────────────────────────────────────────────────────────────────────
class AddListing(StatesGroup):
    waiting_title = State()
    waiting_price = State()
    waiting_descr = State()
    waiting_city = State()
    waiting_category = State()
    waiting_confirm = State()
    waiting_flex = State()

# ─────────────────────────────────────────────────────────────────────────────
# Состояния для редактирования объявления (новые)
# ─────────────────────────────────────────────────────────────────────────────
class EditListing(StatesGroup):
    waiting_title = State()   # шаг 1 — редактирование заголовка
    waiting_price = State()   # шаг 2 — редактирование цены
    waiting_descr = State()   # шаг 3 — редактирование описания
    waiting_flex = State()    # шаг 4 — редактирование flex-полей