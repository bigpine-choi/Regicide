"""Reproduce one actual evaluator observation and enumerate legal queen replies."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import chess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    case = json.loads((HERE / "fixtures.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["id"] == "kings-accepted:9"
    target = "b1c3 d7d5 g4h5 g8f6 f1b5 b8c6".split()
    board = chess.Board(case["initial_fen"])
    for uci in case["history_uci"]:
        board.push_uci(uci)
    assert board.fen(en_passant="fen") == case["before_fen"]
    san = []
    for uci in target:
        move = chess.Move.from_uci(uci)
        san.append(board.san(move))
        board.push_uci(uci)
    queen = chess.H5
    assert board.piece_at(queen) == chess.Piece(chess.QUEEN, chess.WHITE)
    replies = []
    for move in list(board.legal_moves):
        if move.from_square != queen:
            continue
        after = board.copy(stack=True)
        label = after.san(move)
        after.push(move)
        captures = [{"uci": reply.uci(), "san": after.san(reply)}
                    for reply in after.legal_moves
                    if reply.to_square == move.to_square and after.is_capture(reply)]
        replies.append({"queen_move_uci": move.uci(), "san": label,
                        "legal_immediate_queen_captures": captures})
    text = "\t".join([case["initial_fen"], " ".join(case["history_uci"]),
                       "b1c3", " ".join(target)]) + "\n"
    paths = [binary, Path(__file__), ROOT / "scripts/trace_gambit_leaf.cpp", HERE / "fixtures.json"]
    hashes = {str(path): digest(path) for path in paths}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "stdin.tsv").write_text(text, encoding="utf-8")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    command = [str(binary), "--depth", "6", "--nodes", "5000000"]
    response = subprocess.run(command, input=text, capture_output=True, encoding="utf-8",
                              timeout=30, check=True, **flags)
    (args.output / "stdout.json").write_text(response.stdout, encoding="utf-8")
    trace = json.loads(response.stdout)
    assert trace["requested_iteration_completed"] and trace["position_unchanged"]
    assert trace["target_fen"] == board.fen(en_passant="fen")
    assert trace["match_status"] == "observed_static_evaluation" and trace["match_count"] > 0
    assert not trace["matches_truncated"] and not trace["causal_root_support_claim"]
    assert len(trace["matches"]) == trace["match_count"]
    assert all(item["iteration_depth"] == 6 and item["fen"] == trace["target_fen"]
               for item in trace["matches"])
    report = {"recorded_utc": datetime.now(timezone.utc).isoformat(), "command": command,
              "returncode": response.returncode, "stderr": response.stderr,
              "source_sha256": hashes, "sources_unchanged": all(digest(Path(p)) == h for p, h in hashes.items()),
              "case_id": case["id"], "target_pv_san": san, "target_fen": trace["target_fen"],
              "actual_evaluator_matches": trace["match_count"],
              "in_check": board.is_check(),
              "queen_attackers": [chess.square_name(s) for s in board.attackers(chess.BLACK, queen)],
              "legal_queen_moves": replies,
              "every_queen_move_allows_immediate_legal_capture": bool(replies) and all(
                  row["legal_immediate_queen_captures"] for row in replies),
              "scope": "One supplied endpoint and actual observer calls. This does not prove forced queen loss, lack of compensation, or causal attribution of the root score."}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"matches": trace["match_count"], "search": trace["search"],
                      "evaluations": trace["matches"], "queen_moves": len(replies)}))


if __name__ == "__main__":
    main()
