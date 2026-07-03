import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def init_db(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    buffer_percent INTEGER DEFAULT 10
                )
                """
            )
            # Схема сразу содержит новые поля, а ниже миграция добавит их в старую БД.
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    origin TEXT,
                    origin_latitude REAL,
                    origin_longitude REAL,
                    destination TEXT,
                    latitude REAL,
                    longitude REAL,
                    arrival_time TEXT,
                    transport_type TEXT,
                    reminder_minutes INTEGER,
                    buffer_percent INTEGER,
                    travel_time_minutes INTEGER,
                    route_checked_at TEXT,
                    reminded INTEGER DEFAULT 0,
                    created_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (telegram_id)
                )
                """
            )
            self._migrate_trips_table()

    def _migrate_trips_table(self) -> None:
        existing_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(trips)").fetchall()
        }
        migrations = {
            "origin": "ALTER TABLE trips ADD COLUMN origin TEXT",
            "origin_latitude": "ALTER TABLE trips ADD COLUMN origin_latitude REAL",
            "origin_longitude": "ALTER TABLE trips ADD COLUMN origin_longitude REAL",
            "latitude": "ALTER TABLE trips ADD COLUMN latitude REAL",
            "longitude": "ALTER TABLE trips ADD COLUMN longitude REAL",
            "travel_time_minutes": "ALTER TABLE trips ADD COLUMN travel_time_minutes INTEGER",
            "route_checked_at": "ALTER TABLE trips ADD COLUMN route_checked_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in existing_columns:
                self.connection.execute(sql)

    def ensure_user(self, telegram_id: int) -> sqlite3.Row:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO users (telegram_id) VALUES (?)",
                (telegram_id,),
            )
        return self.get_user(telegram_id)

    def get_user(self, telegram_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT telegram_id, buffer_percent FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if row is None:
            return self.ensure_user(telegram_id)
        return row

    def update_user_buffer(self, telegram_id: int, buffer_percent: int) -> None:
        self.ensure_user(telegram_id)
        with self.connection:
            self.connection.execute(
                "UPDATE users SET buffer_percent = ? WHERE telegram_id = ?",
                (buffer_percent, telegram_id),
            )

    def create_trip(
        self,
        user_id: int,
        origin: str,
        origin_latitude: float,
        origin_longitude: float,
        destination: str,
        latitude: float,
        longitude: float,
        arrival_time: str,
        transport_type: str,
        reminder_minutes: int,
        buffer_percent: int,
        travel_time_minutes: int,
    ) -> int:
        created_at = datetime.now().isoformat(timespec="seconds")
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO trips (
                    user_id, origin, origin_latitude, origin_longitude,
                    destination, latitude, longitude, arrival_time, transport_type,
                    reminder_minutes, buffer_percent, travel_time_minutes,
                    route_checked_at, reminded, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    user_id,
                    origin,
                    origin_latitude,
                    origin_longitude,
                    destination,
                    latitude,
                    longitude,
                    arrival_time,
                    transport_type,
                    reminder_minutes,
                    buffer_percent,
                    travel_time_minutes,
                    created_at,
                    created_at,
                ),
            )
        return int(cursor.lastrowid)

    def get_trip(self, trip_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

    def get_user_trips(self, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM trips
                WHERE user_id = ?
                ORDER BY arrival_time ASC
                """,
                (user_id,),
            ).fetchall()
        )

    def get_unreminded_trips(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM trips WHERE reminded = 0 ORDER BY arrival_time ASC"
            ).fetchall()
        )

    def update_trip_field(self, trip_id: int, field: str, value: Any) -> None:
        allowed_fields = {
            "origin",
            "origin_latitude",
            "origin_longitude",
            "destination",
            "latitude",
            "longitude",
            "arrival_time",
            "transport_type",
            "reminder_minutes",
            "travel_time_minutes",
            "route_checked_at",
        }
        if field not in allowed_fields:
            raise ValueError(f"Field {field} cannot be updated")

        # Имя поля проверяется через allow-list выше, поэтому SQL-инъекции здесь нет.
        with self.connection:
            self.connection.execute(
                f"UPDATE trips SET {field} = ?, reminded = 0 WHERE id = ?",
                (value, trip_id),
            )

    def update_trip_route(
        self,
        trip_id: int,
        travel_time_minutes: int,
        route_checked_at: str | None = None,
    ) -> None:
        checked_at = route_checked_at or datetime.now().isoformat(timespec="seconds")
        with self.connection:
            self.connection.execute(
                """
                UPDATE trips
                SET travel_time_minutes = ?, route_checked_at = ?, reminded = 0
                WHERE id = ?
                """,
                (travel_time_minutes, checked_at, trip_id),
            )

    def delete_trip(self, trip_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM trips WHERE id = ?", (trip_id,))

    def mark_trip_reminded(self, trip_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE trips SET reminded = 1 WHERE id = ?",
                (trip_id,),
            )
