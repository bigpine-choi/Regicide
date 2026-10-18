"""Diagnostic harness tests; tiny optional native searches are not measurements."""

import argparse
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import chess
import chess.engine
import numpy as np

import diagnose_neural_cost as diagnostic
from neural_model import export_model, initialize_model, load_model


class FakeEngine:
    def __init__(self, *, bound=False, no_pv=False, wrong_root=False, wrong_depth=False):
        self.options = {name: SimpleNamespace(type="check", default=True, min=None, max=None, var=None)
                        for name in (*diagnostic.OPTIONS, "UseNeuralEvaluation", "EvalFile")}
        self.config = {}
        self.calls = []
        self.id = {"name": "scripted diagnostic engine"}
        self.transport = SimpleNamespace(get_pid=lambda: id(self))
        self.protocol = SimpleNamespace(last_score_event=None)
        self.bound, self.no_pv, self.wrong_root, self.wrong_depth = bound, no_pv, wrong_root, wrong_depth
        self.quit_called, self.close_called = False, False

    def configure(self, options):
        self.config.update(options)

    def ping(self):
        pass

    def play(self, board, limit, **kwargs):
        self.calls.append((board.copy(stack=True), limit, kwargs))
        roots = kwargs.get("root_moves")
        legal = list(board.legal_moves)
        move = roots[0] if roots else legal[0]
        if self.wrong_root and roots:
            move = next(item for item in legal if item != roots[0])
        depth = limit.depth or 3
        info = {"depth": depth - int(self.wrong_depth), "nodes": limit.nodes or 37,
                "time": .002, "score": chess.engine.PovScore(chess.engine.Cp(12), board.turn),
                "pv": [] if self.no_pv else [move], "lowerbound": self.bound,
                "string": "tt probes 30 hits 10 cutoffs 5 stores 20 allocations 1 reuses 0 clears 1 bytes 4096"}
        raw = f"info depth {info['depth']} nodes {info['nodes']} score cp 12" + (" lowerbound" if self.bound else "") + " pv " + ("" if self.no_pv else move.uci())
        self.protocol.last_score_event = diagnostic.parse_score_event(raw)
        return chess.engine.PlayResult(move, None, info=info)

    def quit(self):
        self.quit_called = True

    def close(self):
        self.close_called = True


class FakeGuard:
    def __init__(self, path, *, dirty=False):
        self.path = path
        self.summary = {"contaminated": dirty, "scope": "scripted guard", "transport_engines": {}}

    def start(self):
        self.path.write_text("{}\n", encoding="utf-8")

    def register_transport(self, pid, role, command):
        self.summary["transport_engines"][role] = pid

    def assert_transports(self):
        if len(set(self.summary["transport_engines"].values())) != 2:
            raise RuntimeError("transport count mismatch")
        self.assert_clean()

    def assert_clean(self):
        if self.summary["contaminated"]:
            raise RuntimeError("scripted external load")

    def close(self):
        pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.document, self.positions = diagnostic.load_positions(diagnostic.DEFAULT_POSITIONS)

    def tearDown(self):
        self.temporary.cleanup()

    def test_predeclared_fixture_is_valid_and_disjoint(self):
        self.assertEqual(len(self.positions), 12)
        self.assertEqual(diagnostic.overlap_check(self.positions)["collisions"], 0)
        self.assertTrue(any(item.get("moves") for item in self.positions))
        self.assertEqual({diagnostic.benchmark.board_for(item).turn for item in self.positions}, {True, False})

    def test_fixture_rejects_duplicate_neural_input(self):
        document = copy.deepcopy(self.document)
        duplicate = copy.deepcopy(document["positions"][0])
        duplicate["name"] = "same input with different clocks"
        fields = duplicate["fen"].split()
        fields[-1] = "99"
        duplicate["fen"] = " ".join(fields)
        document["positions"].append(duplicate)
        path = self.root / "positions.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "repeats"):
            diagnostic.load_positions(path)

    def test_opening_collision_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "protected"):
            diagnostic.overlap_check([{"name": "forbidden start", "moves": []}])

    def test_model_only_output_parameters_zeroed(self):
        original = initialize_model(51, version=2)
        original.w3[:] = np.linspace(-.1, .1, 16)
        original.b3[:] = .04
        source, target = self.root / "learned.nn", self.root / "zero.nn"
        export_model(original, source)
        diagnostic.make_zero(source, target)
        zero = load_model(target)
        self.assertEqual(zero.version, 2)
        for name in ("w1", "b1", "w2", "b2"):
            np.testing.assert_array_equal(getattr(original, name), getattr(zero, name))
        self.assertFalse(np.any(zero.w3))
        self.assertFalse(np.any(zero.b3))
        self.assertTrue(np.any(load_model(source).w3))
        with self.assertRaises(FileExistsError):
            diagnostic.make_zero(source, target)

    def test_legacy_model_cannot_be_cost_control(self):
        source = self.root / "legacy.nn"
        export_model(initialize_model(4, version=1), source)
        with self.assertRaisesRegex(ValueError, "V2"):
            diagnostic.make_zero(source, self.root / "zero.nn")

    def test_six_orders_balanced_and_reproducible(self):
        rows = list(diagnostic.schedule(self.positions[:2], 6, 12))
        self.assertEqual(rows, list(diagnostic.schedule(self.positions[:2], 6, 12)))
        for index in (0, 1):
            orders = [tuple(order) for _, selected, order in rows if selected == index]
            self.assertEqual(len(set(orders)), 6)
            for role in diagnostic.ROLES:
                self.assertEqual([order.index(role) for order in orders].count(0), 2)

    def test_sample_preserves_history_fresh_game_and_tt_telemetry(self):
        engine = FakeEngine()
        position = self.positions[5]
        first = diagnostic.sample(engine, position, chess.engine.Limit(depth=2), expected_depth=2)
        diagnostic.sample(engine, position, chess.engine.Limit(depth=2), expected_depth=2)
        self.assertEqual([move.uci() for move in engine.calls[0][0].move_stack], position["moves"])
        self.assertIsNot(engine.calls[0][2]["game"], engine.calls[1][2]["game"])
        self.assertEqual(first["tt_counters"], {"probes": 30, "hits": 10, "cutoffs": 5, "stores": 20})

    def test_bounds_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "exact"):
            diagnostic.sample(FakeEngine(bound=True), self.positions[0], chess.engine.Limit(depth=2))

    def test_empty_pv_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "PV"):
            diagnostic.sample(FakeEngine(no_pv=True), self.positions[0], chess.engine.Limit(depth=2))

    def test_incomplete_requested_depth_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "requested depth"):
            diagnostic.sample(FakeEngine(wrong_depth=True), self.positions[0], chess.engine.Limit(depth=2), expected_depth=2)

    def test_searchmoves_must_be_honored(self):
        move = next(iter(diagnostic.benchmark.board_for(self.positions[0]).legal_moves)).uci()
        with self.assertRaisesRegex(RuntimeError, "searchmoves"):
            diagnostic.sample(FakeEngine(wrong_root=True), self.positions[0], chess.engine.Limit(depth=2), root_move=move)

    def test_work_equivalence_excludes_time_and_allocation_but_includes_tt_work(self):
        result = diagnostic.sample(FakeEngine(), self.positions[0], chess.engine.Limit(depth=2))
        other = copy.deepcopy(result)
        other["wall_time_seconds"] = 1000
        other["engine_time_seconds"] = 999
        other["tt_memory"]["reuses"] = 17
        self.assertFalse(diagnostic.equivalence_difference(result, other))
        other["tt_counters"]["probes"] += 1
        self.assertEqual(diagnostic.equivalence_difference(result, other), ["tt_counters"])

    def test_dirty_timing_summary_has_no_ratio(self):
        item = diagnostic.sample(FakeEngine(), self.positions[0], chess.engine.Limit(depth=2))
        records = [{"mode": "depth", **{role: copy.deepcopy(item) for role in diagnostic.ROLES}}]
        result = diagnostic.summarize(records, clean=False, completed=True)
        self.assertFalse(result["timing_valid"])
        self.assertIsNone(result["modes"]["depth"]["zero_v2"]["median_paired_wall_ratio_over_hce"])
        self.assertTrue(result["modes"]["depth"]["hce_zero_work_equal"])

    def test_work_mismatch_disables_cost_ratio(self):
        item = diagnostic.sample(FakeEngine(), self.positions[0], chess.engine.Limit(depth=2))
        row = {"mode": "nodes", **{role: copy.deepcopy(item) for role in diagnostic.ROLES}}
        row["zero_v2"]["nodes"] += 1
        result = diagnostic.summarize([row], clean=True, completed=True)["modes"]["nodes"]
        self.assertFalse(result["timing_valid"])
        self.assertEqual(result["hce_zero_work_mismatches"], 1)

    def test_rank_ties_cp_and_mate_handling(self):
        def row(cp=None, mate=None, depth=3):
            return {"depth": depth, "score": {"cp": cp, "mate": mate}}
        ranks = diagnostic.rank_moves({"a2a3": row(15), "a2a4": row(10), "b2b3": row(15)})
        self.assertEqual(ranks["moves"]["a2a4"], {"rank_among_compared_moves": 3, "regret_cp": 5})
        self.assertEqual(ranks["moves"]["a2a3"]["rank_among_compared_moves"], 1)
        mates = diagnostic.rank_moves({"a2a3": row(100), "a2a4": row(mate=2)})
        self.assertEqual(mates["moves"]["a2a4"]["rank_among_compared_moves"], 1)
        self.assertIsNone(mates["moves"]["a2a3"]["regret_cp"])

    def test_mismatched_depth_is_not_regret_evidence(self):
        self.assertFalse(diagnostic.rank_moves({"a2a3": {"depth": 2}, "a2a4": {"depth": 3}})["available"])

    def test_mate_range_cp_is_not_regret_evidence(self):
        self.assertFalse(diagnostic.rank_moves({"a2a3": {"depth": 3, "score": {"cp": 29904, "mate": None}}})["available"])

    def test_missing_raw_event_is_rejected(self):
        engine = FakeEngine()
        original = engine.play
        def missing(*args, **kwargs):
            reply = original(*args, **kwargs)
            engine.protocol.last_score_event = None
            return reply
        engine.play = missing
        with self.assertRaisesRegex(RuntimeError, "raw score"):
            diagnostic.sample(engine, self.positions[0], chess.engine.Limit(depth=2))

    def test_stale_raw_depth_cannot_use_accumulated_info(self):
        engine = FakeEngine()
        original = engine.play
        def stale(*args, **kwargs):
            reply = original(*args, **kwargs)
            event = engine.protocol.last_score_event
            event["raw_info"] = event["raw_info"].replace("depth 2", "depth 1")
            return reply
        engine.play = stale
        with self.assertRaisesRegex(RuntimeError, "matching depth"):
            diagnostic.sample(engine, self.positions[0], chess.engine.Limit(depth=2))

    def test_teacher_compares_selected_moves_at_common_depth(self):
        engine = FakeEngine()
        moves = [move.uci() for move in list(diagnostic.benchmark.board_for(self.positions[0]).legal_moves)[:2]]
        result = diagnostic.teacher_diagnosis(engine, self.positions[0], moves, 100, 200)
        self.assertTrue(result["ranks"]["available"])
        self.assertEqual([call[1].nodes for call in engine.calls[:2]], [100, 200])
        self.assertTrue(all(call[1].depth == 3 and call[1].nodes == 200 for call in engine.calls[2:]))
        self.assertTrue(all(len(call[2]["root_moves"]) == 1 for call in engine.calls[2:]))

    def _run(self, dirty=False):
        model = self.root / "model.nn"
        export_model(initialize_model(6, version=2), model)
        binary = self.root / "regicide.exe"
        binary.write_bytes(b"fake executable")
        args = SimpleNamespace(output=self.root / "output", binary=binary, model=model,
            source_commit="a" * 40, positions=diagnostic.DEFAULT_POSITIONS,
            position_document=self.document, selected_positions=self.positions[:1], overlap={},
            depth=2, nodes=20, movetime_ms=10, repeats=1, warmups=1, seed=6,
            teacher_low_nodes=100, teacher_high_nodes=200)
        engines = []
        def factory(*arguments, **keywords):
            engine = FakeEngine()
            engines.append(engine)
            return engine
        with patch.object(diagnostic, "validate_native_model", return_value={"score_cp": 0}):
            result = diagnostic.run(args, engine_factory=factory,
                guard_factory=lambda path: FakeGuard(path, dirty=dirty))
        return result, args, engines

    def test_complete_fake_run_writes_evidence_and_shuts_down(self):
        result, args, engines = self._run()
        self.assertTrue(result["completed"], result["error"])
        self.assertTrue(result["resource_guard"]["valid_for_comparison"])
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(len(result["teacher"]), 1)
        self.assertTrue(all(engine.quit_called and engine.close_called for engine in engines))
        self.assertEqual(json.loads((args.output / "diagnostic.json").read_text(encoding="utf-8")), result)
        with self.assertRaises(FileExistsError):
            diagnostic.run(args)

    def test_contaminated_run_fails_and_retains_raw_evidence(self):
        result, args, engines = self._run(dirty=True)
        self.assertFalse(result["completed"])
        self.assertFalse(result["resource_guard"]["valid_for_comparison"])
        self.assertFalse(result["summary"]["timing_valid"])
        self.assertIn("external load", result["error"])
        self.assertTrue((args.output / "diagnostic.json").exists())
        self.assertTrue(all(engine.quit_called and engine.close_called for engine in engines))


def native_smoke(binary):
    """Optional depth-one HCE/zero equivalence and forced root support only."""
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary).resolve()
        model = folder / "zero.nn"
        export_model(initialize_model(35, version=2), model)
        position = diagnostic.load_positions(diagnostic.DEFAULT_POSITIONS)[1][0]
        engine = diagnostic.LearningEngine.popen_uci([str(binary), "uci"])
        try:
            engine.configure(dict(diagnostic.OPTIONS, UseNeuralEvaluation=False))
            baseline = diagnostic.sample(engine, position, chess.engine.Limit(depth=1), expected_depth=1)
            engine.configure({"EvalFile": str(model)})
            engine.configure({"UseNeuralEvaluation": True})
            zero = diagnostic.sample(engine, position, chess.engine.Limit(depth=1), expected_depth=1)
            assert not diagnostic.equivalence_difference(baseline, zero)
            forced = diagnostic.sample(engine, position, chess.engine.Limit(depth=1), expected_depth=1, root_move=baseline["move"])
            assert forced["move"] == baseline["move"]
        finally:
            engine.quit()
        print("PASS native depth-one zero/HCE work equivalence and searchmoves; timing excluded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--binary", type=Path)
    options, remaining = parser.parse_known_args()
    sys.argv = [sys.argv[0], *remaining]
    native = options.binary.resolve() if options.binary else None
    result = unittest.main(exit=False)
    if not result.result.wasSuccessful():
        sys.exit(1)
    if native:
        native_smoke(native)
