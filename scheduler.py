import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot

from database import Database
from route_service import transport_label


logger = logging.getLogger(__name__)


def calculate_reminder_time(
    arrival_time: datetime,
    travel_time_minutes: int,
    user_reminder_minutes: int,
    buffer_percent: int,
) -> datetime:
    buffer_minutes = travel_time_minutes * buffer_percent / 100
    return arrival_time - timedelta(
        minutes=travel_time_minutes + user_reminder_minutes + buffer_minutes
    )


async def check_and_send_reminders(
    bot: Bot,
    db: Database,
) -> None:
    now = datetime.now()

    for trip in db.get_unreminded_trips():
        if trip["travel_time_minutes"] is None:
            continue

        arrival_time = datetime.fromisoformat(trip["arrival_time"])
        if arrival_time <= now:
            db.mark_trip_reminded(trip["id"])
            continue

        reminder_time = calculate_reminder_time(
            arrival_time=arrival_time,
            travel_time_minutes=trip["travel_time_minutes"],
            user_reminder_minutes=trip["reminder_minutes"],
            buffer_percent=trip["buffer_percent"],
        )

        if reminder_time <= now:
            await bot.send_message(
                trip["user_id"],
                "Пора собираться в поездку!\n\n"
                f"Откуда: {trip['origin']}\n"
                f"Куда: {trip['destination']}\n"
                f"Прибытие: {arrival_time.strftime('%d.%m.%Y %H:%M')}\n"
                f"Транспорт: {transport_label(trip['transport_type'])}\n"
                f"Время в пути: {trip['travel_time_minutes']} мин\n"
                f"Запас: {trip['buffer_percent']}%",
            )
            db.mark_trip_reminded(trip["id"])


async def reminder_loop(
    bot: Bot,
    db: Database,
) -> None:
    while True:
        try:
            await check_and_send_reminders(bot, db)
        except Exception as error:
            # Фоновая проверка не должна останавливать всего бота из-за одной ошибки.
            logger.exception("Reminder loop error: %s", error)
        await asyncio.sleep(60)
