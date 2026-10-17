"""Cheap benchmark evidence checks, including two real depth-one paired repeats.

Usage: python tests/benchmark_search_tests.py path/to/regicide
These are harness checks; their timings must not be reported as an experiment.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile

import chess
import chess.engine


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("benchmark_search", ROOT / "scripts/benchmark_search.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class FakeEngine:
    def __init__(self, cp=12, depth_delta=0, bound=False, illegal_pv=False, fail=False, counters=False,
                 memory=False, node_delta=0):
        self.cp = cp
        self.depth_delta = depth_delta
        self.bound = bound
        self.illegal_pv = illegal_pv
        self.fail = fail
        self.counters = counters
        self.memory = memory
        self.node_delta = node_delta
        self.options = {"Hash": SimpleNamespace(type="spin", default=16, min=1, max=128, var=None)}
        self.id = {"name": "Scripted evidence engine"}
        self.configured = None
        self.calls = []
        self.quit_called = False

    def configure(self, options):
        self.configured = options

    def play(self, board, limit, **kwargs):
        if self.fail:
            raise RuntimeError("scripted search failure")
        self.calls.append((list(move.uci() for move in board.move_stack), limit, kwargs))
        move = next(iter(board.legal_moves))
        info = {
            "score": chess.engine.PovScore(chess.engine.Cp(self.cp), board.turn),
            "depth": limit.depth + self.depth_delta, "nodes": 25 + self.node_delta, "time": 0.001,
            "lowerbound": self.bound,
            "pv": [chess.Move.from_uci("a1a8")] if self.illegal_pv else [move]}
        if self.counters or self.memory:
            info["string"] = "tt probes 20 hits 8 cutoffs 3 stores 15"
        if self.memory:
            first = len(self.calls) == 1
            info["string"] += f" allocations {int(first)} reuses {int(not first)} clears 0 bytes 4096"
        return chess.engine.PlayResult(move, None, info=info)

    def quit(self):
        self.quit_called = True


def fake_args(binary):
    source = ROOT / "tests/data/profile_positions.json"
    document, positions = RUNNER.load_positions(source)
    return SimpleNamespace(baseline=binary, candidate=binary, baseline_commit="a" * 40,
                           candidate_commit="b" * 40, baseline_options={}, candidate_options={"Hash": 8},
                           positions=source, position_document=document, selected_positions=positions[:2],
                           depth=1, repeats=2, warmup=0, require_same_search=False,
                           seed=20261017, allow_score_differences=False)


def fake_run(args, baseline, candidate):
    engines = iter([baseline, candidate])
    commands = []

    def factory(command):
        commands.append(command)
        return next(engines)

    output = io.StringIO()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        document = RUNNER.run(args, output, engine_factory=factory)
    assert document == json.loads(output.getvalue())
    assert commands == [[str(args.baseline), "uci"], [str(args.candidate), "uci"]]
    assert baseline.quit_called and candidate.quit_called
    return document, stderr.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve()
    passed = 0

    source = ROOT / "tests/data/profile_positions.json"
    declared, positions = RUNNER.load_positions(source)
    assert len(positions) == 10
    openings = json.loads((ROOT / "tests/data/comparison_openings.json").read_text(encoding="utf-8"))["openings"]
    assert [{"name": item["name"], "moves": item["moves"]} for item in positions[:8]] == openings
    assert all(RUNNER.board_for(item).fen(en_passant="fen") == item["search_fen"] for item in positions)
    first = list(RUNNER.schedule(positions, 5, 7))
    assert first == list(RUNNER.schedule(positions, 5, 7))
    assert first != list(RUNNER.schedule(positions, 5, 8))
    for index in range(len(positions)):
        orders = [row[2] for row in first if row[1] == index]
        assert len(orders) == 5
        assert all(one == list(reversed(two)) for one, two in zip(orders, orders[1:]))
    print("PASS ten predeclared legal positions, opening compatibility and balanced deterministic scheduling")
    passed += 1

    def stats_record(name, baseline_wall, candidate_wall, baseline_time, candidate_time, score_equal=True):
        return {"position": name, "scores_equal": score_equal,
                "baseline": {"wall_time_seconds": baseline_wall, "engine_time_seconds": baseline_time, "nodes": 100},
                "candidate": {"wall_time_seconds": candidate_wall, "engine_time_seconds": candidate_time, "nodes": 50}}

    records = [stats_record("one", 6, 2, 6, 2), stats_record("one", 4, 2, 0, 0, False),
               stats_record("two", 4, 4, None, None), {"position": "one", "baseline": {}}]
    summary = RUNNER.summarize(records, [{"name": "one"}, {"name": "two"}, {"name": "missing"}])
    assert summary["completed_pairs"] == 3 and summary["score_mismatches"] == 1
    assert summary["median_paired_wall_speedup_baseline_over_candidate"] == 2
    assert summary["median_paired_node_ratio_candidate_over_baseline"] == 0.5
    assert summary["median_paired_engine_speedup_baseline_over_candidate"] == 3
    assert summary["engine_time_ratio_pairs_excluding_zero_or_missing_time"] == 1
    assert summary["per_position"][0]["median_paired_wall_speedup_baseline_over_candidate"] == 2.5
    assert summary["per_position"][2]["baseline"]["median_wall_time_seconds"] is None
    print("PASS paired median ratios, unequal scores, partial records and millisecond-zero time handling")
    passed += 1

    for missing in ({}, {"string": None}, {"string": "unrelated engine message"},
                    {"string": "tt probes 1 hits 0 cutoffs 0 stores"}):
        assert RUNNER.transposition_counters(missing) is None
    assert RUNNER.transposition_counters({"string": "tt probes 0 hits 0 cutoffs 0 stores 0"}) == {
        "probes": 0, "hits": 0, "cutoffs": 0, "stores": 0}
    assert RUNNER.transposition_counters({"string": "tt probes 20 hits 8 cutoffs 3 stores 15"}) == {
        "probes": 20, "hits": 8, "cutoffs": 3, "stores": 15}
    extended = {"string": "tt probes 20 hits 8 cutoffs 3 stores 15 allocations 1 reuses 0 clears 1 bytes 4096"}
    assert RUNNER.transposition_counters(extended) == {"probes": 20, "hits": 8, "cutoffs": 3, "stores": 15}
    assert RUNNER.transposition_memory(extended) == {"allocations": 1, "reuses": 0, "clears": 1, "bytes": 4096}
    assert RUNNER.transposition_memory({"string": "tt probes 20 hits 8 cutoffs 3 stores 15"}) is None
    assert RUNNER.transposition_memory({}) is None
    assert RUNNER.transposition_memory({"string": "tt probes 0 hits 0 cutoffs 0 stores 0 allocations 0 reuses 0 clears 0 bytes 0"}) == {
        "allocations": 0, "reuses": 0, "clears": 0, "bytes": 0}
    try:
        RUNNER.transposition_counters({"string": "tt probes 2 hits 3 cutoffs 1 stores 2"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("inconsistent TT counters were accepted")
    print("PASS optional TT telemetry parser, absent-versus-zero distinction and counter consistency")
    passed += 1

    config = fake_args(binary)
    baseline, candidate = FakeEngine(), FakeEngine(counters=True)
    data, errors = fake_run(config, baseline, candidate)
    assert data["exit_code"] == 0 and not errors
    assert data["warmup"] == 0 and data["warmup_records"] == []
    assert "first measured sample includes" in data["allocation_policy"]
    assert data["summary"]["completed_pairs"] == 4 and data["summary"]["score_mismatches"] == 0
    assert baseline.configured == {} and candidate.configured == {"Hash": 8}
    assert len({id(call[2]["game"]) for call in baseline.calls}) == 4
    assert any(call[0] == openings[1]["moves"] for call in baseline.calls)
    for record in data["records"]:
        for role in RUNNER.ROLES:
            sample = record[role]
            assert sample["nodes"] == 25 and sample["depth"] == 1
            assert sample["wall_time_seconds"] >= 0 and sample["engine_time_seconds"] == 0.001
            assert sample["pv"] == [sample["move"]]
            assert sample["score"] == {"pov": "mover", "cp": 12, "mate": None,
                                        "lowerbound": False, "upperbound": False}
            assert sample["tt_counters"] == ({"probes": 20, "hits": 8, "cutoffs": 3, "stores": 15}
                                               if role == "candidate" else None)
            assert sample["tt_memory"] is None
    assert data["summary"]["transposition_counters"] == {
        "baseline": {"samples_reporting": 0, "probes": None, "hits": None, "cutoffs": None, "stores": None},
        "candidate": {"samples_reporting": 4, "probes": 80, "hits": 32, "cutoffs": 12, "stores": 60}}
    assert data["engines"]["candidate"]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert data["positions_file"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert data["engines"]["candidate"]["advertised_options"]["Hash"]["default"] == 16
    print("PASS fresh UCI game per sample, opening history, option provenance and complete persisted evidence")
    passed += 1

    memory_config = fake_args(binary)
    memory_config.require_same_search = True
    baseline, candidate = FakeEngine(counters=True), FakeEngine(memory=True)
    data, errors = fake_run(memory_config, baseline, candidate)
    assert data["exit_code"] == 0 and not errors and data["summary"]["search_graph_mismatches"] == 0
    assert data["summary"]["transposition_memory"] == {
        "baseline": {"samples_reporting": 0, "allocations": None, "reuses": None, "clears": None, "bytes_max": None},
        "candidate": {"samples_reporting": 4, "allocations": 1, "reuses": 3, "clears": 0, "bytes_max": 4096}}
    assert data["records"][0]["candidate"]["process_search_index"] == 1
    assert data["records"][0]["candidate"]["tt_memory"]["allocations"] == 1
    memory_config.warmup = 2
    baseline, candidate = FakeEngine(counters=True), FakeEngine(memory=True)
    warm, errors = fake_run(memory_config, baseline, candidate)
    assert warm["exit_code"] == 0 and not errors
    assert len(warm["warmup_records"]) == 4 and len(warm["records"]) == 4
    assert warm["summary"]["completed_pairs"] == 4
    assert warm["summary"]["transposition_counters"]["candidate"]["probes"] == 80
    assert warm["summary"]["transposition_memory"]["candidate"] == {
        "samples_reporting": 4, "allocations": 0, "reuses": 4, "clears": 0, "bytes_max": 4096}
    assert [row["position_index"] for row in warm["warmup_records"]] == [0] * 4
    assert [row["role"] for row in warm["warmup_records"]] == ["baseline"] * 2 + ["candidate"] * 2
    assert warm["warmup_records"][2]["result"]["tt_memory"]["allocations"] == 1
    assert warm["warmup_records"][3]["result"]["tt_memory"]["reuses"] == 1
    assert warm["records"][0]["candidate"]["process_search_index"] == 3
    assert [(row["repetition"], row["position_index"], row["order"]) for row in data["records"]] == [
        (row["repetition"], row["position_index"], row["order"]) for row in warm["records"]]
    assert len(candidate.calls) == 6 and len({id(call[2]["game"]) for call in candidate.calls}) == 6
    assert all(call[0] == [] for call in candidate.calls[:2])
    print("PASS cold first allocation, retained-buffer telemetry, warmup exclusion and unchanged paired schedule")
    passed += 1

    memory_config.warmup = 0
    memory_config.allow_score_differences = True
    data, errors = fake_run(memory_config, FakeEngine(), FakeEngine(node_delta=1))
    assert data["exit_code"] == 1 and data["summary"]["score_mismatches"] == 0
    assert data["summary"]["search_graph_mismatches"] == 4
    assert errors.count("SEARCH MISMATCH") == 4
    assert all(row["search_differences"] == ["nodes"] for row in data["records"])
    reference = data["records"][0]["baseline"]
    for field in ("score", "move", "depth", "nodes", "seldepth", "pv", "tt_counters"):
        changed = dict(reference)
        changed[field] = "different"
        assert RUNNER.search_differences(reference, changed) == [field]
    changed = dict(reference, tt_memory={"allocations": 1}, wall_time_seconds=999)
    assert RUNNER.search_differences(reference, changed) == []
    print("PASS strict graph equality, visible mismatch rejection and exclusion of memory/timing counters")
    passed += 1

    for allow, expected in [(False, 1), (True, 0)]:
        config.allow_score_differences = allow
        data, errors = fake_run(config, FakeEngine(), FakeEngine(cp=13))
        assert data["exit_code"] == expected and data["summary"]["score_mismatches"] == 4
        assert errors.count("SCORE MISMATCH") == 4
        assert data["allow_score_differences"] == allow
    print("PASS differing exact scores always visible and strict by default")
    passed += 1

    for engine in [FakeEngine(depth_delta=-1), FakeEngine(bound=True),
                   FakeEngine(illegal_pv=True), FakeEngine(fail=True)]:
        data, errors = fake_run(config, engine, FakeEngine())
        assert data["exit_code"] == 2 and data["error"] and errors
        assert data["summary"]["completed_pairs"] == 0
    config.candidate_options = {"Missing": True}
    data, errors = fake_run(config, FakeEngine(), FakeEngine())
    assert data["exit_code"] == 2 and "does not advertise option Missing" in errors
    print("PASS incomplete depth, bounds, illegal PV, engine errors and unsupported options reject success")
    passed += 1

    with tempfile.TemporaryDirectory(prefix="regicide-benchmark-test-") as directory:
        temporary = Path(directory)
        selected = temporary / "positions.json"
        selected.write_text(json.dumps({"positions": declared["positions"][:2]}), encoding="utf-8")
        output = temporary / "benchmark.json"
        commit = RUNNER.git_command("rev-parse", "HEAD")
        command = [sys.executable, str(ROOT / "scripts/benchmark_search.py"), str(binary), str(binary),
                   "--depth", "1", "--repeats", "2", "--positions", str(selected),
                   "--baseline-commit", commit, "--candidate-commit", commit, "--output", str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, (result.stdout, result.stderr)
        evidence = json.loads(output.read_text(encoding="utf-8"))
        assert evidence["exit_code"] == 0 and evidence["summary"]["completed_pairs"] == 4
        assert evidence["summary"]["score_mismatches"] == 0
        assert len(evidence["records"]) == 4
        assert evidence["warmup"] == 0 and evidence["warmup_records"] == []
        advertises_tt = "UseTranspositionTable" in evidence["engines"]["candidate"]["advertised_options"]
        for record in evidence["records"]:
            assert record["scores_equal"]
            for role in RUNNER.ROLES:
                board = chess.Board(record[role]["search_fen"])
                assert chess.Move.from_uci(record[role]["move"]) in board.legal_moves
                assert record[role]["depth"] == 1 and record[role]["nodes"] > 0
                if advertises_tt:
                    counters = record[role]["tt_counters"]
                    assert counters is not None and counters["probes"] > 0 and counters["stores"] > 0
                    assert counters["cutoffs"] <= counters["hits"] <= counters["probes"]
        for role in RUNNER.ROLES:
            assert evidence["summary"]["transposition_counters"][role]["samples_reporting"] == (
                4 if advertises_tt else 0)
        original = output.read_bytes()
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2 and "output already exists" in result.stderr
        assert output.read_bytes() == original
        result = subprocess.run(command + ["--overwrite"], capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, (result.stdout, result.stderr)
        result = subprocess.run(command + ["--overwrite", "--warmup", "1", "--require-same-search"],
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, (result.stdout, result.stderr)
        warmed = json.loads(output.read_text(encoding="utf-8"))
        assert warmed["warmup"] == 1 and len(warmed["warmup_records"]) == 2
        assert warmed["summary"]["completed_pairs"] == 4 and warmed["summary"]["search_graph_mismatches"] == 0
        assert all(row["search_equal"] for row in warmed["records"])
        for role in RUNNER.ROLES:
            assert warmed["records"][0][role]["process_search_index"] == 2
            if warmed["records"][0][role]["tt_memory"] is not None:
                memory = warmed["summary"]["transposition_memory"][role]
                assert memory["samples_reporting"] == 4
                assert memory["allocations"] == 0 and memory["reuses"] == 4 and memory["clears"] == 0
                assert memory["bytes_max"] > 0
        result = subprocess.run(command + ["--overwrite", "--warmup", "-1"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 2 and "must be a nonnegative integer" in result.stderr
        input_command = command.copy()
        input_command[-1] = str(binary)
        result = subprocess.run(input_command + ["--overwrite"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 2 and "output must not replace an input file" in result.stderr
    print("PASS real UCI repeats, strict warm search equality, persisted telemetry and output/argument protections")
    passed += 1
    print(f"benchmark harness: {passed} checks passed")


if __name__ == "__main__":
    main()
