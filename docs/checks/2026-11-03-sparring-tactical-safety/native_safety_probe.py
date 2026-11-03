"""Run actual native tactical safety verifier against recorded game fixture."""
import hashlib
import json
from pathlib import Path
import sys
import time
import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from game_archive import default_game_directory
import sparring_safety

record = json.loads(next(default_game_directory().glob("*4c98bec3ef0a.json")).read_text(encoding="utf-8"))
binary = Path(record["binary"])
board = chess.Board(record["initial_fen"])
for uci in record["uci_moves"][:16]:
    board.push_uci(uci)
poison = chess.Move.from_uci("b5a7")
budget = int(sys.argv[1]) if len(sys.argv) > 1 else 12000
source = ROOT / "scripts/sparring_safety.py"
out = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
       "safety_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
       "fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
       "options": {"UsePieceActivity": True}, "budget_ms": budget}
engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=5)
try:
    engine.configure(out["options"])
    def search(ms, *, position=None, root_moves=None, publish=False, fresh=True):
        target = position if position is not None else board
        packets, latest = {}, {}
        with engine.analysis(target, chess.engine.Limit(time=ms / 1000), root_moves=root_moves,
                             game=object() if fresh else None) as stream:
            for info in stream:
                if type(info.get("depth")) is int and "score" in info:
                    latest = dict(info)
                    packets[info["depth"]] = latest
            reply = stream.wait()
        return reply, latest, packets
    replacement, receipt, discarded = sparring_safety.verify(board, poison, search, engine.close, budget_ms=budget)
    out.update(receipt=receipt, discarded=discarded,
               replacement=replacement[0].move.uci() if replacement else None,
               source_unchanged=out["safety_source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest())
    Path(__file__).with_name(f"native_safety_probe_{budget}.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
finally:
    try:
        engine.quit()
    except Exception:
        engine.close()
