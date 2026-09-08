"""The catalog declares packages; checking it must not rewrite curated data."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_catalog.py'
spec = importlib.util.spec_from_file_location('hub_catalog_builder', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def entry(self, slug='demo', name='Shared display name'):
        directory = self.root / 'skills' / slug
        directory.mkdir(parents=True)
        contents = {'SKILL.md': f'---\nname: {slug}\nversion: "nightly blue"\ntype: instruction\n---\nSkill body\n'.encode(),
                    'readme.txt': b'public payload\n'}
        for relative, body in contents.items():
            (directory / relative).write_bytes(body)
        return {'slug': slug, 'name': name, 'version': 'nightly blue', 'type': 'instruction',
                'files': [{'path': relative, 'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}
                          for relative, body in contents.items()]}

    def catalog(self, *entries):
        return {'schema_version': 1, 'curated': {'note': 'Keep this metadata'}, 'skills': list(entries)}

    def test_curated_catalog_preserves_format_metadata_and_unlisted_dev_files(self):
        first, second = self.entry('one'), self.entry('two')
        first['custom'] = {'homepage': 'https://example.org/'}
        first['files'][0]['sha256'] = first['files'][0]['sha256'].upper()
        del first['files'][0]['size']  # optional in the consumer's file contract
        (self.root / 'skills/one/development-notes.txt').write_text('not a package member')
        catalog = self.catalog(second, first)
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(catalog, indent=4) + '\n\n')
        before = path.read_bytes(), path.stat().st_mtime_ns
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(['--check'], root=self.root), 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertFalse(any(name == 'ouroboros' or name.startswith('ouroboros.') for name in sys.modules))

    def test_declared_hash_size_path_and_missing_manifest_errors(self):
        entry = self.entry()
        mutations = {
            'missing hash': lambda row: row['files'][0].pop('sha256'),
            'wrong hash': lambda row: row['files'][0].update(sha256='f' * 64),
            'wrong size': lambda row: row['files'][0].update(size=123456),
            'boolean size': lambda row: row['files'][0].update(size=True),
            'missing file': lambda row: row['files'][0].update(path='gone.txt'),
            'unsafe file': lambda row: row['files'][0].update(path='../outside'),
            'duplicate file': lambda row: row['files'].append(dict(row['files'][0])),
            'missing manifest': lambda row: row['files'].pop(0),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(entry)
                mutate(changed)
                with self.assertRaises(ValueError):
                    builder.validate_catalog(self.root, self.catalog(changed))

    def test_raw_base_url_preserves_fallback_and_rejects_unusable_bases(self):
        catalog = self.catalog(self.entry())
        for raw_base in (None, '', 'https://raw.githubusercontent.com/owner/repo/main',
                         'https://github.com/owner/repo/raw/main', 'http://localhost:8123'):
            with self.subTest(valid=raw_base):
                builder.validate_catalog(self.root, {**catalog, 'raw_base_url': raw_base})
        for raw_base in ('not a URL', {'url': 'https://github.com'}, 'https://example.org/',
                         'http://raw.githubusercontent.com/owner/repo/main',
                         'https://github.com/owner/repo?branch=main', 'https://github.com/owner/repo#main',
                         'https://user:password@github.com/owner/repo', 'http://localhost:not-a-port'):
            with self.subTest(invalid=raw_base), self.assertRaisesRegex(ValueError, 'raw_base_url'):
                builder.validate_catalog(self.root, {**catalog, 'raw_base_url': raw_base})

    def test_canonical_slug_collisions_include_normalization_and_truncation(self):
        for left, right in [('a b', 'a_b'), ('a' * 64 + 'x', 'a' * 64 + 'y')]:
            with self.subTest(left=left):
                first, second = self.entry(left), self.entry(right)
                with self.assertRaisesRegex(ValueError, 'canonical slug collision'):
                    builder.validate_catalog(self.root, self.catalog(first, second))
        entry = self.entry('duplicate')
        with self.assertRaisesRegex(ValueError, 'canonical slug collision'):
            builder.validate_catalog(self.root, self.catalog(entry, copy.deepcopy(entry)))

    def test_canonical_name_uses_the_consumer_naming_contract(self):
        for name, canonical in [(' ._Rain ☔._ ', 'Rain'), ('rain cloud', 'rain_cloud'),
                                ('雨-cloud', '雨-cloud'), ('...', '_unnamed'), ('x' * 70, 'x' * 64)]:
            with self.subTest(name=name):
                self.assertEqual(builder._canonical_name(name), canonical)

    def test_generator_validates_before_writing_and_preserves_curated_entry(self):
        entry = self.entry()
        entry['description'] = 'Curated description'
        entry['custom'] = {'keep': True}
        path = self.root / 'catalog.json'
        path.write_text(json.dumps(self.catalog(entry)))
        self.assertEqual(builder.main([], root=self.root), 0)
        generated = json.loads(path.read_text())
        builder.validate_catalog(self.root, generated)
        self.assertEqual(generated['skills'][0]['description'], 'Curated description')
        self.assertEqual(generated['skills'][0]['custom'], {'keep': True})
        self.entry('a b')
        self.entry('a_b')
        before = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(builder.main([], root=self.root), 1)
        self.assertIn('collision', error.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_check_errors_are_nonzero_and_do_not_repair_the_input(self):
        path = self.root / 'catalog.json'
        for raw in ['{', '[]', '{"schema_version":true,"skills":[]}', '{"schema_version":1,"skills":{}}']:
            with self.subTest(raw=raw):
                path.write_text(raw)
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(builder.main(['--check'], root=self.root), 1)
                self.assertIn('Catalog validation failed', error.getvalue())
                self.assertEqual(path.read_text(), raw)


if __name__ == '__main__':
    unittest.main()
