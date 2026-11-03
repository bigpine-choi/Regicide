"""Real Regicide + authenticated localhost API with synthetic public statistics.

No Lichess token or external network access is used. --serve keeps an isolated
fixture server available for visual browser verification until interrupted.
"""

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lichess_credentials import LichessCredentials
from opening_explorer import OpeningExplorer
from play_human import HumanGame, HumanServer


def fixture_response(request, *, timeout):
    assert request.full_url.startswith("https://explorer.lichess.org/")
    assert request.get_header("Authorization", "").startswith("Bearer ")
    parameters = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    board = chess.Board(parameters["fen"][0])
    rows = []
    for index, move in enumerate(list(board.legal_moves)[:5]):
        rows.append({"uci": move.uci(), "white": 200 + index * 60,
                     "draws": 100 + index * 10, "black": 150 + index * 30,
                     "averageRating": 2300})
    payload = {key: sum(row[key] for row in rows) for key in ("white", "draws", "black")}
    payload.update(moves=rows, topGames=[{
        "id": "Demo0001", "white": {"name": "Verification fixture White", "rating": 2400},
        "black": {"name": "Verification fixture Black", "rating": 2400}, "winner": None, "year": 2026}])
    encoded = json.dumps(payload).encode()
    response = io.BytesIO(encoded)
    response.headers = {"Content-Length": str(len(encoded))}
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/opening-explorer")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--godot", type=Path, help="Also drive the real native scene over local HTTP")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix="regicide-explorer-integration-") as temporary:
        folder = Path(temporary)
        credentials = LichessCredentials(folder / "credentials.dpapi")
        credentials.set("verification_fixture_only", remember=False)
        explorer = OpeningExplorer(cache_dir=folder / "cache", credentials=credentials,
                                   opener=fixture_response, min_interval=0)
        game = HumanGame(args.binary, folder / "games")
        try:
            with HumanServer(game, explorer=explorer) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def request(path, data=None):
                    body = json.dumps(data).encode() if data is not None else None
                    headers = {"Origin": server.origin, "X-Regicide-Token": server.token}
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                    with urllib.request.urlopen(urllib.request.Request(server.origin + path, data=body, headers=headers), timeout=15) as response:
                        return json.load(response)

                try:
                    before = game.state()
                    before_files = {path.name: path.read_bytes() for path in game.save_dir.glob("*.*")}
                    report = request("/api/opening-explorer", {"action": "query", "fen": chess.STARTING_FEN,
                                                              "source": "lichess", "ratings": [1600, 1800], "speeds": ["rapid"]})
                    deadline = time.monotonic() + 5
                    while report["status"] == "loading" and time.monotonic() < deadline:
                        time.sleep(.01)
                        report = request("/api/opening-explorer")
                    assert report["status"] == "ready" and report["total"] == 3250
                    assert report["ratings"] == [1600, 1800] and report["speeds"] == ["rapid"]
                    assert abs(sum(row["play_pct"] for row in report["moves"]) - 100) < 1e-8
                    checks.append("real HTTP + explorer worker + filtered synthetic statistics")
                    chosen = report["moves"][0]
                    analysis = request("/api/analysis")
                    analysis = request("/api/analysis", {"action": "start", "fen": chosen["fen_after"],
                                                       "movetime_ms": 100, "revision": analysis["revision"]})
                    deadline = time.monotonic() + 10
                    while analysis["hint"]["status"] != "ready" and time.monotonic() < deadline:
                        time.sleep(.03)
                        analysis = request("/api/analysis")
                    assert analysis["fen"] == chosen["fen_after"]
                    assert analysis["hint"]["status"] == "ready"
                    assert chess.Move.from_uci(analysis["hint"]["move"]) in chess.Board(chosen["fen_after"]).legal_moves
                    checks.append("candidate position reaches independent analysis and real Regicide produces a legal recommendation")
                    assert game.state() == before
                    assert before_files == {path.name: path.read_bytes() for path in game.save_dir.glob("*.*")}
                    checks.append("live game and durable game files are unchanged")
                    request("/api/analysis", {"action": "stop", "revision": analysis["revision"]})
                    if args.serve or args.godot:
                        # The local session secret is bootstrap-only, ignored by
                        # Git, never stdout or the published verification report.
                        (args.output / "fixture-startup.json").write_text(json.dumps({
                            "url": server.origin, "token": server.token}), encoding="utf-8")
                    if args.godot:
                        command = [str(args.godot), "--headless", "--path", str(ROOT / "godot"),
                                   "--script", "res://tests/opening_explorer_live_tests.gd", "--",
                                   "--backend-file", str((args.output / "fixture-startup.json").resolve())]
                        native = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        (args.output / "native-http.log").write_text(native.stdout + native.stderr, encoding="utf-8")
                        assert native.returncode == 0 and "SCRIPT ERROR:" not in native.stdout + native.stderr, native.stdout + native.stderr
                        assert "failures: 0" in native.stdout
                        checks.append("native Godot scene queries HTTP statistics and opens real-engine analysis without changing the game")
                    (args.output / "integration.json").write_text(json.dumps({
                        "statistics_source": "synthetic fixture; no authenticated Lichess lookup performed",
                        "binary": str(args.binary), "binary_sha256": game.binary_sha256,
                        "checks": checks, "total": report["total"], "candidates": len(report["moves"])
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({"checks": checks, "url": server.origin}, ensure_ascii=True), flush=True)
                    if args.serve:
                        try:
                            while thread.is_alive():
                                thread.join(.5)
                        except KeyboardInterrupt:
                            pass
                finally:
                    server.shutdown()
                    thread.join(3)
        finally:
            game.close()


if __name__ == "__main__":
    main()
