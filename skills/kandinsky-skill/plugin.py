"""Kandinsky skill for Ouroboros — точка входа.

Регистрирует инструменты генерации (t2i / i2i / superres / t2v / i2v / avatar)
над Kandinsky API плюс виджет настроек для API-ключа.

Логика разложена по пакету core/ — каждая зона ответственности в своём модуле:
  core.security  — доступ к файловой системе: конфайнмент входных файлов, путь
                   результата, память последнего результата
  core.config    — настройки навыка, транспортный клиент, виджет настроек
  core.delivery  — показ результата: инлайн-событие в чат, запасной путь к файлу
  core.endpoints — реестр операций Kandinsky, оркестрация и сборка всех инструментов
  lib/           — транспортный HTTP-клиент (без знания о файлах и каталогах агента)

register() лишь связывает эти части: определяет границы рабочей области, регистрирует
инструменты и виджет настроек. Прикладной логики здесь нет — она в модулях core.
"""

from __future__ import annotations

from .core import config, endpoints, security


def register(api):
    security.init_roots(api)   # рабочий каталог агента для конфайнмента входных файлов
    for t in endpoints.tools(api):
        # timeout_sec — бюджет хоста на один вызов инструмента. По умолчанию хост даёт
        # 60 с; генерация видео и аватара столько не укладывается, поэтому бюджет
        # задаётся явно для каждого инструмента (см. endpoints._TOOL_TIMEOUT).
        api.register_tool(t["name"], handler=t["handler"],
                          description=t["description"], schema=t["schema"],
                          timeout_sec=t["timeout_sec"])
    config.register_settings(api)  # роут settings/save + виджет (одно поле — API-ключ)
