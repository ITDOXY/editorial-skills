# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pin unit tests: Git responses below are synthetic, never actual supplier commits."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_installer as base

m = base.m
A, B = 'a' * 40, 'b' * 40
ORIGIN = 'https://example.invalid/editorial-core.git'

class GitPinTests(unittest.TestCase):
    setUp = base.InstallerTests.setUp
    write_profile = base.InstallerTests.write_profile
    make_source = base.InstallerTests.make_source
    snapshot = base.InstallerTests.snapshot

    def objects(self, source):
        return {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob('*') if p.is_file()}

    def reader(self, commits, modes=None, resolved=None):
        blobs = {}
        for tree in commits.values():
            for data in tree.values():
                oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                blobs[oid] = data
        def read(source, *args):
            self.assertEqual(source, self.source)
            if args == ('rev-parse', '--show-toplevel'):
                return (str(source) + '\n').encode()
            if args == ('config', '--get', 'remote.origin.url'):
                return (ORIGIN + '\n').encode()
            if args[0] == 'rev-parse':
                commit = args[-1].split('^')[0]
                if commit not in commits:
                    raise m.Invalid('Requested commit unavailable')
                return ((resolved or commit) + '\n').encode()
            if args[:3] == ('ls-tree', '-r', '-z'):
                tree = commits[args[-1]]
                entries = []
                for name, data in tree.items():
                    oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                    mode = (modes or {}).get(name, '100644')
                    kind = 'commit' if mode == '160000' else 'blob'
                    entries.append((mode + ' ' + kind + ' ' + oid + '\t' + name).encode() + b'\0')
                return b''.join(entries)
            if args[:2] == ('cat-file', 'blob'):
                return blobs[args[-1]]
            self.fail('Unexpected Git action: ' + repr(args))
        return read

    def pin(self, commit=A, commits=None, **kwargs):
        commits = commits or {A: self.objects(self.source)}
        with patch.object(m, '_git', side_effect=self.reader(commits)):
            return m.install_git(self.project, self.source, commit, **kwargs)

    def test_cli_lists_explicit_commit_mode(self):
        r = subprocess.run([sys.executable, str(Path(m.__file__)), '--help'], text=True, capture_output=True)
        self.assertEqual(0, r.returncode)
        self.assertIn('--commit', r.stdout)

    def test_rejects_branch_abbreviation_and_noncommit_before_git_read(self):
        for value in ('main', 'v0.1.0', 'abc123', '', 'A' * 40, '--help'):
            with self.subTest(value=value), patch.object(m, '_git') as git:
                before = self.snapshot()
                with self.assertRaises(m.Invalid):
                    m.install_git(self.project, self.source, value)
                git.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_pin_records_full_identity_and_repeat_preserves_mtime(self):
        self.pin()
        before = self.snapshot()
        lock = self.project / m.LOCK
        stamp = lock.stat().st_mtime_ns
        data = json.loads(lock.read_text())
        self.assertEqual(('git', ORIGIN, A), (data['source_mode'], data['source_repository'], data['source_commit']))
        self.assertNotIn('candidate_manifest_sha256', data)
        self.assertEqual(m.sha(self.source / 'package-manifest.json'), data['source_manifest_sha256'])
        self.assertEqual('unchanged', self.pin())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(stamp, lock.stat().st_mtime_ns)
        m.check(self.project)

    def test_dirty_worktree_and_newer_commit_do_not_change_selected_pin(self):
        first = self.objects(self.source)
        newer = self.make_source('second', '0.2.0')
        commits = {A: first, B: self.objects(newer)}
        (self.source / 'skills/research/SKILL.md').write_text('uncommitted and not trusted')
        self.pin(commits=commits)
        self.assertEqual(first['skills/research/SKILL.md'], (self.project / m.ROOTS[0] / 'SKILL.md').read_bytes())
        self.assertEqual(A, json.loads((self.project / m.LOCK).read_text())['source_commit'])

    def test_update_then_rollback_restores_files_and_exact_lock(self):
        newer = self.make_source('second', '0.2.0')
        commits = {A: self.objects(self.source), B: self.objects(newer)}
        self.pin(commits=commits)
        before = self.snapshot()
        self.pin(B, commits)
        self.assertEqual(B, json.loads((self.project / m.LOCK).read_text())['source_commit'])
        self.pin(A, commits)
        self.assertEqual(before, self.snapshot())

    def test_identical_files_at_different_commits_update_pin_identity(self):
        objects = self.objects(self.source)
        self.pin(commits={A: objects, B: objects})
        self.pin(B, {A: objects, B: objects})
        self.assertEqual(B, json.loads((self.project / m.LOCK).read_text())['source_commit'])

    def test_local_edits_block_pinned_update_without_overwriting(self):
        self.pin()
        f = self.project / m.ROOTS[0] / 'SKILL.md'
        f.write_text('local edit')
        before = self.snapshot()
        with self.assertRaisesRegex(m.Invalid, 'Local skill changes'):
            self.pin()
        self.assertEqual(before, self.snapshot())

    def test_partial_copy_failure_restores_pinned_lock_and_both_skills(self):
        newer = self.make_source('second', '0.2.0')
        commits = {A: self.objects(self.source), B: self.objects(newer)}
        self.pin(commits=commits)
        before = self.snapshot()
        real = m.shutil.copytree
        failed = []
        def fail(src, dst, *args, **kwargs):
            if Path(dst) == self.project / m.ROOTS[1] and not failed:
                failed.append(True)
                raise OSError('injected pin copy failure')
            return real(src, dst, *args, **kwargs)
        with patch.object(m.shutil, 'copytree', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'injected'):
                self.pin(B, commits)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / m.JOURNAL).exists())

    def test_lock_write_failure_restores_pinned_state(self):
        newer = self.make_source('second', '0.2.0')
        commits = {A: self.objects(self.source), B: self.objects(newer)}
        self.pin(commits=commits)
        before = self.snapshot()
        real = m.os.replace
        def fail(src, dst):
            if Path(dst) == self.project / m.LOCK:
                raise OSError('injected pin lock failure')
            return real(src, dst)
        with patch.object(m.os, 'replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'injected'):
                self.pin(B, commits)
        self.assertEqual(before, self.snapshot())

    def test_interrupted_journal_blocks_pin_until_recovery(self):
        self.pin()
        before = self.snapshot()
        backup = self.project / '.agents/editorial-backup-pin-test'
        backup.mkdir()
        for i, rel in enumerate(m.ROOTS + (m.LOCK,)):
            original = self.project / rel
            if original.is_dir():
                shutil.copytree(original, backup / str(i))
            else:
                shutil.copy2(original, backup / str(i))
        (self.project / m.JOURNAL).write_text(json.dumps({'backup': '.agents/editorial-backup-pin-test'}))
        (self.project / m.ROOTS[0] / 'SKILL.md').write_text('interrupted')
        with self.assertRaisesRegex(m.Invalid, 'Unfinished transaction'):
            self.pin()
        m.recover(self.project)
        self.assertEqual(before, self.snapshot())

    def test_incompatible_profile_and_committed_manifest_block_before_mutation(self):
        tree = self.objects(self.source)
        manifest = json.loads(tree['package-manifest.json'])
        manifest['dossier_schema_version'] = 2
        tree['package-manifest.json'] = json.dumps(manifest).encode()
        before = self.snapshot()
        with self.assertRaises(m.Invalid):
            self.pin(commits={A: tree})
        self.assertEqual(before, self.snapshot())
        self.config['profile_schema_version'] = 2
        self.write_profile()
        before = self.snapshot()
        with self.assertRaises(m.Invalid):
            self.pin()
        self.assertEqual(before, self.snapshot())

    def test_tampered_blob_and_undeclared_skill_file_block(self):
        for kind in ('tampered', 'undeclared'):
            with self.subTest(kind=kind):
                tree = self.objects(self.source)
                tree['skills/research/SKILL.md' if kind == 'tampered' else 'skills/research/extra.md'] = b'unexpected'
                before = self.snapshot()
                with self.assertRaises(m.Invalid):
                    self.pin(commits={A: tree})
                self.assertEqual(before, self.snapshot())

    def test_symlink_and_gitlink_modes_rejected(self):
        tree = self.objects(self.source)
        for mode in ('120000', '160000'):
            with self.subTest(mode=mode), patch.object(m, '_git', side_effect=self.reader({A: tree}, modes={'skills/research/SKILL.md': mode})):
                with self.assertRaises(m.Invalid):
                    m.install_git(self.project, self.source, A)
                self.assertFalse((self.project / m.LOCK).exists())

    def test_origin_mismatch_missing_commit_and_tag_object_rejected(self):
        before = self.snapshot()
        with self.assertRaises(m.Invalid):
            self.pin(repository='https://example.invalid/other.git')
        with patch.object(m, '_git', side_effect=self.reader({A: self.objects(self.source)}, resolved=B)):
            with self.assertRaises(m.Invalid):
                m.install_git(self.project, self.source, A)
        with self.assertRaises(m.Invalid):
            self.pin(B)
        self.assertEqual(before, self.snapshot())

    def test_unsafe_declared_path_rejected_without_writing_outside(self):
        tree = self.objects(self.source)
        name = 'skills/research/../../escape'
        tree[name] = b'escape'
        manifest = json.loads(tree['package-manifest.json'])
        manifest['files'][name] = hashlib.sha256(b'escape').hexdigest()
        tree['package-manifest.json'] = json.dumps(manifest).encode()
        before = self.snapshot()
        with self.assertRaises(m.Invalid):
            self.pin(commits={A: tree})
        self.assertEqual(before, self.snapshot())

    def test_corrupt_pin_metadata_is_not_accepted_by_check(self):
        self.pin()
        f = self.project / m.LOCK
        x = json.loads(f.read_text())
        x['source_commit'] = 'main'
        f.write_text(json.dumps(x))
        with self.assertRaises(m.Invalid):
            m.check(self.project)


    def test_pin_lock_write_failure_restores_old_identity(self):
        newer = self.make_source('second', '0.2.0')
        commits = {A: self.objects(self.source), B: self.objects(newer)}
        self.pin(commits=commits)
        before = self.snapshot()
        real = m.os.replace
        def fail(src, dst):
            if Path(dst) == self.project / m.LOCK:
                raise OSError('injected pinned lock failure')
            return real(src, dst)
        with patch.object(m.os, 'replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'injected pinned lock'):
                self.pin(B, commits)
        self.assertEqual(before, self.snapshot())

    def test_pinned_interrupted_transaction_blocks_then_recovers(self):
        self.pin()
        before = self.snapshot()
        backup = self.project / '.agents/editorial-backup-pin-test'
        backup.mkdir()
        for index, relative in enumerate(m.ROOTS + (m.LOCK,)):
            src = self.project / relative
            if src.is_dir():
                shutil.copytree(src, backup / str(index))
            else:
                shutil.copy2(src, backup / str(index))
        (self.project / m.JOURNAL).write_text(json.dumps({'backup': backup.relative_to(self.project).as_posix()}))
        (self.project / m.ROOTS[0] / 'SKILL.md').write_text('interrupted')
        (self.project / m.LOCK).unlink()
        interrupted = self.snapshot()
        with self.assertRaisesRegex(m.Invalid, 'Unfinished transaction'):
            self.pin()
        self.assertEqual(interrupted, self.snapshot())
        self.assertEqual('restored', m.recover(self.project))
        self.assertEqual(before, self.snapshot())
        m.check(self.project)

    def test_cli_rejects_mixed_candidate_and_commit(self):
        before = self.snapshot()
        r = subprocess.run([sys.executable, str(Path(m.__file__)), 'install', '--project', str(self.project),
                            '--source', str(self.source), '--candidate', '--commit', A], text=True, capture_output=True)
        self.assertEqual(1, r.returncode)
        self.assertIn('exactly one', r.stderr)
        self.assertEqual(before, self.snapshot())

    def test_full_sha256_object_id_is_supported_as_syntax(self):
        value = 'c' * 64
        self.pin(value, {value: self.objects(self.source)})
        self.assertEqual(value, json.loads((self.project / m.LOCK).read_text())['source_commit'])

    def test_pinned_lock_write_failure_restores_previous_pin(self):
        newer = self.make_source('second', '0.2.0')
        commits = {A: self.objects(self.source), B: self.objects(newer)}
        self.pin(commits=commits)
        before = self.snapshot()
        real = m.atomic_json
        def fail(path, value):
            if path == self.project / m.LOCK:
                raise OSError('injected pinned lock failure')
            return real(path, value)
        with patch.object(m, 'atomic_json', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'injected pinned lock'):
                self.pin(B, commits)
        self.assertEqual(before, self.snapshot())

    def test_candidate_to_pin_and_back_preserves_candidate_bytes(self):
        m.install(self.project, self.source)
        before = self.snapshot()
        self.pin()
        self.assertEqual('git', json.loads((self.project / m.LOCK).read_text())['source_mode'])
        m.install(self.project, self.source)
        self.assertEqual(before, self.snapshot())

    def test_recover_interrupted_pin_state_after_operation_marker_cleanup(self):
        self.pin()
        before = self.snapshot()
        backup = self.project / '.agents/editorial-backup-pinsynthetic'
        backup.mkdir()
        for index, relative in enumerate(m.ROOTS + (m.LOCK,)):
            src, dst = self.project / relative, backup / str(index)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        m.atomic_json(self.project / m.JOURNAL, {'backup': backup.relative_to(self.project).as_posix()})
        (self.project / m.ROOTS[0] / 'SKILL.md').write_text('interrupted write')
        (self.project / m.LOCK).unlink()
        # Fixture models an interrupted process after operator-confirmed marker cleanup.
        pending = self.snapshot()
        with self.assertRaisesRegex(m.Invalid, 'Unfinished transaction'):
            m.check(self.project)
        with self.assertRaisesRegex(m.Invalid, 'Unfinished transaction'):
            self.pin()
        self.assertEqual(pending, self.snapshot())
        self.assertEqual('restored', m.recover(self.project))
        self.assertEqual(before, self.snapshot())
        m.check(self.project)

if __name__ == '__main__':
    unittest.main()
