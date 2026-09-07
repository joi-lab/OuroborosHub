#!/usr/bin/env python3
"""Rebuild catalog.json, or check its declared packages without rewriting it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit

import yaml


def _canonical_name(slug: str) -> str:
    """Ouroboros skill_manifest canonical_skill_name: installation identity.

    Display names are independent. Keep this small public naming contract
    consistent with the consumer without importing its application runtime.
    """
    cleaned = ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in slug.strip()).strip('._')
    return cleaned[:64] if cleaned else '_unnamed'


def _file_facts(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_catalog(root: Path, catalog: object) -> None:
    """Validate published declarations, preserving curated metadata and membership."""
    if not isinstance(catalog, dict) or type(catalog.get('schema_version')) is not int or catalog['schema_version'] != 1:
        raise ValueError('catalog.schema_version must be 1')
    # The consumer derives an omitted/empty base from its GitHub catalog URL.
    # Explicit bases must support appending /skills/... under the same public
    # URL contract as ouroboros.marketplace.ouroboroshub._fetch_bytes.
    raw_base = catalog.get('raw_base_url')
    if raw_base is not None and raw_base != '':
        if not isinstance(raw_base, str):
            raise ValueError('catalog.raw_base_url must be a URL string')
        url = urlsplit(raw_base)
        if (url.scheme not in {'http', 'https'}
                or url.hostname not in {'raw.githubusercontent.com', 'github.com', 'localhost', '127.0.0.1'}
                or (url.scheme == 'http' and url.hostname not in {'localhost', '127.0.0.1'})
                or url.username is not None or url.password is not None or url.query or url.fragment):
            raise ValueError('catalog.raw_base_url must be a supported absolute base URL without credentials, query or fragment')
        try:
            url.port
        except ValueError as exc:
            raise ValueError('catalog.raw_base_url has an invalid port') from exc
    entries = catalog.get('skills')
    if not isinstance(entries, list):
        raise ValueError('catalog.skills must be a list')
    identities: dict[str, str] = {}
    skills_root = (root / 'skills').resolve()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get('slug'), str) or not entry['slug'].strip():
            raise ValueError(f'catalog.skills[{index}].slug must be a non-empty string')
        slug = entry['slug'].strip()
        if '/' in slug or '\\' in slug or ':' in slug or slug in {'.', '..'}:
            raise ValueError(f'{slug!r}: slug must name one skill directory')
        canonical = _canonical_name(slug)
        if canonical == '_unnamed':
            raise ValueError(f'{slug!r}: slug has no usable installation identity')
        if canonical in identities:
            raise ValueError(f'canonical slug collision: {identities[canonical]!r} and {slug!r} install as {canonical!r}')
        identities[canonical] = slug
        if not isinstance(entry.get('version'), str) or not entry['version'].strip():
            raise ValueError(f'{slug}: version must be a non-empty string')
        if 'name' in entry and not isinstance(entry['name'], str):
            raise ValueError(f'{slug}: name must be a string')
        if 'type' in entry and (not isinstance(entry['type'], str) or entry['type'] not in {'instruction', 'script', 'extension'}):
            raise ValueError(f'{slug}: unsupported skill type')
        files = entry.get('files')
        if not isinstance(files, list) or not files:
            raise ValueError(f'{slug}: files must be a non-empty list')
        skill_root = (skills_root / slug).resolve()
        if not skill_root.is_relative_to(skills_root) or not skill_root.is_dir():
            raise ValueError(f'{slug}: skill directory is missing or outside skills/')
        declared: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ValueError(f'{slug}: file path must be a string')
            text = item['path'].strip()
            path = PurePosixPath(text)
            if ('\\' in text or ':' in text or not path.parts or path.is_absolute() or '..' in path.parts
                    or any(part in {'node_modules', '.ouroboros_env', '__pycache__'} for part in path.parts)
                    or path.suffix.lower() in {'.pyc', '.pyo', '.so', '.dylib', '.dll'}):
                raise ValueError(f'{slug}: unsupported catalog file path {text!r}')
            relative = path.as_posix()
            if relative in declared:
                raise ValueError(f'{slug}: duplicate declared file {relative!r}')
            declared.add(relative)
            target = (skill_root / path).resolve()
            if not target.is_relative_to(skill_root) or not target.is_file():
                raise ValueError(f'{slug}/{relative}: declared file is missing or outside its skill')
            expected = item.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected.strip()):
                raise ValueError(f'{slug}/{relative}: sha256 must contain 64 hex characters')
            actual, size = _file_facts(target)
            if actual != expected.strip().lower():
                raise ValueError(f'{slug}/{relative}: sha256 mismatch; update the declared hash')
            if 'size' in item and (type(item['size']) is not int or item['size'] != size):
                raise ValueError(f'{slug}/{relative}: size must equal the file byte count ({size})')
        if 'SKILL.md' not in declared:
            raise ValueError(f'{slug}: declared files must include SKILL.md')


def build_catalog(root: Path) -> dict:
    """Preserve existing order/metadata while rebuilding the usual file inventory."""
    existing_order, existing_entries = {}, {}
    catalog_path = root / 'catalog.json'
    if catalog_path.exists():
        try:
            current = json.loads(catalog_path.read_text(encoding='utf-8'))
            existing_entries = {str(skill.get('slug') or ''): dict(skill)
                                for skill in current.get('skills') or [] if str(skill.get('slug') or '')}
            existing_order = {str(skill.get('slug') or ''): index
                              for index, skill in enumerate(current.get('skills') or [])}
        except (OSError, ValueError, TypeError, AttributeError):
            existing_order, existing_entries = {}, {}
    skills = []
    skill_dirs = [path for path in (root / 'skills').iterdir() if path.is_dir()]
    skill_dirs.sort(key=lambda path: (existing_order.get(path.name, 10_000), path.name))
    for skill_dir in skill_dirs:
        if not (skill_dir / 'SKILL.md').is_file():
            continue
        text = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
        front = {}
        if text.startswith('---'):
            parts = text.split('---', 2)
            if len(parts) >= 3:
                try:
                    front = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError as exc:
                    raise ValueError(f'{skill_dir.name}: invalid SKILL.md frontmatter') from exc
        if not isinstance(front, dict):
            raise ValueError(f'{skill_dir.name}: SKILL.md frontmatter must be an object')
        files = []
        for path in sorted(skill_dir.rglob('*')):
            if '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo', '.so', '.dylib', '.dll', '.wasm'}:
                continue
            if path.is_file():
                digest, size = _file_facts(path)
                files.append({'path': path.relative_to(skill_dir).as_posix(), 'sha256': digest, 'size': size})
        entry = dict(existing_entries.get(skill_dir.name) or {})
        entry.update({'slug': skill_dir.name, 'name': front.get('name', skill_dir.name),
                      'version': str(front.get('version', '0.1.0')), 'type': front.get('type', 'instruction'), 'files': files})
        if not entry.get('description'):
            entry['description'] = front.get('description', '')
        if front.get('install_specs'):
            entry['install_specs'] = front['install_specs']
        skills.append(entry)
    catalog = {'schema_version': 1, 'name': 'OuroborosHub', 'description': 'Official Ouroboros skills catalog.',
               'raw_base_url': 'https://raw.githubusercontent.com/razzant/OuroborosHub/main', 'skills': skills}
    validate_catalog(root, catalog)
    return catalog


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='validate the current catalog without rewriting it')
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[1]
    catalog_path = root / 'catalog.json'
    try:
        if args.check:
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
            validate_catalog(root, catalog)
            print(f"Catalog valid: {len(catalog['skills'])} skills")
        else:
            catalog = build_catalog(root)
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError) as exc:
        print(f'Catalog validation failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
