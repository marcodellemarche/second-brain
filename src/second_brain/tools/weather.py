"""The weather tool: a place name in, a short daily forecast out.

Open-Meteo needs no key, so this module holds no credential: the only egress is the geocoding API
and the forecast API. The two calls are chained here instead of being two tools, so the model
never has to hold coordinates: it asks for a place, the tool resolves it and returns the days.

`place` is validated before it reaches the network. The geocoding endpoint takes free text, so
anything but a plain place name (a URL, a digit, punctuation beyond a comma) is refused instead of
being forwarded. The resolved name and country come from the service and are only a label.

The fetch function is injectable, so the tests run the whole flow without a network. Error codes:
`not_found` when the geocoder knows no such place, `backend_error` for anything else that goes
wrong on the way.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from second_brain.core.types import Autonomy, Effect, ToolSpec
from second_brain.tools.registry import Tool
from second_brain.tools.vault import Provenance

WEATHER_HOSTS: tuple[str, ...] = ("geocoding-api.open-meteo.com", "api.open-meteo.com")
_WEATHER_GEOCODING = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_FORECAST = "https://api.open-meteo.com/v1/forecast"
_DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
)
_PLACE_SEPARATORS = frozenset(" ,'-’")
_SPACES = re.compile(r"\s+")

Fetch = Callable[[str], Any]

# WMO weather codes, as Open-Meteo reports them, folded into one short English line each.
_CONDITIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


class WeatherError(Exception):
    """A refused or failed weather lookup. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Location:
    name: str
    country: str
    latitude: float
    longitude: float


class WeatherParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place: str = Field(
        min_length=2,
        max_length=80,
        description="Name of the place, for example 'Bologna' or 'Paris, France'.",
    )
    days: int = Field(default=3, ge=1, le=7, description="How many days to forecast, 1 to 7.")

    @field_validator("place")
    @classmethod
    def _check_place(cls, value: str) -> str:
        if any(char.isspace() and char not in " " for char in value):
            raise ValueError("place must be one line")
        place = _SPACES.sub(" ", value).strip()
        if not 2 <= len(place) <= 80:
            raise ValueError("place must be between 2 and 80 characters")
        if not any(char.isalpha() for char in place):
            raise ValueError("place must contain a letter")
        if not all(char.isalpha() or char in _PLACE_SEPARATORS for char in place):
            raise ValueError("place may only contain letters, spaces, apostrophes, hyphens and commas")
        return place


def weather_description(code: int) -> str:
    return _CONDITIONS.get(code, "unknown conditions")


def _http_get(url: str, timeout: float = 10.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "second-brain"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise WeatherError("backend_error", f"open-meteo returned {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise WeatherError("backend_error", f"open-meteo unreachable: {exc}") from exc


def _payload(fetch: Fetch, url: str) -> dict[str, Any]:
    """Call the service and insist on a JSON object. Anything else is a broken backend."""
    try:
        payload = fetch(url)
    except WeatherError:
        raise
    except (OSError, ValueError) as exc:
        raise WeatherError("backend_error", f"open-meteo unreachable: {exc}") from exc
    if not isinstance(payload, dict):
        raise WeatherError("backend_error", "open-meteo returned something that is not an object")
    return payload


def _geocode(fetch: Fetch, place: str) -> Location:
    query = urllib.parse.urlencode({"name": place, "count": "1", "language": "it", "format": "json"})
    payload = _payload(fetch, f"{_WEATHER_GEOCODING}?{query}")
    results = payload.get("results")
    if not results or not isinstance(results, list):
        raise WeatherError("not_found", f"no place named {place!r}")
    first = results[0]
    try:
        return Location(
            name=str(first["name"]),
            country=str(first.get("country") or ""),
            latitude=float(first["latitude"]),
            longitude=float(first["longitude"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise WeatherError("backend_error", "the geocoding answer has no coordinates") from exc


def _at(values: list[Any], index: int) -> float | None:
    try:
        value = values[index]
    except IndexError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _series(daily: dict[str, Any], key: str) -> list[Any]:
    values = daily.get(key)
    if not isinstance(values, list):
        raise WeatherError("backend_error", f"the forecast answer has no {key}")
    return values


def _forecast(fetch: Fetch, location: Location, days: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "latitude": f"{location.latitude:.4f}",
            "longitude": f"{location.longitude:.4f}",
            "daily": ",".join(_DAILY_FIELDS),
            "forecast_days": str(days),
            "timezone": "auto",
        }
    )
    payload = _payload(fetch, f"{_WEATHER_FORECAST}?{query}")
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise WeatherError("backend_error", "the forecast answer has no daily block")
    dates = _series(daily, "time")
    if not dates:
        raise WeatherError("backend_error", "the forecast answer has no days")
    maxima = _series(daily, "temperature_2m_max")
    minima = _series(daily, "temperature_2m_min")
    rain = _series(daily, "precipitation_probability_max")
    codes = _series(daily, "weather_code")
    forecast: list[dict[str, Any]] = []
    for index, day in enumerate(dates):
        maximum = _at(maxima, index)
        minimum = _at(minima, index)
        probability = _at(rain, index)
        code = _at(codes, index)
        forecast.append(
            {
                "date": str(day),
                "temperature_max": None if maximum is None else round(maximum, 1),
                "temperature_min": None if minimum is None else round(minimum, 1),
                "precipitation_probability": None if probability is None else int(probability),
                "conditions": "unknown conditions" if code is None else weather_description(int(code)),
            }
        )
    return forecast


def _refused(exc: WeatherError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": exc.message}


def _guarded(run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a lookup, turning a refusal into a result instead of an exception."""
    try:
        return {"ok": True, **run()}
    except WeatherError as exc:
        return _refused(exc)


def weather_tools(fetch: Fetch | None = None) -> list[Tool]:
    get = fetch or _http_get

    def weather(params: BaseModel, provenance: Provenance) -> dict[str, Any]:
        assert isinstance(params, WeatherParams)

        def run() -> dict[str, Any]:
            location = _geocode(get, params.place)
            days = _forecast(get, location, params.days)
            return {
                "place": {"name": location.name, "country": location.country},
                "days": days,
            }

        return _guarded(run)

    return [
        Tool(
            spec=ToolSpec(
                name="weather",
                description=(
                    "Daily forecast for a place: maximum and minimum temperature, chance of "
                    "precipitation and a short description of the conditions, one entry per day. "
                    "The place is resolved through a public geocoding service, so pass a plain "
                    "place name only."
                ),
                params=WeatherParams,
                effect=Effect.READ,
                autonomy=Autonomy.AUTO,
                hosts=WEATHER_HOSTS,
                credentials=(),
            ),
            handler=weather,
        )
    ]
