# app/states.py

from aiogram.fsm.state import State, StatesGroup

class MarketSearch(StatesGroup):
    waiting_for_query = State()
    waiting_for_detail = State()

class AdminCategoryStates(StatesGroup):
    waiting_for_new_category_name = State()
    waiting_for_new_category_slug = State()
    renaming_category_name = State()
    renaming_category_slug = State()

class FeedbackStates(StatesGroup):
    waiting_for_feedback_message = State()