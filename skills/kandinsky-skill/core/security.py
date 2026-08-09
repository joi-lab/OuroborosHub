"""Доступ к файловой системе и связанная с ним безопасность.

Весь контроль доступа к диску собран в одном модуле, чтобы политику можно было
прочитать и проверить целиком. Модуль решает три задачи:

1. Конфайнмент входных файлов (``src_to_b64``). Инструменты принимают изображение
   или аудио как путь к файлу и отправляют его содержимое во внешний Kandinsky API.
   Без ограничения это позволило бы (например, через подмену аргумента) прочитать
   чужой секрет — файл настроек с API-ключом, системный файл — и отправить его
   наружу. Поэтому чтение разрешено только внутри рабочего каталога агента и по
   строгому списку правил.

2. Формирование пути для сохранения результата (``out_path``, ``_safe_ext``) без
   возможности выйти за пределы каталога через специальные символы в расширении.

3. Память последнего результата (``save_last_output`` / ``load_last_output``) —
   чтобы операции «оживи эту картинку» / «увеличь её» работали, не передавая путь к
   файлу модели: она вызывает инструмент без изображения, а навык сам подставляет
   последний созданный файл.

Транспортный клиент (``lib``) в эти правила не вовлечён — он лишь ходит по сети.
"""

import base64
import os
import pathlib
import re
import time
import uuid

from ..lib import KandinskyError

# Предельный размер входного файла (изображение/аудио). Защищает от чтения
# слишком больших файлов и от раздувания запроса к API.
_MAX_INPUT_BYTES = 30 * 1024 * 1024
# Предел на «сырую» base64-строку, переданную вместо пути к файлу. Соответствует
# файловому лимиту с поправкой на ~+33 % накладных расходов кодирования base64.
_MAX_B64_CHARS = (_MAX_INPUT_BYTES // 3 + 1) * 4

# Границы рабочей области агента. Заполняются один раз в register() через
# init_roots(). Значения по умолчанию None означают «границы неизвестны» — в этом
# случае чтение файлов запрещается полностью (см. src_to_b64, ветка fail-closed).
#   _DATA_ROOT     — рабочий каталог агента (например, /data).
#   _STATE_ROOT    — каталог состояния и секретов агента (например, /data/state).
#   _OWN_JOBS_ROOT — каталог результатов самого навыка внутри state; из него читать
#                    разрешено (нужно для повторного использования своих же файлов).
_DATA_ROOT = None      # type: ignore
_STATE_ROOT = None     # type: ignore
_OWN_JOBS_ROOT = None  # type: ignore

# Имя файла в каталоге состояния навыка, где хранится путь к последнему результату.
_LAST_OUTPUT_FILE = "last_output.txt"


def init_roots(api) -> None:
    """Определяет границы рабочей области агента для конфайнмента.

    Вызывается один раз при регистрации навыка. Пути приводятся к прямым слэшам,
    чтобы сравнение префиксов работало одинаково на любой платформе (на Windows
    иначе сравнение сломалось бы и отвергло допустимые пути)."""
    global _DATA_ROOT, _STATE_ROOT, _OWN_JOBS_ROOT
    try:
        # get_state_dir() возвращает, например, /data/state/skills/kandinsky.
        sd = os.path.realpath(api.get_state_dir()).replace("\\", "/")
        _OWN_JOBS_ROOT = sd + "/jobs"                # свои результаты — читать можно
        # Рабочий корень платформа сообщает сама (data_dir); это надёжнее разбора пути
        # и не зависит от того, как устроен каталог состояния в конкретной сборке.
        root = ""
        try:
            root = str((api.get_runtime_info() or {}).get("data_dir") or "")
        except Exception:
            root = ""
        if root:
            _DATA_ROOT = os.path.realpath(root).replace("\\", "/").rstrip("/")
            _STATE_ROOT = _DATA_ROOT + "/state"
            return
        marker = "/state/"
        if marker in sd:
            _DATA_ROOT = sd.split(marker)[0]         # часть до /state/ => рабочий корень (/data)
            _STATE_ROOT = _DATA_ROOT + "/state"      # каталог секретов (/data/state)
    except Exception:
        # Любой сбой определения границ => считаем их неизвестными (fail-closed).
        _DATA_ROOT = _STATE_ROOT = _OWN_JOBS_ROOT = None


def data_root():
    """Рабочий корень агента (или None). Используется delivery для построения
    относительного пути к файлу результата."""
    return _DATA_ROOT


def _safe_ext(ext: str) -> str:
    """Санация расширения файла: только буквы и цифры, не длиннее 5 символов.

    Не даёт подставить в имя результата спецсимволы (например, ``../``) и устроить
    выход за пределы каталога."""
    e = re.sub(r"[^A-Za-z0-9]", "", str(ext or ""))[:5]
    return e or "bin"


def out_path(api, ext: str) -> pathlib.Path:
    """Путь для сохранения результата: отдельный подкаталог задачи внутри области
    результатов навыка, имя файла — временная метка плюс случайный суффикс (чтобы
    файлы не перезаписывали друг друга)."""
    job_dir = api.skill_job_dir("kandinsky-" + uuid.uuid4().hex[:8])
    out_dir = pathlib.Path(job_dir) / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{int(time.time())}-{uuid.uuid4().hex[:6]}.{_safe_ext(ext)}"


# Сигнатуры форматов для определения типа результата по его содержимому. Нужны,
# когда расширение неизвестно (дозабор результата по task_id без ext) или когда
# сервис вернул не тот формат, что ожидала ручка: файл с неверным расширением не
# показывается в чате встроенно.
def sniff_ext(data: bytes) -> str:
    """Определить расширение по первым байтам. Пустая строка — формат неизвестен."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return ""
    b = bytes(data[:16])
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if b.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if b.startswith(b"GIF8"):
        return "gif"
    if b.startswith(b"RIFF"):
        if b[8:12] == b"WEBP":
            return "webp"
        if b[8:12] == b"WAVE":
            return "wav"
    if b[4:8] == b"ftyp":
        return "mp4"
    if b.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if b.startswith(b"OggS"):
        return "ogg"
    if b.startswith(b"ID3") or b.startswith(b"\xff\xfb"):
        return "mp3"
    return ""


def save_result(api, data, ext: str) -> pathlib.Path:
    """Сохранить готовые байты результата и вернуть путь к файлу.

    Каталог задачи создаётся здесь — то есть только когда данные уже получены;
    неудачные генерации пустых каталогов не оставляют. Расширение берётся из
    содержимого, а объявленное ручкой используется как запасной вариант."""
    if isinstance(data, (str, pathlib.Path)):
        # Клиент уже сохранил файл сам (вызов с out=...) — просто отдаём путь.
        return pathlib.Path(str(data))
    out = out_path(api, sniff_ext(data) or ext)
    out.write_bytes(bytes(data))
    return out


def save_last_output(api, path: str) -> None:
    """Запомнить путь к последнему успешному результату (для последующего чейнинга).
    Ошибки записи не критичны — при их наличии чейнинг просто не сработает."""
    try:
        (pathlib.Path(api.get_state_dir()) / _LAST_OUTPUT_FILE).write_text(str(path), encoding="utf-8")
    except Exception:
        pass


def load_last_output(api) -> str:
    """Вернуть путь к последнему результату, если он сохранён и файл ещё существует;
    иначе — пустую строку."""
    try:
        p = pathlib.Path(api.get_state_dir()) / _LAST_OUTPUT_FILE
        if p.exists():
            val = p.read_text(encoding="utf-8").strip()
            return val if val and os.path.isfile(val) else ""
    except Exception:
        pass
    return ""


def src_to_b64(src):
    """Привести источник (путь к файлу, готовый base64 или bytes) к base64-строке.

    Ключевая функция конфайнмента. Если это путь к существующему файлу — файл
    читается только после проверки по списку правил ниже. Если это не путь —
    считаем, что пришла уже готовая base64-строка (с проверкой её размера).
    """
    # bytes: уже двоичные данные, просто кодируем.
    if isinstance(src, bytes):
        return base64.b64encode(src).decode("ascii")

    # Строка, указывающая на существующий файл: применяем конфайнмент.
    if isinstance(src, str) and os.path.exists(src) and os.path.isfile(src):
        # realpath разворачивает симлинки и «..», поэтому проверка идёт по
        # настоящему целевому пути, а не по тому, как его записали.
        real_path = os.path.realpath(src)             # этот путь и будем открывать
        real = real_path.replace("\\", "/")           # копия с прямыми слэшами — только для сравнения
        if _DATA_ROOT:
            # Список правил доступа:
            #  - файл обязан лежать внутри рабочего каталога агента;
            #  - каталог состояния/секретов запрещён,
            #  - кроме собственных результатов навыка (их читать можно).
            # Сравнение с добавлением "/" в конце корня закрывает атаку через
            # соседний каталог с похожим именем (например, /data-evil против /data).
            in_root = real == _DATA_ROOT or real.startswith(_DATA_ROOT.rstrip("/") + "/")
            in_state = _STATE_ROOT and (real == _STATE_ROOT or real.startswith(_STATE_ROOT.rstrip("/") + "/"))
            in_own_jobs = _OWN_JOBS_ROOT and real.startswith(_OWN_JOBS_ROOT.rstrip("/") + "/")
            if not in_root or (in_state and not in_own_jobs):
                raise KandinskyError(
                    f"Чтение файла вне рабочего каталога агента запрещено политикой безопасности: {src}")
        else:
            # Границы рабочей области неизвестны — безопасно ограничить чтение
            # нельзя, поэтому запрещаем чтение файлов полностью (fail-closed).
            # Передача изображения/аудио как base64 при этом остаётся доступной.
            raise KandinskyError(
                "Не удалось определить рабочий каталог агента — чтение файла с диска "
                "запрещено из соображений безопасности. Передай изображение/аудио как base64.")
        # Проверка пройдена — читаем настоящий файл (real_path, не нормализованную копию).
        size = os.path.getsize(real_path)
        if size > _MAX_INPUT_BYTES:
            raise KandinskyError(f"Файл слишком большой ({size} байт > {_MAX_INPUT_BYTES}).")
        with open(real_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")

    # Файла по такому пути нет. Если строка ПОХОЖА на путь (короткая, начинается с
    # /, . или ~, либо оканчивается медиа-расширением) — вероятно, это опечатка или
    # отсутствующий файл; понятная ошибка полезнее, чем мусорный ответ API.
    if isinstance(src, str):
        looks_like_path = len(src) < 512 and (
            src[:1] in "/.~"
            or re.search(r"\.(png|jpe?g|webp|gif|bmp|mp4|webm|mov|m4v|wav|mp3|ogg|flac|aac)$", src, re.I))
        if looks_like_path:
            raise KandinskyError(f"Файл не найден: {src}")
        # Иначе считаем, что это уже готовая base64-строка — с проверкой размера.
        if len(src) > _MAX_B64_CHARS:
            raise KandinskyError(
                f"base64-вход слишком большой ({len(src)} симв. > {_MAX_B64_CHARS}). "
                f"Передай файл или уменьши размер.")
    return src
