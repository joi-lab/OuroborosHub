"""DocDoc REST API 1.0.13 tools for the doctor_booking-v4 skill.

Verified against the official OpenAPI spec:
https://apidocs.sberhealth.ru/external/partner.yaml (API Партнерское v1.0.13 DocDoc)

Key endpoints used:
  GET  /city                                              — list of cities
  GET  /speciality/city/{cityId}/onlySimple/{onlySimple}  — list of specialities
  GET  /clinic/list                                        — clinics (start/count/city/search/...)
  GET  /doctor/list                                        — doctors (start/count/city/speciality/singleClinicId/...)
  GET  /schedule/doctor/doctorIds/{ids}/days/{days}       — doctor schedule slots
  GET  /doctor/slot/list                                   — doctors with actual slots (page/city/slotsDays)
  POST /request                                            — create booking request (phone required)
  GET  /request/cancel/{requestId}                        — cancel booking by request id

Authentication: `pid` query parameter on every request.
Base URL: https://api.docdoc.ru/public/rest/1.0.13
"""
import json
import re
import urllib.error
import urllib.request
import http.cookiejar

BASE = "https://api.docdoc.ru/public/rest/1.0.13"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Pagination limits
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_SCHEDULE_DOCTORS = 50  # spec: doctorIds "не более 50"
_PHONE_RE = re.compile(r"^\\+?[0-9]{7,20}$")

# Heavy fields stripped from clinic responses to stay within process caps
_CLINIC_STRIP = frozenset({
    "Diagnostics", "Schedule", "Services", "Specialities", "Stations",
    "LowCostDoctor", "BranchesId", "logoPath", "Logo",
    "Description", "ShortDescription",
})

# Heavy fields stripped from doctor responses
_DOCTOR_STRIP = frozenset({
    "Description", "TextAbout", "Stations", "Img", "ImgFormat",
    "Telemed", "Extra", "InternalRating",
})

# Shared cookie jar + opener to pass WAF (Stormwall) challenge
_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar)
)


def _read_config(api):
    """Read the owner-granted PID without persisting it in skill state."""
    try:
        return {"pid": str(api.get_settings(["DOCDOC_PID"]).get("DOCDOC_PID") or "").strip()}
    except Exception:
        return {"pid": ""}


def _request(api, path, params=None, method="GET", data=None):
    """Send a REST request to the DocDoc API (pid query param auth)."""
    pid = _read_config(api).get("pid", "")

    if not pid:
        return {
            "status": "configuration_error",
            "message": "DOCDOC_PID is not configured. Add it in Settings → Secrets and grant this skill access.",
        }

    from urllib.parse import urlencode
    query = {"pid": pid}
    if params:
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, list):
                query[k] = ",".join(str(x) for x in v)
            else:
                query[k] = str(v)

    url = f"{BASE}/{path}?{urlencode(query)}"

    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    body = None
    if data is not None and method == "POST":
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=45) as response:
            resp_text = response.read().decode("utf-8")
            if resp_text:
                try:
                    return json.loads(resp_text)
                except (ValueError, json.JSONDecodeError):
                    return {"status": "api_error",
                            "message": "Invalid JSON response",
                            "raw": resp_text[:500]}
            return {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")[:500]
        return {"status": "api_error", "http_status": exc.code, "message": body_text}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": "status_unknown", "message": str(exc)}
    except (ValueError, json.JSONDecodeError) as exc:
        return {"status": "api_error", "message": "Invalid JSON response: " + str(exc)}


def _resolve_pagination(arguments):
    """Extract and clamp limit/offset from tool arguments."""
    try:
        limit = int(arguments.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    try:
        offset = int(arguments.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    return limit, offset


def _strip_clinic(clinic):
    """Remove heavy fields from a clinic dict."""
    return {k: v for k, v in clinic.items() if k not in _CLINIC_STRIP}


def _strip_doctor(doctor):
    """Remove heavy fields from a doctor dict."""
    return {k: v for k, v in doctor.items() if k not in _DOCTOR_STRIP}


def _paginated_response(raw, items_key, strip_fn, limit, offset):
    """Build a paginated response with Total + metadata."""
    total = 0
    items = []
    if isinstance(raw, dict):
        total = raw.get("Total", 0)
        items = raw.get(items_key, [])
    elif isinstance(raw, list):
        total = len(raw)
        items = raw

    items = [strip_fn(item) for item in items if isinstance(item, dict)]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "has_more": (offset + len(items)) < total,
        "items": items,
    }


# --- Tool implementations ---

def get_cities(arguments, api=None):
    """GET /city — list of cities with their DocDoc IDs."""
    raw = _request(api, "city")
    if isinstance(raw, dict) and raw.get("status") in ("api_error", "configuration_error", "status_unknown"):
        return raw
    items = raw.get("CityList", []) if isinstance(raw, dict) else []
    return {"count": len(items), "items": [
        {k: c.get(k) for k in ("Id", "Name", "Alias", "Phone") if k in c}
        for c in items if isinstance(c, dict)
    ]}


def get_specialities(arguments, api=None):
    """GET /speciality/city/{cityId}/onlySimple/{onlySimple} — speciality catalogue."""
    try:
        city_id = int(arguments.get("city", 1))
    except (TypeError, ValueError):
        city_id = 1
    only_simple = 1 if str(arguments.get("onlySimple", "1")).lower() in ("1", "true") else 0
    raw = _request(api, f"speciality/city/{city_id}/onlySimple/{only_simple}")
    if isinstance(raw, dict) and raw.get("status") in ("api_error", "configuration_error", "status_unknown"):
        return raw
    items = raw.get("SpecList", []) if isinstance(raw, dict) else []
    return {"city": city_id, "count": len(items), "items": [
        {k: s.get(k) for k in ("Id", "Name", "Alias", "BranchName", "KidsReception") if k in s}
        for s in items if isinstance(s, dict)
    ]}


def get_clinics(arguments, api=None):
    """GET /clinic/list — clinics by city, name search, speciality."""
    city = arguments.get("city", "1")
    limit, offset = _resolve_pagination(arguments)
    params = {"city": city, "count": limit, "start": offset}
    for key in ("search", "speciality", "type", "stations", "near"):
        v = arguments.get(key)
        if v:
            params[key] = v
    raw = _request(api, "clinic/list", params)
    if isinstance(raw, dict) and raw.get("status") in ("api_error", "configuration_error", "status_unknown"):
        return raw
    return _paginated_response(raw, "ClinicList", _strip_clinic, limit, offset)


def get_doctors(arguments, api=None):
    """GET /doctor/list — doctors by city/speciality/clinic."""
    city = arguments.get("city", "1")
    limit, offset = _resolve_pagination(arguments)
    params = {"city": city, "count": limit, "start": offset}
    for key in ("speciality", "singleClinicId", "order", "deti", "na_dom",
                "slotsDays", "slotsMaxDate"):
        v = arguments.get(key)
        if v is not None:
            params[key] = v
    if arguments.get("withSlots"):
        params["withSlots"] = 1
    raw = _request(api, "doctor/list", params)
    if isinstance(raw, dict) and raw.get("status") in ("api_error", "configuration_error", "status_unknown"):
        return raw
    return _paginated_response(raw, "DoctorList", _strip_doctor, limit, offset)


def get_slots(arguments, api=None):
    """GET /schedule/doctor/doctorIds/{ids}/days/{days} — schedule slots for chosen doctors."""
    doctor_ids = arguments.get("doctorIds") or arguments.get("resources")
    if not doctor_ids:
        return {"status": "validation_error",
                "message": "doctorIds is required (doctor IDs from doctor/list)"}
    if not isinstance(doctor_ids, list):
        doctor_ids = [doctor_ids]
    try:
        doctor_ids = [str(int(d)) for d in doctor_ids]
    except (TypeError, ValueError):
        return {"status": "validation_error",
                "message": "doctorIds must be numeric IDs from doctor/list"}
    if len(doctor_ids) > MAX_SCHEDULE_DOCTORS:
        return {"status": "validation_error",
                "message": f"no more than {MAX_SCHEDULE_DOCTORS} doctorIds per request"}
    try:
        days = int(arguments.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 21))
    path = "schedule/doctor/doctorIds/" + ",".join(doctor_ids) + f"/days/{days}"
    return _request(api, path)


def create_request(arguments, api=None):
    """POST /request — create a booking request (phone required)."""
    phone = arguments.get("phone")
    if not phone:
        return {"status": "validation_error",
                "message": "phone is required (digits, e.g. 79536541232)"}
    phone = str(phone).strip()
    if not _PHONE_RE.fullmatch(phone):
        return {"status": "validation_error",
                "message": "phone must contain 7-20 digits, optionally beginning with +"}
    data = {"phone": phone}
    kind = arguments.get("kind")
    if kind in ("doctor", "diagnostic", "service"):
        data["kind"] = kind
    for key in ("city", "doctor", "clinic", "speciality", "name", "email",
                "dateAdmission", "slotId", "comment", "requestId",
                "startTime", "finishTime"):
        v = arguments.get(key)
        if v is not None:
            data[key] = v
    if arguments.get("validate"):
        data["validate"] = 1
    if arguments.get("validationCode"):
        data["validationCode"] = str(arguments["validationCode"])
    raw = _request(api, "request", method="POST", data=data)
    # Response: {"Response": {"status": "success", "message": ..., "id": ...}}
    if isinstance(raw, dict) and "Response" in raw:
        resp = raw.get("Response") or {}
        out = dict(resp)
        if out.get("status") not in ("success",):
            out.setdefault("status", "slot_unavailable")
        return out
    return raw


def cancel_request(arguments, api=None):
    """GET /request/cancel/{requestId} — cancel a booking by request ID."""
    request_id = arguments.get("requestId")
    if not request_id:
        return {"status": "validation_error",
                "message": "requestId is required (id from the created booking)"}
    try:
        request_id = str(int(request_id))
    except (TypeError, ValueError):
        return {"status": "validation_error",
                "message": "requestId must be numeric (id from POST /request)"}
    return _request(api, f"request/cancel/{request_id}")


def register(api):
    api.register_tool(
        "docdoc_get_cities",
        handler=lambda **arguments: get_cities(arguments, api),
        description="Get the list of cities with their DocDoc city IDs (no parameters).",
        schema={"type": "object", "properties": {}},
    )
    api.register_tool(
        "docdoc_get_specialities",
        handler=lambda **arguments: get_specialities(arguments, api),
        description="Get the catalogue of doctor specialities with their numeric IDs "
                    "(use these IDs for doctor/list and booking).",
        schema={"type": "object", "properties": {
            "city": {"type": "integer", "description": "City ID (default: 1 = Moscow)"},
            "onlySimple": {"type": "string", "enum": ["1", "0"],
                           "description": "1 = only unique specialities (default), 0 = include compound"}
        }, "required": []},
    )
    api.register_tool(
        "docdoc_get_clinics",
        handler=lambda **arguments: get_clinics(arguments, api),
        description="Get list of clinics by city, optional name search or speciality filter.",
        schema={"type": "object", "properties": {
            "city": {"type": "string", "description": "City ID (default: 1 = Moscow; see docdoc_get_cities)"},
            "search": {"type": "string", "description": "Search clinics by name"},
            "speciality": {"type": "string", "description": "Speciality ID filter (see docdoc_get_specialities)"},
            "type": {"type": "string", "enum": ["1", "2", "3"],
                     "description": "1 = clinic, 2 = diagnostic center, 3 = private doctor"},
            "stations": {"type": "string", "description": "Metro station ID(s)"},
            "limit": {"type": "integer", "description": "Max clinics to return (1-50, default 20)"},
            "offset": {"type": "integer", "description": "Pagination offset (default 0)"}
        }, "required": []},
    )
    api.register_tool(
        "docdoc_search_doctors",
        handler=lambda **arguments: get_doctors(arguments, api),
        description="Get list of doctors by city, speciality, or single clinic. "
                    "IDs returned here (Id, Clinics, BookingClinics) feed the slot and booking tools.",
        schema={"type": "object", "properties": {
            "city": {"type": "string", "description": "City ID (default: 1 = Moscow)"},
            "speciality": {"type": "string", "description": "Speciality ID filter (see docdoc_get_specialities)"},
            "singleClinicId": {"type": "integer",
                               "description": "Clinic ID: show only this clinic in the doctor's clinic info"},
            "order": {"type": "string",
                      "enum": ["rating", "price", "experience", "popularity", "reviews", "name"],
                      "description": "Sort order (default rating)"},
            "deti": {"type": "string", "enum": ["1", "0"], "description": "1 = child reception"},
            "na_dom": {"type": "string", "enum": ["1", "0"], "description": "1 = home visits"},
            "withSlots": {"type": "boolean", "description": "Include schedule slots in the response"},
            "slotsDays": {"type": "integer", "description": "Days of schedule when withSlots=1 (default 21)"},
            "limit": {"type": "integer", "description": "Max doctors to return (1-50, default 20)"},
            "offset": {"type": "integer", "description": "Pagination offset (default 0)"}
        }, "required": []},
    )
    api.register_tool(
        "docdoc_get_doctor_slots",
        handler=lambda **arguments: get_slots(arguments, api),
        description="Get the schedule of a chosen doctor: list of available "
                    "date-times for the next N days.",
        schema={"type": "object", "properties": {
            "doctorIds": {"type": "array", "items": {"type": "integer"},
                          "description": "Doctor IDs from docdoc_search_doctors (max 50)"},
            "days": {"type": "integer", "description": "Days ahead for the schedule (1-21, default 7)"}
        }, "required": ["doctorIds"]},
    )
    api.register_tool(
        "docdoc_create_request",
        handler=lambda **arguments: create_request(arguments, api),
        description="Create a booking request (POST /request). Phone is required. "
                    "Returns {status, message, id}; the id is needed for cancellation.",
        schema={"type": "object", "properties": {
            "phone": {"type": "string", "description": "Patient phone, digits only, e.g. 79536541232"},
            "name": {"type": "string", "description": "Patient first name"},
            "city": {"type": "integer", "description": "City ID"},
            "doctor": {"type": "integer", "description": "Doctor ID from docdoc_search_doctors"},
            "clinic": {"type": "integer", "description": "Clinic ID from docdoc_get_clinics"},
            "speciality": {"type": "integer", "description": "Speciality ID"},
            "kind": {"type": "string", "enum": ["doctor", "diagnostic", "service"],
                     "description": "Request type (default: doctor)"},
            "dateAdmission": {"type": "string", "description": "Desired date/time, e.g. '2023-01-20 12:00'"},
            "slotId": {"type": "string", "description": "Slot identifier from the schedule response"},
            "startTime": {"type": "string", "description": "Slot start time"},
            "finishTime": {"type": "string", "description": "Slot end time"},
            "comment": {"type": "string", "description": "Non-medical comment for the clinic"},
            "validate": {"type": "boolean",
                         "description": "true = create a pre-request requiring phone confirmation"},
            "validationCode": {"type": "string", "description": "SMS code confirming a pre-request"},
            "requestId": {"type": "integer", "description": "Pre-request id being confirmed"}
        }, "required": ["phone"]},
    )
    api.register_tool(
        "docdoc_cancel_request",
        handler=lambda **arguments: cancel_request(arguments, api),
        description="Cancel a booking by its request id (the id returned by docdoc_create_request).",
        schema={"type": "object", "properties": {
            "requestId": {"type": "integer", "description": "Request id from docdoc_create_request"}
        }, "required": ["requestId"]},
    )
