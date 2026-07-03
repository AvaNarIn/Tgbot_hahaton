from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from route_service import TransportType


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новая поездка")],
            [KeyboardButton(text="Мои поездки"), KeyboardButton(text="Настройки")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


def transport_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Авто",
                    callback_data=f"{prefix}:{TransportType.CAR.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Общественный транспорт",
                    callback_data=f"{prefix}:{TransportType.PUBLIC.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пешком",
                    callback_data=f"{prefix}:{TransportType.WALK.value}",
                )
            ],
        ]
    )


def future_trip_actions_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Редактировать", callback_data=f"trip_edit:{trip_id}"
                ),
                InlineKeyboardButton(
                    text="Удалить", callback_data=f"trip_delete:{trip_id}"
                ),
            ]
        ]
    )


def edit_fields_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Адрес отправления", callback_data=f"edit_field:{trip_id}:origin"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Адрес назначения", callback_data=f"edit_field:{trip_id}:destination"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Время", callback_data=f"edit_field:{trip_id}:arrival_time"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Транспорт", callback_data=f"edit_field:{trip_id}:transport_type"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Минуты напоминания",
                    callback_data=f"edit_field:{trip_id}:reminder_minutes",
                )
            ],
        ]
    )


def delete_confirm_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить", callback_data=f"delete_yes:{trip_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="delete_no"),
            ]
        ]
    )
