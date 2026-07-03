from aiogram.fsm.state import State, StatesGroup


class NewTrip(StatesGroup):
    origin = State()
    destination = State()
    arrival_time = State()
    transport_type = State()
    reminder_minutes = State()


class EditTrip(StatesGroup):
    choosing_field = State()
    origin = State()
    destination = State()
    arrival_time = State()
    transport_type = State()
    reminder_minutes = State()


class Settings(StatesGroup):
    buffer_percent = State()
