"""Historical evidence is portable; execution source bindings remain strict."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import nnue_reservation_audit as audit


class ReservationAuditTests(unittest.TestCase):
    def test_windows_paths_relocate_by_recorded_root_not_basename(self):
        self.assertEqual(audit.recorded_relative(r'C:\old\repo\scripts\engine.py',
                                                r'C:\old\repo'), 'scripts/engine.py')
        self.assertEqual(audit.recorded_relative('C:/old/repo/data/model.nnue',
                                                r'C:\old\repo'), 'data/model.nnue')

    def test_posix_paths_relocate_independently_of_current_os(self):
        self.assertEqual(audit.recorded_relative('/old/repo/data/model.nnue',
                                                '/old/repo'), 'data/model.nnue')

    def test_other_checkout_drive_and_traversal_are_rejected(self):
        for path, root in ((r'D:\old\repo\data\model.nnue', r'C:\old\repo'),
                           (r'C:\old\repository\data\model.nnue', r'C:\old\repo'),
                           (r'C:\old\repo\..\model.nnue', r'C:\old\repo'),
                           ('/old/repository/model.nnue', '/old/repo'),
                           ('/old/repo/../model.nnue', '/old/repo'),
                           ('data/model.nnue', '/old/repo')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                audit.recorded_relative(path, root)
        with self.assertRaises(ValueError):
            audit._local(audit.ROOT, '../outside')

    def test_actual_chain_survives_a_different_checkout_root_without_json_rewrites(self):
        original = audit._read(audit.ROOT / audit.ORIGINAL / 'reservation.json')
        old_root = audit._recorded_root(original)
        paths = set(audit.ARCHIVED_SOURCES.values())
        for relative in audit.PINS:
            directory = audit.ROOT / relative
            paths.update(relative + '/' + name for name in audit.FILES)
            reservation = audit._read(directory / 'reservation.json')
            paths.update(audit.recorded_relative(path, old_root)
                         for path in reservation['input_sha256'])
        paths.update(audit.SNAPSHOT + '/' + name for name in ('historical-tests.zip', 'manifest.json'))
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / 'different checkout'
            for relative in paths:
                destination = repository / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(audit.ROOT / relative, destination)
            with patch.object(audit, '_input_digest', wraps=audit._input_digest) as checked:
                source, original, previous, reservation, roots, blocked = audit.audit_cost_source(repository)
            self.assertEqual(checked.call_count, 267 + 292 + 301)
            self.assertEqual((len(roots), len(blocked)), (16, 594))
            self.assertTrue(source.is_relative_to(repository))
            self.assertTrue(previous.is_relative_to(repository))
            self.assertEqual(audit._sha(source / 'reservation.json'), audit.PINS[audit.ORIGINAL][0])
            self.assertEqual(audit._sha(previous / 'reservation.json'), audit.PINS[audit.ADOPTED][0])
            # An unarchived live source and an allowed historical archive both
            # remain byte-bound; neither may merely exist under the right name.
            for relative in ('scripts/nnue_tactical_search_gate.py',
                             next(iter(audit.ARCHIVED_SOURCES.values()))):
                target = repository / relative
                before = target.read_bytes()
                target.write_bytes(b'changed frozen input')
                with self.assertRaisesRegex(ValueError, 'Historical input bytes differ'):
                    audit.audit_original(repository)
                target.write_bytes(before)
            # Actual model bytes remain mandatory even though source archives are allowed.
            (repository / audit.MODEL).write_bytes(b'changed model')
            with self.assertRaisesRegex(ValueError, 'Frozen model identity or bytes'):
                audit.audit_original(repository)

    def _minimal_original(self, repository):
        source = repository / audit.ORIGINAL
        source.mkdir(parents=True)
        for name in audit.FILES:
            shutil.copyfile(audit.ROOT / audit.ORIGINAL / name, source / name)
        return source

    def test_changed_reservation_is_rejected_even_if_its_audit_hash_is_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._minimal_original(Path(temporary))
            reservation = audit._read(source / 'reservation.json')
            reservation['model_path'] = '/different/model.nnue'
            (source / 'reservation.json').write_text(json.dumps(reservation), encoding='utf-8')
            receipt = audit._read(source / 'position-only-verification.json')
            receipt['reservation_sha256'] = audit._sha(source / 'reservation.json')
            (source / 'position-only-verification.json').write_text(json.dumps(receipt), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                audit.audit_original(Path(temporary))

    def test_any_query_marker_invalidates_historical_unused_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._minimal_original(Path(temporary))
            (source / 'label-started.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'queried, sealed, consumed'):
                audit.audit_original(Path(temporary))

    def test_only_explicit_source_archive_and_exact_digest_are_accepted(self):
        (relative, expected), archive = next(iter(audit.ARCHIVED_SOURCES.items()))
        self.assertEqual(audit._input_digest(audit.ROOT, relative, expected), expected)
        # A made-up expected digest must read the live source, not search archives.
        self.assertEqual(audit._input_digest(audit.ROOT, relative, '0' * 64),
                         audit._sha(audit.ROOT / relative))
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            target = repository / archive
            target.parent.mkdir(parents=True)
            target.write_bytes(b'changed archived source')
            self.assertNotEqual(audit._input_digest(repository, relative, expected), expected)

    def test_git_snapshot_manifest_and_archive_must_both_match(self):
        relative, expected = next(iter(audit.ZIP_SOURCES))
        self.assertEqual(audit._input_digest(audit.ROOT, relative, expected), expected)
        for changed in ('manifest.json', 'historical-tests.zip'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary)
                destination = repository / audit.SNAPSHOT
                destination.mkdir(parents=True)
                for name in ('manifest.json', 'historical-tests.zip'):
                    shutil.copyfile(audit.ROOT / audit.SNAPSHOT / name, destination / name)
                (destination / changed).write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'Git source archive changed'):
                    audit._input_digest(repository, relative, expected)

    def test_live_execution_authentication_still_rejects_changed_current_source(self):
        # Rebase this disposable fixture only to reach the source check on every OS.
        # Original historical reservation bytes are never rewritten.
        value = deepcopy(audit._read(audit.ROOT / audit.ORIGINAL / 'reservation.json'))
        old_root = audit._recorded_root(value)
        value['model_path'] = str(audit.legacy.MODEL)
        value['input_sha256'] = {
            str(audit.ROOT / audit.recorded_relative(path, old_root)): digest
            for path, digest in value['input_sha256'].items()}
        current = audit.ROOT / 'scripts/neural_training.py'
        self.assertNotEqual(audit._sha(current), value['input_sha256'][str(current)])
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in ('frozen-roots.json', 'blocked-features.json'):
                shutil.copyfile(audit.ROOT / audit.ORIGINAL / name, directory / name)
            (directory / 'reservation.json').write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, r'Frozen reservation input changed: .*neural_training.py'):
                audit.legacy.authenticate_reservation(directory)


if __name__ == '__main__':
    unittest.main()
