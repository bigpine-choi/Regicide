"""Replayable actual-evaluation witnesses and pre-teacher exclusion rules."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import neural_stm as stm
import neural_training as base
import prepare_nnue_leaf as leaf


def fixture():
    model = stm.load_model(leaf.WARM)
    roots, receipts = [], []
    for identity, part, history in [('a', 'train', ['e2e4', 'e7e5']),
                                     ('b', 'tuning', ['d2d4', 'd7d5'])]:
        board = chess.Board()
        for move in history:
            board.push_uci(move)
        root = {'query_id': identity, 'family_id': identity, 'partition': part,
                'initial_fen': chess.STARTING_FEN, 'history': history,
                'fen': board.fen(en_passant='fen')}
        samples = []
        for number, move in enumerate(('g1f3', 'b1c3')):
            child = board.copy(stack=True)
            child.push_uci(move)
            ix, _, turns = stm.pack_records([{'fen': child.fen(), 'target_white_cp': 0}])
            raw = int(stm.predict(model, ix, turns)[0]) * (1 if child.turn else -1)
            samples.append({'fen': child.fen(en_passant='fen'), 'history': history + [move],
                            'path_from_root': [move], 'ply': 1, 'qply': 0,
                            'phase': 'quiescence_stand_pat', 'raw_score_cp': raw,
                            'score_cp': raw, 'in_check': False, 'current_draw_claim': False,
                            'draw_claim_adjusted': False, 'evaluation_index': number + 1,
                            'iteration_depth': 1})
        roots.append(root)
        receipts.append({'id': identity, 'initial_fen': root['initial_fen'],
                         'root_history': history, 'root_fen': root['fen'],
                         'nodes_limit': 10000, 'search': {'nodes': 10000},
                         'sampling': {'capacity': 4, 'seed': leaf.SEED}, 'samples': samples})
    return model, roots, receipts


class LeafDataTests(unittest.TestCase):
    def test_witness_replays_complete_history_and_matches_actual_integer_evaluation(self):
        model, roots, receipts = fixture()
        board = leaf.witness_board(receipts[0], receipts[0]['samples'][0], roots[0], model)
        self.assertEqual(board.move_stack[-1].uci(), 'g1f3')
        self.assertEqual(len(board.move_stack), 3)

    def test_rejects_wrong_recorded_score_and_inconsistent_path(self):
        model, roots, receipts = fixture()
        sample = deepcopy(receipts[0]['samples'][0])
        sample['raw_score_cp'] += 1
        with self.assertRaisesRegex(ValueError, 'recorded NNUE'):
            leaf.witness_board(receipts[0], sample, roots[0], model)
        sample = deepcopy(receipts[0]['samples'][0]); sample['path_from_root'] = ['b1c3']
        with self.assertRaisesRegex(ValueError, 'path differs'):
            leaf.witness_board(receipts[0], sample, roots[0], model)

    def test_pv_endpoint_or_draw_adjusted_score_is_not_an_eligible_witness(self):
        model, roots, receipts = fixture()
        for key, value in [('phase', 'pv_endpoint'), ('draw_claim_adjusted', True),
                            ('current_draw_claim', True), ('in_check', True)]:
            sample = deepcopy(receipts[0]['samples'][0]); sample[key] = value
            with self.assertRaises(ValueError):
                leaf.witness_board(receipts[0], sample, roots[0], model)

    def test_selection_excludes_gate_background_and_mirrors_before_teacher(self):
        model, roots, receipts = fixture()
        blocked = {base.feature_identity(chess.Board(receipts[0]['samples'][0]['fen']).mirror())}
        background = {'test': [{'fen': receipts[1]['samples'][0]['fen']}]}
        rows, exclusions = leaf.select_witnesses(receipts, roots, background, blocked, model)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['partition'] for r in rows}, {'train', 'tuning'})
        self.assertEqual(exclusions['background_or_gate_input'], 2)
        self.assertTrue(all(r['replay_weight'] == 4 for r in rows))

    def test_deduplicates_evaluation_occurrences_without_changing_row_weight(self):
        model, roots, receipts = fixture()
        receipts[0]['samples'].append(deepcopy(receipts[0]['samples'][0]))
        rows, _ = leaf.select_witnesses(receipts, roots, {}, set(), model)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r['feature_hash'] for r in rows}), 4)

    def test_cross_family_split_collision_is_excluded(self):
        model, roots, receipts = fixture()
        roots[1]['history'] = roots[0]['history']; roots[1]['fen'] = roots[0]['fen']
        receipts[1]['root_history'] = roots[1]['history']; receipts[1]['root_fen'] = roots[1]['fen']
        receipts[1]['samples'] = deepcopy(receipts[0]['samples'])
        with self.assertRaisesRegex(ValueError, 'both source partitions'):
            leaf.select_witnesses(receipts, roots, {}, set(), model)


if __name__ == '__main__':
    unittest.main()
