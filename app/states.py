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
