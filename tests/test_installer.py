# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).parents[1] / 'tools/editorial_package.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        for directory in ('pages', 'evidence', 'policies'):
            (self.project / directory).mkdir()
        for key in m.POLICIES:
            (self.project / 'policies' / (key + '.md')).write_text(key)
        self.config = dict(profile_schema_version=1, dossier_schema_version=1,
                           content_root='pages', dossier_root='evidence', language='en')
        self.config.update({key: 'policies/' + key + '.md' for key in m.POLICIES})
        self.write_profile()
        self.source = self.make_source('first', '0.1.0')

    def write_profile(self):
        (self.project / 'editorial.config.yaml').write_text('\n'.join(key + ': ' + str(value) for key, value in self.config.items()))

    def make_source(self, name, version):
        source = self.base / name
        for skill in m.SKILLS:
            directory = source / 'skills' / skill
            (directory / 'references').mkdir(parents=True)
            (directory / 'SKILL.md').write_text(name + ' ' + skill)
            (directory / 'references/rules.md').write_text('rules ' + name)
        files = {path.relative_to(source).as_posix(): m.sha(path) for path in source.rglob('*') if path.is_file()}
        manifest = dict(version=version, profile_schema_version=1, dossier_schema_version=1, files=files)
        (source / 'package-manifest.json').write_text(json.dumps(manifest))
        return source

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}

    def test_p02_candidate_does_not_follow_supplier_changes(self):
        m.install(self.project, self.source)
        before = self.snapshot()
        self.make_source('second', '0.2.0')
        self.assertEqual(before, self.snapshot())
        lock = json.loads((self.project / m.LOCK).read_text())
        self.assertEqual('candidate', lock['source_mode'])
        self.assertIsNone(lock['source_commit'])

    def test_p04_repeat_preserves_bytes_and_mtime_then_rollback(self):
        m.install(self.project, self.source)
        before = self.snapshot()
        lock = self.project / m.LOCK
        stamp = lock.stat().st_mtime_ns
        self.assertEqual('unchanged', m.install(self.project, self.source))
        self.assertEqual(stamp, lock.stat().st_mtime_ns)
        newer = self.make_source('second', '0.2.0')
        m.install(self.project, newer)
        m.install(self.project, self.source)
        self.assertEqual(before, self.snapshot())

    def test_p03_local_edit_or_untracked_file_preserved(self):
        m.install(self.project, self.source)
        path = self.project / m.ROOTS[0] / 'local.md'
        path.write_text('local')
        before = self.snapshot()
        with self.assertRaisesRegex(m.Invalid, 'Local skill changes'):
            m.install(self.project, self.make_source('second', '0.2.0'))
        self.assertEqual(before, self.snapshot())

    def test_p03_partial_copy_failure_restores_both_skills_and_lock(self):
        m.install(self.project, self.source)
        before = self.snapshot()
        newer = self.make_source('second', '0.2.0')
        real_copy = shutil.copytree
        def failing_copy(src, dst, *args, **kwargs):
            if Path(src) == newer / 'skills/article-review':
                raise OSError('injected copy failure')
            return real_copy(src, dst, *args, **kwargs)
        with patch.object(m.shutil, 'copytree', side_effect=failing_copy):
            with self.assertRaisesRegex(OSError, 'injected'):
                m.install(self.project, newer)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / m.JOURNAL).exists())

    def test_tampered_or_undeclared_source_blocked(self):
        (self.source / 'skills/research/SKILL.md').write_text('tampered')
        with self.assertRaisesRegex(m.Invalid, 'hash mismatch'):
            m.install(self.project, self.source)
        self.assertFalse((self.project / m.LOCK).exists())

    def test_undeclared_package_file_blocked(self):
        (self.source / 'skills/research/extra.md').write_text('extra')
        with self.assertRaisesRegex(m.Invalid, 'undeclared'):
            m.install(self.project, self.source)

    def test_lock_write_failure_restores_previous_state(self):
        m.install(self.project, self.source)
        before = self.snapshot()
        newer = self.make_source('second', '0.2.0')
        replace = m.os.replace
        def fail_lock(src, dst):
            if Path(dst) == self.project / m.LOCK:
                raise OSError('injected lock failure')
            return replace(src, dst)
        with patch.object(m.os, 'replace', side_effect=fail_lock):
            with self.assertRaisesRegex(OSError, 'injected lock failure'):
                m.install(self.project, newer)
        self.assertEqual(before, self.snapshot())

    def test_unfinished_transaction_blocks_install(self):
        m.install(self.project, self.source)
        (self.project / m.JOURNAL).write_text('{}')
        with self.assertRaisesRegex(m.Invalid, 'Unfinished transaction'):
            m.install(self.project, self.source)

    def test_invalid_profile_blocks_before_mutation(self):
        for key, value in [('profile_schema_version', 2), ('dossier_schema_version', True), ('research_policy', '../outside'), ('content_root', '/tmp'), ('language', '')]:
            with self.subTest(key=key, value=value):
                old = self.config[key]
                self.config[key] = value
                self.write_profile()
                with self.assertRaises(m.Invalid):
                    m.install(self.project, self.source)
                self.config[key] = old
        self.assertFalse((self.project / '.agents').exists())

    def test_missing_policy_and_duplicate_yaml_keys(self):
        del self.config['testing_profile']
        self.write_profile()
        with self.assertRaisesRegex(m.Invalid, 'testing_profile'):
            m.profile(self.project)
        with (self.project / 'editorial.config.yaml').open('a') as stream:
            stream.write('\nlanguage: ru\n')
        with self.assertRaisesRegex(m.Invalid, 'Invalid YAML'):
            m.profile(self.project)

    def test_symlink_escape_rejected(self):
        (self.project / 'linked').symlink_to(self.base, target_is_directory=True)
        self.config['dossier_root'] = 'linked'
        self.write_profile()
        with self.assertRaisesRegex(m.Invalid, 'Symlink'):
            m.install(self.project, self.source)

    def test_existing_unowned_dir_needs_exact_explicit_adoption(self):
        directory = self.project / m.ROOTS[0]
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text('existing')
        with self.assertRaisesRegex(m.Invalid, 'adopt-manifest'):
            m.install(self.project, self.source)
        adoption = self.base / 'adopt.json'
        adoption.write_text(json.dumps({'files': m.installed_files(self.project)}))
        m.install(self.project, self.source, adoption)
        m.validate_lock(self.project, json.loads((self.project / m.LOCK).read_text()))

    def test_missing_dossier_root_allowed_but_file_rejected(self):
        (self.project / 'evidence').rmdir()
        m.install(self.project, self.source)
        self.assertFalse((self.project / 'evidence').exists())
        (self.project / 'evidence').write_text('wrong type')
        with self.assertRaisesRegex(m.Invalid, 'must be a directory'):
            m.install(self.project, self.source)

    def test_active_operation_blocks_install_check_and_recover(self):
        marker = self.project / m.OPERATION_LOCK
        marker.write_text('operator')
        before = self.snapshot()
        for function, args in [(m.install, (self.source,)), (m.check, ()), (m.recover, ())]:
            with self.assertRaisesRegex(m.Invalid, 'Active operation lock'):
                function(self.project, *args)
            self.assertEqual(before, self.snapshot())

    def test_untracked_empty_dir_rejected(self):
        m.install(self.project, self.source)
        (self.project / m.ROOTS[0] / 'local-empty').mkdir()
        with self.assertRaisesRegex(m.Invalid, 'empty directory'):
            m.install(self.project, self.source)


    def test_profile_parsing_uses_only_python(self):
        with patch('subprocess.Popen', side_effect=AssertionError('External parser invoked')):
            parsed = m.profile(self.project)
        self.assertEqual(parsed, self.config)

    def test_yaml_aliases_tags_and_multiple_documents_rejected(self):
        baseline = (self.project / 'editorial.config.yaml').read_text()
        cases = [
            baseline + '\nlanguage: en\n',
            baseline.replace('language: en', 'language: &value en') + '\nextra: *value\n',
            baseline.replace('language: en', 'language: !!python/object/apply:os.system ["exit 0"]'),
            baseline + '\n---\nlanguage: en\n',
            baseline + '\n? [a, b]\n: invalid\n',
            baseline + '\n3: invalid\n',
        ]
        for text in cases:
            with self.subTest(profile=text):
                (self.project / 'editorial.config.yaml').write_text(text)
                with self.assertRaises(m.Invalid):
                    m.profile(self.project)
        (self.project / 'editorial.config.yaml').write_text(baseline)

    def test_missing_pyyaml_has_actionable_error(self):
        import builtins
        original = builtins.__import__

        def missing(name, *args, **kwargs):
            if name == 'yaml':
                raise ImportError('synthetic missing dependency')
            return original(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=missing):
            with self.assertRaisesRegex(m.Invalid, 'PyYAML'):
                m.profile(self.project)

if __name__ == '__main__':
    unittest.main()
