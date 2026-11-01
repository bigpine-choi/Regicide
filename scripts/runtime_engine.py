"""Select an explicitly activated immutable engine without starting a process.

The activation manifest is authoritative: rebuilding the default engine under
build/ does not silently replace the selected runtime. Install a new candidate
explicitly, or use --deactivate to restore the platform default (regicide.exe on
Windows, regicide elsewhere). The immutable cache filename remains regicide.exe
on every platform. Existing processes and cached copies are never stopped,
replaced, or removed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = 'runtime-engine.json'
_SHA256 = re.compile(r'[0-9a-f]{64}')


class RuntimeEngineError(ValueError):
    """An explicit runtime selection cannot be safely resolved or installed."""


def _root(repo_root):
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError as error:
        raise RuntimeEngineError(f'Repository directory is unavailable: {repo_root}') from error
    if not root.is_dir():
        raise RuntimeEngineError(f'Repository root is not a directory: {root}')
    return root


def _without_redirects(path):
    """Reject symlinks/junctions in the controlled build/cache path."""
    try:
        if path.resolve() != path:
            raise RuntimeEngineError(f'Runtime path redirects through a link or junction: {path}')
    except (OSError, RuntimeError) as error:
        raise RuntimeEngineError(f'Cannot resolve runtime path: {path}') from error
    return path


def _digest(path):
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError as error:
        raise RuntimeEngineError(f'Cannot read engine binary: {path}') from error


def _verify_binary(path, expected):
    _without_redirects(path)
    if not path.is_file():
        raise RuntimeEngineError(f'Activated engine binary is missing or is not a file: {path}')
    if _digest(path) != expected:
        raise RuntimeEngineError(f'Activated engine SHA256 mismatch: {path}')
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeEngineError(f'Duplicate runtime manifest key: {key}')
        result[key] = value
    return result


def _default_engine(root):
    return root / 'build' / ('regicide.exe' if sys.platform == 'win32' else 'regicide')


def resolve_runtime_engine(repo_root=ROOT):
    """Return an absolute executable path; a bad activation never falls back."""
    root = _root(repo_root)
    manifest_path = _without_redirects(root / 'build' / MANIFEST_NAME)
    if not os.path.lexists(manifest_path):
        return _default_engine(root)
    try:
        if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
            raise RuntimeEngineError(f'Runtime manifest must be a small JSON file: {manifest_path}')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeEngineError(f'Cannot read runtime manifest: {manifest_path}') from error
    if not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'sha256', 'path'}:
        raise RuntimeEngineError('Runtime manifest requires exactly schema_version, sha256, and path')
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
        raise RuntimeEngineError('Unsupported runtime manifest schema_version; expected 1')
    sha = manifest['sha256']
    if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
        raise RuntimeEngineError('Runtime manifest sha256 must be 64 lowercase hexadecimal characters')
    relative = f'build/runtime-engines/{sha}/regicide.exe'
    if manifest['path'] != relative:
        raise RuntimeEngineError(f'Runtime manifest path must be exactly {relative}')
    return _verify_binary(root / relative, sha)


def _atomic_manifest(path, manifest):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         prefix='.runtime-engine-', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_runtime_engine(candidate, repo_root=ROOT):
    """Copy, verify, then atomically activate a SHA-addressed immutable binary."""
    root = _root(repo_root)
    source = Path(candidate).resolve()
    if not source.is_file():
        raise RuntimeEngineError(f'Candidate engine is missing or is not a file: {source}')
    sha = _digest(source)
    relative = f'build/runtime-engines/{sha}/regicide.exe'
    target = _without_redirects(root / relative)
    manifest_path = _without_redirects(root / 'build' / MANIFEST_NAME)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            _verify_binary(target, sha)
        else:
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.engine-', suffix='.tmp',
                                             dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                with source.open('rb') as original:
                    shutil.copyfileobj(original, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if _digest(temporary) != sha:
                raise RuntimeEngineError('Candidate engine changed while copying; activation was not changed')
            if sys.platform != 'win32':
                # A .exe suffix is harmless on POSIX, but a tempfile's default
                # mode omits execute permission. Preserve the candidate's mode.
                shutil.copymode(source, temporary)
            try:
                # An exclusive link publishes complete bytes without replacing an
                # existing executable, including a concurrent install's copy.
                os.link(temporary, target)
            except FileExistsError:
                _verify_binary(target, sha)
        _verify_binary(target, sha)
        _atomic_manifest(manifest_path, {'schema_version': 1, 'sha256': sha, 'path': relative})
    except OSError as error:
        raise RuntimeEngineError(f'Could not install runtime engine; existing activation is retained: {error}') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def deactivate_runtime_engine(repo_root=ROOT):
    """Remove only explicit activation; keep every executable and process."""
    root = _root(repo_root)
    manifest_path = _without_redirects(root / 'build' / MANIFEST_NAME)
    try:
        manifest_path.unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeEngineError(f'Could not deactivate runtime engine: {manifest_path}') from error
    return _default_engine(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--resolve', action='store_true', help='Print only the selected absolute executable path')
    action.add_argument('--install', type=Path, metavar='CANDIDATE', help='Copy and explicitly activate a candidate')
    action.add_argument('--deactivate', action='store_true', help='Use the platform default engine under build/ on subsequent launches')
    args = parser.parse_args(argv)
    try:
        if args.resolve:
            print(resolve_runtime_engine())
        elif args.install is not None:
            print(f'Activated runtime engine: {install_runtime_engine(args.install)}', file=sys.stderr)
        else:
            print(f'Deactivated explicit runtime; subsequent launches use: {deactivate_runtime_engine()}', file=sys.stderr)
    except RuntimeEngineError as error:
        print(f'Runtime engine error: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
