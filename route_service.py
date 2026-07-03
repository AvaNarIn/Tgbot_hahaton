import asyncio
import logging
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp

from config import DGIS_API_KEY


DEFAULT_CITY = "Томск"
GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
PUBLIC_TRANSPORT_URL = "https://routing.api.2gis.com/public_transport/2.0"
logger = logging.getLogger(__name__)
EXACT_ADDRESS_NOT_FOUND_MESSAGE = "Точный адрес не найден. Проверьте улицу и номер дома."


class TransportType(str, Enum):
    CAR = "auto"
    PUBLIC = "public"
    WALK = "walk"


TRANSPORT_LABELS = {
    TransportType.CAR.value: "Авто",
    TransportType.PUBLIC.value: "Общественный транспорт",
    TransportType.WALK.value: "Пешком",
}


class RouteServiceError(Exception):
    """Base error for route service failures."""


class AddressNotFoundError(RouteServiceError):
    """Raised when 2GIS cannot find coordinates for an address."""


class ApiLimitError(RouteServiceError):
    """Raised when 2GIS request limit is exceeded."""


class ApiUnavailableError(RouteServiceError):
    """Raised when 2GIS is unavailable or network request fails."""


@dataclass(frozen=True)
class GeocodedAddress:
    address: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RouteEstimate:
    transport_type: str
    duration_minutes: int


class RouteService:
    def __init__(self, api_key: str | None = DGIS_API_KEY) -> None:
        self.api_key = api_key

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ApiUnavailableError("Не задан DGIS_API_KEY в .env.")

    def _address_query(self, address: str) -> str:
        cleaned = address.strip()
        if DEFAULT_CITY.lower() in cleaned.lower():
            return cleaned
        return f"{DEFAULT_CITY}, {cleaned}"

    def _normalize_address_text(self, value: str) -> str:
        normalized = value.lower().replace("ё", "е")
        normalized = re.sub(r"[.,;:()]", " ", normalized)
        normalized = re.sub(r"\bул\b|\bулица\b", " ", normalized)
        normalized = re.sub(r"\bпр\b|\bпроспект\b", " ", normalized)
        normalized = re.sub(r"\bпер\b|\bпереулок\b", " ", normalized)
        normalized = re.sub(r"\bг\b|\bгород\b", " ", normalized)
        normalized = re.sub(r"\bдом\b|\bд\b", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip()

    def _extract_house_number(self, value: str) -> str | None:
        normalized = value.lower().replace("ё", "е")
        match = re.search(
            r"(?:\bд\.?\s*)?(\d+[а-яa-z]?(?:[/-]\d+[а-яa-z]?)?)\b",
            normalized,
        )
        return match.group(1) if match else None

    def _extract_street_tokens(self, value: str) -> set[str]:
        normalized = self._normalize_address_text(value)
        house_number = self._extract_house_number(normalized)
        if house_number:
            normalized = re.sub(rf"\b{re.escape(house_number)}\b", " ", normalized)
        normalized = normalized.replace(DEFAULT_CITY.lower(), " ")
        return {
            token
            for token in normalized.split()
            if len(token) > 1 and not token.isdigit()
        }

    def _item_text(self, item: dict[str, Any]) -> str:
        text_parts = []
        for field in (
            "full_name",
            "address_name",
            "name",
            "purpose_name",
            "adm_div",
        ):
            value = item.get(field)
            if isinstance(value, str):
                text_parts.append(value)
            elif isinstance(value, list):
                text_parts.extend(
                    part.get("name", "")
                    for part in value
                    if isinstance(part, dict)
                )
        return " ".join(part for part in text_parts if part)

    def _item_city(self, item: dict[str, Any]) -> str:
        adm_div = item.get("adm_div")
        if isinstance(adm_div, list):
            for part in adm_div:
                if isinstance(part, dict):
                    name = part.get("name")
                    if isinstance(name, str) and DEFAULT_CITY.lower() in name.lower():
                        return name
        return self._item_text(item)

    def _item_type_is_address(self, item: dict[str, Any]) -> bool:
        known_type_fields = ("type", "subtype", "purpose_code")
        allowed_types = {
            "address",
            "building",
            "house",
            "adm_div.building",
        }
        found_type = False

        for field in known_type_fields:
            value = item.get(field)
            if not isinstance(value, str):
                continue

            found_type = True
            normalized_value = value.lower()
            if normalized_value in allowed_types:
                return True
            if any(address_type in normalized_value for address_type in allowed_types):
                return True

        return not found_type

    def _is_exact_address_match(self, user_input: str, item: dict[str, Any]) -> bool:
        if not self._item_type_is_address(item):
            return False

        point = item.get("point") or {}
        if point.get("lat") is None or point.get("lon") is None:
            return False

        city_text = self._normalize_address_text(self._item_city(item))
        if self._normalize_address_text(DEFAULT_CITY) not in city_text:
            return False

        user_house = self._extract_house_number(user_input)
        if not user_house:
            return False

        item_text = self._item_text(item)
        item_house = self._extract_house_number(item_text)
        if item_house != user_house:
            return False

        user_street_tokens = self._extract_street_tokens(user_input)
        item_street_tokens = self._extract_street_tokens(item_text)
        if not user_street_tokens or not item_street_tokens:
            return False

        return user_street_tokens.issubset(item_street_tokens)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        self._ensure_api_key()
        request_params = {"key": self.api_key}
        if params:
            request_params.update(params)

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, url, params=request_params, json=json
                ) as response:
                    if response.status == 429:
                        raise ApiLimitError("Превышен лимит запросов 2ГИС.")
                    if response.status >= 500:
                        raise ApiUnavailableError("2ГИС временно недоступен.")

                    try:
                        data = await response.json(content_type=None)
                    except ValueError:
                        raise RouteServiceError("2ГИС вернул некорректный ответ.")

                    if response.status >= 400:
                        if isinstance(data, dict):
                            message = (
                                data.get("message")
                                or data.get("error")
                                or "Ошибка 2ГИС."
                            )
                        else:
                            message = "Ошибка 2ГИС."
                        raise RouteServiceError(message)
                    return data
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError):
            raise ApiUnavailableError("Нет соединения с 2ГИС. Проверьте интернет.")
        except aiohttp.ClientError as error:
            raise ApiUnavailableError(f"Ошибка сети при обращении к 2ГИС: {error}")

    async def geocode_address(self, address: str) -> GeocodedAddress:
        if not address.strip():
            raise AddressNotFoundError("Адрес пустой.")

        data = await self._request_json(
            "GET",
            GEOCODER_URL,
            params={
                "q": self._address_query(address),
                "fields": "items.point",
            },
        )

        if not isinstance(data, dict):
            raise RouteServiceError("2ГИС вернул некорректный ответ при поиске адреса.")

        meta_code = data.get("meta", {}).get("code")
        if meta_code == 429:
            raise ApiLimitError("Превышен лимит запросов 2ГИС.")
        if meta_code and meta_code != 200:
            raise RouteServiceError("2ГИС вернул ошибку при поиске адреса.")

        items = data.get("result", {}).get("items") or []
        if not items:
            raise AddressNotFoundError(EXACT_ADDRESS_NOT_FOUND_MESSAGE)

        item = next(
            (
                candidate
                for candidate in items
                if isinstance(candidate, dict)
                and self._is_exact_address_match(address, candidate)
            ),
            None,
        )
        if item is None:
            raise AddressNotFoundError(EXACT_ADDRESS_NOT_FOUND_MESSAGE)

        point = item.get("point") or {}
        latitude = point.get("lat")
        longitude = point.get("lon")
        if latitude is None or longitude is None:
            raise AddressNotFoundError(EXACT_ADDRESS_NOT_FOUND_MESSAGE)

        return GeocodedAddress(
            address=item.get("full_name") or item.get("address_name") or address.strip(),
            latitude=float(latitude),
            longitude=float(longitude),
        )

    async def get_travel_time_minutes(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
        transport_type: str,
    ) -> RouteEstimate:
        if transport_type == TransportType.PUBLIC.value:
            return await self._get_public_transport_time_minutes(
                origin_latitude=origin_latitude,
                origin_longitude=origin_longitude,
                destination_latitude=destination_latitude,
                destination_longitude=destination_longitude,
            )

        routing_transport = self._routing_transport(transport_type)
        data = await self._request_json(
            "POST",
            ROUTING_URL,
            json={
                "locale": "ru",
                "route_mode": "fastest",
                "traffic_mode": "jam",
                "transport": routing_transport,
                "points": [
                    {
                        "lat": origin_latitude,
                        "lon": origin_longitude,
                        "type": "walking",
                    },
                    {
                        "lat": destination_latitude,
                        "lon": destination_longitude,
                        "type": "walking",
                    },
                ],
            },
        )

        if not isinstance(data, dict):
            raise RouteServiceError("2ГИС вернул некорректный ответ при расчете маршрута.")

        if data.get("status") not in (None, "OK") or data.get("type") == "error":
            message = data.get("message") or "2ГИС не смог построить маршрут."
            raise RouteServiceError(message)

        routes = data.get("result") or []
        if not routes:
            raise RouteServiceError("2ГИС не вернул варианты маршрута.")

        route = routes[0]
        if not isinstance(route, dict):
            raise RouteServiceError("2ГИС вернул некорректный маршрут.")

        duration_seconds = route.get("total_duration")
        if duration_seconds is None:
            raise RouteServiceError("2ГИС не вернул время маршрута.")

        return RouteEstimate(
            transport_type=transport_type,
            duration_minutes=max(1, math.ceil(float(duration_seconds) / 60)),
        )

    async def _get_public_transport_time_minutes(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
    ) -> RouteEstimate:
        data = await self._request_json(
            "POST",
            PUBLIC_TRANSPORT_URL,
            json={
                "source": {
                    "point": {
                        "lat": origin_latitude,
                        "lon": origin_longitude,
                    }
                },
                "target": {
                    "point": {
                        "lat": destination_latitude,
                        "lon": destination_longitude,
                    }
                },
                "transport": [
                    "bus",
                    "trolleybus",
                    "tram",
                    "shuttle_bus",
                ],
                "locale": "ru",
                "max_result_count": 1,
            },
        )

        if isinstance(data, dict):
            if data.get("status") not in (None, "OK") or data.get("type") == "error":
                message = data.get("message") or "2ГИС не смог построить маршрут."
                raise RouteServiceError(message)

        route = self._get_first_public_transport_route(data)
        duration_seconds = None
        if isinstance(route, dict):
            duration_seconds = route.get("total_duration")

        if duration_seconds is None:
            duration_seconds = self._extract_public_transport_duration_seconds(route)

        if duration_seconds is None:
            logger.warning("2GIS public transport response: %s", data)
            raise RouteServiceError(
                "2ГИС не вернул общее время маршрута общественного транспорта."
            )

        return RouteEstimate(
            transport_type=TransportType.PUBLIC.value,
            duration_minutes=max(1, math.ceil(float(duration_seconds) / 60)),
        )

    def _get_first_public_transport_route(self, data: Any) -> Any:
        if isinstance(data, list):
            return data[0] if data else None

        if isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, list):
                return result[0] if result else None
            if isinstance(result, dict):
                routes = result.get("routes")
                if isinstance(routes, list):
                    return routes[0] if routes else None

            routes = data.get("routes")
            if isinstance(routes, list):
                return routes[0] if routes else None

        return data

    def _extract_public_transport_duration_seconds(
        self, data: Any
    ) -> float | None:
        duration_fields = {
            "total_duration",
            "duration",
            "duration_seconds",
            "total_time",
            "route_duration",
            "travel_time",
        }

        if isinstance(data, dict):
            for key, value in data.items():
                normalized_key = key.lower()
                if (
                    normalized_key in duration_fields
                    or "duration" in normalized_key
                    or "time" in normalized_key
                ) and isinstance(value, (int, float)):
                    return float(value)

                nested_value = self._extract_public_transport_duration_seconds(value)
                if nested_value is not None:
                    return nested_value

        if isinstance(data, list):
            for item in data:
                nested_value = self._extract_public_transport_duration_seconds(item)
                if nested_value is not None:
                    return nested_value

        return None

    def _routing_transport(self, transport_type: str) -> str:
        if transport_type == TransportType.WALK.value:
            return "walking"
        return "driving"


def transport_label(transport_type: str) -> str:
    return TRANSPORT_LABELS.get(transport_type, transport_type)
