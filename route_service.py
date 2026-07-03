import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp

from config import DGIS_API_KEY


DEFAULT_CITY = "Томск"
GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
PUBLIC_TRANSPORT_URL = "https://routing.api.2gis.com/public_transport/2.0"


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
    pass


class AddressNotFoundError(RouteServiceError):
    pass


class ApiLimitError(RouteServiceError):
    pass


class ApiUnavailableError(RouteServiceError):
    pass


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

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        message = data.get("message") or data.get("error") or "Ошибка 2ГИС."
                        raise RouteServiceError(message)
                    return data
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError):
            raise ApiUnavailableError("Нет соединения с 2ГИС. Проверьте интернет.")
        except aiohttp.ClientError as error:
            raise ApiUnavailableError(f"Ошибка сети при обращении к 2ГИС: {error}")

    async def geocode_address(self, address: str) -> GeocodedAddress:
        data = await self._request_json(
            "GET",
            GEOCODER_URL,
            params={
                "q": self._address_query(address),
                "fields": "items.point",
            },
        )

        meta_code = data.get("meta", {}).get("code")
        if meta_code == 429:
            raise ApiLimitError("Превышен лимит запросов 2ГИС.")
        if meta_code and meta_code != 200:
            raise RouteServiceError("2ГИС вернул ошибку при поиске адреса.")

        items = data.get("result", {}).get("items") or []
        if not items:
            raise AddressNotFoundError("Адрес не найден.")

        item = items[0]
        point = item.get("point") or {}
        latitude = point.get("lat")
        longitude = point.get("lon")
        if latitude is None or longitude is None:
            raise AddressNotFoundError("Адрес найден, но координаты отсутствуют.")

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

        if data.get("status") not in (None, "OK") or data.get("type") == "error":
            message = data.get("message") or "2ГИС не смог построить маршрут."
            raise RouteServiceError(message)

        routes = data.get("result") or []
        if not routes:
            raise RouteServiceError("2ГИС не вернул варианты маршрута.")

        duration_seconds = routes[0].get("total_duration")
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
            print("2GIS public transport response:", data)
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
