#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Editorial skill installer: explicit candidate or immutable local Git commit. Python 3.9+."""
import argparse
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import re
import subprocess
import sys
import tempfile

SKILLS = ('research', 'article-review')
LOCK = '.agents/editorial-skills.lock.json'
JOURNAL = '.agents/editorial-skills.transaction.json'
OPERATION_LOCK = '.editorial-skills.operation.lock'
ROOTS = tuple('.agents/skills/' + name for name in SKILLS)
POLICIES = ('research_policy', 'editorial_style', 'approval_policy', 'quality_policy', 'testing_profile')
FIELDS = ('profile_schema_version', 'dossier_schema_version', 'content_root', 'dossier_root', 'language') + POLICIES

class Invalid(ValueError):
    pass

def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Invalid('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def safe_path(root, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise Invalid('Path must be a nonempty relative project path')
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ('.', '..') for part in value.split('/')) or ':' in value:
        raise Invalid('Unsafe relative path: ' + value)
    path = root
    for part in pure.parts:
        path = path / part
        if path.is_symlink():
            raise Invalid('Symlink is not permitted: ' + value)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise Invalid('Path escapes root: ' + value)
    return path


def parse_profile_yaml(text):
    try:
        import yaml
    except ImportError as error:
        raise Invalid('PyYAML is required; install the package requirements.txt') from error

    class ProfileLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise yaml.YAMLError('YAML aliases are not permitted')
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise yaml.YAMLError('Profile keys must be strings')
                if key in seen:
                    raise yaml.YAMLError('Duplicate YAML key: ' + key)
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    try:
        return yaml.load(text, Loader=ProfileLoader)
    except yaml.YAMLError as error:
        raise Invalid('Invalid YAML profile (check duplicate keys, aliases, tags and document count)') from error


def profile(project):
    profile_path = safe_path(project, 'editorial.config.yaml')
    if not profile_path.is_file():
        raise Invalid('Missing editorial.config.yaml')
    data = parse_profile_yaml(profile_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise Invalid('Profile must be a YAML mapping')
    missing = [key for key in FIELDS if key not in data]
    if missing:
        raise Invalid('Missing profile settings: ' + ', '.join(missing))
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        raise Invalid('Unknown profile settings: ' + ', '.join(unknown))
    for key in ('profile_schema_version', 'dossier_schema_version'):
        if type(data[key]) is not int or data[key] != 1:
            raise Invalid('Unsupported ' + key + '; expected integer 1')
    if not isinstance(data['language'], str) or not data['language'].strip():
        raise Invalid('language must be a nonempty string')
    for key in ('content_root', 'dossier_root') + POLICIES:
        target = safe_path(project, data[key])
        if key in POLICIES and not target.is_file():
            raise Invalid('Missing policy file for ' + key + ': ' + data[key])
        if key == 'content_root' and not target.is_dir():
            raise Invalid('Missing directory for content_root: ' + data[key])
        if key == 'dossier_root' and target.exists() and not target.is_dir():
            raise Invalid('dossier_root must be a directory: ' + data[key])
    return data

@contextmanager
def operation(project):
    marker = safe_path(project, OPERATION_LOCK)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Invalid('Active operation lock; stop other installer/check/recover processes before retrying')
    try:
        os.close(descriptor)
        yield
    finally:
        marker.unlink()

def exclusive(function):
    @wraps(function)
    def wrapped(project, *args, **kwargs):
        with operation(project):
            return function(project, *args, **kwargs)
    return wrapped



def full_commit(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', value):
        raise Invalid('Expected a full lowercase commit SHA, not a branch, tag or abbreviation')
    return value


def _git(source, *args):
    # These callers use only read operations. Never consult caller Git routing variables.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    try:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(source), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
    except OSError as error:
        raise Invalid('Git is required for committed-source installation') from error
    if result.returncode:
        # Avoid copying arbitrary Git configuration/remote credentials into diagnostics.
        raise Invalid('Cannot read supplier Git data with ' + args[0])
    return result.stdout


@contextmanager
def committed_source(source, commit, repository=None):
    commit = full_commit(commit)
    root = Path(_git(source, 'rev-parse', '--show-toplevel').decode('utf-8').strip()).resolve()
    if root != source.resolve():
        raise Invalid('--source must be the supplier Git repository root')
    resolved = _git(source, 'rev-parse', '--verify', commit + '^{commit}').decode('ascii').strip()
    if resolved != commit:
        raise Invalid('The requested object must itself be the exact commit, not a tag object')
    origin = _git(source, 'config', '--get', 'remote.origin.url').decode('utf-8').strip()
    if not origin or any(c in origin for c in '\r\n'):
        raise Invalid('Supplier repository needs one explicit origin URL')
    if repository is not None and repository != origin:
        raise Invalid('Expected repository identity does not match supplier origin')
    entries = {}
    raw = _git(source, 'ls-tree', '-r', '-z', '--full-tree', commit)
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        try:
            header, name = entry.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split(' ')
            name = name.decode('utf-8')
        except (ValueError, UnicodeError) as error:
            raise Invalid('Malformed supplier tree entry') from error
        if name in entries:
            raise Invalid('Duplicate supplier tree path')
        entries[name] = (mode, kind, oid)
    def blob(relative):
        if relative not in entries:
            raise Invalid('Requested commit is missing a declared package file')
        mode, kind, oid = entries[relative]
        if mode not in ('100644', '100755') or kind != 'blob':
            raise Invalid('Only regular committed package files are permitted')
        full_commit(oid)  # Validate opaque object-id syntax before passing it to Git.
        return _git(source, 'cat-file', 'blob', oid)
    # Read objects directly: no checkout, clone, archive/export attributes, filters or hooks.
    with tempfile.TemporaryDirectory(prefix='editorial-commit-') as temporary:
        snapshot = Path(temporary)
        safe_path(snapshot, 'package-manifest.json').write_bytes(blob('package-manifest.json'))
        manifest = read_json(snapshot / 'package-manifest.json')
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
            raise Invalid('Malformed committed package manifest')
        selected = set(manifest['files'])
        # Include undeclared managed files as well so package() rejects extra payloads.
        for name in entries:
            if any(name == 'skills/' + skill or name.startswith('skills/' + skill + '/') for skill in SKILLS):
                selected.add(name)
        for name in sorted(selected):
            target = safe_path(snapshot, name)
            if name == 'package-manifest.json':
                raise Invalid('Manifest must not include itself')
            data = blob(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        package(snapshot)
        yield snapshot, dict(source_mode='git', source_repository=origin, source_commit=commit,
                             source_manifest_sha256=sha(snapshot / 'package-manifest.json'))


def install_git(project, source, commit, repository=None, adopt=None):
    with committed_source(source, commit, repository) as (snapshot, identity):
        return install(project, snapshot, adopt, _identity=identity)

def inventory(root, prefix):
    base = safe_path(root, prefix)
    if not base.is_dir():
        raise Invalid('Missing skill directory: ' + prefix)
    result = {}
    for path in sorted(base.rglob('*')):
        relative = path.relative_to(root).as_posix()
        safe_path(root, relative)
        if path.is_file():
            result[relative] = sha(path)
        elif not path.is_dir():
            raise Invalid('Special file is not permitted: ' + relative)
    for directory in base.rglob('*'):
        if directory.is_dir() and not any(key.startswith(directory.relative_to(root).as_posix() + '/') for key in result):
            raise Invalid('Untracked empty directory: ' + directory.relative_to(root).as_posix())
    return result

def package(source):
    manifest_path = safe_path(source, 'package-manifest.json')
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise Invalid('Malformed package manifest')
    if not isinstance(manifest.get('version'), str) or not manifest['version']:
        raise Invalid('Missing package version')
    for key in ('profile_schema_version', 'dossier_schema_version'):
        if type(manifest.get(key)) is not int or manifest[key] != 1:
            raise Invalid('Unsupported package ' + key)
    for relative, digest in manifest['files'].items():
        path = safe_path(source, relative)
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise Invalid('Invalid SHA-256 for ' + relative)
        if not path.is_file() or sha(path) != digest:
            raise Invalid('Package hash mismatch: ' + relative)
    installed = {}
    for name in SKILLS:
        prefix = 'skills/' + name
        actual = inventory(source, prefix)
        declared = {key: val for key, val in manifest['files'].items() if key.startswith(prefix + '/')}
        if actual != declared or prefix + '/SKILL.md' not in actual:
            raise Invalid('Incomplete or undeclared skill files: ' + name)
        installed.update({'.agents/' + key: val for key, val in actual.items()})
    extras = {key.split('/')[1] for key in manifest['files'] if key.startswith('skills/') and len(key.split('/')) > 1} - set(SKILLS)
    if extras:
        raise Invalid('Unexpected skill directories')
    return manifest, installed, sha(manifest_path)

def installed_files(project):
    result = {}
    for prefix in ROOTS:
        if safe_path(project, prefix).exists():
            result.update(inventory(project, prefix))
    return result

def validate_lock(project, data):
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        raise Invalid('Malformed installed lock')
    mode = data.get('source_mode')
    if mode == 'candidate':
        if data.get('source_commit') is not None or data.get('source_repository') is not None:
            raise Invalid('Candidate lock must not claim a committed source')
        digest = data.get('candidate_manifest_sha256')
    elif mode == 'git':
        full_commit(data.get('source_commit'))
        if not isinstance(data.get('source_repository'), str) or not data['source_repository'].strip():
            raise Invalid('Pinned lock is missing repository identity')
        digest = data.get('source_manifest_sha256')
    else:
        raise Invalid('Unknown installed source mode')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise Invalid('Invalid source manifest hash in installed lock')
    for key in ('profile_schema_version', 'dossier_schema_version'):
        if type(data.get(key)) is not int or data[key] != 1:
            raise Invalid('Unsupported installed ' + key)
    if not isinstance(data.get('version'), str) or not data['version']:
        raise Invalid('Missing installed package version')
    for key in data['files']:
        safe_path(project, key)
        if not any(key.startswith(root + '/') for root in ROOTS):
            raise Invalid('Lock contains out-of-scope file')
    if installed_files(project) != data['files']:
        raise Invalid('Local skill changes or untracked files detected; preserve them before updating')

def atomic_json(path, value):
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _recover(project):
    journal = safe_path(project, JOURNAL)
    if not journal.exists():
        raise Invalid('No unfinished transaction')
    info = read_json(journal)
    # Only a direct transaction child is accepted, never an arbitrary restore path.
    backup_name = info.get('backup', '')
    if not backup_name.startswith('.agents/editorial-backup-') or len(PurePosixPath(backup_name).parts) != 2:
        raise Invalid('Invalid transaction backup path')
    backup = safe_path(project, backup_name)
    for index, relative in enumerate(ROOTS + (LOCK,)):
        target = safe_path(project, relative)
        saved = safe_path(backup, str(index))
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if saved.is_dir():
            shutil.copytree(saved, target)
        elif saved.is_file():
            shutil.copy2(saved, target)
    journal.unlink()
    shutil.rmtree(backup)
    return 'restored'

@exclusive
def install(project, source, adopt=None, _identity=None):
    if safe_path(project, JOURNAL).exists():
        raise Invalid('Unfinished transaction: run recover before editorial operations')
    profile(project)
    manifest, files, identity = package(source)
    lock_path = safe_path(project, LOCK)
    provenance = _identity or dict(source_mode='candidate', source_repository=None, source_commit=None,
                                   candidate_manifest_sha256=identity)
    desired = dict(provenance, version=manifest['version'], profile_schema_version=1,
                   dossier_schema_version=1, files=files)
    if lock_path.exists():
        existing = read_json(lock_path)
        validate_lock(project, existing)
        if existing == desired:
            return 'unchanged'
    elif any(safe_path(project, root).exists() for root in ROOTS):
        if adopt is None:
            raise Invalid('Existing unowned skill directories; explicit --adopt-manifest required')
        adoption = read_json(adopt)
        if not isinstance(adoption, dict) or adoption.get('files') != installed_files(project):
            raise Invalid('Adoption manifest does not match existing skills exactly')
    agents = safe_path(project, '.agents')
    agents.mkdir(exist_ok=True)
    safe_path(project, '.agents/skills').mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='editorial-backup-', dir=agents))
    try:
        for index, relative in enumerate(ROOTS + (LOCK,)):
            original = safe_path(project, relative)
            saved = backup / str(index)
            if original.is_dir():
                shutil.copytree(original, saved)
            elif original.exists():
                shutil.copy2(original, saved)
        atomic_json(safe_path(project, JOURNAL), {'backup': backup.relative_to(project).as_posix()})
    except BaseException:
        shutil.rmtree(backup)
        raise
    try:
        for name, relative in zip(SKILLS, ROOTS):
            target = safe_path(project, relative)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source / 'skills' / name, target)
        atomic_json(lock_path, desired)
        validate_lock(project, read_json(lock_path))
    except BaseException:
        _recover(project)
        raise
    safe_path(project, JOURNAL).unlink()
    shutil.rmtree(backup)
    return 'installed committed Git source' if _identity else 'installed candidate (not a committed-source pin)'

@exclusive
def recover(project):
    return _recover(project)

@exclusive
def check(project, include_lock=True):
    if safe_path(project, JOURNAL).exists():
        raise Invalid('Unfinished transaction: run recover')
    profile(project)
    if include_lock:
        validate_lock(project, read_json(safe_path(project, LOCK)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'check', 'check-profile', 'recover'))
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--candidate', action='store_true')
    parser.add_argument('--commit', help='Full immutable commit SHA available in the local supplier repository')
    parser.add_argument('--repository', help='Expected origin URL; checked without network access')
    parser.add_argument('--adopt-manifest', type=Path)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    try:
        if args.action == 'install':
            if args.source is None or bool(args.candidate) == bool(args.commit):
                raise Invalid('install requires --source and exactly one of --candidate or --commit SHA')
            source = args.source.resolve(strict=True)
            if args.candidate:
                if args.repository is not None:
                    raise Invalid('--repository is only valid with --commit')
                result = install(project, source, args.adopt_manifest)
            else:
                result = install_git(project, source, args.commit, args.repository, args.adopt_manifest)
        elif args.action == 'recover':
            result = recover(project)
        else:
            check(project, args.action == 'check')
            result = 'valid ' + args.action
        print(result)
        return 0
    except (Invalid, OSError, ValueError) as error:
        print('Blocked: ' + str(error), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
