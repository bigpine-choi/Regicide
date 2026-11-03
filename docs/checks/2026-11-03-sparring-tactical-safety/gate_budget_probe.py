"""Sequential verification budgets for the recorded Nxa7 baseline."""
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

record = json.loads(next(default_game_directory().glob("*4c98bec3ef0a.json")).read_text(encoding="utf-8"))
binary = Path(record["binary"])
board = chess.Board(record["initial_fen"])
for uci in record["uci_moves"][:16]:
    board.push_uci(uci)
poison = chess.Move.from_uci("b5a7")
out = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
       "fen": board.fen(en_passant="fen"), "options": {"UsePieceActivity": True},
       "budgets_seconds": [2.7, 3.6, 2.7], "runs": []}
for repeat in range(2):
    engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=5)
    try:
        engine.configure(out["options"])
        child = board.copy(stack=True)
        child.push(poison)
        stages = []
        for stage in range(3):
            allowed = None
            position = child
            if stage == 1:
                position = board
                allowed = [m for m in board.legal_moves if m != poison]
            started = time.monotonic()
            result = engine.analyse(position, chess.engine.Limit(time=out["budgets_seconds"][stage]),
                                    root_moves=allowed, game=object())
            row = {"stage": stage, "depth": result.get("depth"),
                   "elapsed": time.monotonic() - started, "cp_white": result["score"].white().score(),
                   "pv_uci": [m.uci() for m in result.get("pv", [])]}
            stages.append(row)
            print(repeat, json.dumps(row), flush=True)
            if stage == 1:
                child = board.copy(stack=True)
                child.push(result["pv"][0])
        out["runs"].append(stages)
        Path(__file__).with_suffix(".json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    finally:
        engine.quit()
