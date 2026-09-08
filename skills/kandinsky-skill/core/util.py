"""Общие вспомогательные функции, используемые несколькими модулями core.

Здесь только то, что не относится ни к одной конкретной зоне ответственности:
формирование текстового ответа инструмента, проверка обязательных аргументов и
разбор булевых значений из настроек/аргументов.
"""

import json

from ..lib import KandinskyError


def ok(payload: dict) -> str:
    """Успешный ответ инструмента: словарь -> отформатированный JSON-текст.

    Инструменты возвращают агенту строку, поэтому payload сериализуется здесь один
    раз в едином виде (без экранирования кириллицы, с отступами для читаемости)."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


def err(exc: Exception) -> str:
    """Ответ инструмента при ошибке. Наружу отдаём только текст сообщения —
    без трассировки стека и внутренних деталей."""
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)


def require(**named) -> None:
    """Проверка обязательных аргументов до обращения к API.

    Пустое или отсутствующее значение приводит к понятной локальной ошибке, а не к
    мусорному ответу удалённого API на пустой промпт или отсутствующую картинку.
    Пример: ``require(query=query)``."""
    missing = [name for name, val in named.items() if val is None or (isinstance(val, str) and not val.strip())]
    if missing:
        raise KandinskyError("Не заданы обязательные параметры: " + ", ".join(missing))


def as_bool(value, default=False):
    """Разбор булевого значения из настроек или аргумента инструмента.

    Значения приходят из JSON и от модели в разном виде (строки, числа), поэтому
    принимаем распространённые написания «истины». ``None`` трактуется как «значение
    не задано» и заменяется на default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")
