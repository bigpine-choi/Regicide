"""Remote study phases keep candidate selection, fixed work and payload identity separate."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import collect_learning_data as DATA
import endgame_study as STUDY
import endgame_study_tests as BASE


class RemoteStudyTests(unittest.TestCase):
    def fixture(self, temporary):
        args, state, run, report, catalog, curve = BASE.EndgameStudyTests().validation_fixture(Path(temporary).resolve())
        report['validation_profile'] = 'fixed-nodes-remote-time-v1'
        report['checkout'] = {'commit': 'a'*40}
        report['candidate_policy'] = 'first declared seed, largest preregistered arm'
        DATA.checkpoint_json(run/'cycle.json', report)
        return args, state, run, report, catalog, curve

    def test_development_uses_fixed_nodes_for_all_arms_and_never_promotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, run, report, catalog, curve = self.fixture(temporary)
            reply = {'completed': True, 'valid_for_comparison': True, 'summary': {'candidate_score': .5}}
            with patch.object(STUDY, 'frozen_sources', return_value={'fixture': 'frozen'}), \
                 patch('endgame_matches.run_matches', return_value=reply) as matches, \
                 patch('learning_registry.promote') as promote, contextlib.redirect_stdout(io.StringIO()):
                result = STUDY.develop(args, state, catalog)
            self.assertEqual(result['status'], 'ready_for_remote_validation')
            self.assertEqual(matches.call_count, 3)
            self.assertTrue(all(call.kwargs == {'nodes': 1000} for call in matches.call_args_list))
            self.assertEqual([row['added_train'] for row in result['development']], [0, 128, 512])
            self.assertEqual(result['candidate'], report['candidate'])
            self.assertEqual(catalog['attempts_started'], 5)
            self.assertFalse(result['adopted'])
            promote.assert_not_called()

    def test_incomplete_development_fails_without_creating_an_arena(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, run, _, catalog, _ = self.fixture(temporary)
            with patch.object(STUDY, 'frozen_sources', return_value={'fixture': 'frozen'}), \
                 patch('endgame_matches.run_matches', return_value={'completed': False}), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'did not complete'):
                    STUDY.develop(args, state, catalog)
            self.assertFalse((run/'arena').exists())
            self.assertEqual(json.loads((run/'cycle.json').read_text())['status'], 'failed')

    def test_remote_profile_cannot_accidentally_run_local_time_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, state, _, _, catalog, _ = self.fixture(temporary)
            with self.assertRaisesRegex(ValueError, 'no local idle-PC'):
                STUDY.validate(args, state, catalog)

    def export_fixture(self, temporary):
        root = (Path(temporary)/'repo').resolve()
        root.mkdir()
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        (root/'fixture.py').write_text('value = 1\n')
        subprocess.run(['git', '-C', str(root), 'add', 'fixture.py'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-qm', 'synthetic unit fixture'], check=True)
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        args, state, run, report, catalog, curve = self.fixture(root)
        report.update(status='ready_for_remote_validation', checkout={'commit': head},
                      implementation_sha256={'fixture.py': DATA.sha256_file(root/'fixture.py')})
        # Export is specifically a Windows binary transfer, even when this unit
        # suite runs on Linux. The bytes are harmless test fixtures, never run.
        (run/'regicide.exe').write_bytes(args.binary.read_bytes())
        DATA.checkpoint_json(run/'cycle.json', report)
        args.request_directory = root/'docs/checks/fixture/remote-input'
        return root, args, state, run, report, catalog

    def test_export_exact_binary_and_model_with_fresh_post_fit_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, args, state, run, report, catalog = self.export_fixture(temporary)
            with patch.object(STUDY, 'ROOT', root), patch.object(STUDY, 'verify_frozen'), \
                 patch('learning_registry.load_champion', return_value=None), \
                 patch.object(STUDY.secrets, 'randbits', return_value=987654321), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = STUDY.export_remote(args, state, catalog)
            request_file = args.request_directory/'remote-request.json'
            request = json.loads(request_file.read_text())
            self.assertEqual(request['seed'], 987654321)
            self.assertEqual(request['pairs'], 32)
            self.assertEqual(request['movetime_ms'], 100)
            self.assertEqual(request['source_hash_basis'], 'git_blob_bytes')
            self.assertEqual(request['candidate_sha256'], report['candidate']['model_sha256'])
            with zipfile.ZipFile(args.request_directory/'engine.zip') as archive:
                self.assertEqual(archive.namelist(), ['regicide.exe'])
                self.assertEqual(archive.read('regicide.exe'), args.binary.read_bytes())
            self.assertEqual((args.request_directory/'candidate.nn').read_bytes(),
                             (run/report['candidate']['model_path']).read_bytes())
            self.assertEqual(result['status'], 'waiting_for_remote_validation')
            self.assertEqual(result['remote_request']['sha256'], DATA.sha256_file(request_file))
            self.assertEqual(result['candidate'], report['candidate'])
            self.assertEqual(catalog['attempts_started'], 5)
            self.assertFalse((state/'champion.json').exists())

    def test_export_refuses_changed_engine_or_existing_destination(self):
        for changed in ('binary', 'destination'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root, args, state, _, _, catalog = self.export_fixture(temporary)
                if changed == 'binary':
                    args.binary.write_bytes(b'changed')
                else:
                    args.request_directory.mkdir(parents=True)
                with patch.object(STUDY, 'ROOT', root), patch.object(STUDY, 'verify_frozen'), \
                     patch('learning_registry.load_champion', return_value=None):
                    with self.assertRaises((ValueError, FileExistsError)):
                        STUDY.export_remote(args, state, catalog)

    def test_export_refuses_a_different_champion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, args, state, _, _, catalog = self.export_fixture(temporary)
            with patch.object(STUDY, 'ROOT', root), patch.object(STUDY, 'verify_frozen'), \
                 patch('learning_registry.load_champion', return_value={'model_sha256': 'b'*64}):
                with self.assertRaisesRegex(ValueError, 'Previous champion'):
                    STUDY.export_remote(args, state, catalog)
            self.assertFalse(args.request_directory.exists())

    def test_export_refuses_a_different_implementation_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, args, state, run, report, catalog = self.export_fixture(temporary)
            report['checkout']['commit'] = '0'*40
            DATA.checkpoint_json(run/'cycle.json', report)
            with patch.object(STUDY, 'ROOT', root), patch.object(STUDY, 'verify_frozen'), \
                 patch('learning_registry.load_champion', return_value=None):
                with self.assertRaisesRegex(ValueError, 'final implementation'):
                    STUDY.export_remote(args, state, catalog)


if __name__ == '__main__':
    unittest.main()
