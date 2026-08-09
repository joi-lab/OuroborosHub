"""Настройки навыка, транспортный клиент и виджет настроек.

Модуль отвечает за конфигурацию: читает и пишет settings.json в приватном каталоге
состояния навыка, строит на его основе транспортный клиент, определяет значения по
умолчанию для цензуры и транспорта и регистрирует виджет настроек. В виджете два
обязательных поля — адрес API и ключ; остальные параметры имеют рабочие значения
по умолчанию и при необходимости задаются в settings.json вручную.
"""

import json
import os
import pathlib

from starlette.responses import JSONResponse

from ..lib import KandinskyClient, KandinskyError
from .util import as_bool

# Ключи, принимаемые из виджета/настроек. Всё, что приходит в запросе сохранения и
# не входит в этот список, игнорируется (защита от записи посторонних ключей).
_SETTINGS_KEYS = (
    "KANDINSKY_API_KEY",       # ключ Bearer к Kandinsky API (обязателен)
    "KANDINSKY_API_BASE",      # адрес API (обязателен, вводится пользователем)
    "KANDINSKY_ALLOW_INSECURE",  # разрешить plain-HTTP к доверенному инстансу
    "KANDINSKY_CENSOR",        # значение цензуры по умолчанию
)


# Единственное сообщение об отсутствующем ключе — его видит и пользователь в чате,
# и форма во вкладке навыка. Держим в одном месте, чтобы пути не разъезжались.
# Где взять доступ, если его ещё нет: без этой строки пользователь упирается в
# «нужен ключ» и не знает, к кому идти.
ACCESS_REQUEST = (
    "Если адреса и ключа у вас нет, запросите доступ письмом на kandinsky@kandinskylab.ai."
)

NO_KEY_MESSAGE = (
    "Не задан API-ключ Kandinsky. Открой «Настройки» → «Расширенные» → «Kandinsky API», "
    "вставь ключ в поле «API-ключ Kandinsky» и нажми «Сохранить». " + ACCESS_REQUEST
)

# Адрес API тоже обязателен и тоже вводится пользователем: навык намеренно не
# содержит адреса по умолчанию — он не должен тянуть за собой чужой инстанс.
NO_BASE_MESSAGE = (
    "Не задан адрес Kandinsky API. Открой «Настройки» → «Расширенные» → «Kandinsky API», "
    "укажи адрес в поле «Адрес Kandinsky API» (например http://ваш-сервер:5051) "
    "и нажми «Сохранить». " + ACCESS_REQUEST
)


def settings_path(api) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / "settings.json"


def load_settings(api) -> dict:
    """Прочитать настройки навыка. Отсутствие файла или битый JSON => пустой словарь
    (навык откатится на значения по умолчанию)."""
    path = settings_path(api)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def client(api) -> KandinskyClient:
    """Построить транспортный клиент из настроек навыка (с откатом на окружение).

    Инстансы Kandinsky обычно работают по plain HTTP, поэтому небезопасный
    транспорт по умолчанию разрешён; клиент при этом выведет предупреждение (см.
    transport_warning). Отключается через KANDINSKY_ALLOW_INSECURE в настройках."""
    s = load_settings(api)
    api_key = s.get("KANDINSKY_API_KEY") or None
    if not api_key and not os.environ.get("KANDINSKY_API_KEY"):
        # Ключ вставляется в виджете настроек, поэтому и подсказка — про виджет,
        # а не про переменные окружения (сообщение видит конечный пользователь).
        # Путь до поля указан целиком: без него пользователь ищет раздел вслепую.
        raise KandinskyError(NO_KEY_MESSAGE)
    base = s.get("KANDINSKY_API_BASE") or None
    if not base and not os.environ.get("KANDINSKY_API_BASE"):
        # Адреса по умолчанию нет намеренно: навык не привязан к чьему-то инстансу
        # и не раскрывает его. Свой адрес пользователь вводит рядом с ключом.
        raise KandinskyError(NO_BASE_MESSAGE)
    allow_insecure = as_bool(s.get("KANDINSKY_ALLOW_INSECURE"), default=True)
    return KandinskyClient(api_key=api_key, base=base, allow_insecure=allow_insecure)


def censor_default(api) -> bool:
    """Значение цензуры по умолчанию (когда инструмент не задал censor явно).
    По умолчанию включена."""
    return as_bool(load_settings(api).get("KANDINSKY_CENSOR"), default=True)


def transport_warning(api) -> str:
    """Предупреждение, если ключ уходит по незащищённому HTTP на публичный адрес.

    Возвращается в ответе kandinsky_health, чтобы риск был виден пользователю, а не
    только в служебном stderr. Для loopback/приватных адресов предупреждения нет."""
    import urllib.parse as _up
    from ..lib.kandinsky import _is_local_or_private
    base = (load_settings(api).get("KANDINSKY_API_BASE")
            or os.environ.get("KANDINSKY_API_BASE") or "")
    parts = _up.urlparse(base)
    if parts.scheme == "http" and not _is_local_or_private(parts.hostname or ""):
        return ("Запросы идут по незащищённому HTTP на публичный адрес — API-ключ "
                "передаётся открыто и может быть перехвачен. Не используй ценный ключ; "
                "для защиты нужен HTTPS-инстанс.")
    return ""


def _make_settings_save(api):
    """Фабрика обработчика роута сохранения настроек.

    Записывает только разрешённые ключи — ничего больше из запроса не берётся."""
    async def _settings_save(request):
        data = await request.json()
        current = load_settings(api)
        for key in _SETTINGS_KEYS:
            if key in data:
                current[key] = data[key]
        path = settings_path(api)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "Настройки Kandinsky сохранены."})
    return _settings_save


def register_settings(api):
    """Зарегистрировать роут сохранения настроек и виджет с двумя полями.

    Оба поля обязательные: ключ и адрес API. Адреса по умолчанию у навыка нет
    намеренно — он не привязан к чьему-то инстансу и не раскрывает его. Остальные
    параметры (транспорт, цензура) имеют рабочие значения и задаются при
    необходимости в settings.json. Поле ключа имеет тип password и после сохранения
    очищается — так задумано."""
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("POST",))
    api.register_settings_section(
        "kandinsky",
        title="Kandinsky API",
        schema={
            "components": [
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "KANDINSKY_API_BASE",
                         "label": "Адрес Kandinsky API",
                         "type": "text",
                         "required": True,
                         "placeholder": "http://ваш-сервер:5051",
                         "help": "Адрес инстанса Kandinsky API, который вам выдали. Нет доступа — запросите его на kandinsky@kandinskylab.ai.",
                         "description": "Адрес инстанса Kandinsky API, который вам выдали. Нет доступа — запросите на kandinsky@kandinskylab.ai."},
                        {"name": "KANDINSKY_API_KEY",
                         "label": "API-ключ Kandinsky",
                         "type": "password",
                         "required": True,
                         "placeholder": "Вставьте ключ",
                         "help": "После сохранения поле снова станет пустым — это нормально: ключ хранится в защищённом виде и обратно не показывается.",
                         "description": "После сохранения поле снова станет пустым — это нормально: ключ сохранён и скрыт."},
                    ],
                    "submit_label": "Сохранить",
                }
            ]
        },
    )
