"""Runtime activation tests use fake binary bytes and temporary directories."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import runtime_engine
from runtime_engine import (RuntimeEngineError, deactivate_runtime_engine,
                            install_runtime_engine, resolve_runtime_engine)


class RuntimeEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name).resolve()
        (self.root / 'build').mkdir()
        self.default = self.root / 'build' / ('regicide.exe' if sys.platform == 'win32' else 'regicide')
        self.default.write_bytes(b'original installed engine')
        self.candidate = self.root / 'candidate.exe'
        self.candidate.write_bytes(b'candidate engine bytes')
        self.manifest = self.root / 'build/runtime-engine.json'

    def activate(self):
        return install_runtime_engine(self.candidate, self.root)

    def test_no_manifest_uses_default_without_mutating_it(self):
        self.assertEqual(resolve_runtime_engine(self.root), self.default)
        self.assertEqual(self.default.read_bytes(), b'original installed engine')
        self.assertFalse(self.manifest.exists())

    def test_platform_fallback_and_deactivation_preserve_linux_filename(self):
        linux_default = self.root / 'build/regicide'
        with mock.patch.object(runtime_engine.sys, 'platform', 'linux'):
            self.assertEqual(resolve_runtime_engine(self.root), linux_default)
            selected = self.activate()
            self.assertEqual(resolve_runtime_engine(self.root), selected)
            self.assertEqual(deactivate_runtime_engine(self.root), linux_default)
            self.assertEqual(resolve_runtime_engine(self.root), linux_default)
            self.assertTrue(selected.is_file())
        with mock.patch.object(runtime_engine.sys, 'platform', 'win32'):
            self.assertEqual(resolve_runtime_engine(self.root), self.root / 'build/regicide.exe')
            self.assertEqual(deactivate_runtime_engine(self.root), self.root / 'build/regicide.exe')

    def test_posix_mode_copy_failure_preserves_previous_activation(self):
        previous = self.activate()
        manifest_bytes = self.manifest.read_bytes()
        self.candidate.write_bytes(b'new permission-tested candidate')
        with mock.patch.object(runtime_engine.sys, 'platform', 'linux'):
            with mock.patch.object(runtime_engine.shutil, 'copymode', side_effect=PermissionError('mode failed')):
                with self.assertRaisesRegex(RuntimeEngineError, 'existing activation is retained'):
                    self.activate()
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assertEqual(resolve_runtime_engine(self.root), previous)

    def test_install_verifies_copied_bytes_and_resolves_relative_manifest(self):
        target = self.activate()
        sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.assertEqual(target, self.root / f'build/runtime-engines/{sha}/regicide.exe')
        self.assertEqual(target.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(resolve_runtime_engine(self.root), target)
        self.assertEqual(json.loads(self.manifest.read_text()),
                         {'schema_version': 1, 'sha256': sha, 'path': f'build/runtime-engines/{sha}/regicide.exe'})
        self.assertEqual(self.default.read_bytes(), b'original installed engine')

    def test_identical_install_reuses_binary_without_overwrite(self):
        target = self.activate()
        modified = target.stat().st_mtime_ns
        with mock.patch.object(runtime_engine.shutil, 'copyfileobj', side_effect=AssertionError('must reuse')):
            self.assertEqual(self.activate(), target)
        self.assertEqual(target.stat().st_mtime_ns, modified)

    def test_new_candidate_keeps_previous_and_default_executables(self):
        previous = self.activate()
        before = previous.read_bytes()
        self.candidate.write_bytes(b'new candidate bytes')
        selected = self.activate()
        self.assertNotEqual(previous, selected)
        self.assertEqual(previous.read_bytes(), before)
        self.assertEqual(resolve_runtime_engine(self.root), selected)
        self.assertEqual(self.default.read_bytes(), b'original installed engine')

    def test_manifest_is_authoritative_after_default_rebuild_until_deactivation(self):
        selected = self.activate()
        self.default.write_bytes(b'newly rebuilt default engine')
        self.assertEqual(resolve_runtime_engine(self.root), selected)
        self.assertEqual(deactivate_runtime_engine(self.root), self.default)
        self.assertEqual(resolve_runtime_engine(self.root), self.default)
        self.assertTrue(selected.is_file())
        self.assertEqual(self.default.read_bytes(), b'newly rebuilt default engine')
        self.assertEqual(deactivate_runtime_engine(self.root), self.default)

    def test_corrupt_or_missing_active_binary_never_falls_back(self):
        target = self.activate()
        target.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeEngineError, 'SHA256 mismatch'):
            resolve_runtime_engine(self.root)
        with self.assertRaisesRegex(RuntimeEngineError, 'SHA256 mismatch'):
            self.activate()
        self.assertEqual(target.read_bytes(), b'corrupt')
        target.unlink()
        with self.assertRaisesRegex(RuntimeEngineError, 'missing'):
            resolve_runtime_engine(self.root)

    def test_manifest_paths_are_exact_and_cannot_traverse_or_redirect(self):
        self.activate()
        document = json.loads(self.manifest.read_text())
        for path in ['../regicide.exe', 'build/../regicide.exe', str(self.candidate),
                     document['path'].replace('/', '\\'), 'build/runtime-engines/wrong/regicide.exe']:
            with self.subTest(path=path):
                self.manifest.write_text(json.dumps({**document, 'path': path}))
                with self.assertRaisesRegex(RuntimeEngineError, 'path must be exactly'):
                    resolve_runtime_engine(self.root)

    def test_manifest_schema_and_hash_validation(self):
        self.activate()
        original = json.loads(self.manifest.read_text())
        invalid = [[], {}, {**original, 'schema_version': True}, {**original, 'schema_version': 2},
                   {**original, 'sha256': '0' * 63}, {**original, 'sha256': original['sha256'].upper()},
                   {**original, 'sha256': None}, {**original, 'unknown': 1}]
        for document in invalid:
            with self.subTest(document=document):
                self.manifest.write_text(json.dumps(document))
                with self.assertRaises(RuntimeEngineError):
                    resolve_runtime_engine(self.root)

    def test_corrupt_json_duplicate_keys_and_oversized_manifest_fail_clearly(self):
        for data in ['{broken', '{"schema_version":1,"schema_version":1}', ' ' * 65537]:
            with self.subTest(data=data[:30]):
                self.manifest.write_text(data)
                with self.assertRaises(RuntimeEngineError):
                    resolve_runtime_engine(self.root)

    def test_failed_atomic_activation_preserves_previous_selection(self):
        previous = self.activate()
        manifest_bytes = self.manifest.read_bytes()
        self.candidate.write_bytes(b'another candidate')
        with mock.patch.object(runtime_engine.os, 'replace', side_effect=PermissionError('simulated lock')):
            with self.assertRaisesRegex(RuntimeEngineError, 'existing activation is retained'):
                self.activate()
        self.assertEqual(self.manifest.read_bytes(), manifest_bytes)
        self.assertEqual(resolve_runtime_engine(self.root), previous)
        self.assertEqual(list((self.root / 'build').glob('.runtime-engine-*.tmp')), [])

    def test_source_changed_during_copy_does_not_activate(self):
        with mock.patch.object(runtime_engine.shutil, 'copyfileobj', side_effect=lambda source, dest: dest.write(b'changed')):
            with self.assertRaisesRegex(RuntimeEngineError, 'changed while copying'):
                self.activate()
        self.assertFalse(self.manifest.exists())
        self.assertEqual(list((self.root / 'build/runtime-engines').rglob('*.tmp')), [])

    def test_candidate_must_be_a_readable_file(self):
        for candidate in [self.root / 'missing.exe', self.root]:
            with self.assertRaisesRegex(RuntimeEngineError, 'missing or is not a file'):
                install_runtime_engine(candidate, self.root)
        self.assertFalse(self.manifest.exists())

    def test_symlinked_cache_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        cache = self.root / 'build/runtime-engines'
        try:
            cache.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                self.skipTest('Directory symlinks are unavailable for this account')
            # Windows directory junctions do not require symlink privileges.
            import _winapi
            try:
                _winapi.CreateJunction(str(outside), str(cache))
            except (AttributeError, OSError):
                self.skipTest('Neither directory symlinks nor junctions are available')
        with self.assertRaisesRegex(RuntimeEngineError, 'redirects'):
            self.activate()
        self.assertFalse(self.manifest.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_resolve_has_only_absolute_path_on_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(runtime_engine, 'resolve_runtime_engine', return_value=self.default):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(runtime_engine.main(['--resolve']), 0)
        self.assertEqual(stdout.getvalue(), str(self.default) + '\n')
        self.assertEqual(stderr.getvalue(), '')

    def test_cli_errors_and_install_messages_do_not_pollute_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(runtime_engine, 'resolve_runtime_engine', side_effect=RuntimeEngineError('bad manifest')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(runtime_engine.main(['--resolve']), 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('bad manifest', stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(runtime_engine, 'install_runtime_engine', return_value=self.candidate):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(runtime_engine.main(['--install', str(self.candidate)]), 0)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('Activated runtime engine', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
