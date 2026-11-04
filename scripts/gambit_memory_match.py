"""Paired production-gambit matches changing only the exact-state memory.

Own Regicide selects every played move. Prefixes are authored starting
conditions, not forced moves, and are excluded from observed-move statistics.
Only completed legal results count; no external engine or CP adjudication.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time
import zipfile

import chess
import chess.engine
import chess.pgn

import boss_play
import gambit_operations
import gambit_practice_memory as memory
from boss_mode_match import same_board
from boss_strength_match import DisplayOnlyPeer, RecordedEngine, SOURCES, counts, sha, utc, write_json
from game_outcome import automatic_outcome, DRAW_POLICY
from gambit_memory_match_metrics import move_metrics
from neural_search_data.engine import _RawLog, _logging_protocol
from play_game import source_version
from play_human import HumanGame

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SHA256 = "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba"
EVALUATION_ID = "classical_activity_v1"
ARMS = ("baseline", "candidate")


def _board(case):
    if not isinstance(case.get("history_uci"), list):
        raise ValueError("Every starting case requires full standard-start history")
    board = chess.Board()
    for word in case["history_uci"]:
        move = chess.Move.from_uci(word)
        if automatic_outcome(board) is not None or move not in board.legal_moves:
            raise ValueError("Initial history is illegal or continues after an actual ending")
        board.push(move)
    if automatic_outcome(board) is not None:
        raise ValueError("A match starting case must be unfinished")
    if case.get("initial_fen", board.fen(en_passant="fen")) != board.fen(en_passant="fen"):
        raise ValueError("Initial FEN does not match the declared full history")
    return board


def schedule(cases, pairs=8, pair_start=1, seed="20261105"):
    if any(type(value) is not int or value < 1 for value in (pairs, pair_start)):
        raise ValueError("pairs and pair-start must be positive integers")
    if not isinstance(cases, list) or not cases or pair_start + pairs - 1 > len(cases):
        raise ValueError("Requested pair range lies outside the predeclared cases")
    seen, entries = set(), []
    for case in cases:
        if (not isinstance(case, dict) or type(case.get("id")) is not str or not case["id"]
                or case["id"] in seen or case.get("stratum") not in ("style", "target")
                or "seed" in case and (type(case["seed"]) is not str or not case["seed"])):
            raise ValueError("Cases require unique IDs, style/target stratum and a valid optional seed")
        seen.add(case["id"])
        _board(case)
    for pair in range(pair_start, pair_start + pairs):
        case = cases[pair - 1]
        pair_seed = case.get("seed") or hashlib.sha256(json.dumps([str(seed), case["id"]]).encode()).hexdigest()[:32]
        colors = ("white", "black") if pair % 2 else ("black", "white")
        for offset, color in enumerate(colors):
            entries.append({"game": 2 * (pair - 1) + offset + 1, "pair": pair,
                "case": copy.deepcopy(case), "case_id": case["id"], "stratum": case["stratum"],
                "pair_seed": pair_seed, "candidate_color": color,
                "baseline_color": "black" if color == "white" else "white"})
    return entries


def end_state(board, played_plies, cap):
    outcome = automatic_outcome(board)
    if outcome is not None:
        return {"result": outcome.result(), "termination": outcome.termination.name.lower(),
                "completed": True, "legal_result": True, "capped": False}
    if played_plies >= cap:
        return {"result": "*", "termination": "ply_cap", "completed": False, "legal_result": False, "capped": True}
    return None


def _memory(path, binary_sha):
    blob = path.read_bytes()
    if not 0 < len(blob) <= memory.MAX_BYTES:
        raise ValueError("Memory exceeds runtime loader size limit")
    value = memory._validate(json.loads(blob))
    if value.get("status") != "approved" or value.get("acceptance", {}).get("passed") is not True:
        raise ValueError("Both match memories must be approved and accepted")
    if value["engine_identity"] != {"binary_sha256": binary_sha, "evaluation_id": EVALUATION_ID}:
        raise ValueError("Memory binary/evaluator identity does not match the declared engine")
    return value, blob


def _native(binary, raw_path):
    raw = _RawLog(raw_path)
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        engine = chess.engine.SimpleEngine.popen(_logging_protocol(raw), [str(binary), "uci"], timeout=10, **flags)
        return engine, raw
    except BaseException:
        raw.close()
        raise


def _pid(engine):
    try:
        return engine.transport.get_pid()
    except (AttributeError, TypeError):
        return None


def _shutdown(engine, raw, info, *, native):
    try:
        engine.quit()
    except Exception:
        # The host may already have sent quit, or a watchdog closed transport.
        pass
    try:
        engine.close()
    except Exception as error:
        info["cleanup_error"] = type(error).__name__
    future = getattr(engine, "returncode", None)
    try:
        code = future.result(timeout=3) if future is not None else None
        info.update(process_exit_code=code, returncode_observed=future is not None,
                    exit_validation="native" if native else "injected_test_peer")
    except Exception as error:
        info.update(process_exit_code=None, returncode_observed=False, cleanup_error=type(error).__name__)
    finally:
        if raw is not None:
            raw.close()
        info["closed"] = True
    if future is not None or native:
        return (info.get("returncode_observed") is True and type(info.get("process_exit_code")) is int
                and info["process_exit_code"] == 0 and info.get("cleanup_error") is None)
    return info.get("cleanup_error") is None


def _actionable(plan):
    if plan.get("eligible") is not True:
        return []
    rows = [(str(row.get("source", plan.get("candidate_source", ""))), row.get("uci"), row.get("goal"))
            for row in plan.get("knowledge_candidates", [])]
    if plan.get("candidate_uci"):
        rows.append((str(plan.get("candidate_source", "opening")), plan["candidate_uci"], None))
    return list(dict.fromkeys(rows))


def _plans(board, decisions, entry, config):
    values = {}
    for arm in ARMS:
        with memory.using_memory(config[arm + "_memory"]):
            plan = boss_play.plan(board.copy(stack=True), config["movetime_ms"], entry["pair_seed"],
                                  forced=False, decisions=copy.deepcopy(decisions), engine_identity=config["engine_identity"])
            values[arm] = {"eligible": plan.get("eligible"), "mode": plan.get("mode"),
                           "actionable": _actionable(plan), "memory_id": memory.memory_identity()["memory_id"]}
    return {"arms": values, "actionable_delta": values["baseline"]["actionable"] != values["candidate"]["actionable"],
            "basis": "same pre-move board and actor decisions; compare source/uci/goal, not memory ID"}


def match_pgn(board, record, config):
    game = chess.pgn.Game.from_board(board)
    game.headers.update(Event="Regicide gambit memory A/B", Site="local isolated match",
        Round=str(record["game"]), Pair=str(record["pair"]), Result=record["result"],
        White="Regicide Gambit " + ("candidate" if record["candidate_color"] == "white" else "baseline"),
        Black="Regicide Gambit " + ("candidate" if record["candidate_color"] == "black" else "baseline"),
        CaseId=record["case_id"], Stratum=record["stratum"], PairSeed=record["pair_seed"],
        BaselineMemory=config["memory_ids"]["baseline"], CandidateMemory=config["memory_ids"]["candidate"],
        BaselineMoveTimeMS=str(config["movetime_ms"]), OptionalBudgetCapMS=str(config["movetime_ms"]),
        InitialFixturePlies=str(len(record["initial_history_uci"])), NewlyPlayedPlies=str(record["played_plies"]),
        DrawPolicy=DRAW_POLICY, Termination="normal" if record["legal_result"] else "unterminated",
        TerminationReason=record["termination"], MatchPurpose="Memory-only conditional development screen; no Elo or site grade")
    return str(game) + "\n\n"


def verify_pgn(text, board, record):
    game = chess.pgn.read_game(io.StringIO(text))
    if game is None or game.errors or list(game.mainline_moves()) != board.move_stack:
        raise ValueError("Saved PGN does not preserve every legal prefix and played move")
    replay = game.end().board()
    if not same_board(replay, board) or game.headers["Result"] != record["result"] or game.headers.get("DrawPolicy") != DRAW_POLICY:
        raise ValueError("PGN history, final FEN, result or draw policy differs")
    if record["completed"]:
        outcome = automatic_outcome(replay)
        if outcome is None or outcome.result() != record["result"] or outcome.termination.name.lower() != record["termination"]:
            raise ValueError("PGN result is not justified by the actual final position")
    return {"passed": True, "total_plies": len(replay.move_stack), "result": game.headers["Result"],
            "final_fen": replay.fen(en_passant="fen")}


def play_one(config, entry, output, factories):
    board = _board(entry["case"])
    initial = list(board.move_stack)
    record = {**copy.deepcopy(entry), "started_at": utc(), "initial_fen": board.fen(en_passant="fen"),
        "initial_history_uci": [move.uci() for move in initial], "uci_moves": [move.uci() for move in initial],
        "played_plies": 0, "total_plies": len(initial), "moves": [], "hosts": {}, "error": None,
        "result": "*", "termination": "not_started", "completed": False, "legal_result": False, "capped": False,
        "record_file": f"game-{entry['game']:03d}.json", "pgn_file": f"game-{entry['game']:03d}.pgn",
        "draw_policy": DRAW_POLICY}
    hosts, owned = {}, []
    started = time.monotonic()
    def fail(message):
        record.update(error=(record["error"] + "; " if record["error"] else "") + message,
                      result="*", termination="engine_error", completed=False, legal_result=False)
    try:
        for arm in ARMS:
            details = {"engines": [], "save_directory": f"host/game-{entry['game']:03d}/{arm}"}
            record["hosts"][arm] = details
            def create(path, arm=arm, details=details):
                native = factories[arm] is None
                raw_name = f"native-game-{entry['game']:03d}-{arm}-{len(details['engines']) + 1:03d}.jsonl"
                engine, raw = _native(path, output / raw_name) if native else (factories[arm](path), None)
                info = {"pid": _pid(engine), "raw_uci_file": raw_name if native else None, "native": native}
                wrapped = RecordedEngine(engine, info)
                owned.append((engine, raw, info, native))
                details["engines"].append(info)
                if not {"Hash", "UsePieceActivity"}.issubset(wrapped.options):
                    raise ValueError("Both engines must expose Hash and UsePieceActivity")
                settings = {"Hash": 16, **({"Threads": 1} if "Threads" in wrapped.options else {})}
                wrapped.configure(settings)
                return wrapped
            with memory.using_memory(config[arm + "_memory"]):
                host = HumanGame(Path(config["binary"]), output / details["save_directory"], engine_factory=create,
                    hint_engine_factory=lambda _: DisplayOnlyPeer(), evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
                    neural_model=None, opening_variety_enabled=True, dynamic_play_enabled=True)
                hosts[arm] = host
                host.new_game("black" if entry[arm + "_color"] == "white" else "white",
                              config["movetime_ms"], host.revision, boss_mode=True, time_control=None)
                host.board = board.copy(stack=True)
                host._opening_seed = entry["pair_seed"]
                host.revision += 1
                host._sync_evaluation()
                host._save()
                if host._forced_plies or host._clock.enabled or not host.boss_mode:
                    raise ValueError("Initial fixture disabled the ordinary gambit path")
                details.update(game_id=host.game_id, memory_id=memory.memory_identity()["memory_id"],
                               evaluation=host.classical_evaluation_mode)
        with (output / f"game-{entry['game']:03d}.moves.jsonl").open("x", encoding="utf-8") as trace:
            while True:
                if not all(same_board(host.board, board) for host in hosts.values()):
                    raise ValueError("Hosts diverged from independent full-history board")
                ending = end_state(board, record["played_plies"], config["max_plies"])
                if ending:
                    record.update(ending)
                    break
                if time.monotonic() - started >= config["game_wall_seconds"]:
                    record.update(result="*", termination="wall_cap", capped=True)
                    break
                arm = "candidate" if board.turn == (entry["candidate_color"] == "white") else "baseline"
                other = "baseline" if arm == "candidate" else "candidate"
                actor, receiver, before = hosts[arm], hosts[other], board.copy(stack=True)
                decisions = list(actor._boss_decisions.values())
                plan_comparison = _plans(before, decisions, entry, config)
                with memory.using_memory(config[arm + "_memory"]):
                    if memory.memory_identity()["memory_id"] != config["memory_ids"][arm]:
                        raise ValueError("Selected memory changed during the match")
                    budget = actor.state()["engine"]
                    if budget["movetime_ms"] != config["movetime_ms"] or not 0 <= budget["extra_budget_ms"] <= config["movetime_ms"]:
                        raise ValueError("Production search allowance exceeds declared memory A/B limits")
                    operation = gambit_operations.context(before, decisions)
                    proposals = memory.proposals(before, entry["pair_seed"], engine_identity=config["engine_identity"], context=operation)
                    began = time.monotonic()
                    state = actor.engine_move(actor.revision)
                    elapsed = (time.monotonic() - began) * 1000
                    if state.get("error"):
                        raise RuntimeError(state["error"])
                    if len(actor.board.move_stack) != len(board.move_stack) + 1:
                        raise ValueError("Host did not append exactly one native-selected move")
                    move = actor.board.peek()
                    if move not in before.legal_moves:
                        raise ValueError("Host returned an illegal move")
                    expected = before.copy(stack=True)
                    expected.push(move)
                    if not same_board(actor.board, expected):
                        raise ValueError("Actor differs from independent replay")
                    decision = copy.deepcopy(actor._boss_decisions.get(len(expected.move_stack)))
                    metrics = move_metrics(before, move, decision=decision, operation=operation, proposals=proposals)
                relay_started = time.monotonic()
                with memory.using_memory(config[other + "_memory"]):
                    receiver.human_move(move.uci(), receiver.revision)
                if not same_board(receiver.board, expected):
                    raise ValueError("Receiver diverged after receiving the selected move")
                board = expected
                turn = {"ply": len(board.move_stack), "played_ply": record["played_plies"] + 1,
                    "actor": arm, "actor_color": "white" if before.turn else "black", "uci": move.uci(), "san": before.san(move),
                    "history_uci": [word.uci() for word in before.move_stack], "before_fen": before.fen(en_passant="fen"),
                    "after_fen": board.fen(en_passant="fen"), "phase_before": budget["boss_phase"],
                    "baseline_budget_ms": budget["movetime_ms"], "advertised_extra_budget_ms": budget["extra_budget_ms"],
                    "elapsed_ms": round(elapsed, 3), "relay_elapsed_ms": round((time.monotonic() - relay_started) * 1000, 3),
                    "memory_proposals_before": copy.deepcopy(proposals), "plan_comparison": plan_comparison,
                    "boss_decision": decision, "metrics": metrics}
                record["moves"].append(turn)
                record["uci_moves"].append(move.uci())
                record.update(played_plies=turn["played_ply"], total_plies=turn["ply"])
                trace.write(json.dumps(turn, ensure_ascii=False, allow_nan=False) + "\n")
                trace.flush()
    except BaseException as error:
        fail(f"{type(error).__name__}: {error}")
        if isinstance(error, KeyboardInterrupt):
            record["termination"] = "interrupted"
    finally:
        for arm, host in hosts.items():
            with memory.using_memory(config[arm + "_memory"]):
                try:
                    record["hosts"][arm].update(final_fen=host.board.fen(en_passant="fen"),
                        uci_moves=[move.uci() for move in host.board.move_stack], boss_play=host._boss_metadata(),
                        scoped_memory=memory.memory_identity(), forced_plies=sorted(host._forced_plies), clock_enabled=host._clock.enabled)
                    host.close()
                except Exception as error:
                    fail(f"{arm} host cleanup: {error}")
        for engine, raw, info, native in owned:
            if not _shutdown(engine, raw, info, native=native):
                fail("Native process exit was not observed as zero")
        record["final_fen"] = board.fen(en_passant="fen")
        try:
            pgn = match_pgn(board, record, config)
            record["pgn_verification"] = verify_pgn(pgn, board, record)
        except Exception as error:
            fail("PGN export/replay: " + str(error))
            fallback = chess.pgn.Game.from_board(board)
            fallback.headers.update(Result="*", DrawPolicy=DRAW_POLICY)
            pgn = str(fallback) + "\n\n"
        record.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - started, 3))
        (output / record["pgn_file"]).write_text(pgn, encoding="utf-8")
        write_json(output / record["record_file"], record)
    return record, pgn


def _summary(games):
    return counts([{**row, "boss_color": row["candidate_color"]} for row in games], legal_only=True)


def run_match(config, baseline_factory=None, candidate_factory=None):
    config = {"pairs": 8, "pair_start": 1, "movetime_ms": 500, "max_plies": 240, "seed": "20261105",
              "workers": 1, "game_wall_seconds": 300, **config}
    if config["workers"] != 1 or type(config["workers"]) is not int:
        raise ValueError("This reproducible runner is serial; workers must be 1")
    if type(config["movetime_ms"]) is not int or config["movetime_ms"] != 500:
        raise ValueError("Memory-only A/B uses exactly 500ms baseline and at most 500ms production extra")
    if type(config["max_plies"]) is not int or config["max_plies"] < 1:
        raise ValueError("max-plies is a positive newly played ply cap")
    if type(config["game_wall_seconds"]) not in (int, float) or not math.isfinite(config["game_wall_seconds"]) or config["game_wall_seconds"] <= 0:
        raise ValueError("game-wall-seconds must be finite and positive")
    for key in ("binary", "baseline_memory", "candidate_memory", "cases"):
        config[key] = str(Path(config[key]).resolve(strict=True))
    output = Path(config["output_dir"]).resolve()
    if output.exists() or output.is_relative_to(ROOT.parent / "sources") or output.is_relative_to(ROOT / "data"):
        raise ValueError("Use a new isolated output outside data/ and synced sources")
    binary_sha = sha(config["binary"])
    if (baseline_factory is None or candidate_factory is None) and binary_sha != RUNTIME_SHA256:
        raise ValueError("Native match requires the fixed 5011 runtime binary")
    values, blobs = {}, {}
    for arm in ARMS:
        values[arm], blobs[arm] = _memory(Path(config[arm + "_memory"]), binary_sha)
    config.update(binary_sha256=binary_sha, engine_identity={"binary_sha256": binary_sha, "evaluation_id": EVALUATION_ID},
                  memory_ids={arm: values[arm]["memory_id"] for arm in ARMS}, output_dir=str(output))
    cases_blob = Path(config["cases"]).read_bytes()
    cases = json.loads(cases_blob)
    entries = schedule(cases, config["pairs"], config["pair_start"], config["seed"])
    source_paths = tuple(dict.fromkeys([*SOURCES, "scripts/neural_search_data/engine.py"]))
    sources = {name: sha(ROOT / name) for name in source_paths}
    inputs = {config["binary"]: binary_sha, config["cases"]: hashlib.sha256(cases_blob).hexdigest(),
              **{config[arm + "_memory"]: hashlib.sha256(blobs[arm]).hexdigest() for arm in ARMS}}
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs").mkdir()
    for arm in ARMS:
        (output / "inputs" / (arm + "-operations_memory.json")).write_bytes(blobs[arm])
    (output / "inputs/cases.json").write_bytes(cases_blob)
    with zipfile.ZipFile(output / "host-sources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sources:
            if name in {"scripts/gambit_memory_match.py", "scripts/gambit_memory_match_metrics.py", "scripts/gambit_practice_memory.py",
                        "scripts/play_human.py", "scripts/boss_play.py", "scripts/gambit_initiative.py", "scripts/gambit_operations.py",
                        "scripts/gambit_tactics.py", "scripts/engine_pacing.py", "scripts/game_outcome.py", "scripts/boss_mode_match.py",
                        "scripts/boss_strength_match.py", "scripts/gambit_knowledge.py", "scripts/gambit_repertoire_play.py"}:
                archive.write(ROOT / name, name)
        archive.write(ROOT / "scripts/neural_search_data/engine.py", "scripts/neural_search_data/engine.py")
    report = {"schema": "regicide.gambit-memory-match.v1", "started_at": utc(), "status": "running", "config": config,
        "source": source_version(), "source_sha256": sources, "inputs_sha256": inputs,
        "python": platform.python_version(), "python_chess": chess.__version__, "platform": platform.platform(),
        "schedule": entries, "games": [], "errors": [], "draw_policy": DRAW_POLICY,
        "scope": "Memory-only paired development games; target repetitions are conditional exposure checks, not eight independent openings or Elo evidence",
        "timing": "Both production boss hosts, clocks disabled, 500ms baseline + at most 500ms optional; equal allowances, not equal actual CPU time",
        "initial_prefixes_excluded_from_observed_metrics": True, "native_only_move_selection": True}
    write_json(output / "match.json", report)
    pgns = []
    for entry in entries:
        report["active_game"] = entry["game"]
        write_json(output / "match.json", report)
        record, pgn = play_one(config, entry, output, {"baseline": baseline_factory, "candidate": candidate_factory})
        report["games"].append({key: value for key, value in record.items() if key not in ("moves", "hosts", "uci_moves", "case")})
        pgns.append(pgn)
        (output / "match.pgn").write_text("".join(pgns), encoding="utf-8")
        report.update(summary=_summary(report["games"]), strata={stratum: _summary([row for row in report["games"] if row["stratum"] == stratum])
                                                              for stratum in ("style", "target")})
        if record["error"]:
            report["errors"].append(record["error"])
        write_json(output / "match.json", report)
        print(json.dumps({"game": entry["game"], "stratum": entry["stratum"], "played_plies": record["played_plies"],
                          "result": record["result"], "termination": record["termination"], "error": record["error"]}), flush=True)
        if record["error"]:
            break
    report.update(finished_at=utc(), active_game=None,
        sources_unchanged=all(sha(ROOT / path) == digest for path, digest in sources.items()),
        inputs_unchanged=all(sha(path) == digest for path, digest in inputs.items()))
    report["status"] = ("completed" if len(report["games"]) == len(entries) and not report["errors"]
                        and report["sources_unchanged"] and report["inputs_unchanged"] else "failed")
    write_json(output / "match.json", report)
    return report


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--baseline-memory", type=Path, required=True)
    parser.add_argument("--candidate-memory", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--movetime-ms", type=int, default=500)
    parser.add_argument("--max-plies", type=int, default=240)
    parser.add_argument("--pairs", type=int, default=8)
    parser.add_argument("--pair-start", type=int, default=1)
    parser.add_argument("--seed", default="20261105")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--game-wall-seconds", type=float, default=300)
    return {key: str(value) if isinstance(value, Path) else value for key, value in vars(parser.parse_args(argv)).items()}


def main(argv=None):
    return 0 if run_match(arguments(argv))["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
