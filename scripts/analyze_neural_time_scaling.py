"""Post-match, full-history diagnostic searches. Never changes engine or model.

Prepare selects scenes from historical logs before any new search. Run uses fresh
game tokens, three paired time repetitions and an independent node-limited child
comparison. The latter is a diagnostic reference, not an oracle or a new match.
"""

import argparse
from collections import Counter
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import chess
import chess.engine

import compare_engines as comparison
import run_neural_strength_comparison as strength
import see_clean_guard as guarded


ROOT = Path(__file__).resolve().parents[1]
OLD = Path("docs/matches/2026-10-19-neural-strength")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, data):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def board_for(initial, history):
    board = chess.Board(initial)
    for move in history:
        board.push_uci(move)
    return board


def select_positions(short, long):
    """Only historical inputs; all changed results, including deterioration."""
    positions, lookup, changed = [], {}, []

    def add(game, move, rule, extra=None):
        history = game["uci_moves"][:move["ply"] - 1]
        board = board_for(game["initial_fen"], history)
        assert board.fen(en_passant="fen") == move["fen_before"]
        key = (game["initial_fen"], tuple(history))
        if key not in lookup:
            record = {"position_id": f"P{len(positions) + 1:02}",
                      "initial_fen": game["initial_fen"], "history": history,
                      "fen": board.fen(en_passant="fen"),
                      "original_mover": "white" if board.turn else "black", "sources": []}
            lookup[key] = record
            positions.append(record)
        source = {"game": game["game"], "pair": game["pair"],
                  "opening": game["opening"], "selection_rule": rule,
                  "source_movetime_ms": 100, "ply": move["ply"],
                  "historical_role": move["role"], "historical_move": move["move"],
                  "historical_search": move}
        if extra:
            source.update(extra)
        lookup[key]["sources"].append(source)

    for a, b in zip(short["games"], long["games"]):
        assert a["game"] == b["game"] and a["candidate_points"] is not None
        if a["candidate_points"] == b["candidate_points"]:
            continue
        changed.append({"game": a["game"], "pair": a["pair"], "opening": a["opening"],
                        "candidate_color": "white" if a["white"] == "candidate" else "black",
                        "short_points": a["candidate_points"], "long_points": b["candidate_points"]})
        index = next(i for i, (x, y) in enumerate(zip(a["uci_moves"], b["uci_moves"])) if x != y)
        first = next(m for m in a["search_moves"] if m["ply"] == index + 1)
        alternate = next(m for m in b["search_moves"] if m["ply"] == index + 1)
        add(a, first, "first_divergence", {"historical_long_search": alternate})
        own = [m for m in a["search_moves"] if m["role"] == "candidate"]
        cp = lambda m: m["score"]["cp"] if m["score"] else None
        falls = [(cp(y) - cp(x), x["ply"], x, y) for x, y in zip(own, own[1:])
                 if cp(x) is not None and cp(y) is not None and cp(x) >= -200
                 and cp(y) - cp(x) <= -100]
        if falls:
            delta, _, pre, post = min(falls, key=lambda item: (item[0], item[1]))
            add(a, pre, "largest_nonmate_drop_pre_at_least_minus200",
                {"recorded_drop_cp": delta, "next_own_search": post})
        for i in range(1, len(own) - 2):
            group = own[i:i + 3]
            if all(cp(m) is not None and cp(m) <= -100 for m in group):
                add(a, own[i - 1], "before_first_three_own_scores_at_most_minus100",
                    {"subsequent_own_scores_cp": [cp(m) for m in group]})
                break
    return {"schema_version": 1, "changed_games": changed, "positions": positions}


def prepare(output):
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new output directory")
    sources = [OLD / f"attempt-001-time{tc}/time{tc}.json" for tc in (100, 1000)]
    selected = select_positions(*(read(ROOT / p) for p in sources))
    output.mkdir(parents=True)
    write(output / "positions.json", selected)
    old_freeze = ROOT / OLD / "freeze.json"
    files = {Path(p).relative_to(ROOT).as_posix(): expected
             for p, expected in strength.bound_files(read(old_freeze)).items()}
    files.update({p.as_posix(): digest(ROOT / p) for p in sources})
    for p in [old_freeze, Path(__file__), output / "positions.json",
              ROOT / "scripts/.gitattributes",
              ROOT / "tests/.gitattributes", ROOT / "docs/analysis/.gitattributes"]:
        files[p.resolve().relative_to(ROOT).as_posix()] = digest(p)
    protocol = {"schema_version": 1, "created_utc": now(),
                "engine_source_commit": comparison.git_command("rev-parse", "HEAD"),
                "old_freeze": OLD.as_posix() + "/freeze.json",
                "positions": (output / "positions.json").resolve().relative_to(ROOT).as_posix(),
                "files": files, "selection": "All seven changed games, including deterioration. Merge identical full histories. Per game: first divergence; largest consecutive own nonmate cp fall >=100 with pre-score >=-200 (earliest tie); turn before first three consecutive own cp<=-100. All selection uses old 100ms logs only, before new searches.",
                "root_time_controls_ms": [100, 1000], "root_repeats": 3,
                "root_order": "For each position in selection order and repeat 1..3: baseline100,candidate100,baseline1000,candidate1000; reverse these four on even (position_index+repeat).",
                "fresh_game_each_query": True, "preserve_full_history": True,
                "continuation_nodes": 200000, "continuation_repeats": 1,
                "continuation_selection": "Union of modal chosen moves from all four root conditions, plus historical short move(s) and historical long first-divergence move(s). Modal ties resolve by ascending UCI text. Sort all candidate moves lexically. Search child position with both evaluators at 200000 nodes, preserving history; skip any terminal/claimable outcome under original claim policy.",
                "common_options": read(old_freeze)["common_effective_options"],
                "monitor": {"idle_seconds": 10, "poll_seconds": 0.5,
                            "max_snapshot_gap_seconds": 2, "external_engine_cpu_ticks": 0},
                "interpretation": "Posthoc diagnostic, not fresh strength testing. Child references at equal node budget are not ground truth, root scores, or proof of best play. Original game TT contents cannot be reconstructed. Root queries are controlled full-history searches with empty TT. Keep all repeats and all adverse evidence; no model changes or tuning."}
    write(output / "protocol.json", protocol)
    print(json.dumps({"prepared": str(output), "positions": len(selected["positions"]),
                      "changed_games": len(selected["changed_games"])}), flush=True)


class RawLog:
    def __init__(self, path):
        self.stream = Path(path).open("x", encoding="utf-8", newline="\n")
        self.lock, self.seq = threading.Lock(), 0
        self.lines = []

    def line(self, role, direction, line):
        with self.lock:
            self.seq += 1
            row = {"seq": self.seq, "monotonic": time.monotonic(), "utc": now(),
                   "role": role, "direction": direction, "line": line}
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stream.flush()
            self.lines.append(row)

    def protocol(self, role):
        log = self

        class RecordedUci(chess.engine.UciProtocol):
            def send_line(self, line):
                log.line(role, "send", line)
                super().send_line(line)

            def _line_received(self, line):
                log.line(role, "recv", line)
                super()._line_received(line)

        return RecordedUci


def mode_move(rows):
    counts = Counter(row["move_uci"] for row in rows)
    return min(counts, key=lambda move: (-counts[move], move))


def candidate_moves(position, rows):
    choices = {mode_move([r for r in rows if r["position_id"] == position["position_id"]
                         and r["role"] == role and r["limit"]["movetime_ms"] == tc])
               for role in ("baseline", "candidate") for tc in (100, 1000)}
    for source in position["sources"]:
        choices.add(source["historical_move"])
        if "historical_long_search" in source:
            choices.add(source["historical_long_search"]["move"])
    return sorted(choices)


def check_files(protocol, expected_sha, protocol_path):
    assert digest(protocol_path) == expected_sha
    assert comparison.git_command("rev-parse", "HEAD") == protocol["engine_source_commit"]
    observed = {}
    for name, expected in protocol["files"].items():
        actual = digest(ROOT / name)
        if actual != expected:
            raise ValueError("Frozen diagnostic input changed: " + name)
        observed[name] = actual
    return {"utc": now(), "files": observed, "protocol_sha256": expected_sha}


def run(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Preserve previous attempts; use a new output directory")
    protocol, frozen_sha = read(protocol_path), digest(protocol_path)
    before = check_files(protocol, frozen_sha, protocol_path)
    selected = read(ROOT / protocol["positions"])
    freeze_path = ROOT / protocol["old_freeze"]
    freeze = read(freeze_path)
    strength.assert_frozen(freeze_path, freeze, digest(freeze_path))
    output.mkdir(parents=True)
    raw = RawLog(output / "uci.jsonl")
    guard = guarded.EngineCpuGuard(guarded.WindowsProcesses(), output / "monitor.jsonl")
    awake, engines, rows = strength.Awake(), {}, []
    state = {"schema_version": 1, "started_utc": now(), "protocol_path": str(protocol_path),
             "protocol_sha256": frozen_sha, "before": before, "after": None,
             "commands": {}, "options": None, "model_ack": None,
             "all_searches_completed": False, "error": None, "valid": False}
    queries = (output / "queries.jsonl").open("x", encoding="utf-8", newline="\n")

    def query(position, role, limit, repeat, candidate_move=None):
        board = board_for(position["initial_fen"], position["history"])
        if candidate_move:
            board.push_uci(candidate_move)
        query_id = f"Q{len(rows) + 1:04}"
        row = {"query_id": query_id, "phase": "continuation" if candidate_move else "root",
               "position_id": position["position_id"], "role": role, "repeat": repeat,
               "limit": limit, "initial_fen": position["initial_fen"],
               "history": [m.uci() for m in board.move_stack],
               "fen": board.fen(en_passant="fen"), "original_mover": position["original_mover"],
               "candidate_move": candidate_move}
        outcome = board.outcome(claim_draw=True)
        if candidate_move and outcome:
            row["skipped_outcome"] = {"result": outcome.result(),
                                      "winner": None if outcome.winner is None else "white" if outcome.winner else "black",
                                      "termination": outcome.termination.name.lower()}
        else:
            assert not outcome, (position["position_id"], outcome)
            guard.sample("before-query:" + query_id)
            guard.assert_transports()
            row.update(started_monotonic=time.monotonic(), uci_begin_seq=raw.seq + 1)
            reply = engines[role].play(board, chess.engine.Limit(
                time=limit.get("movetime_ms", 0) / 1000 or None,
                nodes=limit.get("nodes")), game=object(), info=chess.engine.INFO_ALL)
            row.update(finished_monotonic=time.monotonic(), uci_end_seq=raw.seq)
            guard.sample("after-query:" + query_id)
            guard.assert_clean()
            assert reply.move in board.legal_moves
            pv = list(reply.info.get("pv", []))
            assert pv and pv[0] == reply.move
            replay = board.copy()
            san = []
            for move in pv:
                assert move in replay.legal_moves
                san.append(replay.san(move))
                replay.push(move)
            row.update(move_uci=reply.move.uci(), move_san=board.san(reply.move),
                       score=comparison.score_record(reply.info, board.turn),
                       depth=reply.info.get("depth"), seldepth=reply.info.get("seldepth"),
                       nodes=reply.info.get("nodes"), engine_time_seconds=reply.info.get("time"),
                       elapsed_seconds=row["finished_monotonic"] - row["started_monotonic"],
                       pv_uci=[m.uci() for m in pv], pv_san=san,
                       pv_final_fen=replay.fen(en_passant="fen"))
        rows.append(row)
        queries.write(json.dumps(row, ensure_ascii=False) + "\n")
        queries.flush()
        if len(rows) % 12 == 0:
            print(json.dumps({"queries": len(rows), "phase": row["phase"],
                              "position": position["position_id"]}), flush=True)

    try:
        awake.start()
        guard.start()
        guard.require_idle(protocol["monitor"]["idle_seconds"])
        for role in ("baseline", "candidate"):
            command = [str((ROOT / freeze[role]["binary"]).resolve()), "uci"]
            state["commands"][role] = command
            engines[role] = chess.engine.SimpleEngine.popen(raw.protocol(role), command, timeout=30)
            guard.register_transport(engines[role].transport.get_pid(), role, command)
        options = {"UseNNUEEvaluation": True, "UseIncrementalNeuralEvaluation": True,
                   "NeuralModelFile": str((ROOT / freeze["model"]["path"]).resolve())}
        state["options"] = strength.check_options(engines, freeze, options)
        engines["candidate"].configure(options)
        engines["candidate"].ping()
        checksum = str(freeze["model"]["checksum"])
        acknowledgements = [r for r in raw.lines if r["role"] == "candidate"
                            and r["direction"] == "recv" and checksum in r["line"]]
        assert len(acknowledgements) == 1, "Require actual model-load checksum acknowledgement"
        state["model_ack"] = acknowledgements[0]
        guard.assert_transports()
        for i, position in enumerate(selected["positions"]):
            for repeat in range(1, protocol["root_repeats"] + 1):
                conditions = [("baseline", 100), ("candidate", 100),
                              ("baseline", 1000), ("candidate", 1000)]
                if (i + repeat) % 2 == 0:
                    conditions.reverse()
                for role, tc in conditions:
                    query(position, role, {"movetime_ms": tc}, repeat)
        root_rows = list(rows)
        for position in selected["positions"]:
            for move in candidate_moves(position, root_rows):
                for role in ("baseline", "candidate"):
                    query(position, role, {"nodes": protocol["continuation_nodes"]}, 1, move)
        state["all_searches_completed"] = True
    except BaseException as exc:
        state["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        for engine in engines.values():
            try:
                engine.quit()
            except Exception as exc:
                state["error"] = state["error"] or ("Engine cleanup: " + str(exc))
            finally:
                engine.close()
        guard.close()
        awake.close()
        queries.close()
        raw.stream.close()
        state["wake_request"] = awake.record
        guard.summary["valid_for_comparison"] = bool(state["all_searches_completed"]
                                                      and not guard.summary["contaminated"])
        write(output / "monitor.json", guard.summary)
        try:
            state["after"] = check_files(protocol, frozen_sha, protocol_path)
            state["valid"] = bool(guard.summary["valid_for_comparison"]
                                  and awake.record["restore_succeeded"] and not state["error"])
        finally:
            state.update(finished_utc=now(), query_count=len(rows))
            write(output / "run-state.json", state)

    # Static evaluations are outside the timed two-transport CPU-monitor interval.
    # Include every searched root/child and PV endpoint, plus every chosen child.
    fens = set()
    for row in rows:
        board = board_for(row["initial_fen"], row["history"])
        fens.add(board.fen(en_passant="fen"))
        if "pv_uci" in row:
            for move in row["pv_uci"]:
                board.push_uci(move)
                fens.add(board.fen(en_passant="fen"))
    inputs = "\n".join(sorted(fens)) + "\n"
    (output / "static-inputs.txt").write_text(inputs, encoding="utf-8", newline="\n")
    command = [str(ROOT / freeze["baseline"]["binary"]), "eval-batch", "--nnue-model",
               str(ROOT / freeze["model"]["path"])]
    result = subprocess.run(command, input=inputs, text=True, capture_output=True, check=True, timeout=60)
    (output / "static.tsv").write_text(result.stdout, encoding="utf-8", newline="\n")
    lines = result.stdout.splitlines()
    assert len(lines) == len(fens)
    static = []
    for fen, line in zip(sorted(fens), lines):
        classical, full, incremental = map(int, line.split("\t"))
        assert full == incremental
        static.append({"fen": fen, "classical": classical, "full": full, "incremental": incremental})
    write(output / "static.json", {"schema_version": 1, "command": command,
                                  "outside_timed_monitor": True, "positions": static})
    print(json.dumps({"status": "PASS" if state["valid"] else "FAIL", "queries": len(rows),
                      "static_positions": len(static), "output": str(output)}), flush=True)
    return 0 if state["valid"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--output", type=Path, required=True)
    execute = sub.add_parser("run")
    execute.add_argument("--protocol", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output.resolve())
        return 0
    return run(args.protocol, args.output)


if __name__ == "__main__":
    sys.exit(main())
