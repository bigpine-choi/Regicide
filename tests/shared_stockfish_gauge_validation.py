"""Real local HTTP/Stockfish display checks and three native opponent turns.

Authored fixtures use private games. Display reference scores never supply a
move. Native UCI requests/returns are retained, along with source/binary hashes.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import chess
import game_clock
import play_human
from boss_strength_match import DisplayOnlyPeer, native_engine
from common_human_pacing_validation import TimedTraceEngine, timing_seed
from gambit_character_validation import safe
from runtime_engine import resolve_runtime_engine

FEN = "6k1/5ppp/8/8/3q4/8/3R4/5K2 w - - 0 30"
MODES = {"basic": {}, "gambit": {"boss_mode": True}, "sparring": {"sparring_mode": True}}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hashes():
    paths = list(ROOT.joinpath("scripts").glob("*.py")) + [Path(__file__),
        ROOT / "tests/common_human_pacing_validation.py", ROOT / "tests/gambit_character_validation.py"]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))}


def write(path, value):
    path.write_text(json.dumps(safe(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class Session:
    def __init__(self, binary, stockfish, directory):
        self.directory = directory
        directory.mkdir()
        self.events, self.identities = [], []
        self.origin = time.monotonic()
        self.trace = (directory / "native-searches.jsonl").open("x", encoding="utf-8")

        def factory(path):
            peer = TimedTraceEngine(native_engine(path, boss=True), self.trace, self.identities,
                                    self.events, origin=self.origin)
            peer.configure({key: value for key, value in (("Threads", 1), ("Hash", 16)) if key in peer.options})
            return peer

        self.game = play_human.HumanGame(binary, directory / "games", engine_factory=factory,
            hint_engine_factory=lambda path: native_engine(path, boss=True),
            evaluation_engine_factory=lambda _: DisplayOnlyPeer(),
            opening_variety_enabled=False, dynamic_play_enabled=True, referee_binary=stockfish)
        self.server = play_human.HumanServer(self.game)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def request(self, path, data=None):
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Origin": self.server.origin, "X-Regicide-Token": self.server.token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())

    def ready(self, path="/api/evaluation", key="reference_evaluation", timeout=8):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            value = self.request(path)
            reference = value.get(key, {}) if key else value
            if reference.get("status") in ("ready", "terminal", "error", "unavailable"):
                assert reference["status"] in ("ready", "terminal"), value
                return value
            time.sleep(.02)
        raise AssertionError(("reference timeout", path, value))

    def edit(self, side, delta):
        state = self.request("/api/state")
        value = self.request("/api/clock-adjust", {"game_id": state["game_id"],
            "clock_revision": state["clock"]["edit_revision"], "side": side, "delta_ms": delta})
        assert value["game_id"] == state["game_id"] and value["clock"]["editable"]
        return {"side": side, "delta_ms": delta, "clock": value["clock"]}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(5)
        self.game.close()
        self.trace.close()
        assert not self.worker.is_alive() and self.game._engine is None


def assert_reference(value, state):
    assert value["source"] == "stockfish_gauge" and value["status"] == "ready", value
    assert value["bound"] == "exact" and value["depth"] > 0
    assert all(value[key] == state[key] for key in ("game_id", "revision", "fen", "turn"))
    assert "uci" not in value and "pv_uci" not in value
    assert (value["cp"] is None) != (value["mate"] is None)


def run(binary, stockfish, output):
    output.mkdir(parents=True, exist_ok=False)
    before, rows = hashes(), []
    native_sha, sf_sha = sha(binary), sha(stockfish)
    write(output / "fixtures.json", {"fen": FEN, "modes": MODES,
        "note": "Authored display/timing positions; not a playing-strength experiment."})
    for mode in MODES:
        session = Session(binary, stockfish, output / mode)
        row = {"mode": mode}
        try:
            state = session.request("/api/state")
            session.request("/api/new", {"revision": state["revision"], "color": "black",
                "time_control": "10+0", **MODES[mode]})
            board = chess.Board(FEN)
            with session.game._lock:
                session.game.board = board.copy(stack=True)
                session.game.revision += 1
                session.game._opening_seed = timing_seed(board, "clear_queen_gain", 600000)
                session.game._clock = game_clock.GameClock("10+0", timeout_policy="engine_only",
                                                           soft_timeout_color="black")
                session.game._sync_evaluation()
                session.game._save()
            state = session.request("/api/state")
            value = session.ready()
            assert_reference(value["reference_evaluation"], state)
            assert value["stockfish_reference"]["enabled"]
            assert value["reference_evaluation"]["binary_sha256"] == sf_sha
            assert session.game._gauge._referee is not session.game._referee
            row["before"] = value
            row["clock_edits"] = [session.edit(side, delta) for side in ("human", "engine")
                                   for delta in (10000, -10000)]
            revision = session.request("/api/state")["revision"]
            event_start = len(session.events)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(session.request, "/api/engine", {"revision": revision})
                deadline = time.monotonic() + 3
                busy_seen = False
                while not future.done() and time.monotonic() < deadline:
                    if session.game._engine_searching or session.game._active_pacing is not None:
                        busy_seen = True
                        row["clock_edits"].append(session.edit("engine", 10000))
                        row["clock_edits"].append(session.edit("engine", -10000))
                        break
                    time.sleep(.001)
                state = future.result(timeout=60)
            row["native_wall_ms"] = round((time.monotonic() - started) * 1000, 3)
            assert state.get("error") is None and len(session.game.board.move_stack) == 1
            selected = session.game.board.peek().uci()
            events = session.events[event_start:]
            searches = {(event["instance"], event["search"]): event for event in events if event["event"] == "search"}
            native_return = any(event["event"] == "bestmove" and event["move"] == selected
                and searches[event["instance"], event["search"]]["fen"] == FEN for event in events)
            assert native_return, (selected, events)
            after = session.ready()
            assert_reference(after["reference_evaluation"], state)
            row.update(after=after, selected_uci=selected, selected_returned_by_native=True,
                       clock_edit_while_engine_active=busy_seen,
                       pacing=copy.deepcopy(session.game._engine_pacing_decisions[1]),
                       identities=session.identities)
            write(session.directory / "native-turn.json", row)

            # Analysis and coaching own separate Stockfish sessions. Browsing
            # them must not replace a game position or its display generation.
            if mode == "basic":
                live_context = (session.game.game_id, session.game.revision, session.game.board.fen())
                saved = {p.name: p.read_bytes() for p in session.game.save_dir.glob("*.json")}
                analysis = session.request("/api/analysis")
                session.request("/api/analysis", {"action": "start", "fen": FEN,
                    "movetime_ms": 200, "revision": analysis["revision"]})
                analysis = session.ready("/api/analysis")
                assert_reference(analysis["reference_evaluation"], analysis)
                deadline = time.monotonic() + 8
                while analysis["hint"].get("status") == "searching" and time.monotonic() < deadline:
                    time.sleep(.02)
                    analysis = session.request("/api/analysis")
                assert analysis["hint"].get("source") != "stockfish_gauge"
                assert analysis["hint"]["move"] in [move.uci() for move in board.legal_moves], analysis
                row["analysis"] = analysis
                coaching = session.request("/api/coaching-reference", {"request_id": "actual-coaching",
                    "root_fen": chess.STARTING_FEN, "history_uci": ["e2e4"],
                    "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"})
                coaching = session.ready("/api/coaching-reference", key=None)
                assert coaching["source"] == "stockfish_coaching_reference" and coaching["status"] == "ready"
                assert "pv_uci" not in coaching and "uci" not in coaching
                row["coaching"] = coaching
                assert live_context == (session.game.game_id, session.game.revision, session.game.board.fen())
                assert saved == {p.name: p.read_bytes() for p in session.game.save_dir.glob("*.json")}
                assert_reference(session.ready()["reference_evaluation"], state)
                row["browsing_isolated"] = True
            state = session.request("/api/resign", {"revision": state["revision"]})
            labeled = session.request("/api/rating-label", {"revision": state["revision"],
                "game_id": state["game_id"], "rating": 1500})
            assert labeled["engine"]["rating"]["label"]["rating"] == 1500
            row["completed_rating_label"] = labeled["engine"]["rating"]["label"]
            write(session.directory / "receipt.json", row)
            rows.append(row)
            print(json.dumps({"mode": mode, "selected": selected, "sf_before": value["reference_evaluation"]["cp"],
                "clock_edit_while_active": busy_seen, "rating_saved": True}), flush=True)
        finally:
            session.close()
    after = hashes()
    checks = {"three_modes_ready": len(rows) == 3, "all_selected_returned_by_native": all(r["selected_returned_by_native"] for r in rows),
        "all_clock_edits_while_active": all(r["clock_edit_while_engine_active"] for r in rows),
        "five_display_contexts_ready": "analysis" in rows[0] and "coaching" in rows[0],
        "unchanged_sources": before == after, "unchanged_binaries": native_sha == sha(binary) and sf_sha == sha(stockfish),
        "all_sessions_closed": True}
    result = {"utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "platform": platform.platform(), "binary": str(binary), "native_sha256": native_sha,
        "stockfish": str(stockfish), "stockfish_sha256": sf_sha, "checks": checks, "cases": rows,
        "source_hashes_before": before, "source_hashes_after": after}
    write(output / "result.json", result)
    assert all(checks.values()), checks
    print(json.dumps(checks), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--stockfish", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve() if args.binary else resolve_runtime_engine(ROOT)
    run(Path(binary), args.stockfish.resolve(), args.output.resolve())
