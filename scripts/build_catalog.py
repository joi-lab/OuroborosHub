#!/usr/bin/env python3
"""Rebuild catalog.json from skills/* folders."""
from __future__ import annotations
import hashlib, json, pathlib, yaml
root = pathlib.Path(__file__).resolve().parents[1]
skills = []
for skill_dir in sorted((root / 'skills').iterdir()):
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
    skills.append({'slug': skill_dir.name, 'name': front.get('name', skill_dir.name), 'description': front.get('description', ''), 'version': str(front.get('version', '0.1.0')), 'type': front.get('type', 'instruction'), 'files': files})
catalog = {'schema_version': 1, 'name': 'OuroborosHub', 'description': 'Official Ouroboros skills catalog.', 'raw_base_url': 'https://raw.githubusercontent.com/joi-lab/OuroborosHub/main', 'skills': skills}
(root / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
