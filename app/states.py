# app/states.py

from aiogram.fsm.state import State, StatesGroup

class MarketSearch(StatesGroup):
    waiting_for_query = State()
    waiting_for_detail = State()
