"""One production, clocked sparring game for a color-paired diagnostic shard.

Real native searches and the production full-strength referee choose moves.
Only display helpers are disabled. Cosmetic wait advances an injected clock,
so it costs the same game time without sleeping; actual search time is real.
No result is adjudicated from an evaluation. Ply caps remain unfinished.
"""
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import play_human
import sparring_play
import sparring_safety
from boss_strength_match import DisplayOnlyPeer, native_engine, sha, stockfish_options, utc, write_json
from sparring_strength_match import QuietHints
from play_game import source_version
from runtime_engine import resolve_runtime_engine


def safe(value):
    if isinstance(value, chess.Move):
        return value.uci()
    if isinstance(value, chess.engine.PovScore):
        return {"score_turn": "white" if value.turn else "black", "cp_white": value.white().score(),
                "mate_white": value.white().mate(), "cp_stm": value.relative.score(),
                "mate_stm": value.relative.mate()}
    if isinstance(value, chess.engine.PovWdl):
        return {"white": list(value.white()), "turn": "white" if value.turn else "black"}
    if isinstance(value, dict):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class ChargedClock:
    def __init__(self):
        self.virtual_wait_seconds = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return time.monotonic() + self.virtual_wait_seconds

    def wait(self, seconds, cancel_event):
        if not cancel_event.is_set():
            with self.lock:
                self.virtual_wait_seconds += seconds


class TraceAnalysis:
    def __init__(self, native, owner, index):
        self.native, self.owner, self.index = native, owner, index

    def __getattr__(self, name):
        return getattr(self.native, name)

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, *args):
        return self.native.__exit__(*args)

    def __iter__(self):
        for packet in self.native:
            self.owner.emit({"event": "packet", "search": self.index, "packet": safe(packet)})
            yield packet

    def wait(self):
        result = self.native.wait()
        self.owner.emit({"event": "bestmove", "search": self.index,
                         "move": safe(result.move), "ponder": safe(result.ponder)})
        return result


class TraceEngine:
    def __init__(self, native, stream, identities, label):
        self.native, self.stream, self.label = native, stream, label
        self.index, self.lock = 0, threading.Lock()
        self.identity = {"label": label, "id": dict(native.id), "configured_options": {}}
        identities.append(self.identity)

    def __getattr__(self, name):
        return getattr(self.native, name)

    def emit(self, item):
        with self.lock:
            self.stream.write(json.dumps({"engine": self.label, **item}, ensure_ascii=False, allow_nan=False) + "\n")
            self.stream.flush()

    def configure(self, values):
        self.native.configure(values)
        self.identity["configured_options"].update(values)

    def analysis(self, board, limit, **options):
        self.index += 1
        index = self.index
        self.emit({"event": "search", "search": index, "ply": len(board.move_stack),
                   "fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
                   "requested_seconds": limit.time, "requested_depth": limit.depth,
                   "root_moves": safe(options.get("root_moves"))})
        return TraceAnalysis(self.native.analysis(board, limit, **options), self, index)


def validate(record, pgn):
    board = chess.Board(record["initial_fen"])
    for index, row in enumerate(record["moves"], 1):
        assert row["ply"] == index and row["before_fen"] == board.fen(en_passant="fen")
        move = chess.Move.from_uci(row["uci"])
        assert move in board.legal_moves and board.san(move) == row["san"]
        if row["actor"] == "sparring":
            decision = row["sparring_decision"]
            if decision is not None:
                assert decision["selected_uci"] == row["uci"]
                assert decision["before_fen"] == row["before_fen"]
                assert decision["history_uci"] == [m.uci() for m in board.move_stack]
        board.push(move)
        assert row["after_fen"] == board.fen(en_passant="fen")
    assert [m.uci() for m in board.move_stack] == record["uci_moves"]
    assert board.fen(en_passant="fen") == record["final_fen"]
    parsed = chess.pgn.read_game(io.StringIO(pgn))
    assert parsed is not None and not parsed.errors
    assert [m.uci() for m in parsed.mainline_moves()] == record["uci_moves"]
    assert parsed.headers["Result"] == record["result"]
    outcome = board.outcome(claim_draw=True)
    if outcome:
        assert outcome.result() == record["result"]
    elif record["result"] != "*":
        assert record["termination"] in ("time_forfeit", "time_forfeit_insufficient_material")
    return {"passed": True, "legal_plies": len(board.move_stack), "pgn_matches": True,
            "board_outcome": outcome.result() if outcome else None}


def run(args):
    binary = (args.binary or resolve_runtime_engine(ROOT)).resolve(strict=True)
    stockfish = args.stockfish.resolve(strict=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted([*[path for path in ROOT.joinpath("scripts").glob("*.py")
                       if path.name != "audit_sparring_validation.py"], Path(__file__).resolve()])
    source_hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): sha(path) for path in sources}
    seed = hashlib.sha256(json.dumps([args.seed, "sparring-validation", args.elo]).encode()).hexdigest()[:32]
    config = {"binary": str(binary), "stockfish": str(stockfish), "sparring_color": args.color,
              "stockfish_elo": args.elo, "configured_sparring_ms": args.sparring_ms,
              "opponent_ms": args.opponent_ms, "max_plies": args.max_plies, "time_control": "10+0",
              "seed_input": args.seed, "sparring_seed": seed, "threads": 1, "hash_mb": 16,
              "referee_hash_mb": 32, "referee_enabled": True}
    prefix, prefix_board = [], chess.Board()
    if args.resume_game is not None:
        previous = json.loads(args.resume_game.read_text(encoding="utf-8"))
        if previous.get("sparring_color") != args.color or previous.get("initial_fen") != chess.STARTING_FEN:
            raise ValueError("Resume requires the same sparring color and a standard-start game.")
        prefix = copy.deepcopy(previous["moves"][:args.resume_ply])
        if len(prefix) != args.resume_ply:
            raise ValueError("Resume game is shorter than the requested prefix.")
        for index, row in enumerate(prefix, 1):
            move = chess.Move.from_uci(row["uci"])
            if (row["ply"] != index or row["before_fen"] != prefix_board.fen(en_passant="fen")
                    or move not in prefix_board.legal_moves):
                raise ValueError("Resume prefix contains inconsistent move evidence.")
            prefix_board.push(move)
            if row["after_fen"] != prefix_board.fen(en_passant="fen"):
                raise ValueError("Resume prefix has an inconsistent resulting position.")
            row["recorded_prefix"] = True
        if prefix_board.is_game_over(claim_draw=True):
            raise ValueError("Resume prefix is already terminal.")
        config["recorded_prefix"] = {"path": str(args.resume_game.resolve()), "sha256": sha(args.resume_game),
                                     "plies": len(prefix), "fen": prefix_board.fen(en_passant="fen"),
                                     "scope": "replayed historical moves, not newly played or rescored"}
        if args.resume_clock_ms is not None:
            config["recorded_prefix"]["continuation_clock_reset_ms_each"] = args.resume_clock_ms
    record = {"schema": "regicide.sparring-validation-game.v1", "started_at": utc(),
              "initial_fen": chess.STARTING_FEN, "final_fen": chess.STARTING_FEN,
              "sparring_color": args.color, "stockfish_elo": args.elo,
              "sparring_seed": seed, "uci_moves": [], "moves": [], "plies": 0,
              "result": "*", "termination": "running", "completed": False, "capped": False,
              "error": None, "engines": [], "config": config}
    if prefix:
        record.update(moves=prefix, uci_moves=[row["uci"] for row in prefix],
                      plies=len(prefix), final_fen=prefix_board.fen(en_passant="fen"),
                      continuation_start_ply=len(prefix))
    metadata = {"schema": "regicide.sparring-validation-shard.v1", "config": config,
                "started_at": record["started_at"], "source": source_version(), "source_sha256": source_hashes,
                "binary_sha256": {"regicide": sha(binary), "stockfish": sha(stockfish)},
                "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
                "python": platform.python_version(), "python_chess": chess.__version__,
                "protocol": {"start": "standard initial position; no forced prefix", "pairing": "same seed per Elo and color pair",
                    "clock": "real monotonic computation plus virtual cosmetic idle charged exactly by production wait hook",
                    "display": "hints/live screen-only evaluation disabled", "referee": "production full-strength Stockfish service",
                    "result": "legal board or production clock outcome only; ply cap unfinished",
                    "scores": "limited opponent merged play info is not selected-move evidence; independent audit is separate",
                    "limitations": "small functional diagnostic, not calibrated human rating or strength estimate; virtual waits change background scheduling"},
                "status": "running"}
    write_json(output / "metadata.json", metadata)
    clock, host, opponent = ChargedClock(), None, None
    trace = (output / "native-searches.jsonl").open("x", encoding="utf-8")
    moves = (output / "moves.jsonl").open("x", encoding="utf-8")
    start = time.monotonic()
    try:
        opponent = native_engine(stockfish, boss=False)
        opponent_options = stockfish_options(opponent, args.elo, 1, 16)
        metadata["opponent"] = {"id": opponent.id, "configured_options": opponent_options}
        def factory(path):
            engine = TraceEngine(native_engine(path, boss=True), trace, record["engines"], f"regicide-{len(record['engines']) + 1}")
            engine.configure({"Hash": 16, **({"Threads": 1} if "Threads" in engine.options else {})})
            return engine
        with patch.object(play_human, "MoveHints", QuietHints):
            host = play_human.HumanGame(binary, output / "host", engine_factory=factory,
                hint_engine_factory=lambda _: DisplayOnlyPeer(), evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
                neural_model=None, opening_variety_enabled=False, dynamic_play_enabled=False,
                monotonic=clock, pacing_wait=clock.wait, referee_binary=stockfish)
        state = host.new_game("black" if args.color == "white" else "white", args.sparring_ms,
                              host.revision, sparring_mode=True, time_control="10+0")
        if prefix:
            # Isolated test host: retain complete rule/history context and the
            # recorded clock balances. No prefix move is searched again.
            host.board = prefix_board.copy(stack=True)
            host._sparring_decisions = {row["ply"]: copy.deepcopy(row["sparring_decision"])
                                       for row in prefix if row.get("sparring_decision")}
            host._engine_pacing_decisions = {row["ply"]: copy.deepcopy(row["pacing"])
                                            for row in prefix if row.get("pacing")}
            host._clock.history = [{"ply": row["ply"], "white_ms": row["clock"]["white_ms"],
                                    "black_ms": row["clock"]["black_ms"],
                                    "mover": "white" if row["ply"] % 2 else "black",
                                    "elapsed_ms": row.get("elapsed_ms", 0) + row.get("virtual_wait_ms", 0)}
                                   for row in prefix]
            if args.resume_clock_ms is not None:
                host._clock.history[-1].update(white_ms=args.resume_clock_ms, black_ms=args.resume_clock_ms)
            host._clock.restore_ply(len(prefix), "white" if host.board.turn else "black")
            metadata["protocol"]["start"] = "recorded standard-start prefix; only later plies newly played"
        host._opening_seed = seed
        host._save()
        metadata["referee"] = host._referee_snapshot()
        assert metadata["referee"].get("available") is True
        write_json(output / "metadata.json", metadata)
        board, token = prefix_board.copy(stack=True), object()
        while True:
            state = host.state()
            if state["error"]:
                raise RuntimeError(state["error"])
            if state["result"] != "*":
                record.update(result=state["result"], termination=state["termination"], completed=True)
                break
            if len(board.move_stack) >= args.max_plies:
                record.update(termination="ply_cap", capped=True)
                break
            before = board.copy(stack=True)
            assert before.fen(en_passant="fen") == host.board.fen(en_passant="fen")
            own_turn = board.turn != host.human_color
            started, idle_before = time.monotonic(), clock.virtual_wait_seconds
            opponent_info = None
            if own_turn:
                state = host.engine_move(host.revision)
            else:
                reply = opponent.play(before, chess.engine.Limit(time=args.opponent_ms / 1000),
                                      game=token, info=chess.engine.INFO_ALL, ponder=False)
                assert reply.move is not None and reply.move in before.legal_moves
                opponent_info = {"merged_root_info": safe(reply.info), "selected_move": reply.move.uci(),
                                 "is_selected_move_score": False}
                state = host.human_move(reply.move.uci(), host.revision)
            if state["error"]:
                raise RuntimeError(state["error"])
            if len(host.board.move_stack) == len(before.move_stack):
                assert state["result"] != "*", "No move and no terminal outcome"
                record.update(result=state["result"], termination=state["termination"], completed=True)
                break
            assert len(host.board.move_stack) == len(before.move_stack) + 1
            move = host.board.peek()
            assert move in before.legal_moves
            board.push(move)
            assert board.fen(en_passant="fen") == host.board.fen(en_passant="fen")
            ply = len(board.move_stack)
            row = {"ply": ply, "actor": "sparring" if own_turn else "stockfish", "uci": move.uci(),
                   "san": before.san(move), "before_fen": before.fen(en_passant="fen"),
                   "after_fen": board.fen(en_passant="fen"), "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                   "virtual_wait_ms": round((clock.virtual_wait_seconds - idle_before) * 1000, 3),
                   "sparring_decision": copy.deepcopy(host._sparring_decisions.get(ply)) if own_turn else None,
                   "pacing": copy.deepcopy(host._engine_pacing_decisions.get(ply)) if own_turn else None,
                   "clock": state["clock"], "opponent_info": opponent_info}
            record["moves"].append(row)
            record["uci_moves"].append(move.uci())
            record.update(plies=ply, final_fen=row["after_fen"])
            moves.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            moves.flush()
            event = {"event": "move", "elo": args.elo, "color": args.color, "ply": ply,
                     "actor": row["actor"], "uci": row["uci"], "elapsed_ms": row["elapsed_ms"],
                     "virtual_wait_ms": row["virtual_wait_ms"], "remaining_white_ms": state["clock"]["white_ms"],
                     "remaining_black_ms": state["clock"]["black_ms"]}
            write_json(output / "progress.json", event)
            print(json.dumps(event), flush=True)
    except BaseException as exc:
        record.update(error=f"{type(exc).__name__}: {exc}", termination="error", result="*", completed=False)
    finally:
        if host is not None:
            try:
                state = host.state()
                record["final_clock"] = state["clock"]
                record["host_game_id"] = host.game_id
                pgn = host.pgn()
                host.close()
                sidecar = host.save_dir / (host.game_id + ".json")
                record["host_sidecar"] = str(sidecar.relative_to(output))
                record["host_sidecar_sha256"] = sha(sidecar)
            except Exception as exc:
                record["error"] = (record["error"] or "") + f"; finalization: {exc}"
                pgn = str(chess.pgn.Game.from_board(host.board)) + "\n"
        else:
            pgn = str(chess.pgn.Game()) + "\n"
        if opponent is not None:
            opponent.close()
        moves.close()
        trace.close()
        # For a capped game production PGN is already '*'. Errors never score.
        parsed = chess.pgn.read_game(io.StringIO(pgn))
        parsed.headers.update(Result=record["result"], Event="Clocked sparring validation", Round=f"{args.elo}-{args.color}",
                              White="Regicide Sparring" if args.color == "white" else f"Stockfish limited {args.elo}",
                              Black="Regicide Sparring" if args.color == "black" else f"Stockfish limited {args.elo}")
        pgn = str(parsed) + "\n\n"
        (output / "game.pgn").write_text(pgn, encoding="utf-8")
        try:
            record["validation"] = validate(record, pgn)
        except Exception as exc:
            record["validation"] = {"passed": False, "error": repr(exc)}
            record["error"] = (record["error"] or "") + f"; artifact validation: {exc}"
        current_hashes = {name: sha(ROOT / name) for name in source_hashes}
        metadata.update(source_sha256_after=current_hashes, sources_unchanged=source_hashes == current_hashes,
                        binaries_unchanged=metadata["binary_sha256"] == {"regicide": sha(binary), "stockfish": sha(stockfish)},
                        finished_at=utc(), status="completed" if not record["error"] else "failed")
        record.update(finished_at=utc(), elapsed_seconds=round(time.monotonic() - start, 3),
                      virtual_wait_seconds=round(clock.virtual_wait_seconds, 3))
        write_json(output / "game.json", record)
        write_json(output / "metadata.json", metadata)
        print(json.dumps({"event": "finished", "result": record["result"], "termination": record["termination"],
                          "plies": record["plies"], "error": record["error"], "output": str(output)}), flush=True)
    return 0 if not record["error"] and metadata["sources_unchanged"] and metadata["binaries_unchanged"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--stockfish", required=True, type=Path)
    parser.add_argument("--elo", required=True, type=int, choices=(1320, 1700))
    parser.add_argument("--color", required=True, choices=("white", "black"))
    parser.add_argument("--seed", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--sparring-ms", type=int, default=3000)
    parser.add_argument("--opponent-ms", type=int, default=300)
    parser.add_argument("--max-plies", type=int, default=220)
    parser.add_argument("--resume-game", type=Path)
    parser.add_argument("--resume-ply", type=int, default=40)
    parser.add_argument("--resume-clock-ms", type=int)
    args = parser.parse_args()
    if not 600 <= args.sparring_ms <= 5000 or args.opponent_ms < 1 or args.max_plies < 1:
        parser.error("sparring-ms 600..5000; opponent-ms and max-plies must be positive")
    if args.resume_game is not None and not 0 < args.resume_ply < args.max_plies:
        parser.error("resume-ply must be positive and less than max-plies")
    if args.resume_clock_ms is not None and (args.resume_game is None or args.resume_clock_ms < 1000):
        parser.error("resume-clock-ms requires resume-game and at least 1000ms")
    raise SystemExit(run(args))
