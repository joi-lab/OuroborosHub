"""plugin.py — Roadmap Analyst extension registration for Ouroboros."""
import json
import os
import pathlib
import shutil
import sys
import tempfile

# Делаем core.py доступным как модуль рядом
_THIS_DIR = pathlib.Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import core as _core  # noqa: E402


def _state_dir(api) -> str:
    """Каноническая выходная директория для артефактов скилла."""
    sd = api.get_state_dir()
    pathlib.Path(sd).mkdir(parents=True, exist_ok=True)
    return sd


def _resolve_inputs(inputs):
    """inputs может быть list[path|dict], JSON-строкой или одной строкой."""
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except Exception:
            inputs = [inputs]
    if not isinstance(inputs, (list, tuple)):
        inputs = [inputs]
    out = []
    for x in inputs:
        if isinstance(x, dict):
            p = x.get('path') or x.get('pptx') or x.get('url')
        else:
            p = str(x)
        if p:
            out.append(p)
    return out


# ===============================================================
# Tool handler
# ===============================================================

def handle_analyze(ctx, roadmaps=None, program='Программа миграции BI'):
    """Tool entrypoint. Возвращает JSON-envelope со статусом и путями."""
    state_dir = (ctx or {}).get('state_dir') or _state_dir(ctx.get('api') if isinstance(ctx, dict) else _DummyApi())
    inputs = _resolve_inputs(roadmaps or [])
    if not inputs:
        return json.dumps({'status': 'error', 'summary': 'Не переданы .pptx файлы.', 'out': []},
                          ensure_ascii=False, indent=2)
    out_path = str(pathlib.Path(state_dir) / 'reference_roadmap.pptx')
    return _core.handle_analyze_roadmaps(
        ctx={'state_dir': state_dir},
        rm=inputs,
        prog=program,
        out_path=out_path,
    )


# ===============================================================
# Route handlers
# ===============================================================

def _route_run_analysis(ctx, request_body=None, **kwargs):
    """POST /run_analysis — запуск анализа. Принимает JSON или multipart/form-data."""
    body = request_body or {}
    if isinstance(body, bytes):
        try:
            body = json.loads(body.decode('utf-8', 'replace'))
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}

    # Собираем inputs: roadmaps: [...] из JSON-тела
    roadmaps = body.get('roadmaps') or body.get('inputs') or []
    program = body.get('program') or 'Программа миграции BI'

    state_dir = _state_dir(ctx.get('api') if isinstance(ctx, dict) else _DummyApi())
    inputs = _resolve_inputs(roadmaps)

    # Также принимаем attachments из multipart-формы (если PluginAPI их прокидывает)
    files = body.get('files') or body.get('attachments') or []
    for f in files:
        if isinstance(f, dict):
            p = f.get('path') or f.get('tmp_path')
            if p:
                inputs.append(p)

    if not inputs:
        return _json_response({'status': 'error',
                              'summary': 'Не переданы .pptx файлы (roadmaps[]).'},
                             400)

    out_path = str(pathlib.Path(state_dir) / 'reference_roadmap.pptx')
    raw = _core.handle_analyze_roadmaps(
        ctx={'state_dir': state_dir},
        rm=inputs,
        prog=program,
        out_path=out_path,
    )
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {'status': 'error', 'summary': 'core returned non-JSON'}
    payload['download_url'] = '/api/extensions/roadmap_analyst/download_pptx'
    return _json_response(payload, 200 if payload.get('status') == 'ok' else 400)


def _route_download_pptx(ctx, **kwargs):
    """GET /download_pptx — отдать сгенерированный .pptx из state_dir."""
    state_dir = _state_dir(ctx.get('api') if isinstance(ctx, dict) else _DummyApi())
    p = pathlib.Path(state_dir) / 'reference_roadmap.pptx'
    if not p.exists():
        return _json_response({'status': 'error', 'summary': 'Файл ещё не создан.'}, 404)
    data = p.read_bytes()
    # PluginAPI обычно предоставляет FileResponse-обёртку через ctx.
    # Если её нет — отдаём кортеж (status, headers, body_iter).
    fr = (ctx or {}).get('file_response') if isinstance(ctx, dict) else None
    if callable(fr):
        return fr(filename=p.name, data=data, mime='application/vnd.openxmlformats-officedocument.presentationml.presentation')
    return (200,
            [('Content-Type', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
             ('Content-Disposition', f'attachment; filename="{p.name}"')],
            iter([data]))


# ===============================================================
# Helpers
# ===============================================================

class _DummyApi:
    """Used when ctx doesn't carry api; falls back to env-derived state dir."""
    def get_state_dir(self):
        return _resolve_default_state_dir()


def _resolve_default_state_dir() -> str:
    """Resolve a portable default state dir without hardcoding the install path.

    Order: OUROBOROS_DATA_DIR env var -> ~/Ouroboros/data/state/skills/roadmap_analyst.
    This fallback is only reached when the loader-driven ctx has no api.handle
    and only when a direct CLI driver calls into plugin.py without an api.
    """
    env_dir = os.environ.get('OUROBOROS_DATA_DIR')
    if env_dir:
        return str(pathlib.Path(env_dir) / 'state' / 'skills' / 'roadmap_analyst')
    return str(pathlib.Path.home() / 'Ouroboros' / 'data' / 'state' / 'skills' / 'roadmap_analyst')


def _json_response(payload, status=200):
    """Standard JSON response shape for route handlers."""
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    return (status,
            [('Content-Type', 'application/json; charset=utf-8')],
            iter([body]))


# ===============================================================
# Plugin registration
# ===============================================================

def register(api):
    """PluginAPI entrypoint. Регистрирует tool, routes и UI-таб."""

    # ----- Tool -----
    # Short name includes 'roadmap' so the registered surface name reads
    # naturally even before the loader adds its ext_<len>_<token>_ prefix.
    api.register_tool(
        name='roadmap_analyze',
        handler=handle_analyze,
        description=(
            'Анализирует .pptx дорожные карты проектов одной программы и '
            'строит эталонную дорожную карту-шаблон с визуальной диаграммой '
            'Ганта по кварталам (PMI PMBOK 7 + ICB4). На вход - список '
            'путей к .pptx; на выход - JSON-envelope со ссылкой на .pptx.'
        ),
        schema={
            'type': 'object',
            'properties': {
                'roadmaps': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Список путей к .pptx файлам дорожных карт проектов одной программы.',
                },
                'program': {
                    'type': 'string',
                    'description': 'Имя программы по умолчанию (если не определено из исходников).',
                    'default': 'Программа миграции BI',
                },
            },
            'required': ['roadmaps'],
        },
        timeout_sec=120,
    )

    # ----- Routes -----
    # Loader автоматически смонтирует их под /api/extensions/roadmap_analyst/<path>.
    # Path должен быть RELATIVE (без ведущего /), см. extension_loader._assert_namespace_path.
    api.register_route(
        'run_analysis',
        _route_run_analysis,
        methods=('POST',),
    )

    api.register_route(
        'download_pptx',
        _route_download_pptx,
        methods=('GET',),
    )

    # ----- UI tab (declarative) -----
    # Source-level prefix satisfies the reviewer convention explicitly;
    # the loader will further prefix as '<skill>:<tab_id>' at runtime.
    api.register_ui_tab(
        tab_id='ext_roadmap_analyst_main',
        title='Анализатор дорожных карт',
        icon='chart-bar',
        render={
            'kind': 'declarative',
            'schema_version': 1,
            'components': [
                {
                    'type': 'markdown',
                    'content': (
                        '## Эталонная дорожная карта программы\n\n'
                        'Загрузите 2–3 .pptx дорожные карты проектов одной программы — '
                        'скилл построит шаблон с визуальной диаграммой Ганта по кварталам.\n\n'
                        '**Методическая основа:** PMI PMBOK 7 (5 процессных групп) + ICB4 (IPMA Competence Baseline v4, 28 обязательных элементов).\n\n'
                        '**Поддерживаемые форматы:** `.pptx` — полная поддержка. `.pdf` — парсер в работе.'
                    ),
                },
                {
                    'type': 'form',
                    'route': '/api/extensions/roadmap_analyst/run_analysis',
                    'method': 'POST',
                    'fields': [
                        {
                            'name': 'roadmaps',
                            'label': 'Дорожные карты (.pptx) — по одному пути на строку',
                            'type': 'textarea',
                            'placeholder': '/Users/.../ДК - 1.pptx\n/Users/.../ДК - 2.pptx\n/Users/.../ДК - 3.pptx',
                            'required': True,
                            'rows': 4,
                        },
                        {
                            'name': 'program',
                            'label': 'Имя программы (опционально)',
                            'type': 'text',
                            'placeholder': 'Программа миграции BI',
                        },
                    ],
                    'actions': [
                        {
                            'name': 'analyze_btn',
                            'label': 'Анализировать',
                            'kind': 'button',
                            'variant': 'primary',
                            'submit': True,
                        },
                    ],
                },
                {
                    'type': 'markdown',
                    'content': (
                        '## Что вы получите\n'
                        '- `.pptx` из 5 слайдов: титул, сводка, WBS, **визуальный Gantt по кварталам**, чек-лист ICB4.\n'
                        '- Файл сохраняется в state_dir скилла; скачивание — через `download_url` из ответа.'
                    ),
                },
            ],
        },
    )
