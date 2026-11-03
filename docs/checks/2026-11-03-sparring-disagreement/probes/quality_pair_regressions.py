"""Native shallow-candidate promotion for three newly observed regressions."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import reproduce as probe
import chess
import chess.engine

ROOT = probe.ROOT
sys.path.insert(0, str(ROOT / "tests"))
from sparring_quality_native_integration import independent_audit

SOURCE = ROOT / "docs/matches/2026-11-03-sparring-quality/1700-white/game.json"
OUT = Path(__file__).resolve().parent / "quality-pair-regressions-v1.json"


def run():
    saved = json.loads(SOURCE.read_text(encoding="utf-8"))
    binary = probe.resolve_runtime_engine(ROOT)
    sf = Path(saved["config"]["stockfish"])
    launch = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    native = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10, **launch)
    native.configure({"Hash": 16, "UsePieceActivity": True})
    output = {"started_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE.relative_to(ROOT)), "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "native": {"path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "id": native.id,
                   "configured_options": {**{k:v.default for k,v in native.options.items()}, "Hash":16, "UsePieceActivity":True}},
        "stockfish": {"path": str(sf), "sha256": hashlib.sha256(sf.read_bytes()).hexdigest()},
        "protocol": "Full recorded history. Fresh native400ms root exclusion x2; recorded/native-generated roots only get singleton400ms promotion (900ms only if still belowdepth4). Independent fresh SF3s root and children after generation. No SF-generated candidate injection.",
        "cases": []}
    try:
        for ply, recorded in ((33, ("g2d5", "b1a1")), (39, ("g5f7", "g5f3")), (41, ("e4f6",))):
            row = saved["moves"][ply - 1]
            board = chess.Board(saved["initial_fen"])
            for uci in saved["uci_moves"][:ply - 1]:
                board.push_uci(uci)
            assert board.fen(en_passant="fen") == row["before_fen"]
            guard = row["sparring_decision"]["quality_guard"]
            record = {"ply": ply, "fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
                      "original_uci": row["uci"], "recorded_guard": guard, "recorded_native_roots": list(recorded),
                      "discoveries": [], "promotions": {}, "sf": {}}
            native_candidates = set(recorded)
            excluded = {row["uci"]}
            for _ in range(2):
                allowed = [move for move in board.legal_moves if move.uci() not in excluded]
                result = probe.search(native, board, chess.engine.Limit(time=.4), board.turn, allowed)
                record["discoveries"].append({"excluded_uci": sorted(excluded), **result})
                native_candidates.add(result["bestmove"])
                excluded.add(result["bestmove"])
            for uci in sorted(native_candidates):
                move = chess.Move.from_uci(uci)
                assert move in board.legal_moves
                result = probe.search(native, board, chess.engine.Limit(time=.4), board.turn, [move])
                record["promotions"][uci] = {"400ms": result}
                if not result["completed"] or result["completed"]["depth"] < 4:
                    record["promotions"][uci]["900ms"] = probe.search(native, board, chess.engine.Limit(time=.9), board.turn, [move])
            record["sf"]["root"] = independent_audit(sf, board, board.turn, 3000)
            for uci in sorted(native_candidates | {row["uci"]}):
                child = board.copy(stack=True)
                child.push_uci(uci)
                record["sf"][uci] = independent_audit(sf, child, board.turn, 3000)
            output["cases"].append(record)
            probe.dump(OUT, output)
            print(json.dumps({"ply": ply,
                 "discoveries": [(r["bestmove"], (r["completed"] or {}).get("depth")) for r in record["discoveries"]],
                 "promotions": {uci:{ms:{"depth":(r["completed"] or {}).get("depth"),"cp":(r["completed"] or {}).get("cp_own"),"elapsed_ms":r["elapsed_ms"]} for ms,r in runs.items()} for uci,runs in record["promotions"].items()},
                 "sf_cp_own": {uci:(r["completed"] or {}).get("cp_own") for uci,r in record["sf"].items()}}), flush=True)
    finally:
        native.quit()
    output["finished_utc"] = datetime.now(timezone.utc).isoformat()
    output["source_sha256_after"] = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    probe.dump(OUT, output)


if __name__ == "__main__":
    run()
