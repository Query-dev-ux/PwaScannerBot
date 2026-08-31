from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    choosing_proxy = State()
    waiting_url = State()
