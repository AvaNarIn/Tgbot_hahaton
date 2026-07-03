import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import BOT_TOKEN, DATABASE_PATH
from database import Database
from keyboards import (
    cancel_keyboard,
    delete_confirm_keyboard,
    delete_only_keyboard,
    edit_fields_keyboard,
    future_trip_actions_keyboard,
    main_menu_keyboard,
    transport_keyboard,
)
from route_service import (
    AddressNotFoundError,
    ApiLimitError,
    ApiUnavailableError,
    RouteService,
    RouteServiceError,
    transport_label,
)
from scheduler import calculate_reminder_time, reminder_loop
from states import EditTrip, NewTrip, Settings


db = Database(DATABASE_PATH)
route_service = RouteService()
dp = Dispatcher()
MAX_REMINDER_MINUTES = 1440


def text_value(message: Message) -> str:
    return (message.text or "").strip()


def parse_arrival_time(value: str) -> datetime | None:
    formats = ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M")
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def parse_non_negative_int(value: str) -> int | None:
    try:
        number = int(value.strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def is_future_trip(trip) -> bool:
    return datetime.fromisoformat(trip["arrival_time"]) > datetime.now()


async def build_trip_text(trip) -> str:
    arrival_time = datetime.fromisoformat(trip["arrival_time"])
    travel_time_minutes = trip["travel_time_minutes"]
    if travel_time_minutes is None:
        reminder_time_text = "не рассчитано"
        travel_time_text = "не рассчитано"
    else:
        reminder_time = calculate_reminder_time(
            arrival_time=arrival_time,
            travel_time_minutes=travel_time_minutes,
            user_reminder_minutes=trip["reminder_minutes"],
            buffer_percent=trip["buffer_percent"],
        )
        reminder_time_text = reminder_time.strftime("%d.%m.%Y %H:%M")
        travel_time_text = f"{travel_time_minutes} мин"
    status = "Будущая" if arrival_time > datetime.now() else "Прошедшая"

    return (
        f"#{trip['id']} - {status} поездка\n"
        f"Откуда: {trip['origin'] or 'не указано'}\n"
        f"Куда: {trip['destination']}\n"
        f"Прибытие: {arrival_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"Транспорт: {transport_label(trip['transport_type'])}\n"
        f"Время в пути: {travel_time_text}\n"
        f"Напомнить за: {trip['reminder_minutes']} мин до выхода\n"
        f"Запас: {trip['buffer_percent']}%\n"
        f"Время напоминания: {reminder_time_text}"
    )


def route_error_text(error: RouteServiceError) -> str:
    if isinstance(error, AddressNotFoundError):
        return str(error)
    if isinstance(error, ApiLimitError):
        return "Превышен лимит запросов 2ГИС. Попробуйте позже."
    if isinstance(error, ApiUnavailableError):
        return str(error)
    return f"Не удалось рассчитать маршрут: {error}"


async def calculate_trip_route(
    trip,
    *,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    destination_latitude: float | None = None,
    destination_longitude: float | None = None,
    transport_type: str | None = None,
):
    return await route_service.get_travel_time_minutes(
        origin_latitude=origin_latitude
        if origin_latitude is not None
        else trip["origin_latitude"],
        origin_longitude=origin_longitude
        if origin_longitude is not None
        else trip["origin_longitude"],
        destination_latitude=destination_latitude
        if destination_latitude is not None
        else trip["latitude"],
        destination_longitude=destination_longitude
        if destination_longitude is not None
        else trip["longitude"],
        transport_type=transport_type or trip["transport_type"],
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    await message.answer(
        "Привет! Я помогу запланировать поездку и вовремя напомню, когда выходить.",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.text == "Отмена")
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())


@dp.message(F.text == "Новая поездка")
async def new_trip_start(message: Message, state: FSMContext) -> None:
    db.ensure_user(message.from_user.id)
    await state.set_state(NewTrip.origin)
    await message.answer("Введите адрес отправления:", reply_markup=cancel_keyboard())


@dp.message(NewTrip.origin)
async def new_trip_origin(message: Message, state: FSMContext) -> None:
    address = text_value(message)
    if not address:
        await message.answer("Введите адрес отправления текстом.")
        return

    try:
        origin = await route_service.geocode_address(address)
    except RouteServiceError as error:
        await message.answer(route_error_text(error))
        return

    await state.update_data(
        origin=origin.address,
        origin_latitude=origin.latitude,
        origin_longitude=origin.longitude,
    )
    await state.set_state(NewTrip.destination)
    await message.answer("Введите адрес назначения:", reply_markup=cancel_keyboard())


@dp.message(NewTrip.destination)
async def new_trip_destination(message: Message, state: FSMContext) -> None:
    address = text_value(message)
    if not address:
        await message.answer("Введите адрес назначения текстом.")
        return

    try:
        destination = await route_service.geocode_address(address)
    except RouteServiceError as error:
        await message.answer(route_error_text(error))
        return

    await state.update_data(
        destination=destination.address,
        latitude=destination.latitude,
        longitude=destination.longitude,
    )
    await state.set_state(NewTrip.arrival_time)
    await message.answer(
        "Введите дату и время прибытия в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
        "Например: 15.07.2026 09:30"
    )


@dp.message(NewTrip.arrival_time)
async def new_trip_arrival_time(message: Message, state: FSMContext) -> None:
    arrival_time = parse_arrival_time(text_value(message))
    if arrival_time is None:
        await message.answer("Не понял дату. Используйте формат ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    if arrival_time <= datetime.now():
        await message.answer("Время прибытия должно быть в будущем.")
        return

    await state.update_data(arrival_time=arrival_time.isoformat(timespec="minutes"))
    await state.set_state(NewTrip.transport_type)
    await message.answer(
        "Выберите способ передвижения:",
        reply_markup=transport_keyboard("new_transport"),
    )


@dp.callback_query(NewTrip.transport_type, F.data.startswith("new_transport:"))
async def new_trip_transport(callback: CallbackQuery, state: FSMContext) -> None:
    transport_type = callback.data.split(":", 1)[1]
    await state.update_data(transport_type=transport_type)
    await state.set_state(NewTrip.reminder_minutes)
    await callback.message.answer("За сколько минут до выхода напомнить?")
    await callback.answer()


@dp.message(NewTrip.reminder_minutes)
async def new_trip_reminder_minutes(message: Message, state: FSMContext) -> None:
    reminder_minutes = parse_non_negative_int(text_value(message))
    if reminder_minutes is None:
        await message.answer("Введите целое число минут не меньше 0.")
        return
    if reminder_minutes > MAX_REMINDER_MINUTES:
        await message.answer("Введите значение не больше 1440 минут.")
        return

    data = await state.get_data()
    try:
        route = await route_service.get_travel_time_minutes(
            origin_latitude=data["origin_latitude"],
            origin_longitude=data["origin_longitude"],
            destination_latitude=data["latitude"],
            destination_longitude=data["longitude"],
            transport_type=data["transport_type"],
        )
    except RouteServiceError as error:
        await message.answer(route_error_text(error))
        return

    user = db.get_user(message.from_user.id)
    trip_id = db.create_trip(
        user_id=message.from_user.id,
        origin=data["origin"],
        origin_latitude=data["origin_latitude"],
        origin_longitude=data["origin_longitude"],
        destination=data["destination"],
        latitude=data["latitude"],
        longitude=data["longitude"],
        arrival_time=data["arrival_time"],
        transport_type=data["transport_type"],
        reminder_minutes=reminder_minutes,
        buffer_percent=user["buffer_percent"],
        travel_time_minutes=route.duration_minutes,
    )
    await state.clear()

    trip = db.get_trip(trip_id)
    await message.answer(
        "Поездка создана!\n\n" + await build_trip_text(trip),
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.text == "Мои поездки")
async def my_trips(message: Message) -> None:
    db.ensure_user(message.from_user.id)
    trips = db.get_user_trips(message.from_user.id)
    if not trips:
        await message.answer("Поездок пока нет.", reply_markup=main_menu_keyboard())
        return

    await message.answer("Ваши поездки:", reply_markup=main_menu_keyboard())
    for trip in trips:
        markup = (
            future_trip_actions_keyboard(trip["id"])
            if is_future_trip(trip)
            else delete_only_keyboard(trip["id"])
        )
        await message.answer(await build_trip_text(trip), reply_markup=markup)


async def answer_updated_trip(message: Message, trip_id: int, action_text: str) -> None:
    trip = db.get_trip(trip_id)
    if trip is None:
        await message.answer("Поездка не найдена.", reply_markup=main_menu_keyboard())
        return

    await message.answer(
        f"{action_text}:\n\n" + await build_trip_text(trip),
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.text == "Настройки")
async def settings_start(message: Message, state: FSMContext) -> None:
    user = db.ensure_user(message.from_user.id)
    await state.set_state(Settings.buffer_percent)
    await message.answer(
        f"Текущий запас времени: {user['buffer_percent']}%.\n"
        "Введите новый процент запаса для будущих поездок:",
        reply_markup=cancel_keyboard(),
    )


@dp.message(Settings.buffer_percent)
async def settings_buffer_percent(message: Message, state: FSMContext) -> None:
    try:
        buffer_percent = int(text_value(message))
    except ValueError:
        await message.answer("Введите целое число процентов.")
        return
    if not 0 <= buffer_percent <= 100:
        await message.answer("Введите значение от 0 до 100.")
        return

    db.update_user_buffer(message.from_user.id, buffer_percent)
    await state.clear()
    await message.answer(
        f"Готово. Для новых поездок запас времени будет {buffer_percent}%.",
        reply_markup=main_menu_keyboard(),
    )


@dp.callback_query(F.data.startswith("edit_field:"))
async def edit_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, trip_id_raw, field = callback.data.split(":", 2)
    trip_id = int(trip_id_raw)
    trip = db.get_trip(trip_id)
    if trip is None or trip["user_id"] != callback.from_user.id:
        await callback.answer("Поездка не найдена.", show_alert=True)
        return
    if not is_future_trip(trip):
        await callback.answer("Прошедшие поездки можно только смотреть.", show_alert=True)
        return

    await state.update_data(trip_id=trip_id, edit_field=field)

    if field == "origin":
        await state.set_state(EditTrip.origin)
        await callback.message.answer(
            f"Поездка #{trip_id}. Введите новый адрес отправления:",
            reply_markup=cancel_keyboard(),
        )
    elif field == "destination":
        await state.set_state(EditTrip.destination)
        await callback.message.answer(
            f"Поездка #{trip_id}. Введите новый адрес назначения:",
            reply_markup=cancel_keyboard(),
        )
    elif field == "arrival_time":
        await state.set_state(EditTrip.arrival_time)
        await callback.message.answer(
            f"Поездка #{trip_id}. Введите новую дату и время прибытия "
            "в формате ДД.ММ.ГГГГ ЧЧ:ММ:",
            reply_markup=cancel_keyboard(),
        )
    elif field == "transport_type":
        await state.set_state(EditTrip.transport_type)
        await callback.message.answer(
            f"Поездка #{trip_id}. Выберите новый способ передвижения:",
            reply_markup=transport_keyboard("edit_transport"),
        )
    elif field == "reminder_minutes":
        await state.set_state(EditTrip.reminder_minutes)
        await callback.message.answer(
            f"Поездка #{trip_id}. Введите новое количество минут:",
            reply_markup=cancel_keyboard(),
        )
    else:
        await callback.answer("Неизвестное поле редактирования.", show_alert=True)
        return
    await callback.answer()


@dp.message(EditTrip.origin)
async def edit_origin(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    address = text_value(message)
    if not address:
        await message.answer("Введите адрес отправления текстом.")
        return

    try:
        origin = await route_service.geocode_address(address)
        trip = db.get_trip(data["trip_id"])
        if trip is None:
            raise RouteServiceError("Поездка не найдена.")
        route = await calculate_trip_route(
            trip,
            origin_latitude=origin.latitude,
            origin_longitude=origin.longitude,
        )
        db.update_trip_field(data["trip_id"], "origin", origin.address)
        db.update_trip_field(data["trip_id"], "origin_latitude", origin.latitude)
        db.update_trip_field(data["trip_id"], "origin_longitude", origin.longitude)
        db.update_trip_route(data["trip_id"], route.duration_minutes)
    except RouteServiceError as error:
        await message.answer(route_error_text(error))
        return

    await state.clear()
    await answer_updated_trip(
        message, data["trip_id"], "Адрес отправления обновлен для поездки"
    )


@dp.message(EditTrip.destination)
async def edit_destination(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    address = text_value(message)
    if not address:
        await message.answer("Введите адрес назначения текстом.")
        return

    try:
        destination = await route_service.geocode_address(address)
        trip = db.get_trip(data["trip_id"])
        if trip is None:
            raise RouteServiceError("Поездка не найдена.")
        route = await calculate_trip_route(
            trip,
            destination_latitude=destination.latitude,
            destination_longitude=destination.longitude,
        )
        db.update_trip_field(data["trip_id"], "destination", destination.address)
        db.update_trip_field(data["trip_id"], "latitude", destination.latitude)
        db.update_trip_field(data["trip_id"], "longitude", destination.longitude)
        db.update_trip_route(data["trip_id"], route.duration_minutes)
    except RouteServiceError as error:
        await message.answer(route_error_text(error))
        return

    await state.clear()
    await answer_updated_trip(
        message, data["trip_id"], "Адрес назначения обновлен для поездки"
    )


@dp.message(EditTrip.arrival_time)
async def edit_arrival_time(message: Message, state: FSMContext) -> None:
    arrival_time = parse_arrival_time(text_value(message))
    if arrival_time is None:
        await message.answer("Не понял дату. Используйте формат ДД.ММ.ГГГГ ЧЧ:ММ.")
        return
    if arrival_time <= datetime.now():
        await message.answer("Время прибытия должно быть в будущем.")
        return

    data = await state.get_data()
    db.update_trip_field(
        data["trip_id"], "arrival_time", arrival_time.isoformat(timespec="minutes")
    )
    await state.clear()
    await answer_updated_trip(message, data["trip_id"], "Время обновлено для поездки")


@dp.callback_query(EditTrip.transport_type, F.data.startswith("edit_transport:"))
async def edit_transport(callback: CallbackQuery, state: FSMContext) -> None:
    transport_type = callback.data.split(":", 1)[1]
    data = await state.get_data()
    try:
        trip = db.get_trip(data["trip_id"])
        if trip is None:
            raise RouteServiceError("Поездка не найдена.")
        route = await calculate_trip_route(trip, transport_type=transport_type)
        db.update_trip_field(data["trip_id"], "transport_type", transport_type)
        db.update_trip_route(data["trip_id"], route.duration_minutes)
    except RouteServiceError as error:
        await callback.message.answer(route_error_text(error))
        await callback.answer()
        return

    await state.clear()
    await answer_updated_trip(
        callback.message, data["trip_id"], "Транспорт обновлен для поездки"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("new_transport:") | F.data.startswith("edit_transport:"))
async def stale_transport_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Этот выбор транспорта уже не актуален. Начните действие заново.",
        show_alert=True,
    )


@dp.message(EditTrip.reminder_minutes)
async def edit_reminder_minutes(message: Message, state: FSMContext) -> None:
    reminder_minutes = parse_non_negative_int(text_value(message))
    if reminder_minutes is None:
        await message.answer("Введите целое число минут не меньше 0.")
        return
    if reminder_minutes > MAX_REMINDER_MINUTES:
        await message.answer("Введите значение не больше 1440 минут.")
        return

    data = await state.get_data()
    db.update_trip_field(data["trip_id"], "reminder_minutes", reminder_minutes)
    await state.clear()
    await answer_updated_trip(
        message, data["trip_id"], "Минуты напоминания обновлены для поездки"
    )


@dp.callback_query(F.data.startswith("trip_edit:"))
async def trip_edit(callback: CallbackQuery, state: FSMContext) -> None:
    trip_id = int(callback.data.split(":", 1)[1])
    trip = db.get_trip(trip_id)

    if trip is None or trip["user_id"] != callback.from_user.id:
        await callback.answer("Поездка не найдена.", show_alert=True)
        return

    if not is_future_trip(trip):
        await callback.answer("Прошедшие поездки можно только смотреть.", show_alert=True)
        return

    await state.set_state(EditTrip.choosing_field)
    await state.update_data(trip_id=trip_id)

    await callback.message.answer(
        "Вы редактируете эту поездку:\n\n"
        + await build_trip_text(trip)
        + "\n\nЧто хотите изменить?",
        reply_markup=edit_fields_keyboard(trip_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("trip_delete:"))
async def trip_delete(callback: CallbackQuery, state: FSMContext) -> None:
    trip_id = int(callback.data.split(":", 1)[1])
    trip = db.get_trip(trip_id)

    if trip is None or trip["user_id"] != callback.from_user.id:
        await callback.answer("Поездка не найдена.", show_alert=True)
        return

    await state.clear()
    await callback.message.answer(
        "Вы собираетесь удалить эту поездку:\n\n"
        + await build_trip_text(trip)
        + "\n\nТочно удалить?",
        reply_markup=delete_confirm_keyboard(trip_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_yes:"))
async def delete_yes(callback: CallbackQuery, state: FSMContext) -> None:
    trip_id = int(callback.data.split(":", 1)[1])
    trip = db.get_trip(trip_id)

    if trip is None or trip["user_id"] != callback.from_user.id:
        await callback.answer("Поездка не найдена.", show_alert=True)
        return

    await state.clear()
    deleted_trip_text = await build_trip_text(trip)

    db.delete_trip(trip_id)

    await callback.message.answer(
        "Поездка удалена:\n\n" + deleted_trip_text,
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_no:"))
async def delete_no(callback: CallbackQuery, state: FSMContext) -> None:
    trip_id = int(callback.data.split(":", 1)[1])
    trip = db.get_trip(trip_id)

    if trip is None or trip["user_id"] != callback.from_user.id:
        await callback.answer("Поездка не найдена.", show_alert=True)
        return

    await state.clear()
    await callback.message.answer(
        "Удаление отменено. Поездка сохранена:\n\n" + await build_trip_text(trip),
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "delete_no")
async def delete_no_without_trip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Удаление отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.message()
async def unknown_message(message: Message) -> None:
    await message.answer(
        "Выберите действие в меню или отправьте /start.",
        reply_markup=main_menu_keyboard(),
    )


async def main() -> None:
    db.init_db()
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(reminder_loop(bot, db))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
