"""Default-horizon support provenance and leakage checks; no external engines."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import neural_stm as stm
import neural_training as base
import nnue_live_pairs as live
import nnue_choice_pairs_tests as fixtures
from prepare_neural_huber import sha


def live_pair(*, depth=2, history=(), partition='train', family='live-train', model=None):
    model = fixtures.tiny_model() if model is None else model
    pair = fixtures.backed_pair(history=history, partition=partition, family=family,
                                root_id='live-' + partition, model=model)
    pair.update(kind='live_search_backed_gap', branch_depth=depth, critical_refutation=False,
                depth_decision={'requested_nodes': 10000, 'default_completed_depth': depth,
                                'branch_depth': depth, 'policy': 'default_last_completed_depth'})
    for side in ('a', 'b'):
        support = pair[side]['support_receipt']
        support.update(reason='observed_completed_pv_support', depth_limit=depth,
                       completed_observations_count=5)
        support['options'].update({key: True for key in live.DEFAULT_FLAGS})
        support['options']['hash_mb'] = 16
        support['search'].update(depth=depth, tt_cutoffs=2, tt_probes=10, pvs_probes=3, lmr_reductions=1)
        support['last_completed_iteration'] = deepcopy(support['search'])
        support['support_leaf'].update(iteration_depth=depth, qply=len(support['search']['pv']) - depth)
    return pair


class LivePairTests(unittest.TestCase):
    def test_both_colors_variable_completed_depth_and_off_pv_shortcuts(self):
        for history in ([], ['a2a3']):
            for depth in (1, 2, 3):
                with self.subTest(history=history, depth=depth):
                    pair = live_pair(depth=depth, history=history)
                    features = live.validate_pair(pair, fixtures.tiny_model(), model_sha256='b' * 64,
                                                  teacher_sha256='a' * 64)
                    self.assertIn(pair['a']['feature_hash'], features)
                    self.assertGreaterEqual(len(features), 5)

    def test_completed_prefix_excludes_unfinished_tail_but_does_not_reject_total_stopped(self):
        pair = live_pair()
        for side in ('a', 'b'):
            pair[side]['support_receipt']['search'].update(nodes=10000, stopped=True)
        live.validate_pair(pair, fixtures.tiny_model())
        pair['a']['support_receipt']['completed_observations_count'] = 3
        with self.assertRaisesRegex(ValueError, 'index invalid'):
            live.validate_pair(pair, fixtures.tiny_model())

    def test_all_default_options_and_completed_score_must_authenticate(self):
        # Bind loop variables rather than allowing all closures to share one key.
        mutations = [(lambda p, key=key: p['a']['support_receipt']['options'].update({key: False}))
                     for key in live.DEFAULT_FLAGS]
        mutations += [lambda p: p['a']['support_receipt']['options'].update(hash_mb=32),
            lambda p: p['a']['support_receipt']['last_completed_iteration'].update(depth=1),
            lambda p: p['a']['support_receipt']['last_completed_iteration'].update(score_cp=123),
            lambda p: p['a']['support_receipt']['support_leaf'].update(raw_score_cp=123),
            lambda p: p['a']['support_receipt']['support_leaf'].update(draw_claim_adjusted=True),
            lambda p: p['a']['support_receipt'].update(model_sha256='c' * 64),
            lambda p: p['a']['root_child']['teacher_receipt'].update(binary_sha256='c' * 64),
            lambda p: p['a']['root_child']['teacher_receipt']['raw_score_info_packet'].update(upperbound=True)]
        for mutation in mutations:
            pair = live_pair(); mutation(pair)
            with self.assertRaises(ValueError):
                live.validate_pair(pair, fixtures.tiny_model(), model_sha256='b' * 64, teacher_sha256='a' * 64)

    def test_critical_parent_horizon_policy_is_explicit_and_train_only(self):
        pair = live_pair(depth=1)
        pair.update(critical_refutation=True, depth_decision={'requested_nodes': 10000,
            'default_completed_depth': 4, 'parent_completed_depth': 2, 'branch_depth': 1,
            'policy': 'critical_parent_completed_depth_minus_one'})
        live.validate_pair(pair, fixtures.tiny_model())
        tuning = deepcopy(pair)
        tuning['partition'] = 'tuning'
        with self.assertRaisesRegex(ValueError, 'TRAIN only'):
            live.validate_pair(tuning, fixtures.tiny_model())
        pair['depth_decision']['parent_completed_depth'] = 3
        with self.assertRaisesRegex(ValueError, 'depth policy'):
            live.validate_pair(pair, fixtures.tiny_model())

    def _bank(self, path):
        rows, manifest, _ = fixtures.ChoicePairTests()._bank(path)
        # An independently bound new head may retain the same hidden tensors.
        model = fixtures.tiny_model()
        model['output_weights'][0] += 1
        stm.save_model(model, path / 'live.nnue')
        digest = sha(path / 'live.nnue')
        manifest.update(live_warm_model={'path': str(path / 'live.nnue'), 'sha256': digest},
            live_gate={'blocked_features': str(path / 'blocked.json'), 'blocked_features_sha256': sha(path / 'blocked.json')},
            live_teacher_sha256='a' * 64, live_pair_files={}, live_pair_rows={}, live_combined_pair_rows={})
        new, decisions = {}, []
        for part, history in (('train', ['e2e4', 'c7c5', 'g1f3', 'd7d6']),
                              ('tuning', ['d2d4', 'd7d5', 'c2c4', 'e7e6'])):
            pair = live_pair(history=history, partition=part, family='live-' + part, model=model)
            for side in ('a', 'b'):
                pair[side]['support_receipt']['model_sha256'] = digest
            name = f'pairs-live-{part}.jsonl'
            fixtures.write_rows(path / name, [pair])
            manifest['live_pair_files'][name] = sha(path / name)
            manifest['live_pair_rows'][part] = 1
            manifest['live_combined_pair_rows'][part] = 3
            new[part] = pair
            root = {'query_id': pair['root_id'], 'initial_fen': pair['initial_fen'], 'history': pair['root_history'],
                    'fen': pair['root_fen'], 'partition': part, 'family_id': pair['family_id']}
            native = deepcopy(pair['b']['support_receipt'])
            native.update(forced_root_move=None, depth_limit=64)
            native['options']['full_window_forced_root'] = False
            recommendation = deepcopy(pair['a']['root_child']['teacher_receipt'])
            recommendation.update(fen=pair['root_fen'], bestmove_uci=pair['move_a'])
            decisions.append({'root': root, 'native_receipt': native, 'depth_decision': pair['depth_decision'],
                              'teacher_recommendation_receipt': recommendation,
                              'teacher_move': pair['move_a'], 'student_move': pair['move_b']})
        (path / 'student-choices.json').write_text(json.dumps(decisions), encoding='utf-8')
        manifest['live_decisions_sha256'] = sha(path / 'student-choices.json')
        return rows, manifest, new

    def test_loader_keeps_old_model_receipts_and_byte_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            rows, manifest, new = self._bank(path)
            old = {name: (path / name).read_bytes() for name in (*manifest['pair_files'], *manifest['backed_pair_files'])}
            pairs, hashes = live.load_pairs(path, rows, manifest)
            self.assertEqual({p: len(v) for p, v in pairs.items()}, {'train': 3, 'tuning': 3})
            self.assertEqual(len(hashes), 7)
            self.assertEqual(pairs['train'][-1], new['train'])
            for name, original in old.items(): self.assertEqual((path / name).read_bytes(), original)

    def test_loader_rejects_live_pv_gate_crosssplit_and_family_leakage(self):
        for boundary in ('gate', 'test', 'family'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                rows, manifest, new = self._bank(path)
                support = new['train']['a']['support_receipt']
                board = fixtures.board_from(support['initial_fen'], support['root_history'] + support['search']['pv'][:2])
                if boundary == 'gate':
                    (path / 'new-block.json').write_text(json.dumps([base.feature_identity(board)]), encoding='utf-8')
                    manifest['live_gate'] = {'blocked_features': str(path / 'new-block.json'), 'blocked_features_sha256': sha(path / 'new-block.json')}
                elif boundary == 'test':
                    rows['test'].append({'fen': board.fen(en_passant='fen'), 'family_id': 'new-exposed-test'})
                else:
                    rows['test'].append({'fen': '6k1/pp4pp/8/8/8/8/PP6/1K1NBR2 w - - 0 1', 'family_id': 'live-train'})
                with self.assertRaisesRegex(ValueError, 'reserved gate|cross partitions'):
                    live.load_pairs(path, rows, manifest)

    def test_loader_rejects_tampered_bytes_duplicates_and_new_model_confusion(self):
        for kind in ('bytes', 'duplicate', 'old-model'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                rows, manifest, new = self._bank(path)
                name = 'pairs-live-train.jsonl'
                if kind == 'bytes':
                    with (path / name).open('a', encoding='utf-8') as handle: handle.write('\n')
                else:
                    records = [new['train']]
                    if kind == 'duplicate':
                        records *= 2; manifest['live_pair_rows']['train'] = 2; manifest['live_combined_pair_rows']['train'] = 4
                    else:
                        records[0]['a']['support_receipt']['model_sha256'] = manifest['warm_model']['sha256']
                    fixtures.write_rows(path / name, records); manifest['live_pair_files'][name] = sha(path / name)
                with self.assertRaises(ValueError): live.load_pairs(path, rows, manifest)

    def test_default_horizon_must_match_authenticated_root_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            rows, manifest, _ = self._bank(path)
            decisions = json.loads((path / 'student-choices.json').read_text(encoding='utf-8'))
            decisions[0]['native_receipt']['last_completed_iteration']['depth'] += 1
            (path / 'student-choices.json').write_text(json.dumps(decisions), encoding='utf-8')
            manifest['live_decisions_sha256'] = sha(path / 'student-choices.json')
            with self.assertRaisesRegex(ValueError, 'completed horizon'):
                live.load_pairs(path, rows, manifest)


if __name__ == '__main__':
    unittest.main()
