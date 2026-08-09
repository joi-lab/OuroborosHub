"""Генеративные ручки Kandinsky + оркестрация задачи + сборка всех инструментов.

КАК ДОБАВИТЬ НОВУЮ РУЧКУ:
  1) Напиши функцию build(a) -> (path, params): валидирует аргументы (через
     require), собирает params для Kandinsky, входные файлы прогоняет через
     security.src_to_b64 (конфайнмент!). Возвращает путь эндпоинта и словарь params.
  2) Добавь один элемент в _ENDPOINTS: name/ext/desc/schema/build (+ chain_image,
     если ручка принимает входную картинку).
  Всё — инструмент зарегистрируется сам (censor, поллинг, инлайн-показ,
  таймаут/дозабор через task_id — общие, добавлять не нужно).

Аргумент `censor` (bool) можно объявить в schema любой ручки — он обрабатывается
централизованно (дефолт из настроек), в build его трогать не нужно.
"""

import time

from ..lib import KandinskyClient, KandinskyError, KandinskyTerminalError
from . import config, delivery, security
from .util import as_bool, err, ok, require

# Бюджеты времени. Хост даёт инструменту ровно столько секунд, сколько указано в
# register_tool(timeout_sec=...) — по умолчанию 60, чего для видео и аватара мало.
# Поэтому бюджет задаётся явно, а внутренние лимиты держатся строго ниже него,
# чтобы инструмент успел корректно вернуть task_id, а не был прибит хостом.
#   _TOOL_TIMEOUT  — бюджет хоста на вызов генеративного инструмента;
#   _TOTAL_BUDGET  — внутренний потолок «поллинг + скачивание»;
#   _DEFAULT_TIMEOUT — из него на поллинг статуса.
# Всё это ниже манифестного timeout_sec=300 (жёсткий предел платформы).
_TOOL_TIMEOUT = 290
_TOTAL_BUDGET = 280
_DEFAULT_TIMEOUT = 240
# Бюджет для дешёвых инструментов (health, статус задачи). Выше сетевого таймаута
# клиента (120 с), чтобы истекал сетевой таймаут с понятной ошибкой, а не хост.
_FAST_TOOL_TIMEOUT = 130

# допустимые разрешения по маршрутам (вынесено, чтобы легко расширять)
_RES_T2I = ["1024x1024", "768x768", "768x1280", "1280x768", "auto"]
_RES_T2V_LITE = {"512x512", "512x768", "768x512"}
_RES_T2V_PRO = {"768x1280", "1280x768"}
_BEAUT = ["enabled", "disabled", "gigachat-max"]


def _add_beaut(params: dict, a: dict) -> dict:
    if a.get("beautificator"):
        params["beautificator"] = a["beautificator"]
    return params


def _build_image(a):
    require(query=a.get("query"))
    return "/tasks/k6-image-t2i", _add_beaut(
        {"query": a["query"], "resolution": a.get("resolution") or "1024x1024"}, a)


def _build_edit(a):
    require(query=a.get("query"))
    srcs = [s for s in (a.get("images") or [a.get("image")]) if s]
    if not srcs:
        raise KandinskyError("Не задан источник: image или images")
    return "/tasks/k6-i2i", _add_beaut(
        {"query": a["query"], "image": [security.src_to_b64(s) for s in srcs]}, a)


def _build_upscale(a):
    require(image=a.get("image"))
    raw_up = a.get("upscale")
    try:
        up = 2 if raw_up is None else int(raw_up)  # None → дефолт; 0/прочее не глотаем
    except (TypeError, ValueError):
        raise KandinskyError("upscale должен быть числом 2 или 4")
    if up not in (2, 4):
        raise KandinskyError("upscale может быть только 2 или 4")
    p = {"image": security.src_to_b64(a["image"]), "upscale": up}
    if a.get("one_step_t") is not None:
        try:
            ost = float(a["one_step_t"])
        except (TypeError, ValueError):
            raise KandinskyError("one_step_t должен быть числом от 0 до 1")
        if not 0.0 <= ost <= 1.0:
            raise KandinskyError("one_step_t вне диапазона: допустимо 0..1")
        p["one_step_t"] = ost
    return "/tasks/k6_superres", p


def _build_video(a):
    require(query=a.get("query"))
    is_pro = as_bool(a.get("pro"), False)
    res = a.get("resolution") or ("1280x768" if is_pro else "768x512")
    allowed = _RES_T2V_PRO if is_pro else _RES_T2V_LITE
    if res not in allowed:
        raise KandinskyError(
            f"Недопустимое разрешение '{res}' для режима {'pro' if is_pro else 'lite'}. "
            f"Допустимо: {', '.join(sorted(allowed))}.")
    path = "/tasks/k5_video_t2v_pro" if is_pro else "/tasks/k5_video_t2v_lite"
    return path, _add_beaut({"query": a["query"], "resolution": res}, a)


def _build_animate(a):
    require(image=a.get("image"), query=a.get("query"))
    path = {"lite": "/tasks/k5-i2v-lite", "sd": "/tasks/k5-i2v-sd",
            "hd": "/tasks/k5-i2v-hd"}.get(a.get("quality") or "lite")
    if not path:
        raise KandinskyError("quality: lite | sd | hd")
    return path, _add_beaut({"query": a["query"], "image": security.src_to_b64(a["image"])}, a)


def _build_avatar(a):
    require(image=a.get("image"), audio=a.get("audio"))
    return "/tasks/giga_avatar", {
        "query": a.get("query") or "",
        "image": security.src_to_b64(a["image"]),
        "audio": security.src_to_b64(a["audio"]),
    }


_SHOW = ("Навык сам показывает результат пользователю в чате. При inline_delivered=true отвечать не "
         "нужно вообще: ни подтверждающей фразы, ни путей, ни ссылок, ни base64, ни «FINAL ANSWER»; "
         "send_photo и send_video не вызывай — файл уже доставлен. При inline_delivered=false следуй "
         "полю show_this. Для чейнинга путь передавать не нужно: чтобы применить операцию к "
         "последнему результату, вызови инструмент без image.")
_CENSOR_PROP = {"censor": {"type": "boolean"}}


def _obj(props, required=None):
    return {"type": "object", "properties": dict(props, **_CENSOR_PROP),
            "required": required or []}


# Единый источник правды по генеративным ручкам. Добавление новой = +1 элемент.
_ENDPOINTS = [
    {
        "name": "kandinsky_image", "ext": "png", "build": _build_image,
        "desc": "Текст → картинка. " + _SHOW,
        "schema": _obj({
            "query": {"type": "string", "description": "Промпт"},
            "resolution": {"type": "string", "enum": _RES_T2I, "default": "1024x1024"},
            "beautificator": {"type": "string", "enum": _BEAUT},
        }, ["query"]),
    },
    {
        "name": "kandinsky_edit_image", "ext": "png", "build": _build_edit, "chain_image": True,
        "desc": "Картинка(и) + текст → отредактированная картинка. `image` — путь к файлу или base64. " + _SHOW,
        "schema": _obj({
            "image": {"type": "string", "description": "Путь к файлу или base64"},
            "images": {"type": "array", "items": {"type": "string"}, "description": "Несколько источников (опц.)"},
            "query": {"type": "string"},
            "beautificator": {"type": "string", "enum": _BEAUT},
        }, ["query"]),
    },
    {
        "name": "kandinsky_upscale", "ext": "png", "build": _build_upscale, "chain_image": True,
        "desc": "Увеличить качество картинки ×2/×4. " + _SHOW + " Иногда воркер не берёт задачу — тогда вернётся task_id и подсказка.",
        "schema": _obj({
            "image": {"type": "string", "description": "Путь к файлу или base64"},
            "upscale": {"type": "integer", "enum": [2, 4], "default": 2},
            "one_step_t": {"type": "number", "description": "0..1 — сила следования оригиналу"},
        }, ["image"]),
    },
    {
        "name": "kandinsky_video", "ext": "mp4", "build": _build_video,
        "desc": "Текст → короткое видео. pro=true — качественнее и дольше. " + _SHOW,
        "schema": _obj({
            "query": {"type": "string"},
            "pro": {"type": "boolean", "default": False},
            "resolution": {"type": "string", "description": "lite: 512x512|512x768|768x512; pro: 768x1280|1280x768"},
            "beautificator": {"type": "string", "enum": _BEAUT},
        }, ["query"]),
    },
    {
        "name": "kandinsky_animate", "ext": "mp4", "build": _build_animate, "chain_image": True,
        "desc": "Оживить картинку: картинка → видео. query — промпт движения объекта. " + _SHOW,
        "schema": _obj({
            "image": {"type": "string", "description": "Путь к файлу или base64"},
            "query": {"type": "string", "description": "Промпт движения (про объект, не только камеру)"},
            "quality": {"type": "string", "enum": ["lite", "sd", "hd"], "default": "lite"},
            "beautificator": {"type": "string", "enum": _BEAUT},
        }, ["image", "query"]),
    },
    {
        "name": "kandinsky_avatar", "ext": "mp4", "build": _build_avatar, "chain_image": True,
        "desc": "Фото + аудио → говорящий аватар. image/audio — пути к файлам или base64. " + _SHOW,
        "schema": _obj({
            "image": {"type": "string"},
            "audio": {"type": "string"},
            "query": {"type": "string"},
        }, ["image", "audio"]),
    },
]


def _run_task(api, client: KandinskyClient, path: str, params: dict, *, censor: bool,
              ext: str, timeout: int, ctx=None, caption: str = ""):
    """Создать задачу → дождаться → забрать → сохранить. При таймауте/сбое вернуть task_id.

    Возвращает пару (ответ инструмента, путь к сохранённому файлу или None). Путь
    нужен вызывающему только для памяти чейнинга — в ответ модели он не попадает.

    Каталог задачи создаётся ТОЛЬКО когда данные уже скачаны: иначе каждая неудачная
    генерация оставляла бы пустой каталог в области результатов навыка."""
    start = time.time()
    task_id = client.create_task(path, params, censor=censor)
    try:
        client.wait(task_id, timeout=timeout)
        # Ужимаем таймаут скачивания под оставшийся бюджет, чтобы суммарное время
        # (поллинг + загрузка) не перескочило бюджет вызова и хост не прибил нас до
        # того, как мы корректно вернём результат.
        dl = max(20, int(_TOTAL_BUDGET - (time.time() - start)))
        data = client.result(task_id, download_timeout=dl)
    except KandinskyTerminalError as e:
        # задача завершилась, но результат пустой/отклонён — повтор не поможет,
        # поэтому НЕ предлагаем «забери позже» (иначе агент зациклится на пустоте).
        return {"ok": False, "task_id": task_id, "error": str(e)}, None
    except KandinskyError as e:
        # транзиентная ошибка (таймаут ожидания/скачивания, 5xx, обрыв) — задача
        # ещё может выполняться либо результат дозаберётся позже.
        return {
            "ok": False,
            "task_id": task_id,
            "error": str(e),
            "hint": "Задача ещё может выполняться или результат пока недоступен. "
                    "Проверь task_status(task_id) и забери через task_result(task_id) позже.",
        }, None
    saved = security.save_result(api, data, ext)
    res = {"ok": True, "task_id": task_id}
    res.update(delivery.delivery(ctx, saved, caption))
    # Путь к файлу в тексте не навязываем — модель показывает его по инструкции из поля
    # file_path. Для чейнинга последний результат запоминается на стороне навыка.
    return res, str(saved)


def _make_gen_handler(api, spec):
    """Единый хендлер для любой генеративной ручки из _ENDPOINTS: собирает params
    через spec.build, применяет censor-дефолт, гоняет задачу, показывает результат."""
    build = spec["build"]
    ext = spec["ext"]

    # Явная (широкая) сигнатура — совместимо с тем, как хост зовёт хендлеры по
    # именам аргументов. Новый параметр нужен редко; если да — добавь сюда одну
    # строку и в схему соответствующей ручки.
    def handler(ctx, query="", resolution=None, beautificator=None, censor=None,
                image="", images=None, upscale=2, one_step_t=None, pro=False,
                quality="lite", audio=""):
        a = {"query": query, "resolution": resolution, "beautificator": beautificator,
             "image": image, "images": images, "upscale": upscale, "one_step_t": one_step_t,
             "pro": pro, "quality": quality, "audio": audio}
        try:
            # Чейнинг без выдачи пути модели: если инструмент работает с картинкой,
            # но image не передан — подставляем последний сгенерированный результат
            # (путь резолвится на стороне навыка, модель его не видит).
            if spec.get("chain_image") and not a["image"] and not a["images"]:
                last = security.load_last_output(api)
                if last:
                    a["image"] = last
            path, params = build(a)
            cen = config.censor_default(api) if censor is None else as_bool(censor, True)
            res, saved = _run_task(api, config.client(api), path, params, censor=cen,
                                   ext=ext, timeout=_DEFAULT_TIMEOUT, ctx=ctx)
            if saved:
                security.save_last_output(api, saved)  # запомнить для чейнинга «оживи/увеличь эту…»
            return ok(res)
        except Exception as e:  # noqa: BLE001
            return err(e)
    return handler


def _make_health_handler(api):
    """Инструмент префлайта: дешёвая проверка доступности API и валидности настроек
    без запуска генерации. Дополнительно возвращает предупреждение о небезопасном
    транспорте, если оно есть."""
    def handler(ctx):
        try:
            res = {"ok": True, "health": config.client(api).health()}
            w = config.transport_warning(api)
            if w:
                res["warning"] = w
            return ok(res)
        except Exception as e:  # noqa: BLE001
            return err(e)
    return handler


def _make_task_status_handler(api):
    """Инструмент проверки статуса задачи по task_id (для долгих задач: агент мог
    получить task_id при таймауте и захотеть узнать, готово ли)."""
    def handler(ctx, task_id=""):
        try:
            require(task_id=task_id)
            return ok({"ok": True, "task_id": task_id, "status": config.client(api).status(task_id)})
        except Exception as e:  # noqa: BLE001
            return err(e)
    return handler


def _make_task_result_handler(api):
    """Инструмент дозабора результата готовой задачи по task_id (когда генерация не
    уложилась в бюджет времени одного вызова и вернула task_id). Показывает результат
    так же, как обычная генерация, и запоминает его для чейнинга."""
    def handler(ctx, task_id="", ext="bin"):
        try:
            require(task_id=task_id)
            # Скачиваем в память и определяем тип по содержимому: модель часто не
            # передаёт ext, а файл с расширением .bin не показать инлайн в чате.
            data = config.client(api).result(task_id)
            saved = security.save_result(api, data, ext or "bin")
            res = {"ok": True, "task_id": task_id}
            res.update(delivery.delivery(ctx, saved))
            security.save_last_output(api, str(saved))  # запомнить для чейнинга; путь модели не отдаём
            return ok(res)
        except Exception as e:  # noqa: BLE001
            return err(e)
    return handler


def tools(api) -> list:
    """Список всех инструментов навыка для регистрации в register(api):
    health + генеративные ручки из _ENDPOINTS + task_status/task_result.

    Поле timeout_sec — бюджет хоста на один вызов; генеративным ручкам нужен полный
    бюджет, дешёвым проверкам достаточно короткого."""
    items = [{
        "name": "kandinsky_health",
        "handler": _make_health_handler(api),
        "description": "Префлайт: проверить, что Kandinsky API жив и настройки валидны (дёшево, без генерации). Вызывай перед первой/дорогой генерацией или при непонятной ошибке — чтобы отличить проблему настроек/ключа от проблемы промпта.",
        "schema": {"type": "object", "properties": {}},
        "timeout_sec": _FAST_TOOL_TIMEOUT,
    }]
    for spec in _ENDPOINTS:
        items.append({
            "name": spec["name"],
            "handler": _make_gen_handler(api, spec),
            "description": spec["desc"],
            "schema": spec["schema"],
            "timeout_sec": _TOOL_TIMEOUT,
        })
    items.append({
        "name": "kandinsky_task_status",
        "handler": _make_task_status_handler(api),
        "description": "Статус задачи по task_id (new → processing → done, ошибка — fail).",
        "schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        "timeout_sec": _FAST_TOOL_TIMEOUT,
    })
    items.append({
        "name": "kandinsky_task_result",
        "handler": _make_task_result_handler(api),
        "timeout_sec": _TOOL_TIMEOUT,
        "description": "Забрать результат готовой задачи по task_id (ext необязателен — тип файла определяется по содержимому). Навык сам показывает результат в чате: при inline_delivered=true отвечать не нужно вообще, send_photo и send_video не вызывай. При inline_delivered=false следуй полю show_this. Пути, ссылки и base64 в чат не выводи.",
        "schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "ext": {"type": "string", "default": "bin"}},
            "required": ["task_id"],
        },
    })
    return items
