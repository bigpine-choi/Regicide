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
import tempfile
import unittest

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import classical_evaluation as EVAL

PROBE = None
REPORT = None
BINARY = None

# Independently counted total (middlegame, endgame) terms. The corresponding
# native test enumerates target squares rather than using python-chess attacks.
ACTIVITY_COMPONENTS = [
    ("7k/8/4p3/1P6/3N4/8/8/K7 w - - 0 1", (6, 4)),
    ("6k1/8/3p4/8/8/3P4/2B5/6K1 w - - 0 1", (-9, -9)),
    ("6k1/8/3p4/8/8/4P3/2B5/6K1 w - - 0 1", (6, 6)),
    ("6k1/8/8/8/8/8/8/2B2BK1 w - - 0 1", (20, 30)),
    ("6k1/8/8/8/8/8/8/2B1B1K1 w - - 0 1", (0, 0)),
    ("6k1/3p4/8/8/8/8/3P4/3R2K1 w - - 0 1", (-4, -6)),
    ("6k1/3p4/8/8/8/8/4P3/3R2K1 w - - 0 1", (18, 20)),
    ("6k1/p7/8/8/8/8/4P3/3R2K1 w - - 0 1", (28, 27)),
    ("6k1/8/8/8/Q7/8/8/K7 w - - 0 1", (6, 6)),
    ("7k/8/8/3P4/8/8/8/K7 w - - 0 1", (0, 0)),
]

SPECIAL_SEQUENCES = [
    ("r3k2r/pp3ppp/8/8/8/8/PP3PPP/R3K2R w KQkq - 0 1", "e1g1 e8c8"),
    ("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1", "e5d6 h8g8"),
    ("4k3/P7/8/8/8/8/7p/4K3 w - - 0 1", "a7a8q e8f7 a8h8 h2h1n"),
    ("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8q e8f7"),
]


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
    ] + [fen for fen, _ in ACTIVITY_COMPONENTS]


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
    for fen, moves in SPECIAL_SEQUENCES:
        board, history = chess.Board(fen), []
        cases.append((fen, [], board.copy(stack=True)))
        for move in moves.split():
            board.push_uci(move)
            history.append(move)
            cases.append((fen, list(history), board.copy(stack=True)))
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
                for activity in (False, True):
                    self.assertEqual(EVAL.classical_cp_stm(chess.Board(), shield, structure, activity), 0)

    def test_manual_activity_components_and_default_off_legacy_contract(self):
        for fen, expected in ACTIVITY_COMPONENTS:
            board = chess.Board(fen)
            self.assertEqual(EVAL._activity_score(board), expected, fen)
            self.assertEqual(EVAL._activity_score(board.mirror()), tuple(-value for value in expected), fen)
            for shield in (False, True):
                for structure in (False, True):
                    self.assertEqual(EVAL.classical_cp_stm(board, shield, structure),
                                     EVAL.classical_cp_stm(board, shield, structure, False))

    def test_activity_follows_special_moves_and_undo_without_mutation(self):
        for fen, moves in SPECIAL_SEQUENCES:
            board = chess.Board(fen)
            expected = [EVAL.classical_cp_stm(board, piece_activity=True)]
            for move in moves.split():
                board.push_uci(move)
                state = (board.fen(en_passant="fen"), list(board.move_stack))
                expected.append(EVAL.classical_cp_stm(board, piece_activity=True))
                self.assertEqual(state, (board.fen(en_passant="fen"), list(board.move_stack)))
            while board.move_stack:
                board.pop()
                expected.pop()
                self.assertEqual(EVAL.classical_cp_stm(board, piece_activity=True), expected[-1])
            self.assertEqual(board.fen(en_passant="fen"), fen)

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
                    for activity in (False, True):
                        score = EVAL.classical_cp_stm(board, shield, structure, activity)
                        self.assertEqual(EVAL.classical_cp_stm(board.mirror(), shield, structure, activity), score)
                        flipped = board.copy(stack=True)
                        flipped.turn = not flipped.turn
                        flipped.ep_square = None
                        if flipped.is_valid():
                            self.assertEqual(EVAL.classical_cp_stm(flipped, shield, structure, activity), -score)

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
        with self.assertRaises(ValueError):
            EVAL.classical_cp_stm(chess.Board(), piece_activity=1)

    def test_uci_toggle_changes_only_classical_search_and_keeps_model_semantics(self):
        if BINARY is None:
            self.skipTest("pass --binary to validate UCI/search evaluator routing")
        board = chess.Board("6k1/3p4/8/8/8/8/4P3/3R2K1 w - - 0 1")
        root_move = chess.Move.from_uci("d1d3")
        leaf = board.copy()
        leaf.push(root_move)
        with tempfile.TemporaryDirectory(prefix="regicide-activity-routing-") as directory:
            models = {}
            for version in (1, 2):
                path = Path(directory) / f"zero-v{version}.txt"
                header = f"REGICIDE_NN_V{version} 768 32 16 600" + (" 100" if version == 2 else "")
                path.write_text(header + "\n" + "0 " * 25153 + "\n", encoding="ascii")
                models[version] = path
            with chess.engine.SimpleEngine.popen_uci([str(BINARY), "uci"], timeout=30) as engine:
                self.assertIs(engine.options["UsePieceActivity"].default, False)
                engine.configure({"UseQuiescence": False})

                def score(activity):
                    engine.configure({"UsePieceActivity": activity})
                    result = engine.analyse(board, chess.engine.Limit(depth=1), root_moves=[root_move])
                    self.assertEqual(result["pv"][0], root_move)
                    return result["score"].white().score()

                baseline, active = score(False), score(True)
                self.assertEqual(baseline, -EVAL.classical_cp_stm(leaf))
                self.assertEqual(active, -EVAL.classical_cp_stm(leaf, piece_activity=True))
                self.assertNotEqual(active, baseline)
                engine.configure({"UsePositionalEvaluation": False})
                self.assertEqual(score(False), score(True), "material-only evaluation must ignore activity")
                engine.configure({"UsePositionalEvaluation": True})
                for version in (1, 2):
                    engine.configure({"EvalFile": str(models[version]), "UseNeuralEvaluation": True})
                    off, on = score(False), score(True)
                    self.assertEqual(on, off, f"neural V{version} semantics changed with activity")
                    if version == 2:
                        self.assertEqual(on, baseline, "zero residual must retain its legacy classical baseline")
                    engine.configure({"UseNeuralEvaluation": False})
                engine.configure({"UseNNUEEvaluation": True})
                for incremental in (False, True):
                    engine.configure({"UseIncrementalNeuralEvaluation": incremental})
                    self.assertEqual(score(False), score(True), "NNUE must ignore classical activity")

    def test_batched_native_static_parity_on_hundreds_of_new_legal_positions(self):
        if PROBE is None:
            self.skipTest("pass --probe or --binary for native C++ parity")
        cases = legal_cases()
        payload = "".join(fen + "\t" + " ".join(history) + "\n" for fen, history, _ in cases)
        help_text = subprocess.run([str(PROBE), "--help"], capture_output=True, text=True,
                                   check=True, timeout=30).stdout
        activity_args = ["--piece-activity"] if "--piece-activity" in help_text else []
        response = subprocess.run([str(PROBE), *activity_args], input=payload, capture_output=True, text=True,
                                  check=True, timeout=120)
        native = response.stdout.splitlines()
        self.assertEqual(len(native), len(cases))
        activity_comparisons = 0
        for (fen, history, board), line in zip(cases, native):
            fields = line.split("\t")
            self.assertIn(len(fields), (13, 17))
            self.assertEqual(fields[0], board.fen(en_passant="fen"))
            for index, (shield, structure) in enumerate(((True, True), (True, False), (False, True), (False, False)), 3):
                self.assertEqual(int(fields[index]), EVAL.classical_cp_stm(board, shield, structure),
                                 f"native/Python classical mismatch options={shield, structure} fen={board.fen()}")
            if len(fields) == 17:
                for index, (shield, structure) in enumerate(((True, True), (True, False), (False, True), (False, False)), 13):
                    self.assertEqual(int(fields[index]), EVAL.classical_cp_stm(board, shield, structure, True),
                                     f"native/Python activity mismatch options={shield, structure} fen={board.fen()}")
                    activity_comparisons += 1
        self.assertIn(activity_comparisons, (0, len(cases) * 4), "probe output mixed legacy and activity schemas")
        if activity_args:
            self.assertEqual(activity_comparisons, len(cases) * 4, "activity opt-in did not produce four extra scores")
            legacy = subprocess.run([str(PROBE)], input=payload, capture_output=True, text=True,
                                    check=True, timeout=120).stdout.splitlines()
            self.assertEqual(legacy, ["\t".join(line.split("\t")[:13]) for line in native],
                             "default probe must preserve the exact original thirteen-field schema")
        option_count = 8 if activity_comparisons else 4
        result = {"schema": "regicide.classical-python-parity.v1", "status": "passed", "positions": len(cases),
                  "option_pairs": option_count, "comparisons": len(cases) * option_count,
                  "activity_comparisons": activity_comparisons, "max_cp_difference": 0,
                  "probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
                  "python_mirror_sha256": hashlib.sha256(Path(EVAL.__file__).read_bytes()).hexdigest(),
                  "input_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                  "provenance": "Self-authored fixtures plus deterministic random legal playouts; no reserved arena positions or labels"}
        if REPORT:
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Python/classical C++ parity: {len(cases)} positions x {option_count} options, exact 0 cp difference", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--report", type=Path)
    args, remainder = parser.parse_known_args()
    BINARY = args.binary.resolve(strict=True) if args.binary else None
    PROBE = args.probe
    if PROBE is None and args.binary:
        PROBE = args.binary.with_name("regicide_evaluation_probe" + args.binary.suffix)
    if PROBE is not None:
        PROBE = PROBE.resolve(strict=True)
    REPORT = args.report
    unittest.main(argv=[sys.argv[0], *remainder])
