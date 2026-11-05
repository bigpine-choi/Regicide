"""Paired production-Host sparring segment diagnostic; run each arm in a fresh Python process.

Only HumanGame's presentation wait and display-only analyses are suppressed.
Native search, clocks, recovery and candidate/referee selection stay production.
Full-strength independent judging runs OFFLINE, after all playing processes close.
Caps and errors remain unfinished. These short segments do not establish Elo.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "regicide.sparring-close-paired.v1"
RATINGS = (1320, 1700, 2100, 2500)
OPENINGS = ({"id": "start", "moves": []},
            {"id": "open-game", "moves": ["e2e4", "e7e5", "g1f3", "b8c6"]})


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def frozen_sources(root):
    paths = list((root / "scripts").glob("*.py"))
    paths += [path for path in (root / "data/openings").glob("*") if path.is_file()]
    return {path.relative_to(root).as_posix(): sha(path) for path in sorted(paths)}


def source_identity(root):
    def git(*arguments):
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                                text=True, timeout=15, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    return {"root": str(root), "commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain"))}


def schedule(ratings=RATINGS, seed="sparring-close-2026-11-03", colors=("white", "black"), openings=OPENINGS[:1]):
    if (not ratings or len(set(ratings)) != len(ratings) or any(type(x) is not int or x not in RATINGS for x in ratings)
            or not colors or len(set(colors)) != len(colors) or any(x not in ("white", "black") for x in colors)):
        raise ValueError("Use unique supported opponent ratings and colors")
    result = []
    for opening_index, opening in enumerate(openings):
        board = chess.Board()
        if not isinstance(opening.get("id"), str) or not isinstance(opening.get("moves"), list):
            raise ValueError("Opening requires id and legal UCI moves")
        for uci in opening["moves"]:
            board.push_uci(uci)
        for rating in RATINGS:
            if rating not in ratings:
                continue
            pair = opening_index * len(RATINGS) + RATINGS.index(rating) + 1
            pair_key = hashlib.sha256(json.dumps([str(seed), opening["id"], opening["moves"], rating]).encode()).hexdigest()
            for offset, color in enumerate(("white", "black")):
                if color in colors:
                    result.append({"game": 2 * (pair - 1) + offset + 1, "pair": pair,
                                   "pair_key": pair_key, "sparring_seed": pair_key[:32],
                                   "opponent_elo": rating, "sparring_color": color,
                                   "opening_id": opening["id"], "opening_uci": list(opening["moves"])})
    return result


class QuietHints:
    def __init__(self, *args, **kwargs):
        pass
    def snapshot(self):
        return {"enabled": False, "status": "disabled", "candidates": []}
    def snapshot_with_evaluation(self):
        return self.snapshot(), {}
    def set_enabled(self, enabled):
        return self.snapshot()
    def update_position(self, **kwargs):
        pass
    def close(self):
        pass


class ScreenOnlyReferee:
    """Keep production candidate screening while disabling its display actor."""
    def __init__(self, peer):
        self.peer = peer
    def update_position(self, **kwargs):
        return self.peer.update_position(**{**kwargs, "active": False})
    def snapshot(self):
        return self.peer.snapshot()
    def screen_candidates(self, **kwargs):
        return self.peer.screen_candidates(**kwargs)
    def cancel(self):
        return self.peer.cancel()
    def close(self):
        return self.peer.close()


def experiment_host(base):
    class Host(base):
        def _wait_for_engine_turn(self, pacing, game_id, token, revision, cancel_event):
            # Do not set skip/cancel events: production treats them as referee
            # cancellation. Suppress only the final, artificial cadence wait.
            clock = self._clock.snapshot()
            return {**pacing, "spent_before_wait_ms": clock.get("turn_elapsed_ms", 0), "waited_ms": 0,
                    "completion_reason": "experiment_presentation_wait_omitted"}
    return Host


def load_runtime(root, assisted):
    scripts = (root / "scripts").resolve()
    previous = sys.modules.get("play_human")
    if previous is not None and Path(previous.__file__).resolve().parent != scripts:
        raise RuntimeError("Run a different source-root arm in a fresh Python process")
    sys.path.insert(0, str(scripts))
    host = importlib.import_module("play_human")
    policy = importlib.import_module("sparring_play")
    referee_module = importlib.import_module("sparring_referee") if assisted else None
    return SimpleNamespace(host=host, policy=policy, referee=referee_module)


class RecordedEngine:
    def __init__(self, engine, role, ledger):
        self.engine = engine
        self.record = {"role": role, "id": dict(engine.id), "options": {}, "closed": False}
        try:
            self.record["pid"] = engine.transport.get_pid()
        except AttributeError:
            self.record["pid"] = None
        ledger.append(self.record)
    def __getattr__(self, key):
        return getattr(self.engine, key)
    def configure(self, values):
        self.engine.configure(values)
        self.record["options"].update(values)
    def close(self):
        self.engine.close()
        self.record["closed"] = True


def native_factory(path, role, ledger):
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    command = [str(path), "uci"] if role == "regicide" else [str(path)]
    peer = RecordedEngine(chess.engine.SimpleEngine.popen_uci(command, timeout=3, **flags), role, ledger)
    if role == "regicide":
        peer.configure({key: value for key, value in {"Threads": 1, "Hash": 16}.items() if key in peer.options})
    return peer


def ending(board, cap):
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        return {"result": outcome.result(), "termination": outcome.termination.name.lower(),
                "legal_result": True, "capped": False}
    if len(board.move_stack) >= cap:
        return {"result": "*", "termination": "ply_cap", "legal_result": False, "capped": True}
    return None


def _timed_call(function, timeout, close_owned):
    expired = threading.Event()
    def stop():
        expired.set()
        close_owned()
    guard = threading.Timer(timeout, stop)
    guard.daemon = True
    start = time.monotonic()
    guard.start()
    try:
        try:
            result = function()
        except Exception as error:
            if expired.is_set():
                raise TimeoutError("Declared operation wall limit exceeded") from error
            raise
        return result, round((time.monotonic() - start) * 1000, 3), expired.is_set()
    finally:
        guard.cancel()
        guard.join(.2)


def play_one(config, entry, runtime, output, *, factory=native_factory):
    record = {**entry, "variant": config["variant"], "referee_mode": config["referee"],
              "started_at": utc(), "moves": [], "result": "*", "legal_result": False, "capped": False,
              "termination": "not_started", "errors": [], "processes": [], "initial_fen": chess.STARTING_FEN}
    board, host, opponent, service = chess.Board(), None, None, None
    owned = []
    def start(path, role):
        peer = factory(Path(path), role, record["processes"])
        owned.append(peer)
        return peer
    def close_owned():
        if service is not None:
            service.close()
        for peer in list(owned):
            try:
                peer.close()
            except Exception:
                pass
    try:
        opponent = start(config["stockfish"], "opponent")
        opponent_options = {"UCI_LimitStrength": True, "UCI_Elo": entry["opponent_elo"], "Threads": 1, "Hash": 16}
        for key in opponent_options:
            if key not in opponent.options:
                raise ValueError("Opponent missing required option " + key)
        opponent.configure(opponent_options)
        record["opponent_options"] = opponent_options
        extra = {}
        if config["referee"] == "on":
            if runtime.referee is None:
                raise RuntimeError("Selected source has no referee implementation")
            service = ScreenOnlyReferee(runtime.referee.SparringReferee(
                config["referee_binary"], lambda path: start(path, "candidate_referee")))
            extra = {"referee": service, "referee_info": {"name": "Stockfish", "sha256": sha(config["referee_binary"])}}
        elif "referee" in inspect.signature(runtime.host.HumanGame).parameters:
            extra = {"referee": None}
        Host = experiment_host(runtime.host.HumanGame)
        with patch.object(runtime.host, "MoveHints", QuietHints):
            host = Host(Path(config["binary"]), output / "host" / f"game-{entry['game']:03d}",
                        engine_factory=lambda path: start(path, "regicide"), neural_model=None,
                        opening_variety_enabled=True, dynamic_play_enabled=True, **extra)
        human = "black" if entry["sparring_color"] == "white" else "white"
        host.new_game(human, config["own_ms"], host.revision, sparring_mode=True,
                      time_control=None if config["time_control"] == "none" else config["time_control"])
        host._opening_seed = entry["sparring_seed"]
        host._save()
        state = host.state()
        metadata = state.get("engine", {}).get("sparring_referee", {})
        record["host_configuration"] = {"sparring_mode": state["engine"].get("sparring_mode"),
                                        "referee": metadata, "clock": state.get("clock"),
                                        "policy": runtime.policy.POLICY}
        if config["referee"] == "on" and not metadata.get("configured"):
            raise RuntimeError("Assisted arm did not configure a referee")
        game_token = object()
        with (output / f"game-{entry['game']:03d}.moves.jsonl").open("x", encoding="utf-8") as stream:
            while True:
                finish = ending(board, config["max_plies"])
                if finish:
                    record.update(finish)
                    break
                before = board.copy(stack=True)
                is_own = before.turn != host.human_color
                ply = len(board.move_stack) + 1
                forced = ply <= len(entry["opening_uci"])
                started = time.monotonic()
                late = False
                if forced:
                    move = chess.Move.from_uci(entry["opening_uci"][ply - 1])
                    with host._lock:
                        if move not in host.board.legal_moves:
                            raise ValueError("Illegal fixed opening move")
                        if not host._clock.complete_move("white" if before.turn else "black", ply):
                            raise RuntimeError("Opening clock could not advance")
                        host.board.push(move)
                        host.revision += 1
                        host._sync_evaluation()
                        host._save()
                elif is_own:
                    state, _, late = _timed_call(lambda: host.engine_move(host.revision),
                                                  config["move_timeout"], close_owned)
                    if state.get("error"):
                        raise RuntimeError("Production host error: " + str(state["error"]))
                    if len(host.board.move_stack) != ply:
                        raise RuntimeError("Production host did not append one move")
                    move = host.board.peek()
                else:
                    reply, _, late = _timed_call(lambda: opponent.play(before.copy(stack=True),
                        chess.engine.Limit(time=config["opponent_ms"] / 1000), game=game_token,
                        info=chess.engine.INFO_ALL, ponder=False), config["move_timeout"], close_owned)
                    move = reply.move
                    if move is None or move not in before.legal_moves:
                        raise ValueError("Opponent returned illegal or missing move")
                    host.human_move(move.uci(), host.revision)
                if move not in before.legal_moves:
                    raise ValueError("Illegal played move")
                board.push(move)
                if (host.board.fen(en_passant="fen") != board.fen(en_passant="fen")
                        or host.board.move_stack != board.move_stack):
                    raise RuntimeError("Production host and independent replay diverged")
                row = {"ply": ply, "actor": "regicide" if is_own else "opponent", "uci": move.uci(),
                       "san": before.san(move), "before_fen": before.fen(en_passant="fen"),
                       "after_fen": board.fen(en_passant="fen"), "forced_opening": forced,
                       "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "late": late,
                       "clock_after": host.state().get("clock"),
                       "decision": copy.deepcopy(host._sparring_decisions.get(ply)) if is_own and not forced else None}
                record["moves"].append(row)
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                if late:
                    raise TimeoutError("Move returned after the declared wall limit")
                if ply % 4 == 0:
                    print(json.dumps({"event": "playing", "variant": config["variant"], "game": entry["game"], "ply": ply}), flush=True)
    except Exception as error:
        record["errors"].append({"stage": "playing", "type": type(error).__name__, "message": str(error),
                                 "failed_at_ply": len(board.move_stack) + 1})
        record.update(result="*", legal_result=False, termination="engine_error")
    finally:
        if host is not None:
            try:
                host.close()
            except Exception as error:
                record["errors"].append({"stage": "close", "type": type(error).__name__})
        close_owned()
        for peer in owned:
            try:
                peer.record["exit_code"] = peer.returncode.result(timeout=2)
                peer.record["exited"] = True
            except AttributeError:
                pass
            except Exception:
                if hasattr(peer, "record"):
                    peer.record["exited"] = False
                    record["errors"].append({"stage": "cleanup", "type": "OwnedProcessNotExited"})
        record.update(finished_at=utc(), plies=len(board.move_stack), final_fen=board.fen(en_passant="fen"),
                      uci_moves=[move.uci() for move in board.move_stack])
        pgn = chess.pgn.Game.from_board(board)
        pgn.headers.update(Event="Paired sparring segment diagnostic", Result=record["result"],
                           VariantLabel=config["variant"], RefereeMode=config["referee"],
                           SparringColor=entry["sparring_color"], OpponentUCIElo=str(entry["opponent_elo"]),
                           SparringSeed=entry["sparring_seed"], CapPolicy="unfinished", Termination=record["termination"])
        (output / f"game-{entry['game']:03d}.pgn").write_text(str(pgn) + "\n", encoding="utf-8")
        write_json(output / f"game-{entry['game']:03d}.json", record)
    return record


def score_packet(board, info):
    if (not isinstance(info, dict) or type(info.get("depth")) is not int or info["depth"] < 1
            or info.get("lowerbound") or info.get("upperbound") or info.get("multipv", 1) != 1):
        return None
    score, pv = info.get("score"), info.get("pv")
    if (not isinstance(score, chess.engine.PovScore) or score.turn != board.turn
            or not isinstance(pv, (tuple, list)) or not 0 < len(pv) <= 256):
        return None
    replay = board.copy(stack=True)
    for move in pv:
        if not isinstance(move, chess.Move) or move not in replay.legal_moves:
            return None
        replay.push(move)
    cp, mate = score.white().score(), score.white().mate()
    if cp is not None and (type(cp) is not int or replay.is_checkmate()):
        return None
    return {"status": "exact", "cp_white": cp, "mate_white": mate, "depth": info["depth"],
            "nodes": info.get("nodes"), "pv_uci": [move.uci() for move in pv], "bound": "exact"}


def judge_one(engine, board, config):
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        return {"status": "terminal_rules", "result": outcome.result(), "termination": outcome.termination.name.lower(),
                "cp_white": None, "mate_white": None, "depth": None}
    limit = chess.engine.Limit(nodes=config["judge_nodes"]) if config.get("judge_nodes") else chess.engine.Limit(depth=config["judge_depth"])
    def search():
        packets = []
        if "Clear Hash" in engine.options:
            engine.configure({"Clear Hash": None})
        with engine.analysis(board.copy(stack=True), limit, game=object(), info=chess.engine.INFO_ALL) as analysis:
            for info in analysis:
                packet = score_packet(board, info)
                if packet is not None:
                    packets.append(packet)
                    packets = packets[-8:]
            returned = analysis.wait().move
        valid = [packet for packet in packets if packet["pv_uci"][0] == returned.uci()] if returned else []
        if not valid:
            raise RuntimeError("No exact completed judge packet for returned root")
        packet = max(valid, key=lambda row: row["depth"])
        if not config.get("judge_nodes") and packet["depth"] < config["judge_depth"]:
            raise RuntimeError("Independent judge did not complete the fixed depth")
        return packet
    packet, elapsed, expired = _timed_call(search, config["judge_timeout"], engine.close)
    if expired:
        raise TimeoutError("Independent judge exceeded its wall limit")
    return {**packet, "elapsed_ms": elapsed}


def judge_offline(record, config, *, factory=native_factory):
    record["judgements"] = []
    ledger, owned, engine = [], [], None
    board = chess.Board()
    for ply in range(record["plies"] + 1):
        try:
            if engine is None:
                engine = factory(Path(config["judge_binary"]), "independent_judge", ledger)
                owned.append(engine)
                engine.configure({"Threads": 1, "Hash": 32, "Skill Level": 20, "UCI_LimitStrength": False})
            packet = judge_one(engine, board, config)
        except Exception as error:
            packet = {"status": "error", "cp_white": None, "mate_white": None,
                      "type": type(error).__name__, "message": str(error)}
            record["errors"].append({"stage": "offline_judge", "ply": ply, "type": type(error).__name__})
            if engine is not None:
                engine.close()
                engine = None
        row = {"ply": ply, "fen": board.fen(en_passant="fen"), **packet}
        record["judgements"].append(row)
        if ply < record["plies"]:
            board.push_uci(record["uci_moves"][ply])
    if engine is not None:
        engine.close()
    for peer in owned:
        try:
            peer.record["exit_code"] = peer.returncode.result(timeout=2)
            peer.record["exited"] = True
        except AttributeError:
            pass
        except Exception:
            if hasattr(peer, "record"):
                peer.record["exited"] = False
            record["errors"].append({"stage": "judge_cleanup", "type": "OwnedProcessNotExited"})
    record["judge_processes"] = ledger
    return record


def close_metrics(record, window=30):
    values = {row["ply"]: row for row in record.get("judgements", [])}
    after = [values[ply] for ply in range(1, window + 1) if ply in values]
    finite = [row for row in after if row.get("status") == "exact" and type(row.get("cp_white")) is int]
    within100 = sum(abs(row["cp_white"]) <= 100 for row in finite)
    within200 = sum(abs(row["cp_white"]) <= 200 for row in finite)
    sign = 1 if record["sparring_color"] == "white" else -1
    jump_rows = []
    first_exit = None
    for ply in range(1, min(record["plies"], window) + 1):
        row, previous = values.get(ply, {}), values.get(ply - 1, {})
        cp, old = row.get("cp_white"), previous.get("cp_white")
        if row.get("status") == "exact" and (row.get("mate_white") is not None or type(cp) is int and abs(cp) > 100):
            first_exit = first_exit or ply
        if row.get("status") == previous.get("status") == "exact" and type(cp) is type(old) is int:
            change = max(abs(cp) - 100, 0) - max(abs(old) - 100, 0)
            jump_rows.append({"ply": ply, "actor": record["moves"][ply - 1]["actor"],
                              "imbalance_excess_increase_cp": change, "imbalance_drop": change >= 200,
                              "raw_cp_loss_own": (old - cp) * sign,
                              "scope": "sequential fixed-judge score change; not equal-root move regret"})
    return {"window_plies": window, "observed_positions": len(after), "exact_finite": len(finite),
            "mate_positions": sum(row.get("mate_white") is not None for row in after),
            "rule_terminal_positions": sum(row.get("status") == "terminal_rules" for row in after),
            "unknown_observed": sum(row.get("status") not in ("exact", "terminal_rules") for row in after),
            "missing_or_not_reached": window - len(after), "within_100": within100, "within_200": within200,
            "density_100_of_finite": within100 / len(finite) if finite else None,
            "density_200_of_finite": within200 / len(finite) if finite else None,
            "conservative_100_of_window": within100 / window, "conservative_200_of_window": within200 / window,
            "reached_window": record["plies"] >= window,
            "strict_all_window_within_100": len(finite) == window and within100 == window,
            "first_exit_100_ply": first_exit,
            "bad_overshoot_below_minus200": sum(row["cp_white"] * sign < -200 for row in finite),
            "winning_overshoot_above200": sum(row["cp_white"] * sign > 200 for row in finite),
            "imbalance_drop_count": sum(row["imbalance_drop"] for row in jump_rows), "changes": jump_rows,
            "game_errors": len(record.get("errors", [])), "termination": record["termination"],
            "censored": bool(record.get("capped") or record.get("errors")),
            "early_terminal_before_window": bool(record.get("legal_result") and record["plies"] < window)}


def run(config, *, runtime=None, factory=native_factory):
    config = copy.deepcopy(config)
    root, output = Path(config["source_root"]).resolve(), Path(config["output"]).resolve()
    if config.get("stage") == "judge":
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads((output / f"game-{entry['game']:03d}.json").read_text(encoding="utf-8"))
                   for entry in manifest["games"]]
        return finish_offline(manifest, records, output, factory=factory)
    if config["max_plies"] < config["close_plies"]:
        raise ValueError("max-plies must cover the full planned close window")
    output.mkdir(parents=True, exist_ok=False)
    sources = frozen_sources(root)
    binaries = {name: sha(config[name]) for name in ("binary", "stockfish", "judge_binary", "referee_binary")}
    entries = schedule(config["ratings"], config["seed"], config["colors"], config["openings"])
    if config.get("game_ids"):
        entries = [row for row in entries if row["game"] in config["game_ids"]]
    if not entries:
        raise ValueError("No games selected")
    manifest = {"pipeline": PIPELINE, "started_at": utc(), "config": config, "schedule": entries,
                "source": source_identity(root), "source_sha256_before": sources,
                "driver_sha256_before": sha(Path(__file__)), "binary_sha256_before": binaries,
                "environment": {"python": platform.python_version(), "python_chess": chess.__version__,
                                "platform": platform.platform(), "logical_cpus": os.cpu_count()},
                "protocol": {"pairing": "same opening, seed, color and opponent settings per arm",
                             "opponent_randomness": "Stockfish limited-strength RNG has no exposed seed; exact reproducibility not claimed",
                             "wait": "only _wait_for_engine_turn overridden to receipt-only; no skip/cancel event set",
                             "display": "QuietHints and ScreenOnlyReferee in every arm; candidate screening retained when on",
                             "judge": "offline after every playing game/process ended; fixed depth/nodes; fresh game and Clear Hash each position",
                             "denominator": f"actual post-move positions 1..{config['close_plies']}; initial position excluded; unknown/missing retained",
                             "clock": "production time control and search allocation; no artificial post-search waiting",
                             "cap": "unfinished *; never an artificial draw", "claims": "small segment diagnostic, not Elo or general strength"},
                "games": [], "errors": [], "status": "playing"}
    write_json(output / "manifest.json", manifest)
    runtime = runtime or load_runtime(root, config["referee"] == "on")
    records = []
    for entry in entries:
        if frozen_sources(root) != sources:
            manifest["errors"].append("source_changed_before_game")
            break
        print(json.dumps({"event": "game_start", "variant": config["variant"], **entry}), flush=True)
        record = play_one(config, entry, runtime, output, factory=factory)
        records.append(record)
        manifest["games"].append({key: record[key] for key in ("game", "plies", "result", "termination", "errors")})
        write_json(output / "manifest.json", manifest)
    if config.get("stage") == "play":
        manifest.update(status="awaiting_offline_judging", play_finished_at=utc(),
                        source_sha256_after_play=frozen_sources(root),
                        driver_sha256_after_play=sha(Path(__file__)))
        write_json(output / "manifest.json", manifest)
        return manifest
    return finish_offline(manifest, records, output, factory=factory)


def finish_offline(manifest, records, output, *, factory=native_factory):
    config = manifest["config"]
    root, entries = Path(config["source_root"]), manifest["schedule"]
    sources, binaries = manifest["source_sha256_before"], manifest["binary_sha256_before"]
    manifest["status"] = "offline_judging"
    write_json(output / "manifest.json", manifest)
    for record in records:
        judge_offline(record, config, factory=factory)
        record["close_metrics"] = close_metrics(record, config["close_plies"])
        write_json(output / f"game-{record['game']:03d}.json", record)
    observed = len(records)
    summary = {"scheduled_games": len(entries), "recorded_games": observed, "not_run_games": len(entries) - observed,
               "planned_close_positions": len(entries) * config["close_plies"],
               "within_100": sum(row["close_metrics"]["within_100"] for row in records),
               "within_200": sum(row["close_metrics"]["within_200"] for row in records),
               "exact_finite": sum(row["close_metrics"]["exact_finite"] for row in records),
               "game_errors": sum(len(row["errors"]) for row in records),
               "reached_window": sum(row["close_metrics"]["reached_window"] for row in records)}
    summary["conservative_100_of_planned"] = summary["within_100"] / summary["planned_close_positions"]
    summary["conservative_200_of_planned"] = summary["within_200"] / summary["planned_close_positions"]
    manifest.update(finished_at=utc(), summary=summary, source_sha256_after=frozen_sources(root),
                    binary_sha256_after={name: sha(config[name]) for name in binaries}, driver_sha256_after=sha(Path(__file__)))
    manifest["sources_unchanged"] = manifest["source_sha256_after"] == sources
    manifest["binaries_unchanged"] = manifest["binary_sha256_after"] == binaries
    manifest["driver_unchanged"] = manifest["driver_sha256_after"] == manifest["driver_sha256_before"]
    manifest["status"] = ("completed" if not manifest["errors"] and not summary["game_errors"] and observed == len(entries)
                           and all(manifest[key] for key in ("sources_unchanged", "binaries_unchanged", "driver_unchanged"))
                           else "completed_with_errors")
    write_json(output / "manifest.json", manifest)
    return manifest


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--referee", required=True, choices=("off", "on"))
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--judge-binary", type=Path)
    parser.add_argument("--referee-binary", type=Path)
    parser.add_argument("--ratings", type=int, nargs="+", choices=RATINGS, default=list(RATINGS))
    parser.add_argument("--colors", nargs="+", choices=("white", "black"), default=["white", "black"])
    parser.add_argument("--game-ids", "--case-ids", dest="game_ids", type=int, nargs="+")
    parser.add_argument("--stage", choices=("all", "play", "judge"), default="all")
    parser.add_argument("--seed", default="sparring-close-2026-11-03")
    parser.add_argument("--opening-file", type=Path)
    parser.add_argument("--own-ms", type=int, default=1000)
    parser.add_argument("--opponent-ms", type=int, default=1000)
    parser.add_argument("--time-control", choices=("10+0", "10+5", "15+10", "none"), default="10+5")
    parser.add_argument("--max-plies", type=int, default=44)
    parser.add_argument("--close-plies", type=int, default=30)
    limits = parser.add_mutually_exclusive_group()
    limits.add_argument("--judge-depth", type=int, default=14)
    limits.add_argument("--judge-nodes", type=int)
    parser.add_argument("--move-timeout", type=float, default=40)
    parser.add_argument("--judge-timeout", type=float, default=15)
    value = vars(parser.parse_args(argv))
    for name in ("source_root", "binary", "stockfish"):
        value[name] = str(value[name].resolve(strict=True))
    value["judge_binary"] = str((value["judge_binary"] or Path(value["stockfish"])).resolve(strict=True))
    value["referee_binary"] = str((value["referee_binary"] or Path(value["stockfish"])).resolve(strict=True))
    value["output"] = str(value["output"].resolve())
    value["openings"] = json.loads(value.pop("opening_file").read_text(encoding="utf-8")) if value["opening_file"] else list(OPENINGS[:1])
    if "opening_file" in value:
        del value["opening_file"]
    if (value["max_plies"] < value["close_plies"] or value["close_plies"] < 1 or
            any(value[key] < 1 for key in ("own_ms", "opponent_ms", "judge_depth"))):
        parser.error("positive budgets and max-plies >= close-plies are required")
    return value


if __name__ == "__main__":
    result = run(arguments())
    print(json.dumps({"status": result["status"], "summary": result.get("summary")}), flush=True)
    raise SystemExit(0 if result["status"] in ("completed", "awaiting_offline_judging") else 1)
