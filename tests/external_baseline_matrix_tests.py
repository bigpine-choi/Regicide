"""Offline matrix freeze, replay, audit and owned-worker failure regressions.

Engine bytes are inert fixtures. Popen, process observation and termination are
mocked; this suite never starts an engine or stops an existing user process.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import external_baseline_matrix as matrix


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ScriptedEngine:
    def __init__(self, sequence):
        self.sequence = sequence
        self.calls = []

    def play(self, board, limit, **kwargs):
        self.calls.append((list(board.move_stack), limit, kwargs))
        move = chess.Move.from_uci(self.sequence[board.ply()])
        return chess.engine.PlayResult(move, None, info={'nodes': 1, 'depth': 1, 'pv': [move]})


class NoObservation:
    def sample(self, pid):
        return {'worker_pid': pid, 'known_external_engines': [], 'scope': 'mocked offline test'}

    def close(self):
        pass


class MatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True,
                                    text=True, check=True).stdout.strip()

    def setUp(self):
        # This suite checks evidence/control flow; the existing comparison suite
        # owns bootstrap statistics. Keep deterministic summaries inexpensive.
        bootstrap = patch.object(matrix.comparison, 'BOOTSTRAP_SAMPLES', 200)
        bootstrap.start()
        self.addCleanup(bootstrap.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix='regicide-matrix-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.binary = self.folder / 'inert-engine.exe'
        self.binary.write_bytes(b'never executed engine fixture')
        self.artifact = self.folder / 'inert-source.py'
        self.artifact.write_text('# Never imported or executed\n', encoding='utf-8')
        self.paths = {}
        for identifier, kind in [('classical-a', 'regicide'), ('classical-b', 'regicide'), ('opponent', 'external')]:
            profile = {'schema_version': 1, 'id': identifier, 'kind': kind,
                       'evaluation': 'classical' if kind == 'regicide' else 'external',
                       'binary': self.reference(self.binary), 'args': ['uci'] if kind == 'regicide' else [],
                       'options': {'Hash': 16}, 'source': {'commit': self.commit} if kind == 'regicide' else {
                           'name': 'Inert fixture', 'version': '1', 'source_url': 'https://example.invalid/fixture', 'revision': 'fixture-only'},
                       'models': {}, 'artifacts': [self.reference(self.artifact) | {'role': 'source'}]}
            if kind == 'regicide':
                profile['options'].update(UseNeuralEvaluation=False, UseNNUEEvaluation=False,
                    UseIncrementalNeuralEvaluation=True, UsePawnShield=True, UsePawnStructure=True, UseLMR=True)
            path = self.folder / f'{identifier}.json'
            path.write_text(json.dumps(profile), encoding='utf-8')
            self.paths[identifier] = path
        self.opening_path = self.folder / 'openings.json'
        self.opening_document = {'schema_version': 1, 'openings': [
            {'name': 'Fool prefix', 'family_id': 'family-f', 'moves': ['f2f3', 'e7e5']},
            {'name': 'Scholar prefix', 'family_id': 'family-e', 'moves': ['e2e4', 'e7e5']}]}
        self.opening_path.write_text(json.dumps(self.opening_document), encoding='utf-8')
        self.plan_path = self.folder / 'plan.json'
        self.plan = {'schema_version': 1, 'id': 'offline-fixture', 'phase': 'pilot',
                     'schedule': 'rotate-candidates-per-block', 'movetimes_ms': [50, 100],
                     'max_plies': 100, 'draw_policy': 'claim', 'cell_timeout_seconds': 20, 'total_timeout_seconds': 100,
                     'candidates': [self.reference(self.paths['classical-a']), self.reference(self.paths['classical-b'])],
                     'opponents': [self.reference(self.paths['opponent'])], 'openings': self.reference(self.opening_path)}
        self.write_plan()

    def reference(self, path):
        return {'path': Path(path).name, 'sha256': digest(path)}

    def write_plan(self, value=None):
        self.plan_path.write_text(json.dumps(self.plan if value is None else value), encoding='utf-8')

    def freeze(self, name='frozen'):
        directory = self.folder / name
        frozen = matrix.freeze_plan(self.plan_path, directory)
        _, profiles = matrix.verify_frozen(directory)
        return directory, frozen, profiles

    def metadata(self, complete=True, profiles=None, cell=None):
        _, openings = matrix.comparison.load_openings(self.opening_path)
        records = []
        args = SimpleNamespace(nodes=None, depth=None, movetime=50, max_plies=100 if complete else 3, draw_policy='claim')
        sequences = [['f2f3', 'e7e5', 'g2g4', 'd8h4'],
                     ['e2e4', 'e7e5', 'd1h5', 'b8c6', 'f1c4', 'g8f6', 'h5f7']]
        for index, opening in enumerate(openings, 1):
            for candidate_white in (True, False):
                record = matrix.comparison.new_record(len(records) + 1, index, opening, candidate_white)
                engine = ScriptedEngine(sequences[index - 1])
                matrix.comparison.play_one(args, {'baseline': engine, 'candidate': engine}, record)
                records.append(record)
        engines = {}
        for role, identifier in [('candidate', cell['candidate'] if cell else 'classical-a'),
                                 ('baseline', cell['opponent'] if cell else 'opponent')]:
            if profiles:
                cfg = matrix.compare_arguments(profiles[identifier])
                engines[role] = matrix.comparison.binary_provenance(Path(cfg['binary']), cfg['commit'], cfg['options'], cfg['external_source'])
                engines[role]['validated_options'] = deepcopy(cfg['options'])
            else:
                engines[role] = {'sha256': digest(self.binary), 'provided_source_commit': self.commit if role == 'candidate' else None}
            engines[role]['engine_id'] = {'name': 'Scripted fixture only'}
        result = {'schema_version': 1, 'started_at': '2026-10-21T00:00:00+00:00',
                  'draw_policy': 'claim', 'games': records, 'selected_openings': openings,
                  'games_requested': len(openings) * 2, 'completed': complete, 'exit_code': 0 if complete else 2,
                  'errors': [], 'engines': engines, 'max_plies': args.max_plies,
                  'opening_file_sha256': digest(self.opening_path),
                  'limits': {'nodes': None, 'depth': None, 'movetime_ms': cell['movetime_ms'] if cell else 50}}
        result['summary'] = matrix.comparison.summarize(records, openings)
        return result

    def write_cell_evidence(self, directory, cell, metadata):
        target = directory / 'cells' / cell['id']
        target.mkdir(parents=True, exist_ok=True)
        pgn = target / 'games.pgn'
        pgn.write_text(matrix.comparison.pgn_text(metadata), encoding='utf-8')
        matrix.write_json(pgn.with_suffix('.json'), metadata)
        return dict(cell, status='COMPLETE' if metadata['completed'] else 'INCOMPLETE',
                    exit_code=metadata['exit_code'], elapsed_seconds=0.01, summary=metadata['summary'],
                    pgn_sha256=digest(pgn), games_json_sha256=digest(pgn.with_suffix('.json')))

    def write_report(self, directory, frozen, cells, status='COMPLETE'):
        report = {'status': status, 'cells': cells, 'frozen_plan_sha256': digest(directory / 'frozen-plan.json'),
                  'scheduled_cells': len(frozen['cells']), 'scheduled_games': len(frozen['cells']) * frozen['scheduled_games_per_cell'],
                  'unplayed_cells': len(frozen['cells']) - len(cells), 'phase': 'pilot', 'strength_eligible': False, 'elo_estimate': None}
        matrix.write_json(directory / 'report.json', report)
        return report

    def test_block_rotation_covers_cartesian_product_without_changing_color_pairing(self):
        cells = matrix.schedule(['a', 'b', 'c'], ['x', 'y'], [50, 100])
        self.assertEqual([c['candidate'] for c in cells], ['a', 'b', 'c', 'b', 'c', 'a', 'c', 'a', 'b', 'a', 'b', 'c'])
        self.assertEqual(len({(c['candidate'], c['opponent'], c['movetime_ms']) for c in cells}), 12)
        metadata = self.metadata()
        self.assertEqual([g['white'] for g in metadata['games']], ['candidate', 'baseline'] * 2)
        self.assertEqual([g['pair'] for g in metadata['games']], [1, 1, 2, 2])
        self.assertEqual([g['candidate_points'] for g in metadata['games']], [0, 1, 1, 0])

    def test_schema_numbers_modes_and_duplicate_ids_are_rejected(self):
        for key, value in [('schema_version', True), ('phase', 'confirmatory'), ('id', '../escape'),
                           ('movetimes_ms', [50, 50]), ('movetimes_ms', [True]), ('max_plies', 0),
                           ('cell_timeout_seconds', -1), ('total_timeout_seconds', 1.5), ('draw_policy', 'invented'),
                           ('schedule', 'random'), ('candidates', []), ('opponents', [])]:
            with self.subTest(key=key, value=value):
                bad = deepcopy(self.plan)
                bad[key] = value
                self.write_plan(bad)
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    matrix.load_plan(self.plan_path)
        bad = deepcopy(self.plan)
        bad['candidates'].append(bad['candidates'][0])
        self.write_plan(bad)
        with self.assertRaises(ValueError):
            matrix.load_plan(self.plan_path)

    def test_profile_references_are_hashed_and_role_checked(self):
        for role in ['candidates', 'opponents']:
            bad = deepcopy(self.plan)
            bad[role][0]['sha256'] = '0' * 64
            self.write_plan(bad)
            with self.assertRaises(ValueError):
                matrix.load_plan(self.plan_path)
        bad = deepcopy(self.plan)
        bad['opponents'] = bad['candidates'][:1]
        self.write_plan(bad)
        with self.assertRaises(ValueError):
            matrix.load_plan(self.plan_path)

    def test_opening_families_positions_legality_and_ply_cap_are_checked(self):
        mutations = [lambda rows: rows[1].update(family_id=rows[0]['family_id']),
                     lambda rows: rows[1].pop('family_id'),
                     lambda rows: rows[1].update(moves=rows[0]['moves']),
                     lambda rows: rows[1].update(moves=['e2e5'])]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                document = deepcopy(self.opening_document)
                mutate(document['openings'])
                self.opening_path.write_text(json.dumps(document), encoding='utf-8')
                self.plan['openings'] = self.reference(self.opening_path)
                self.write_plan()
                with self.assertRaises(ValueError):
                    matrix.load_plan(self.plan_path)
        self.opening_path.write_text(json.dumps(self.opening_document), encoding='utf-8')
        self.plan['openings'] = self.reference(self.opening_path)
        self.plan['max_plies'] = 2
        self.write_plan()
        with self.assertRaises(ValueError):
            matrix.load_plan(self.plan_path)

    def test_freeze_copies_inputs_new_destination_only_and_rechecks_every_hash(self):
        directory, frozen, profiles = self.freeze()
        self.assertEqual(frozen['scheduled_games_per_cell'], 4)
        self.assertEqual(len(frozen['cells']), 4)
        self.assertTrue(all(Path(p['binary']['path']).is_relative_to(directory) for p in profiles.values()))
        original = (directory / 'seal.json').read_bytes()
        with self.assertRaises(FileExistsError):
            matrix.freeze_plan(self.plan_path, directory)
        self.assertEqual((directory / 'seal.json').read_bytes(), original)
        for key in ['openings.json', 'frozen-plan.json', 'harness/compare_engines.py']:
            path = directory / key
            contents = path.read_bytes()
            path.write_bytes(contents + b'\n')
            with self.assertRaises(ValueError):
                matrix.verify_frozen(directory)
            path.write_bytes(contents)
        binary = Path(profiles['classical-a']['binary']['path'])
        binary.write_bytes(b'tampered frozen binary')
        with self.assertRaises(ValueError):
            matrix.verify_frozen(directory)
        self.assertEqual(self.binary.read_bytes(), b'never executed engine fixture')

    def test_freeze_failure_leaves_no_usable_seal_and_cannot_be_reused(self):
        directory = self.folder / 'partial-freeze'
        with patch.object(matrix, 'snapshot_profile', side_effect=OSError('controlled copy failure')):
            with self.assertRaises(OSError):
                matrix.freeze_plan(self.plan_path, directory)
        self.assertEqual(json.loads((directory / 'freeze-failure.json').read_text())['status'], 'FAIL')
        self.assertFalse((directory / 'seal.json').exists())
        with self.assertRaises(FileExistsError):
            matrix.freeze_plan(self.plan_path, directory)

    def test_replay_real_legal_scripted_mates_and_unfinished_cap_without_scoring(self):
        for complete in [True, False]:
            metadata = self.metadata(complete)
            path = self.folder / f'{complete}.pgn'
            path.write_text(matrix.comparison.pgn_text(metadata), encoding='utf-8')
            self.assertEqual(matrix.replay_cell(metadata, path), metadata['summary'])
        self.assertIsNone(self.metadata(False)['summary']['candidate_score'])

    def test_replay_rejects_false_complete_wrong_score_result_final_board_or_extra_game(self):
        baseline = self.metadata()
        changes = [('points', lambda data: data['games'][0].update(candidate_points=1)),
                   ('result', lambda data: data['games'][0].update(result='1-0')),
                   ('final', lambda data: data['games'][0].update(final_fen=chess.STARTING_FEN)),
                   ('plies', lambda data: data['games'][0].update(plies=0)),
                   ('outcome', lambda data: data['games'][0].update(termination='stalemate')),
                   ('summary', lambda data: data['summary'].update(candidate_score=1.0))]
        path = self.folder / 'replay.pgn'
        for label, mutate in changes:
            with self.subTest(label=label):
                data = deepcopy(baseline)
                mutate(data)
                path.write_text(matrix.comparison.pgn_text(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    matrix.replay_cell(data, path)
        data = self.metadata(False)
        data['games'][0].update(completed=True, result='1-0', candidate_points=1)
        path.write_text(matrix.comparison.pgn_text(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            matrix.replay_cell(data, path)
        path.write_text(matrix.comparison.pgn_text(baseline) + matrix.comparison.pgn_text(baseline), encoding='utf-8')
        with self.assertRaises(ValueError):
            matrix.replay_cell(baseline, path)

    def test_replay_rejects_empty_match_claiming_completion(self):
        data = self.metadata()
        data['games'] = []
        data['summary'] = matrix.comparison.summarize([], data['selected_openings'])
        path = self.folder / 'empty.pgn'
        path.write_text('', encoding='utf-8')
        with self.assertRaises(ValueError):
            matrix.replay_cell(data, path)

    def test_audit_accepts_full_evidence_and_detects_missing_or_tampered_results(self):
        directory, frozen, profiles = self.freeze()
        results = [self.write_cell_evidence(directory, cell, self.metadata(profiles=profiles, cell=cell)) for cell in frozen['cells']]
        self.write_report(directory, frozen, results)
        audited = matrix.audit_matrix(directory)
        self.assertEqual(audited['matrix_status'], 'COMPLETE')
        self.assertFalse(audited['strength_eligible'])
        pgn = directory / 'cells' / results[0]['id'] / 'games.pgn'
        pgn.write_text(pgn.read_text() + '\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            matrix.audit_matrix(directory)

    def test_cell_identity_options_and_limits_must_match_the_frozen_profile(self):
        directory, frozen, profiles = self.freeze()
        cell = frozen['cells'][0]
        good = self.metadata(profiles=profiles, cell=cell)
        matrix.verify_cell_inputs(good, directory, frozen, profiles, cell)
        changes = [lambda data: data['limits'].update(movetime_ms=51),
                   lambda data: data.update(max_plies=99),
                   lambda data: data.update(draw_policy='automatic'),
                   lambda data: data.update(opening_file_sha256='0' * 64),
                   lambda data: data['engines']['candidate'].update(sha256='0' * 64),
                   lambda data: data['engines']['candidate'].update(command=['unfrozen-program']),
                   lambda data: data['engines']['candidate']['requested_options'].update(Hash=32),
                   lambda data: data['engines']['candidate']['validated_options'].update(Hash=32),
                   lambda data: data['engines']['candidate'].update(provided_source_commit='0' * 40)]
        for change in changes:
            bad = deepcopy(good)
            change(bad)
            with self.assertRaises(ValueError):
                matrix.verify_cell_inputs(bad, directory, frozen, profiles, cell)

    def test_incomplete_audit_pass_reports_incomplete_not_a_completed_experiment(self):
        directory, frozen, _ = self.freeze()
        cells = [dict(frozen['cells'][0], status='ERROR', summary=None, exit_code=2)]
        self.write_report(directory, frozen, cells, status='INCOMPLETE')
        result = matrix.audit_matrix(directory)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['matrix_status'], 'INCOMPLETE')
        self.assertFalse(result['strength_eligible'])

    def test_audit_rejects_complete_cells_without_replayable_evidence(self):
        directory, frozen, _ = self.freeze()
        cells = [dict(cell, status='COMPLETE', summary=None, exit_code=0) for cell in frozen['cells']]
        self.write_report(directory, frozen, cells)
        with self.assertRaises((ValueError, FileNotFoundError)):
            matrix.audit_matrix(directory)

    def test_audit_rejects_missing_incomplete_reordered_or_extra_cells_marked_complete(self):
        directory, frozen, profiles = self.freeze()
        good = [self.write_cell_evidence(directory, cell, self.metadata(profiles=profiles, cell=cell)) for cell in frozen['cells']]
        variants = [good[:-1], [dict(good[0], status='INCOMPLETE'), *good[1:]], list(reversed(good)), [*good, good[-1]]]
        for cells in variants:
            self.write_report(directory, frozen, cells)
            with self.assertRaises(ValueError):
                matrix.audit_matrix(directory)

    def test_owned_cleanup_uses_only_worker_pid_and_never_image_name_or_unrelated_process(self):
        process = Mock(pid=781234)
        process.poll.return_value = None
        with patch.object(matrix.os, 'name', 'nt'), patch.object(matrix.subprocess, 'run') as terminate:
            matrix.terminate_owned_tree(process)
        self.assertEqual(terminate.call_args.args[0], ['taskkill', '/PID', '781234', '/T', '/F'])
        process.wait.assert_called_once()
        self.assertNotIn('/IM', terminate.call_args.args[0])
        process.poll.return_value = 0
        with patch.object(matrix.subprocess, 'run') as terminate:
            matrix.terminate_owned_tree(process)
            terminate.assert_not_called()

    def test_run_cell_passes_only_frozen_inputs_and_cleans_its_own_worker(self):
        directory, frozen, profiles = self.freeze()
        cell = frozen['cells'][0]
        process = Mock(pid=890123, returncode=0)
        process.poll.return_value = 0
        def launch(command, **kwargs):
            pgn = Path(command[command.index('--pgn') + 1])
            metadata = self.metadata(profiles=profiles, cell=cell)
            pgn.write_text(matrix.comparison.pgn_text(metadata), encoding='utf-8')
            matrix.write_json(pgn.with_suffix('.json'), metadata)
            return process
        start = Mock(side_effect=launch)
        worker_api = SimpleNamespace(Popen=start, STDOUT=subprocess.STDOUT,
                                     CREATE_NEW_PROCESS_GROUP=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        with (patch.object(matrix, 'LoadObservation', NoObservation),
              patch.object(matrix, 'subprocess', worker_api),
              patch.object(matrix, 'terminate_owned_tree') as stop):
            result = matrix.run_cell(directory, frozen, profiles, cell, 5)
        self.assertEqual(result['status'], 'COMPLETE')
        command = start.call_args.args[0]
        self.assertEqual(command[command.index('--movetime') + 1], '50')
        self.assertEqual(command[command.index('--openings') + 1], str(directory / 'openings.json'))
        self.assertEqual(command[2:4], [profiles['opponent']['binary']['path'], profiles['classical-a']['binary']['path']])
        stop.assert_called_once_with(process)
        matrix.verify_frozen(directory)

    def test_worker_missing_evidence_and_timeout_never_complete_and_cleanup_is_owned(self):
        directory, frozen, profiles = self.freeze()
        process = Mock(pid=890123, returncode=0)
        process.poll.return_value = 0
        worker_api = SimpleNamespace(Popen=Mock(return_value=process), STDOUT=subprocess.STDOUT,
                                     CREATE_NEW_PROCESS_GROUP=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        with (patch.object(matrix, 'LoadObservation', NoObservation),
              patch.object(matrix, 'subprocess', worker_api),
              patch.object(matrix, 'terminate_owned_tree') as stop):
            result = matrix.run_cell(directory, frozen, profiles, frozen['cells'][0], 1)
        self.assertNotEqual(result['status'], 'COMPLETE')
        stop.assert_called_once_with(process)
        process.poll.return_value = None
        process.returncode = -1
        with (patch.object(matrix, 'LoadObservation', NoObservation),
              patch.object(matrix, 'subprocess', worker_api),
              patch.object(matrix, 'terminate_owned_tree') as stop,
              patch.object(matrix.time, 'monotonic', side_effect=[0, 2, 2])):
            timed_out = matrix.run_cell(directory, frozen, profiles, frozen['cells'][1], 1)
        self.assertEqual(timed_out['status'], 'TIMEOUT')
        self.assertTrue(all(call.args == (process,) for call in stop.call_args_list))

    def test_matrix_stops_after_failed_cell_and_counts_remaining_without_rerunning(self):
        directory, frozen, _ = self.freeze()
        bad = dict(frozen['cells'][0], status='ERROR', summary=None)
        with patch.object(matrix, 'run_cell', return_value=bad) as run:
            report = matrix.run_matrix(directory)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['unplayed_cells'], len(frozen['cells']) - 1)
        self.assertEqual(report['stop_reason'], 'cell_not_complete')
        run.assert_called_once()
        original = (directory / 'report.json').read_bytes()
        with self.assertRaises(FileExistsError):
            matrix.run_matrix(directory)
        self.assertEqual((directory / 'report.json').read_bytes(), original)

    def test_total_budget_and_keyboard_interrupt_are_published_without_false_completion(self):
        directory, frozen, _ = self.freeze()
        with patch.object(matrix.time, 'monotonic', side_effect=[0, 101, 101]), patch.object(matrix, 'run_cell') as run:
            report = matrix.run_matrix(directory)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['unplayed_cells'], len(frozen['cells']))
        self.assertEqual(report['cells'], [])
        self.assertEqual(report['stop_reason'], 'total_wall_budget_exhausted')
        run.assert_not_called()
        interrupted, frozen, _ = self.freeze('interrupted')
        with patch.object(matrix, 'run_cell', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            matrix.run_matrix(interrupted)
        report = json.loads((interrupted / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'INTERRUPTED')
        self.assertEqual(report['unplayed_cells'], len(frozen['cells']) - 1)
        self.assertEqual(report['cells'][0]['status'], 'INTERRUPTED')
        self.assertFalse(report['strength_eligible'])

    def test_mutation_during_worker_is_detected_before_matrix_completion(self):
        directory, frozen, profiles = self.freeze()
        def mutate(_directory, _frozen, _profiles, cell, _timeout):
            Path(profiles[cell['candidate']]['binary']['path']).write_bytes(b'changed during worker')
            return dict(cell, status='COMPLETE', summary=None)
        with patch.object(matrix, 'run_cell', side_effect=mutate):
            report = matrix.run_matrix(directory)
        self.assertEqual(report['status'], 'ERROR')
        self.assertIn('sha256 mismatch', report['stop_reason'].lower())
        self.assertFalse(report['strength_eligible'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
