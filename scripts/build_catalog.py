#!/usr/bin/env python3
"""Rebuild catalog.json from skills/* folders."""
from __future__ import annotations
import hashlib, json, pathlib, yaml
root = pathlib.Path(__file__).resolve().parents[1]
existing_order = {}
existing_entries = {}
catalog_path = root / 'catalog.json'
if catalog_path.exists():
    try:
        current = json.loads(catalog_path.read_text(encoding='utf-8'))
        existing_entries = {
            str(skill.get('slug') or ''): dict(skill)
            for skill in current.get('skills') or []
            if str(skill.get('slug') or '')
        }
        existing_order = {
            str(skill.get('slug') or ''): idx
            for idx, skill in enumerate(current.get('skills') or [])
        }
    except Exception:
        existing_order = {}
        existing_entries = {}
skills = []
skill_dirs = [p for p in (root / 'skills').iterdir() if p.is_dir()]
skill_dirs.sort(key=lambda p: (existing_order.get(p.name, 10_000), p.name))
for skill_dir in skill_dirs:
    if not skill_dir.is_dir() or not (skill_dir / 'SKILL.md').is_file():
        continue
    text = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
    front = {}
    if text.startswith('---'):
        parts = text.split('---', 2)
        if len(parts) >= 3:
            front = yaml.safe_load(parts[1]) or {}
    files = []
    for path in sorted(skill_dir.rglob('*')):
        rel = path.relative_to(skill_dir).as_posix()
        if '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo', '.so', '.dylib', '.dll', '.wasm'}:
            continue
        if path.is_file():
            data = path.read_bytes()
            files.append({'path': rel, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
    entry = dict(existing_entries.get(skill_dir.name) or {})
    entry.update({'slug': skill_dir.name, 'name': front.get('name', skill_dir.name), 'version': str(front.get('version', '0.1.0')), 'type': front.get('type', 'instruction'), 'files': files})
    if not entry.get('description'):
        entry['description'] = front.get('description', '')
    if front.get('install_specs'):
        entry['install_specs'] = front.get('install_specs')
    skills.append(entry)
catalog = {'schema_version': 1, 'name': 'OuroborosHub', 'description': 'Official Ouroboros skills catalog.', 'raw_base_url': 'https://raw.githubusercontent.com/razzant/OuroborosHub/main', 'skills': skills}
catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
