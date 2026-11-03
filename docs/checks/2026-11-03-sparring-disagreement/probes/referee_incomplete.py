"""Read-only capture of completed referee packets for two incomplete cases."""
import json
import hashlib
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import chess
from sparring_referee import SparringReferee
from sparring_quality_native_integration import cases, SAVED


def run():
    binary = json.loads((SAVED / "1700-white/metadata.json").read_text(encoding="utf-8"))["config"]["stockfish"]
    output = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "source_sha256": hashlib.sha256((ROOT / "scripts/sparring_referee.py").read_bytes()).hexdigest(),
              "cases": []}
    original = SparringReferee._analyse
    try:
        for case in cases():
            if case["id"] not in ("1320-black-42", "1320-white-53"):
                continue
            board = case["board"]
            rows = []
            def captured(engine, board, control, seconds, **kwargs):
                result = original(engine, board, control, seconds, **kwargs)
                returned, packets, invalid = result
                rows.append({"fen": board.fen(en_passant="fen"), "seconds": seconds,
                             "returned": returned.uci(), "packets": packets, "invalid_depths": sorted(invalid)})
                return result
            SparringReferee._analyse = staticmethod(captured)
            referee = SparringReferee(binary)
            record = {"id": case["id"], "assessments": [], "analyses": rows}
            sequence = [case["original_uci"]]
            if case["id"] == "1320-black-42":
                sequence += ["c7c6", "d6d5"]
            try:
                with referee.quality_session():
                    for i, uci in enumerate(sequence):
                        assessment = referee.assess_move_quality(game_id=case["id"], revision=len(board.move_stack),
                            board=board.copy(stack=True), selected_move=chess.Move.from_uci(uci),
                            budget_ms=2150 if not i else 650)
                        record["assessments"].append(assessment)
            finally:
                referee.close()
            output["cases"].append(record)
            print(json.dumps({"case": case["id"], "assessments": [{k:a.get(k) for k in ("selected_uci", "status", "reason", "engine_reused")} for a in record["assessments"]],
                              "analyses": [{"returned": r["returned"], "depths": sorted(r["packets"]), "invalid": r["invalid_depths"],
                                            "matching": [d for d,p in r["packets"].items() if r["returned"] in p]} for r in rows]}), flush=True)
    finally:
        SparringReferee._analyse = staticmethod(original)
    output["source_sha256_after"] = hashlib.sha256((ROOT / "scripts/sparring_referee.py").read_bytes()).hexdigest()
    (Path(__file__).resolve().parent / "activity-enabled/referee-incomplete.json").write_text(json.dumps(output, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    run()
