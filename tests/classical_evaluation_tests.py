"""Exact classical baseline invariants and optional batched native parity.

python tests/classical_evaluation_tests.py --probe build/regicide_evaluation_probe.exe
--binary is also accepted and discovers the sibling evaluation probe. This uses
new deterministic legal positions and self-authored fixtures, never arena data.
"""

import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import classical_evaluation as EVAL

PROBE = None
REPORT = None


def fixtures():
    return [
        chess.STARTING_FEN,
        "7k/8/8/8/3P4/8/8/K7 w - - 0 1",
        "7k/8/8/3P4/8/8/8/K7 w - - 0 1",
        "7k/8/8/3P4/3P4/8/8/K7 w - - 0 1",
        "7k/8/8/3P4/2P5/8/8/K7 w - - 0 1",
        "7k/8/3N4/3P4/8/8/8/K7 w - - 0 1",
        "7k/8/2p5/3P4/8/8/8/K7 w - - 0 1",
        "7k/P7/8/8/8/8/8/6K1 w - - 0 1",
        "7k/8/8/8/8/8/p7/6K1 b - - 0 1",
        "7k/8/8/8/8/8/8/KN6 w - - 0 1",
        "7k/8/8/8/8/8/3Q4/K7 w - - 0 1",
        "4r2k/8/8/8/8/8/8/4K3 w - - 0 1",
        "r3k2r/pp3ppp/8/8/8/8/PP3PPP/R3K2R w KQkq - 0 1",
        "r3k2r/pp3ppp/8/8/8/6PP/PP3P2/R3K2R w KQkq - 0 1",
        "qqqqkqqq/pppppppp/8/8/8/8/5PPP/6K1 w - - 0 1",
        "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
    ]


def legal_cases():
    cases = []
    for fen in fixtures():
        board = chess.Board(fen)
        if not board.is_valid():
            raise ValueError(f"invalid self-authored baseline fixture: {fen}")
        cases.append((fen, [], board.copy(stack=True)))
        mirror = board.mirror()
        cases.append((mirror.fen(en_passant="fen"), [], mirror))
    rng = random.Random(2026101901)
    for game_number in range(12):
        board, history = chess.Board(), []
        for ply in range(100):
            if board.outcome(claim_draw=True) is not None:
                break
            move = rng.choice(sorted(board.legal_moves, key=lambda move: move.uci()))
            board.push(move)
            history.append(move.uci())
            if ply % 3 == 0:
                cases.append((chess.STARTING_FEN, list(history), board.copy(stack=True)))
    return cases


class ClassicalBaselineTests(unittest.TestCase):
    def test_initial_symmetric_position_is_zero_for_all_switches(self):
        for shield in (False, True):
            for structure in (False, True):
                self.assertEqual(EVAL.classical_cp_stm(chess.Board(), shield, structure), 0)

    def test_signed_integer_truncation_matches_cpp_without_float_rounding(self):
        for numerator, expected in ((25, 1), (23, 0), (-25, -1), (-23, 0), (0, 0)):
            self.assertEqual(EVAL.trunc_div(numerator, 24), expected)
        self.assertEqual(EVAL.trunc_div(-(10 ** 100 + 1), 24), -((10 ** 100 + 1) // 24))
        for numerator, denominator in ((1.0, 24), (1, 0), (True, 24)):
            with self.assertRaises(ValueError):
                EVAL.trunc_div(numerator, denominator)

    def test_hand_calculated_pawn_endgame_structure_and_advancement(self):
        cases = [(fixtures()[1], 138, 130), (fixtures()[2], 170, 142),
                 (fixtures()[3], 276, 272), (fixtures()[4], 330, 270)]
        for fen, enabled, disabled in cases:
            with self.subTest(fen=fen):
                board = chess.Board(fen)
                self.assertEqual(EVAL.classical_cp_stm(board), enabled)
                self.assertEqual(EVAL.classical_cp_stm(board, pawn_structure=False), disabled)

    def test_foremost_pawn_blocking_halves_nonnegative_passed_bonus(self):
        clear = chess.Board(fixtures()[2])
        blocked = chess.Board(fixtures()[5])
        own, enemy = [0] * 8, [0] * 8
        own[chess.FILE_NAMES.index("d")] = 1 << 4
        self.assertEqual(EVAL._pawn_score(clear, own, enemy, chess.WHITE), (10, 28))
        self.assertEqual(EVAL._pawn_score(blocked, own, enemy, chess.WHITE), (0, 8))
        enemy[2] = 1 << 2
        self.assertEqual(EVAL._pawn_score(clear, own, enemy, chess.WHITE), (-10, -12))

    def test_phase_blend_hand_calculated_fraction_uses_truncation(self):
        knight = chess.Board("7k/8/8/8/8/8/8/KN6 w - - 0 1")
        queen = chess.Board("7k/8/8/8/8/8/3Q4/K7 w - - 0 1")
        self.assertEqual(EVAL.classical_cp_stm(knight, False, False), 324)
        self.assertEqual(EVAL.classical_cp_stm(queen, False, False), 911)
        queen.turn = chess.BLACK
        self.assertEqual(EVAL.classical_cp_stm(queen, False, False), -911)

    def test_home_minor_penalty_uses_only_original_matching_minor_squares(self):
        board = chess.Board()
        self.assertEqual(EVAL._home_minors(board, chess.WHITE), 4)
        board.push_uci("g1f3")
        self.assertEqual(EVAL._home_minors(board, chess.WHITE), 3)
        self.assertEqual(EVAL._home_minors(board, chess.BLACK), 4)
        board.set_piece_at(chess.G1, chess.Piece(chess.ROOK, chess.WHITE))
        self.assertEqual(EVAL._home_minors(board, chess.WHITE), 3)

    def test_color_rank_reflection_and_turn_sign_for_all_options(self):
        for fen in fixtures():
            board = chess.Board(fen)
            for shield in (False, True):
                for structure in (False, True):
                    score = EVAL.classical_cp_stm(board, shield, structure)
                    self.assertEqual(EVAL.classical_cp_stm(board.mirror(), shield, structure), score)
                    flipped = board.copy(stack=True)
                    flipped.turn = not flipped.turn
                    flipped.ep_square = None
                    if flipped.is_valid():
                        self.assertEqual(EVAL.classical_cp_stm(flipped, shield, structure), -score)

    def test_history_claim_rights_and_piece_state_are_unchanged(self):
        board = chess.Board()
        for move in "g1f3 g8f6 f3g1 f6g8 g1f3 g8f6".split():
            board.push_uci(move)
        before = (board.fen(en_passant="fen"), list(board.move_stack), board.can_claim_threefold_repetition(),
                  board.castling_rights, board.ep_square)
        for shield in (False, True):
            for structure in (False, True):
                self.assertEqual(EVAL.classical_cp_stm(board, shield, structure),
                                 EVAL.classical_cp_stm(chess.Board(board.fen()), shield, structure))
        self.assertEqual(before, (board.fen(en_passant="fen"), list(board.move_stack),
                                  board.can_claim_threefold_repetition(), board.castling_rights, board.ep_square))

    def test_static_score_does_not_adjudicate_terminal_positions(self):
        checkmated = chess.Board()
        for move in "f2f3 e7e5 g2g4 d8h4".split():
            checkmated.push_uci(move)
        self.assertTrue(checkmated.is_checkmate())
        self.assertIsInstance(EVAL.classical_cp_stm(checkmated), int)

    def test_invalid_board_or_flag_types_are_rejected(self):
        for board in (None, "startpos", chess.Board(None), chess.Board(chess960=True)):
            with self.assertRaises(ValueError):
                EVAL.classical_cp_stm(board)
        with self.assertRaises(ValueError):
            EVAL.classical_cp_stm(chess.Board(), 1)

    def test_batched_native_static_parity_on_hundreds_of_new_legal_positions(self):
        if PROBE is None:
            self.skipTest("pass --probe or --binary for native C++ parity")
        cases = legal_cases()
        payload = "".join(fen + "\t" + " ".join(history) + "\n" for fen, history, _ in cases)
        response = subprocess.run([str(PROBE)], input=payload, capture_output=True, text=True,
                                  check=True, timeout=120)
        native = response.stdout.splitlines()
        self.assertEqual(len(native), len(cases))
        for (fen, history, board), line in zip(cases, native):
            fields = line.split("\t")
            self.assertEqual(len(fields), 13)
            self.assertEqual(fields[0], board.fen(en_passant="fen"))
            for index, (shield, structure) in enumerate(((True, True), (True, False), (False, True), (False, False)), 3):
                self.assertEqual(int(fields[index]), EVAL.classical_cp_stm(board, shield, structure),
                                 f"native/Python classical mismatch options={shield, structure} fen={board.fen()}")
        result = {"schema": "regicide.classical-python-parity.v1", "status": "passed", "positions": len(cases),
                  "option_pairs": 4, "comparisons": len(cases) * 4, "max_cp_difference": 0,
                  "probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
                  "python_mirror_sha256": hashlib.sha256(Path(EVAL.__file__).read_bytes()).hexdigest(),
                  "input_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                  "provenance": "Self-authored fixtures plus deterministic random legal playouts; no reserved arena positions or labels"}
        if REPORT:
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Python/classical C++ parity: {len(cases)} positions x 4 options, exact 0 cp difference", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--report", type=Path)
    args, remainder = parser.parse_known_args()
    PROBE = args.probe
    if PROBE is None and args.binary:
        PROBE = args.binary.with_name("regicide_evaluation_probe" + args.binary.suffix)
    if PROBE is not None:
        PROBE = PROBE.resolve(strict=True)
    REPORT = args.report
    unittest.main(argv=[sys.argv[0], *remainder])
