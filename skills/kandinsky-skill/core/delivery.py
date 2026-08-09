"""Показ результата пользователю в чате.

Двоичные данные результата модели не возвращаются. Вместо этого модуль кладёт
событие в очередь событий контекста (тем же механизмом, что и штатная отправка
фото/видео) — платформа показывает медиа в чате сама, независимо от того, что
напишет модель.

Ответ инструмента имеет два непересекающихся состояния:
  - инлайн удался => только ``inline_delivered`` и ``shown``, где ``shown`` просит
    модель не писать ничего. Это намеренно: результат уже перед глазами, а всё, что
    попадёт в ответ, рано или поздно окажется в чате рядом с картинкой — лишним
    текстом или, того хуже, путём к файлу;
  - инлайн не удался (нет контекста чата, не медиа, файл больше лимита) =>
    добавляются ``file_path`` и ``show_this``: без них пользователь не найдёт уже
    сгенерированный файл.
"""

import base64
import os

from . import security

# Расширения, которые показываем встроенно в чате.
_IMG_EXT = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
_VID_EXT = {"mp4", "webm", "mov", "m4v"}

# MIME-типы для события встроенного показа.
_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
    "gif": "image/gif", "bmp": "image/bmp",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "m4v": "video/x-m4v",
}
# Порог, выше которого встроенный показ отключается (base64 сильно раздувает кадр
# передачи); для таких файлов остаётся только путь.
_MAX_INLINE_IMG = 12 * 1024 * 1024
_MAX_INLINE_VID = 40 * 1024 * 1024


def _deliver_inline(ctx, saved_path, caption: str = "") -> bool:
    """Показать результат встроенно в чате.

    Кладёт в ``ctx.pending_events`` событие того же вида, что и штатная отправка
    фото/видео; супервизор рендерит его в переписке. Возвращает True, если событие
    поставлено в очередь. Любая нештатная ситуация (нет контекста, файл не найден,
    не медиа, слишком большой) => False, и вызывающий откатится на путь к файлу."""
    try:
        if ctx is None:
            return False
        pe = getattr(ctx, "pending_events", None)
        chat_id = getattr(ctx, "current_chat_id", None)
        if pe is None or not chat_id:
            return False
        real = os.path.realpath(str(saved_path))
        if not os.path.isfile(real):
            return False
        ext = real.rsplit(".", 1)[-1].lower() if "." in os.path.basename(real) else ""
        is_img, is_vid = ext in _IMG_EXT, ext in _VID_EXT
        if not (is_img or is_vid):
            return False
        size = os.path.getsize(real)
        if (is_img and size > _MAX_INLINE_IMG) or (is_vid and size > _MAX_INLINE_VID):
            return False
        with open(real, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        # Метаданные задачи (если есть) пробрасываем в событие для привязки к треду.
        meta = getattr(ctx, "task_metadata", {})
        meta = meta if isinstance(meta, dict) else {}
        evt = {
            "type": "send_photo" if is_img else "send_video",
            "chat_id": chat_id,
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "parent_task_id": str(meta.get("parent_task_id") or ""),
            "root_task_id": str(meta.get("root_task_id") or ""),
            "mime": _MIME.get(ext, "image/png" if is_img else "video/mp4"),
            "caption": caption or "",
        }
        evt["image_base64" if is_img else "video_base64"] = b64
        pe.append(evt)
        return True
    except Exception:
        return False


def _file_path(saved) -> dict:
    """Абсолютный путь к результату — но только если он внутри рабочего каталога.

    Отдаётся лишь в запасном сценарии (инлайн-показ не удался): иначе пользователю
    нечем найти уже сгенерированный файл. Вне рабочего корня путь не раскрываем."""
    try:
        root = security.data_root()
        if not root:
            return {}
        real = os.path.realpath(str(saved))
        if os.path.relpath(real, root).startswith(".."):
            return {}
        return {"file_path": real}
    except Exception:
        return {}


def delivery(ctx, saved, caption: str = "") -> dict:
    """Сформировать поля ответа инструмента для показа результата.

    Два непересекающихся состояния. Если инлайн-показ удался, в ответе нет ни
    ссылки, ни пути, ни готовой фразы: модель не может напечатать то, чего не
    получила. Путь появляется только там, где он единственный способ не потерять
    уже сгенерированный файл."""
    if _deliver_inline(ctx, saved, caption=caption):
        return {
            "inline_delivered": True,
            "shown": ("Результат уже показан пользователю в чате. Отвечать не нужно: "
                      "ни подтверждающей фразы, ни путей, ни ссылок, ни base64, ни "
                      "«FINAL ANSWER». Файл уже доставлен — send_photo и send_video "
                      "не вызывай."),
        }
    d = {"inline_delivered": False}
    d.update(_file_path(saved))
    d["show_this"] = ("Показать результат в чате не удалось, но файл сохранён. Скажи об этом "
                      "одной строкой и укажи, где он лежит: «Файл: <значение поля file_path>» — "
                      "его видно во вкладке «Файлы». Пересылать файл не нужно: send_photo и "
                      "send_video не вызывай, base64 не выводи.")
    return d
