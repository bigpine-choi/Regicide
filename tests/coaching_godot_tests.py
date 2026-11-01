"""Native coaching fixtures and a real-engine HTTP workflow in a private store."""
import argparse
import json
from pathlib import Path
import tempfile
import threading

import chess
import chess.pgn

import godot_integration_tests as integration
from pgn_preview import preview_pgn


def frame(board, ply=0):
    return {"ply": ply, "fen": board.fen(en_passant="fen"),
            "board": {chess.square_name(s): p.symbol() for s, p in board.piece_map().items()},
            "turn": "white" if board.turn else "black", "in_check": board.is_check(),
            "last_move": board.peek().uci() if board.move_stack else None,
            "legal_moves": [], "human_to_move": False, "engine_to_move": False}


def line(board, moves, cp):
    frames, sans = [frame(board)], []
    replay = board.copy(stack=True)
    for uci in moves:
        move = chess.Move.from_uci(uci)
        assert move in replay.legal_moves
        sans.append(replay.san(move))
        replay.push(move)
        frames.append(frame(replay))
    return {"uci": moves[0], "san": sans[0], "pv_uci": moves, "pv_san": sans,
            "pv_frames": frames, "score_cp_white": cp, "mate_white": None,
            "winner": None, "depth": 7, "nodes": 12000, "bound": "exact"}


def fixture():
    board = chess.Board()
    sequence = ["f2f3", "e7e5", "g2g4", "d8h4"]
    rows, frames = [], [frame(board)]
    for ply, uci in enumerate(sequence, 1):
        move = chess.Move.from_uci(uci)
        best_line = ["e2e4", "b8c6"] if ply == 3 else [uci]
        played_line = ["g2g4", "d8h4"] if ply == 3 else [uci]
        row = {"ply": ply, "color": "white" if board.turn else "black", "uci": uci,
               "san": board.san(move), "fen_before": board.fen(en_passant="fen"),
               "classification": "blunder" if ply == 3 else ("mistake" if ply == 2 else "good"),
               "label": "큰 실수" if ply == 3 else ("실수" if ply == 2 else "좋은 수"),
               "loss_cp": 300 if ply == 3 else (120 if ply == 2 else 0),
               "best": line(board, best_line, 0), "played": line(board, played_line, -300),
               "retry_legal_moves": [candidate.uci() for candidate in board.legal_moves],
               "coach": {"mood": "warning" if ply == 3 else "encourage",
                         "dialogue": "상대가 무엇을 위협하는지 살펴보자." if ply == 3 else "이 수에서 배울 점을 같이 찾아보자.",
                         "hint": "우리 왕으로 향하는 대각선을 먼저 확인해 봐.",
                         "evidence": [{"kind": "mate", "text": "상대 퀸의 체크메이트를 판에서 확인하자.", "squares": ["h4", "e1"], "line": "played", "ply_index": 2}] if ply == 3 else [],
                         "focus_squares": []},
               "explanation": "독립적으로 합법 재생한 UI 시험 자료입니다."}
        board.push(move)
        row["fen_after"] = board.fen(en_passant="fen")
        rows.append(row)
        frames.append(frame(board, ply))
    pgn = str(chess.pgn.Game.from_board(board)) + "\n"
    report = {"revision": 2, "job_id": "native-fixture", "status": "completed",
              "progress": {"completed": 4, "total": 4},
              "game": {"human_color": "white", "result": "0-1", "ply_count": 4},
              "settings": {"movetime_ms": 500}, "frames": frames, "moves": rows,
              "summary": {"message": "실수한 국면을 확인하세요."}, "critical_plies": [3],
              "coach": {"name": "루크", "summary": "양쪽의 응수를 비교하면서 네 군데에서 배워 보자.", "lesson_plies": [1, 2, 3, 4], "version": 1},
              "cached": False, "error": None}
    long_board = chess.Board()
    long_rows, long_frames = [], [frame(long_board)]
    for ply, uci in enumerate(["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1", "f8e7"], 1):
        move = chess.Move.from_uci(uci)
        assert move in long_board.legal_moves
        row = {"ply": ply, "color": "white" if long_board.turn else "black", "uci": uci,
               "san": long_board.san(move), "fen_before": long_board.fen(en_passant="fen"),
               "classification": "good", "label": "좋은 수"}
        long_board.push(move)
        row["fen_after"] = long_board.fen(en_passant="fen")
        long_rows.append(row)
        long_frames.append(frame(long_board, ply))
    walkthrough_report = {"revision": 3, "job_id": "native-long-walkthrough", "status": "completed",
                          "progress": {"completed": 10, "total": 10},
                          "game": {"human_color": "white", "result": "*", "ply_count": 10},
                          "frames": long_frames, "moves": long_rows, "critical_plies": []}
    promotion = chess.Board("8/P7/8/8/8/8/7k/K7 w - - 0 1")
    assert promotion.is_valid()
    promotion_before = frame(promotion)
    promotion_moves = [move.uci() for move in promotion.legal_moves]
    promotion_line = line(promotion, ["a7a8q"], 900)
    promotion.push_uci("a7a8q")
    promotion_report = {"revision": 4, "job_id": "native-promotion", "status": "completed",
                        "frames": [promotion_before, frame(promotion, 1)],
                        "moves": [{"ply": 1, "color": "white", "uci": "a7a8q", "san": "a8=Q",
                                   "fen_before": promotion_before["fen"], "fen_after": promotion.fen(en_passant="fen"),
                                   "classification": "best", "label": "추천 수", "best": promotion_line,
                                   "played": promotion_line, "retry_legal_moves": promotion_moves}],
                        "critical_plies": [], "game": {"human_color": "white", "ply_count": 1}, "coach": {"lesson_plies": [1]}}
    badge_cases = []
    for name, fen, uci in [
        ("white short castle", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
        ("white long castle", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"),
        ("black short castle", "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8g8"),
        ("black long castle", "r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8"),
        ("white queen promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q"),
        ("white rook promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8r"),
        ("white bishop promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8b"),
        ("white knight promotion", "4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n"),
        ("capture promotion", "1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7b8q"),
        ("black corner promotion", "4k3/8/8/8/8/8/7p/4K3 b - - 0 1", "h2h1n"),
    ]:
        position = chess.Board(fen)
        move = chess.Move.from_uci(uci)
        assert position.is_valid() and move in position.legal_moves, name
        before = frame(position)
        branch = line(position, [uci], 0)
        legal = [candidate.uci() for candidate in position.legal_moves]
        san = position.san(move)
        position.push(move)
        after = frame(position, 1)
        badge_cases.append({"name": name, "report": {
            "revision": 7, "job_id": "badge-fixture-" + name, "status": "completed",
            "frames": [before, after], "game": {"human_color": before["turn"], "ply_count": 1},
            "critical_plies": [], "coach": {"lesson_plies": [1]},
            "moves": [{"ply": 1, "color": before["turn"], "uci": uci, "san": san,
                       "fen_before": before["fen"], "fen_after": after["fen"], "classification": "best",
                       "label": "추천 수", "best": branch, "played": branch, "retry_legal_moves": legal}]}})
    return {"pgn": pgn, "preview": preview_pgn(pgn), "report": report, "walkthrough_report": walkthrough_report,
            "promotion_report": promotion_report, "badge_cases": badge_cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=integration.ROOT / "build/coaching-godot")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture_file = args.output / "native-fixture.json"
    fixture_file.write_text(json.dumps(fixture(), ensure_ascii=False, indent=2), encoding="utf-8")
    integration.verify_fresh_checkout(args)
    common = ["--fixture", fixture_file, "--evidence-dir", args.output]
    integration.run_godot(args, "coaching_tests.gd", common, "coaching-synthetic")
    with tempfile.TemporaryDirectory(prefix="regicide-coaching-godot-") as temporary:
        folder = Path(temporary)
        game = integration.backend.HumanGame(args.binary, folder / "games")
        game.new_game("white", 100, game.state()["revision"])
        with game._lock:
            for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:
                game.board.push_uci(uci)
            game.revision += 1
            game._sync_evaluation()
            game._save()
        server = integration.backend.HumanServer(game)
        server.RequestHandlerClass = integration.AuditedHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = folder / "backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        initial = game.state()
        originals = {p.name: p.read_bytes() for p in game.save_dir.iterdir() if p.is_file()}
        try:
            integration.run_godot(args, "coaching_tests.gd", [*common, "--backend-file", config], "coaching-live")
            assert game.state() == initial, "coaching modified live board"
            assert originals == {p.name: p.read_bytes() for p in game.save_dir.iterdir() if p.is_file()}, "coaching modified saved games"
            posts = [e["path"] for e in server.audit if e["method"] == "POST"]
            assert posts and set(posts) <= {"/api/pgn-preview", "/api/coaching"}, posts
            (args.output / "coaching-integration.json").write_text(json.dumps({
                "engine": game.engine_name, "binary_sha256": game.binary_sha256,
                "live_state_unchanged": True, "original_archive_files_unchanged": True,
                "routes": server.audit,
            }, indent=2), encoding="utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
    print("Native coaching replay, PV, stable geometry, progress/cancel and game isolation passed.")


if __name__ == "__main__":
    main()
