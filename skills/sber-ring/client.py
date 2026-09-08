"""Life Balance (Sber Ring) HTTP client — zero external dependencies."""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

BASE_URL = "https://app.life-balance.tech/external/sync"

VALID_TYPES = frozenset(
    ["HEART_RATE", "HRV", "SPO2", "SLEEP", "STEP", "STRESS", "TEMPERATURE"]
)

# The Life Balance API expects lowercase URL path segments.
_TYPE_TO_PATH: dict[str, str] = {
    "HEART_RATE": "heartrate",
    "HRV": "hrv",
    "SPO2": "spo2",
    "SLEEP": "sleep",
    "STEP": "step",
    "STRESS": "stress",
    "TEMPERATURE": "temperature",
}


def fetch(
    token: str,
    data_type: str,
    ts_from: int | None = None,
    ts_to: int | None = None,
    page: int = 0,
    page_size: int = 100,
    timeout_sec: int = 30,
) -> dict[str, Any]:
    """Fetch a single data type from the Life Balance API.

    Returns the parsed JSON response dict on success.
    Raises ``RuntimeError`` with a human-readable message on failure.
    """
    data_type = data_type.upper()
    if data_type not in VALID_TYPES:
        raise ValueError(
            f"Unknown data_type '{data_type}'. "
            f"Valid: {', '.join(sorted(VALID_TYPES))}"
        )

    params: list[str] = [f"page={page}", f"pageSize={page_size}"]
    if ts_from is not None:
        params.append(f"from={ts_from}")
    if ts_to is not None:
        params.append(f"to={ts_to}")

    path_segment = _TYPE_TO_PATH[data_type]
    url = f"{BASE_URL}/{path_segment}?{'&'.join(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Life Balance API returned HTTP {exc.code} for {data_type}: "
            f"{exc.read().decode('utf-8', errors='replace')[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Network error fetching {data_type}: {exc.reason}"
        ) from exc
