"""Protect the cleanup boundary and the committed audit/coverage snapshots."""
import ast
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class StructureTests(unittest.TestCase):
    def test_active_imports_do_not_depend_on_retired_packages(self):
        for base in ('app', 'scripts', 'evals/incident'):
            for path in (ROOT/base).rglob('*.py'):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    names = [a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                    for name in names:
                        self.assertFalse(name.split('.')[0] in {'backend','mult_agents','archive'}, f'{path}: {name}')
                        self.assertNotEqual(name, 'evals.run', str(path))

    def test_archived_files_are_preserved_without_content_changes(self):
        archive = ROOT/'archive/research'
        manifest = json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
        paths = {item['original_path'] for item in manifest['items']}
        self.assertTrue({'app/mult_agents/nodes.py', 'app/mult_agents/state.py',
            'app/mult_agents/harness/coverage.py', 'app/mult_agents/harness/validation.py'}.issubset(paths))
        for item in manifest['items']:
            self.assertEqual(hashlib.sha256((archive/item['archive_path']).read_bytes()).hexdigest(), item['sha256'], item['original_path'])
