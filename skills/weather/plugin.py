"""Rich weather extension for route/tool/widget PluginAPI surfaces.

Network access is bounded to wttr.in by host+scheme checks and redirect refusal.
The widget remains declarative and text-only: no bundled binary icons, no API key,
no custom browser JavaScript.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse


_ALLOWED_HOST = "wttr.in"
_TIMEOUT_SEC = 10
_USER_AGENT = "Ouroboros-Weather/0.3"
_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX_AGE_SEC = 6 * 60 * 60
_STATE_DIR: Optional[Path] = None


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects from the allowed weather endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target != _ALLOWED_HOST:
            raise urllib.error.URLError(
                f"weather: cross-host redirect refused: {target!r} not in allowlist"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StrictRedirectHandler())


_CONDITION_EMOJI = {
    "113": "☀️",
    "116": "🌤️",
    "119": "☁️",
    "122": "☁️",
    "143": "🌫️",
    "176": "🌦️",
    "179": "🌨️",
    "182": "🌧️",
    "185": "🌧️",
    "200": "⛈️",
    "227": "❄️",
    "230": "🌨️",
    "248": "🌫️",
    "260": "🌫️",
    "263": "🌦️",
    "266": "🌧️",
    "281": "🌧️",
    "284": "🌧️",
    "293": "🌦️",
    "296": "🌧️",
    "299": "🌧️",
    "302": "🌧️",
    "305": "🌧️",
    "308": "🌧️",
    "311": "🌧️",
    "314": "🌧️",
    "317": "🌧️",
    "320": "🌨️",
    "323": "🌨️",
    "326": "🌨️",
    "329": "❄️",
    "332": "❄️",
    "335": "🌨️",
    "338": "❄️",
    "350": "🌧️",
    "353": "🌦️",
    "356": "🌧️",
    "359": "🌧️",
    "362": "🌧️",
    "365": "🌧️",
    "368": "🌨️",
    "371": "🌨️",
    "374": "🌧️",
    "377": "🌧️",
    "386": "⛈️",
    "389": "⛈️",
    "392": "⛈️",
    "395": "🌨️",
}


_SEVERE_WORDS = (
    "thunder",
    "storm",
    "heavy rain",
    "heavy snow",
    "blizzard",
    "freezing",
    "torrential",
    "sleet",
)


def _fetch(city: str, units: str = "metric", *, allow_cache: bool = True) -> Dict[str, Any]:
    """Resolve rich current conditions and compact forecast for route/tool output."""
    cleaned = (city or "").strip() or "Moscow"
    unit_mode = _clean_units(units)
    if len(cleaned) > 80:
        return {"error": "city is too long"}
    cached = _read_cache(cleaned, unit_mode) if allow_cache else None
    if cached and time.time() - cached.get("cached_at_epoch", 0) < _CACHE_TTL_SEC:
        cached["cache_state"] = "fresh cache"
        _refresh_markdown_fields(cached)
        return cached
    raw_result = _fetch_raw(cleaned)
    if "error" in raw_result:
        if cached and time.time() - cached.get("cached_at_epoch", 0) < _CACHE_MAX_AGE_SEC:
            cached["cache_state"] = "stale fallback"
            cached["warning"] = f"Live refresh failed; showing cached data. {raw_result['error']}"
            _refresh_markdown_fields(cached)
            return cached
        return raw_result
    payload = _shape_payload(raw_result, cleaned, unit_mode)
    _write_cache(cleaned, unit_mode, payload)
    return payload


def _fetch_raw(cleaned: str) -> Dict[str, Any]:
    url = f"https://{_ALLOWED_HOST}/{urllib.parse.quote(cleaned)}?format=j1"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != _ALLOWED_HOST:
        return {"error": f"refusing host {parsed.netloc!r}"}
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"error": f"upstream HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"error": f"network: {exc.reason!r}"}
    except TimeoutError:
        return {"error": f"upstream timed out after {_TIMEOUT_SEC}s"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": "upstream returned non-JSON payload"}


def _shape_payload(data: Dict[str, Any], requested_city: str, units: str) -> Dict[str, Any]:
    current = (data.get("current_condition") or [{}])[0]
    nearest = (data.get("nearest_area") or [{}])[0]
    weather_days = data.get("weather") or []
    today = weather_days[0] if weather_days else {}
    astronomy = (today.get("astronomy") or [{}])[0]
    code = str(current.get("weatherCode") or "").strip()
    condition = _first_value(current.get("weatherDesc")) or "Unknown"
    temp_c = _coerce_int(current.get("temp_C"))
    feels_c = _coerce_int(current.get("FeelsLikeC"))
    wind_kph = _coerce_int(current.get("windspeedKmph"))
    humidity = _coerce_int(current.get("humidity"))
    pressure = _coerce_int(current.get("pressure"))
    visibility = _coerce_int(current.get("visibility"))
    uv_index = _coerce_int(current.get("uvIndex"))
    temp_value = _format_temperature(temp_c, units)
    feels_value = _format_temperature(feels_c, units)
    forecast_rows = _forecast_rows(weather_days, units)
    hourly_chart = _hourly_chart(today.get("hourly") or [], units)
    alert_level, alert_text = _alert(condition, wind_kph, humidity, uv_index)
    resolved_to = _join_nonempty(_first_value(nearest.get("areaName")), _first_value(nearest.get("country")))
    coordinates = _join_nonempty(
        str(nearest.get("latitude") or "").strip(),
        str(nearest.get("longitude") or "").strip(),
        sep=", ",
    )
    wind_dir = str(current.get("winddir16Point") or "").strip()
    payload = {
        "city": requested_city,
        "resolved_to": resolved_to or requested_city,
        "country": _first_value(nearest.get("country")),
        "coordinates": coordinates,
        "unit_mode": "°F" if units == "imperial" else "°C",
        "temp_c": temp_c,
        "feels_like_c": feels_c,
        "temp_display": temp_value,
        "feels_like_display": feels_value,
        "condition": condition,
        "condition_emoji": _CONDITION_EMOJI.get(code, "🌡️"),
        "humidity_pct": humidity,
        "humidity_display": f"{humidity}%",
        "wind_kph": wind_kph,
        "wind_display": f"{_format_wind(wind_kph, units)} {wind_dir}".strip(),
        "wind_dir": wind_dir,
        "pressure_hpa": pressure,
        "pressure_display": _format_pressure(pressure, units),
        "visibility_km": visibility,
        "visibility_display": _format_visibility(visibility, units),
        "uv_index": uv_index,
        "uv_display": str(uv_index) if uv_index else "—",
        "observation_time": str(current.get("observation_time") or "").strip(),
        "sunrise": str(astronomy.get("sunrise") or "").strip(),
        "sunset": str(astronomy.get("sunset") or "").strip(),
        "moon_phase": str(astronomy.get("moon_phase") or "").strip(),
        "moon_illumination": str(astronomy.get("moon_illumination") or "").strip(),
        "forecast_rows": forecast_rows,
        "hourly_chart": hourly_chart,
        "alert_level": alert_level,
        "alert_text": alert_text,
        "warning": "",
        "cache_state": "live",
        "cached_at_epoch": time.time(),
    }
    payload["astro_markdown"] = _astro_markdown(payload)
    payload["comfort_index"] = _comfort_index(temp_c, humidity, wind_kph)
    payload["comfort_display"] = f"{payload['comfort_index']}/100"
    _refresh_markdown_fields(payload)
    return payload


def _forecast_rows(days: Iterable[Dict[str, Any]], units: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for day in list(days)[:5]:
        hourly = day.get("hourly") or []
        midday = _pick_midday(hourly)
        code = str(midday.get("weatherCode") or "").strip()
        desc = _compact_condition(_first_value(midday.get("weatherDesc")) or "—")
        high = _format_temperature(_coerce_int(day.get("maxtempC")), units)
        low = _format_temperature(_coerce_int(day.get("mintempC")), units)
        rain_chance = f"{_coerce_int(midday.get('chanceofrain'))}%"
        wind = _format_wind(_coerce_int(midday.get('windspeedKmph')), units)
        rows.append(
            {
                "date": str(day.get("date") or "—"),
                "day": _short_date(str(day.get("date") or "")),
                "condition": f"{_CONDITION_EMOJI.get(code, '🌡️')} {desc}",
                "outlook": f"{_CONDITION_EMOJI.get(code, '🌡️')} {desc} · {rain_chance}",
                "high": high,
                "low": low,
                "range": f"{low}–{high}",
                "rain_chance": rain_chance,
                "snow_chance": f"{_coerce_int(midday.get('chanceofsnow'))}%",
                "wind": wind,
            }
        )
    return rows


def _hourly_chart(hourly: Iterable[Dict[str, Any]], units: str) -> Dict[str, Any]:
    points = list(hourly)[::2] or list(hourly)
    labels: List[str] = []
    temps: List[float] = []
    rain: List[float] = []
    for item in points[:8]:
        labels.append(_hour_label(str(item.get("time") or "0")))
        temp_c = _coerce_int(item.get("tempC"))
        temps.append(_c_to_f(temp_c) if units == "imperial" else temp_c)
        rain.append(_coerce_int(item.get("chanceofrain")))
    return {
        "labels": labels,
        "datasets": [
            {"label": "Temp", "data": temps},
            {"label": "Rain %", "data": rain},
        ],
    }


def _pick_midday(hourly: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not hourly:
        return {}
    return min(hourly, key=lambda item: abs(_coerce_int(item.get("time")) - 1200))


def _compact_condition(value: str, *, limit: int = 22) -> str:
    text = " ".join(str(value or "—").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _weather_summary(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """A one-row cockpit summary rendered by the host table component."""
    return [
        {
            "now": _hero_markdown(payload),
            "feels": str(payload.get("feels_like_display") or "—"),
            "wind": str(payload.get("wind_display") or "—"),
            "humidity": str(payload.get("humidity_display") or "—"),
        }
    ]


def _metric_chips(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Compact key/value rows; denser than six large markdown lines."""
    return [
        {"label": "Comfort", "value": str(payload.get("comfort_display") or "—")},
        {"label": "Pressure", "value": str(payload.get("pressure_display") or "—")},
        {"label": "Visibility", "value": str(payload.get("visibility_display") or "—")},
        {"label": "UV", "value": str(payload.get("uv_display") or "—")},
        {"label": "Cache", "value": str(payload.get("cache_state") or "live")},
    ]


def _hero_markdown(payload: Dict[str, Any]) -> str:
    emoji = str(payload.get("condition_emoji") or "🌡️")
    temp = str(payload.get("temp_display") or "—")
    condition = _compact_condition(str(payload.get("condition") or "Weather"), limit=26)
    resolved_to = str(payload.get("resolved_to") or payload.get("city") or "Selected city")
    return f"{emoji} {temp} · {condition} · {resolved_to}"


def _advisory_markdown(payload: Dict[str, Any]) -> str:
    warning = payload.get("warning")
    prefix = "⚠️" if payload.get("alert_level") == "watch" else "🟢"
    text = str(payload.get("alert_text") or "No active weather watch.")
    pieces = [f"{prefix} {text}"]
    if warning:
        pieces.append(f"  \n{warning}")
    return "".join(pieces)


def _refresh_markdown_fields(payload: Dict[str, Any]) -> None:
    _normalize_forecast_ranges(payload)
    payload["weather_summary"] = _weather_summary(payload)
    payload["metric_chips"] = _metric_chips(payload)
    payload["hero_markdown"] = _hero_markdown(payload)
    payload["advisory_markdown"] = _advisory_markdown(payload)


def _normalize_forecast_ranges(payload: Dict[str, Any]) -> None:
    rows = payload.get("forecast_rows")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        low = str(row.get("low") or "").strip()
        high = str(row.get("high") or "").strip()
        if not row.get("range"):
            if low and high:
                row["range"] = f"{low}–{high}"
            else:
                row["range"] = low or high or "—"
        if not row.get("day"):
            row["day"] = _short_date(str(row.get("date") or ""))
        if not row.get("outlook"):
            condition = _compact_condition(str(row.get("condition") or "—").strip())
            rain = str(row.get("rain_chance") or "—").strip()
            row["outlook"] = f"{condition} · {rain}"


def _astro_markdown(payload: Dict[str, Any]) -> str:
    return (
        f"🌅 Sunrise **{payload.get('sunrise') or '—'}** · 🌇 sunset **{payload.get('sunset') or '—'}**\n\n"
        f"🌙 Moon: **{payload.get('moon_phase') or '—'}**, illumination **{payload.get('moon_illumination') or '—'}%**\n\n"
        f"📍 Coordinates: `{payload.get('coordinates') or 'unknown'}`"
    )


def _alert(condition: str, wind_kph: int, humidity: int, uv_index: int) -> tuple[str, str]:
    lowered = condition.lower()
    if any(word in lowered for word in _SEVERE_WORDS):
        return "watch", "Weather watch: conditions may disrupt travel or outdoor plans."
    if wind_kph >= 45:
        return "watch", "Wind watch: secure loose items and expect difficult cycling/walking."
    if uv_index >= 8:
        return "watch", "UV watch: use shade and sun protection during midday."
    if humidity >= 90:
        return "watch", "Moisture watch: high humidity may make the air feel heavier."
    return "clear", "No severe signal in the current public wttr.in report."


def _comfort_index(temp_c: int, humidity: int, wind_kph: int) -> int:
    score = 100
    score -= min(45, abs(temp_c - 21) * 2)
    score -= max(0, humidity - 60) // 2
    score -= max(0, wind_kph - 18) // 2
    return max(0, min(100, score))


def _format_temperature(temp_c: int, units: str) -> str:
    if units == "imperial":
        return f"{round(_c_to_f(temp_c))}°F"
    return f"{temp_c}°C"


def _format_wind(wind_kph: int, units: str) -> str:
    if units == "imperial":
        return f"{round(wind_kph * 0.621371)} mph"
    return f"{wind_kph} km/h"


def _format_visibility(visibility_km: int, units: str) -> str:
    if visibility_km <= 0:
        return "—"
    if units == "imperial":
        return f"{visibility_km * 0.621371:.1f} mi"
    return f"{visibility_km} km"


def _format_pressure(pressure_hpa: int, units: str) -> str:
    if pressure_hpa <= 0:
        return "—"
    if units == "imperial":
        return f"{pressure_hpa * 0.02953:.2f} inHg"
    return f"{pressure_hpa} hPa"


def _c_to_f(temp_c: int) -> float:
    return temp_c * 9 / 5 + 32


def _hour_label(raw: str) -> str:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    hour = value // 100
    return f"{hour:02d}:00"


def _short_date(raw: str) -> str:
    parts = (raw or "").split("-")
    if len(parts) == 3:
        return f"{parts[2]}.{parts[1]}"
    return raw or "—"


def _first_value(items: Any) -> str:
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            return str(first.get("value") or "").strip()
        return str(first or "").strip()
    return ""


def _join_nonempty(*items: str, sep: str = ", ") -> str:
    return sep.join(item for item in (str(x).strip() for x in items) if item)


def _coerce_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clean_units(value: str) -> str:
    normalized = (value or "metric").strip().lower()
    return "imperial" if normalized in {"imperial", "f", "fahrenheit"} else "metric"


def _cache_path(city: str, units: str) -> Optional[Path]:
    if _STATE_DIR is None:
        return None
    safe = "".join(ch if ch.isalnum() else "_" for ch in city.lower()).strip("_")[:50] or "weather"
    return _STATE_DIR / f"cache_{safe}_{units}.json"


def _read_cache(city: str, units: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(city, units)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_cache(city: str, units: str, payload: Dict[str, Any]) -> None:
    path = _cache_path(city, units)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Cache is a convenience. Route/tool output must not fail because state write failed.
        return


async def _route_forecast(request: Request) -> JSONResponse:
    """GET forecast route; blocking urllib work runs off the event loop."""
    city = (request.query_params.get("city") or "Moscow").strip()
    units = _clean_units(request.query_params.get("units") or "metric")
    payload = await asyncio.to_thread(_fetch, city, units)
    status = 200 if "error" not in payload else 502
    return JSONResponse(payload, status_code=status)


def _tool_fetch(*, city: str = "", units: str = "metric") -> str:
    """Agent-callable tool. Returns a JSON string for the chat surface."""
    payload = _fetch(city or "Moscow", units)
    return json.dumps(payload, ensure_ascii=False)


def _widget_render() -> Dict[str, Any]:
    return {
        "kind": "declarative",
        "schema_version": 1,
        "span": 2,
        "components": [
            {
                "type": "form",
                "route": "forecast",
                "method": "GET",
                "target": "result",
                "submit_label": "Update",
                "fields": [
                    {"name": "city", "label": "City", "type": "text", "default": "Moscow", "required": True},
                ],
            },
            {
                "type": "table",
                "target": "result",
                "path": "weather_summary",
                "columns": [
                    {"label": "Now", "path": "now"},
                    {"label": "Feels", "path": "feels"},
                    {"label": "Wind", "path": "wind"},
                    {"label": "Humidity", "path": "humidity"},
                ],
            },
            {"type": "markdown", "target": "result", "path": "advisory_markdown"},
            {"type": "key_value", "target": "result", "path": "metric_chips"},
            {
                "type": "tabs",
                "target": "result",
                "tabs": [
                    {
                        "label": "Forecast",
                        "components": [
                            {
                                "type": "table",
                                "path": "forecast_rows",
                                "columns": [
                                    {"label": "Day", "path": "day"},
                                    {"label": "Sky", "path": "outlook"},
                                    {"label": "Temp", "path": "range"},
                                ],
                            }
                        ],
                    },
                    {
                        "label": "Hourly",
                        "components": [
                            {"type": "chart", "path": "hourly_chart", "chart_type": "line"}
                        ],
                    },
                    {
                        "label": "Details",
                        "components": [
                            {
                                "type": "kv",
                                "fields": [
                                    {"label": "Location", "path": "resolved_to"},
                                    {"label": "Temperature", "path": "temp_display"},
                                    {"label": "Feels like", "path": "feels_like_display"},
                                    {"label": "Condition", "path": "condition"},
                                    {"label": "Cache", "path": "cache_state"},
                                ],
                            },
                            {"type": "markdown", "path": "astro_markdown"},
                        ],
                    },
                ],
            },
        ],
    }


def register(api: Any) -> None:
    """PluginAPI entry point called once per extension load."""
    global _STATE_DIR
    try:
        _STATE_DIR = Path(api.get_state_dir())
    except Exception:
        _STATE_DIR = None
    api.register_tool(
        "fetch",
        _tool_fetch,
        description=(
            "Fetch a rich current weather and compact forecast dashboard for a city via wttr.in. "
            "Returns JSON with current conditions, comfort index, wind, pressure, UV, astronomy, "
            "hourly chart data, and a 5-day forecast table."
        ),
        schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name to look up (e.g., Moscow, Tokyo)."},
                "units": {"type": "string", "description": "metric or imperial", "default": "metric"},
            },
            "required": ["city"],
        },
        timeout_sec=15,
    )
    api.register_route("forecast", _route_forecast, methods=("GET",))
    api.register_ui_tab("widget", "Weather", icon="cloud", render=_widget_render())
    api.log("info", "weather: compact extension registered (route, tool, ui_tab, cache)")


__all__ = ["register"]
