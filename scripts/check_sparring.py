"""Isolated native host and 50/70-move boundaries; behavior checks, not Elo.

The short game uses a fast Regicide peer as a human-move stand-in. Boundary
positions are legal prefixes of recorded own-engine games, not played anew.
No user game directory or running application is opened.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import chess
import chess.engine
import chess.pgn

from check_opening_variety import QuietScreenHints
from boss_strength_match import native_engine
import evaluation_stability
import play_human
from game_archive import GameArchive

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_game(binary, folder):
    with patch.object(play_human, "MoveHints", QuietScreenHints):
        return play_human.HumanGame(binary, folder, engine_factory=lambda path: native_engine(path, boss=True),
                                    dynamic_play_enabled=True)


def reference_game():
    path = ROOT / "docs/matches/2026-11-02-boss-v-normal/pairs-5-6/match.pgn"
    with path.open(encoding="utf-8") as stream:
        for _ in range(3):
            game = chess.pgn.read_game(stream)
    return path, list(game.mainline_moves())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--opponent-ms", type=int, default=100)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {"scope": "native functional smoke; no rating or perceived-drama measurement",
               "runtime_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "screen_helpers": "disabled during host checks; independent current-position render analysis afterwards",
               "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ("scripts/play_human.py", "scripts/sparring_play.py",
                                              "scripts/evaluation_stability.py", "scripts/game_archive.py")},
               "short_game": [], "boundaries": []}
    with tempfile.TemporaryDirectory(prefix="regicide-sparring-native-") as temporary:
        folder = Path(temporary)
        opponent = native_engine(binary, boss=True)
        opponent.configure({"UsePieceActivity": True})
        game = make_game(binary, folder / "short")
        try:
            game.new_game("white", 1000, game.revision, sparring_mode=True)
            game._opening_seed = "7" * 32
            render_state, render_board = game.state(), game.board.copy(stack=True)
            for index in range(args.rounds):
                if game.state()["result"] != "*":
                    break
                reply = opponent.play(game.board.copy(stack=True), chess.engine.Limit(time=args.opponent_ms / 1000), game=object())
                assert reply.move in game.board.legal_moves
                game.human_move(reply.move.uci(), game.revision)
                if game.state()["result"] != "*":
                    break
                started = time.monotonic()
                state = game.engine_move(game.revision)
                assert state["error"] is None, state["error"]
                decision = game._sparring_decisions[len(game.board.move_stack)]
                assert decision["selected_uci"] == state["last_move"]
                assert decision["baseline_budget_ms"] == 1000
                summary["short_game"].append({"round": index + 1, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                                               "decision": decision})
                if state["result"] == "*":
                    render_state, render_board = state, game.board.copy(stack=True)
                print(f"round {index + 1}: {state['last_move']} {decision['reason']}", flush=True)
            state = game.state()
            summary["short_result"] = {"result": state["result"], "plies": len(game.board.move_stack),
                                       "profile": state["engine"]["sparring"]}
            (out / "short-game.pgn").write_text(game.pgn(), encoding="utf-8")
            archive = GameArchive(game.save_dir).get_game(game.game_id)
            assert archive["summary"]["sparring_mode"]
            assert archive["frames"][-1]["fen"] == game.board.fen(en_passant="fen")
            summary["archive_replay_matches"] = True
            # Same position, own-engine unrestricted score for the visible UI fixture.
            packets = {}
            with opponent.analysis(render_board, chess.engine.Limit(time=1), game=object(),
                                   info=chess.engine.INFO_ALL) as analysis:
                for info in analysis:
                    if type(info.get("depth")) is int and "score" in info:
                        packets[info["depth"]] = dict(info)
                analysis.wait()
            latest = packets[max(packets)]
            white = latest["score"].pov(chess.WHITE)
            state = render_state
            evaluation = {"game_id": state["game_id"], "revision": state["revision"], "fen": state["fen"],
                          "turn": state["turn"], "status": "ready", "cp": white.score(), "mate": white.mate(),
                          "winner": None, "result": state["result"], "depth": latest["depth"],
                          "bound": "lower" if latest.get("lowerbound") else "upper" if latest.get("upperbound") else "exact",
                          "source": "search", **evaluation_stability.assess(render_board, packets)}
            if white.mate() is not None:
                evaluation["winner"] = "white" if white.score(mate_score=30000) > 0 else "black"
            write(out / "ui-fixture.json", {"state": state, "evaluation": evaluation,
                                          "scope": "actual host state; independent own-engine current-position analysis"})
        finally:
            game.close()
            opponent.quit()

        source, moves = reference_game()
        summary["boundary_source"] = {"path": str(source.relative_to(ROOT)), "game_index": 3,
                                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        for ply, expected_phase in ((99, "sparring"), (100, "ramp"), (120, "ramp"), (140, "finish")):
            game = make_game(binary, folder / str(ply))
            try:
                board = chess.Board()
                for move in moves[:ply]:
                    assert move in board.legal_moves and not board.is_game_over(claim_draw=True)
                    board.push(move)
                assert not board.is_game_over(claim_draw=True)
                game.new_game("black" if board.turn else "white", 1000, game.revision, sparring_mode=True)
                with game._lock:
                    game.board = board.copy(stack=True)
                    game.revision += 1
                    game._sync_evaluation()
                assert game.state()["engine_to_move"]
                state = game.engine_move(game.revision)
                assert state["error"] is None, state["error"]
                decision = game._sparring_decisions[ply + 1]
                assert decision["phase"] == expected_phase
                if expected_phase == "finish":
                    assert decision["selected_uci"] == decision["baseline_uci"]
                    assert decision["extra_budget_ms"] == 0
                    assert decision["reason"] == "finish_full_strength"
                summary["boundaries"].append({"ply_before": ply, "expected_phase": expected_phase, "decision": decision})
                print(f"boundary {ply}: {expected_phase} {decision['reason']}", flush=True)
            finally:
                game.close()
    write(out / "native-summary.json", summary)
    print("Native sparring host, archive and all schedule boundaries passed.")


if __name__ == "__main__":
    main()
