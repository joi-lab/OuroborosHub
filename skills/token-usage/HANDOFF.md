# Передача Token Observatory 1.1.0

Доработан существующий `token-usage` в приватном snapshot. Исходные 13 файлов сохранены родителем как baseline; в этой копии изменены 12 файлов, синтетическая `fixtures/numeric_snapshot.json` оставлена прежней. Новый проект или второй виджет не создавался. Для интеграции нужно использовать итоговый diff этой копии относительно baseline, а не накладывать старый orphan snapshot.

## Изменённые файлы

| Файл | Результат |
| --- | --- |
| `SKILL.md` | Версия 1.1.0, точный runtime-контракт и семантика нормализованного input |
| `plugin.py` | Точный PluginAPI 2.0, уникальные маршруты, один GET/PUT preferences handler, регистрация UI, on_unload, ленивый reader, отмена catch-up, JSON encoding вне ASGI loop |
| `ingestion.py` | Независимая проекция input_token_usage, целочисленная точность, defensive copy, ограниченные проверки отмены с rollback |
| `accounting.py` | Нормализованный/legacy выбор без сложения, counts доступности, отдельные session series/rankings, точные строковые поля, явные границы истории |
| `widget.js` | Точный `/api/extensions/token-usage`, отдельные переключаемые views, нормализованные поля/known/missing, сохранение view, компактные warnings, график перед cache/cost, доступность и защита preference race |
| `tests/test_ingestion.py` | Строгие normalized cases, lifecycle, реальные измерения нулевых чтений, cancellation |
| `tests/test_accounting.py` | Session chart/ranking conservation, own/descendants, периоды/DST, матрица normalized values и tampered exact strings |
| `tests/test_plugin.py` | Уникальные пути/методы, UI/on_unload, preferences, cleanup, реальные Starlette ASGI tests и off-loop JSON serialization |
| `tests/oracle.py` | Независимо структурированная арифметика effective/normalized/legacy, checksum/filter membership, проверка exact strings |
| `tests/widget_contract.cjs` | Prefix, session view и rankings, exact/unknown/zero, presets/filters/DST, prefs, races, catch-up accessibility, disposal, значения вне Number range |
| `README.md` | Точные API, схема чисел/представлений, команды и ограничения проверки |
| `HANDOFF.md` | Этот отчёт |

## Контракт, который использован

Переданный родителем реальный API применён напрямую:

- `register_route(path, handler, *, methods=...)`; три уникальных пути `/data`, `/export`, `/preferences`, последний сразу с `['GET', 'PUT']`.
- `register_ui_tab('observatory', 'Token Observatory', icon='◉', render={'kind':'module','entry':'widget.js','height':560,'span':2,'start':'manual'})`.
- `on_unload(service.close)`; `data_dir` и собственный `state_dir` из `get_runtime_info()`.
- Starlette Request/JSONResponse обязательны. Нет угадывания сигнатур, setup alias или успешного dictionary fallback при отсутствии зависимости.
- `window.__ouroWidgetOnDispose(fn)` отменяет запросы/таймеры/listeners/URL в браузере. Start/Stop принадлежит host; unload plugin останавливает backend.

`input_token_usage` принимается только с точным набором `total_tokens`, `cache_read_tokens`, `cache_write_tokens`; каждое значение — null либо non-bool int ≥ 0. Любой неверный ключ/тип/значение делает объект целиком отсутствующим. Валидный объект заменяет legacy input/cache в effective session metrics; null остаётся unknown. Legacy поля сохраняются отдельно. Подписочные сессии и физические попытки не складываются; cache не прибавляется к input; reasoning не выдумывается. Recorded harness/route, provider и model не подменяют друг друга.

Сохранены фиксированные read-only источники, header-linked verified archive chain относительно data_dir, исключение baseline/group rollups и latest attempt_id по порядку поколений при seq reset. Только числовая телеметрия и необходимые task/project связи. Source repair, monetary locks/writes, статические цены не добавлялись.

## Выполненная проверка

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/oracle.py fixtures/numeric_snapshot.json
node --check widget.js
node tests/widget_contract.cjs
```

Итог Python: **84 обнаружено; 57 passed; 27 skipped; 0 failed**. Это 32 accounting, 24 ingestion и 1 тест явного import failure. 27 тестов plugin/preferences/реальных ASGI routes не исполнялись из-за отсутствующей Starlette. Тестовый `pip --isolated --target .test-deps --no-cache-dir` не смог установить её из-за DNS resolution failure. Поддельная Starlette или runtime fallback не использовались; пакет/requirements в payload не добавлялся.

Node **v24.16.0** доступен в этой копии. `node --check widget.js` и `node tests/widget_contract.cjs` — PASS. JS harness использует mock DOM/fetch, поэтому не доказывает геометрию, настоящее Widgets, PyWebView или WebKit. Проверены в том числе положительные session series/rankings, раздельность view, сохранение exact >2^53, предел Number, 23- и 25-часовой календарный день, поздние GET/PUT, intervening user selection при ошибке preferences, aria-busy и Dispose.

Oracle исходной синтетической фикстуры — PASS: 8 физических вызовов; input 1 250; output 40 140; сумма сообщённых input/output 41 390; cache-read 3 038 016; cache-write 449 091; confirmed $1.46, estimated $0.125, active held $1.20. Эти данные не являются телеметрией владельца.

Арифметические тесты отдельно покрывают нормализованные отсутствующие/частичные/лишние/null/zero/bool/negative/huge значения, сохранение legacy, неизвестные поля, все фильтры и периоды, границы календаря, own + descendants = aggregate и непересечение physical/session. Oracle ловит также 13 видов изменения точных строк при заново рассчитанном checksum.

Реальные `read`/`readline` инструментированы: неизменившийся refresh читает **0 байт** live ledger и архивов. Отмена во время catch-up сохраняет прошлый snapshot и допускает корректное продолжение. Missing/corrupt archives дают partial history и не восстанавливаются из устаревшего кэша.

## Параллельная работа и её статус

Три конкретных ветви Astra исправляли ingestion, accounting/oracle и host adapter; основной исполнитель интегрировал frontend и документацию. Sol выполнил отдельную диагностическую consumer QA без редактирования. Его замечания про catch-up aria-busy, preference race, смешанный record explorer и недостающие frontend tests устранены. Это диагностические результаты, а не approval своего изменения. Внешне наблюдаемый subscription harness не выводился из имени модели.

Oracle поддерживался авторами в ходе реализации. Запрошенная родителем финальная независимая **non-author Sol numeric verification после интеграции** по-прежнему необходима; этот отчёт её не подменяет.

## Что осталось родителю

1. Применить итоговый payload diff штатным lane. Предыдущие task artifacts, orphan reconciliation, terminality, grants/review/enablement и live payload находятся за пределами полномочий этой копии; они здесь не читались и не изменялись. Точные API/SSOT приняты из родительского контракта; docs/CREATING_SKILLS.md и DESIGN не доступны внутри payload.
2. В интерпретаторе host выполнить `python3 -B -c 'import starlette'` и полный Python suite без skipped. В suite уже есть настоящая Starlette ASGI dispatch-проверка без зависимости от httpx, строгая регистрация UI/routes, чтение/запись preferences и unload/off-loop tests.
3. Выполнить финальную независимую числовую сверку источника/экспорта подходящим Sol actor. Экспортная арифметика сама по себе не доказывает полноту доступной archive chain.
4. После штатной host-интеграции проверить настоящие Widgets при **desktop 1100×722**, Chromium и релевантном PyWebView/WebKit: Start/Stop, все presets/calendar/model/harness/project/root-task, own/descendants, оба accounting view, charts/rankings, details, retry/export, клавиатуру и восстановление фильтров. Осмотреть и отправить реальные screenshots. Live-клики, vision-inspection и enablement здесь не заявлены.

Ограничения реализации: индекс остаётся в памяти; restart перечитывает историю; chain limit 128 раскрывает partial; сохранённые inode/размер/mtime/anchors не позволяют обнаружить скрытое внутреннее переписывание. Неизвестные токены/связи/маршруты не восстанавливаются догадками. Виджет использует один внутренний scroller и максимальную высоту 560, но окончательная геометрия внутри host требует указанной live-проверки.

Commit, перемещение HEAD, публикация, enable, self-review, изменения core/других widgets/контролей не выполнялись. Дополнительная внешняя OS filesystem boundary по раскрытию Claudexor не применялась; действовал native harness access mode. Одна дочерняя попытка `py_compile` была перенаправлена окружением в внешний bytecode cache и отказана PermissionError; успешной внешней записи не было. Последующие Python-проверки выполнялись с `-B`/отключённой записью bytecode, синтаксис — in-memory compile. Scoped HOME не рассматривался как ограничение доступа.

## Parent integration corrections

Actual host rejects leading slashes in register_route: registered names are data, export, preferences. Parent corrected these names and strict API/ASGI tests after applying the snapshot. Root-task selection now defaults to Own + descendants so rating clicks retain the meaning of their displayed sum. Final live validation remains a separate parent obligation.
