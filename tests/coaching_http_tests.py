"""Real-engine coaching via authenticated HTTP, with disposable game files."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from play_human import HumanGame, HumanServer

PGN = '[Event "Coaching verification"]\n[White "Human"]\n[Black "Regicide"]\n[Result "0-1"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/coaching-http")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix="regicide-coaching-http-") as temporary:
        save_dir = Path(temporary) / "games"
        game = HumanGame(args.binary, save_dir)
        server = HumanServer(game)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before_state = game.state()
        before_analysis = game.analysis.snapshot()
        before_files = {path.name: path.read_bytes() for path in save_dir.glob("*.*")}

        def request(path="/api/coaching", payload=None, token=True, raw=None):
            headers = {"Origin": server.origin}
            if token:
                headers["X-Regicide-Token"] = server.token
            if payload is not None or raw is not None:
                headers["Content-Type"] = "application/json"
            data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(server.origin + path, data=data, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                body = response.read()
                return response.status, json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body

        def start(revision, pgn=PGN, side="white", budget=500):
            return request(payload={"action": "start", "pgn": pgn, "human_color": side,
                                    "movetime_ms": budget, "revision": revision})

        try:
            assert request(token=False)[0] == 403
            assert request(payload={"action": "cancel", "revision": 0}, token=False)[0] == 403
            checks.append("GET and POST require the session token")
            code, initial = request()
            assert code == 200 and initial["status"] == "idle"
            code, script = request("/coaching.js")
            assert code == 200 and len(script) > 100
            for payload in ({"action": "unknown"}, {"action": "start", "pgn": "bad", "human_color": "white", "revision": 0}):
                code, invalid = request(payload=payload)
                assert code == 400 and "coaching" in invalid and "state" not in invalid
            code, invalid = request(raw=b"{")
            assert code == 400 and "coaching" in invalid and "state" not in invalid
            assert game.coaching.snapshot() == initial
            checks.append("invalid requests leave coaching and the live game untouched")

            code, running = start(initial["revision"])
            assert code == 200 and running["status"] in ("running", "completed")
            code, stale = request(payload={"action": "cancel", "revision": initial["revision"]})
            assert code == 409 and "coaching" in stale and "state" not in stale
            deadline = time.monotonic() + 40
            partial_seen = False
            while time.monotonic() < deadline:
                code, report = request()
                assert code == 200 and report["job_id"] == running["job_id"]
                partial_seen |= 0 < report["progress"]["completed"] < report["progress"]["total"]
                if report["status"] != "running":
                    break
                time.sleep(.1)
            assert report["status"] == "completed", report.get("error")
            assert report["progress"] == {"completed": 4, "total": 4}
            assert len(report["frames"]) == 5 and len(report["moves"]) == 4
            expected = chess.Board()
            for index, row in enumerate(report["moves"]):
                assert row["fen_before"] == expected.fen(en_passant="fen")
                assert row["san"] == expected.san(chess.Move.from_uci(row["uci"]))
                for kind in ("best", "played", "alternative"):
                    branch = row[kind]
                    if kind == "alternative" and branch is None:
                        continue
                    board = expected.copy(stack=True)
                    assert branch and branch["pv_uci"], (index, kind, row)
                    assert branch["pv_frames"][0]["fen"] == board.fen(en_passant="fen")
                    for step, uci in enumerate(branch["pv_uci"], 1):
                        assert chess.Move.from_uci(uci) in board.legal_moves
                        board.push_uci(uci)
                        assert branch["pv_frames"][step]["fen"] == board.fen(en_passant="fen")
                    if kind == "played":
                        assert branch["uci"] == row["uci"]
                    elif kind == "alternative":
                        assert row["best"]["uci"] == row["uci"]
                        assert branch["uci"] != row["uci"]
                expected.push_uci(row["uci"])
                assert report["frames"][index + 1]["fen"] == expected.fen(en_passant="fen")
                annotation = row["annotation"]
                assert annotation["scope"] == "actual_move" and annotation["provisional"] is True
                assert annotation["ply"] == row["ply"] and annotation["uci"] == row["uci"]
                assert annotation["square"] == row["uci"][2:4]
                assert annotation["fen_after"] == expected.fen(en_passant="fen")
                assert annotation["label"] and annotation["reason"]
                if annotation["symbol"] == "!!":
                    best, alternative = row["best"], row["alternative"]
                    direction = 1 if row["color"] == "white" else -1
                    assert row["classification"] == "best" and alternative is not None
                    assert best["bound"] == alternative["bound"] == "exact"
                    assert (best["winner"] == row["color"] if best["mate_white"] is not None
                            else direction * best["score_cp_white"] >= 0)
                    assert (alternative["winner"] != row["color"] if alternative["mate_white"] is not None
                            else direction * alternative["score_cp_white"] <= -200)
            assert expected.is_checkmate()
            assert report["moves"][2]["classification"] == "allows_mate", report["moves"][2]
            assert report["moves"][2]["annotation"]["symbol"] == "??"
            assert report["moves"][2]["annotation"]["square"] == "g4"
            assert report["moves"][3]["best"]["uci"] == "d8h4"
            assert 3 in report["critical_plies"]
            checks.append("real engine identifies g4 allowing mate and Qh4#; every returned variation replays legally")
            checks.append("move badges are anchored to the actual destination/FEN; optional alternatives exclude the actual move and !! requires exact comparison evidence")
            checks.append("incremental progress was observed" if partial_seen else "completed report was observed")
            assert report["coach"]["name"] == "Rook"
            lessons = report["coach"]["lesson_plies"]
            assert lessons == sorted(set(lessons)) and 1 <= len(lessons) <= 5
            assert 3 in lessons
            for row in report["moves"]:
                position = chess.Board(row["fen_before"])
                assert set(row["retry_legal_moves"]) == {move.uci() for move in position.legal_moves}
                assert row["coach"]["dialogue"] and row["coach"]["hint"]
                assert len(row["coach"]["dialogue"].split()) <= 40
                assert row["coach"]["dialogue"].isascii()
                for evidence in row["coach"]["evidence"]:
                    assert evidence["line"] in ("best", "played")
                    branch = row[evidence["line"]]
                    assert 0 <= evidence["ply_index"] < len(branch["pv_frames"])
                    assert all(square in chess.SQUARE_NAMES for square in evidence["squares"])
            checks.append("character lessons and per-move evidence refer to legal positions and retry choices")

            retry_payload = {"action": "retry", "revision": report["revision"], "job_id": report["job_id"],
                             "ply": 3, "uci": "g2g4"}
            code, response = request(payload={**retry_payload, "uci": "e1e8"})
            assert code == 400 and "coaching" in response
            code, response = request(payload={**retry_payload, "job_id": "stale-game"})
            assert code == 409 and "coaching" in response
            code, response = request(payload={**retry_payload, "revision": report["revision"] - 1})
            assert code == 409 and "coaching" in response
            assert game.coaching.snapshot() == report
            code, retry = request(payload=retry_payload)
            assert code == 200, retry
            assert retry["job_id"] == report["job_id"] and retry["revision"] == report["revision"]
            assert retry["ply"] == 3 and retry["uci"] == "g2g4" and retry["verdict"] == "try_again", retry
            assert retry["message"] and retry["line"]["uci"] == "g2g4"
            for branch in (retry["line"], retry["best"]):
                position = chess.Board(report["moves"][2]["fen_before"])
                for index, uci in enumerate(branch["pv_uci"], 1):
                    assert chess.Move.from_uci(uci) in position.legal_moves
                    position.push_uci(uci)
                    assert branch["pv_frames"][index]["fen"] == position.fen(en_passant="fen")
            assert game.coaching.snapshot() == report
            checks.append("retry rejects illegal/stale input and independently evaluates g4 without modifying the report")
            (args.output / "retry-result.json").write_text(json.dumps(retry, ensure_ascii=False, indent=2), encoding="utf-8")
            (args.output / "fools-mate-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            code, cached = start(report["revision"])
            assert code == 200 and cached["status"] == "completed" and cached["cached"]
            assert cached["job_id"] != report["job_id"]
            assert cached["moves"] == report["moves"]
            checks.append("completed reports are reused from the matching cache")

            retry_responses = []
            retry_thread = threading.Thread(target=lambda: retry_responses.append(request(payload={
                "action": "retry", "revision": cached["revision"], "job_id": cached["job_id"], "ply": 3, "uci": "g2g4"})))
            retry_thread.start()
            time.sleep(.08)
            retry_cancel_started = time.monotonic()
            code, cancelled_retry = request(payload={"action": "cancel", "revision": cached["revision"]})
            retry_thread.join(5)
            assert code == 200 and not retry_thread.is_alive()
            assert time.monotonic() - retry_cancel_started < 3
            assert retry_responses and retry_responses[0][0] == 409, retry_responses
            assert game.coaching.snapshot() == cancelled_retry
            assert cancelled_retry["moves"] == report["moves"]
            cached = cancelled_retry
            checks.append("cancelling an actual retry terminates its search and rejects the stale response")

            code, running = start(cached["revision"], budget=5000)
            assert code == 200 and running["status"] == "running"
            time.sleep(.2)
            started = time.monotonic()
            code, cancelled = request(payload={"action": "cancel", "revision": running["revision"]})
            elapsed = time.monotonic() - started
            assert code == 200 and cancelled["status"] == "cancelled" and elapsed < 3
            time.sleep(.6)
            assert game.coaching.snapshot() == cancelled
            checks.append("deep search cancellation is bounded and late results cannot overwrite it")
            assert game.state() == before_state
            assert game.analysis.snapshot() == before_analysis
            assert {path.name: path.read_bytes() for path in save_dir.glob("*.*")} == before_files
            checks.append("current game, analysis board and saved JSON/PGN bytes remain unchanged")
            evidence = {"checks": checks, "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                        "engine": game.engine_name, "cancel_seconds": elapsed,
                        "source": "disposable Fool's Mate PGN; own Regicide engine over authenticated HTTP",
                        "personal_game_files_used": False}
            (args.output / "http-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            game.close()


if __name__ == "__main__":
    main()
