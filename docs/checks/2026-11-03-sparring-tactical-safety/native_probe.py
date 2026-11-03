"""Bounded native diagnosis of the recorded 9.Nxa7 poisoned pawn."""
import hashlib
import json
from pathlib import Path
import platform
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
child = board.copy(stack=True)
child.push(poison)
output = {
    "binary": str(binary), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "platform": platform.platform(), "processor": platform.processor(),
    "python": sys.version, "options": {"UsePieceActivity": True},
    "fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
    "recorded_baseline": next(d["baseline"] for d in record["sparring_play"]["decisions"] if d["ply"] == 17),
    "scope": "Fresh processes per case; limited native diagnosis, not strength or win-rate evidence.",
    "cases": [],
}
cases = [("root_3s", board, 3, None), ("root_6s", board, 6, None),
         ("root_10s", board, 10, None), ("child_3s", child, 3, None),
         ("child_6s", child, 6, None),
         ("root_exclude_3s", board, 3, [m for m in board.legal_moves if m != poison]),
         ("root_exclude_6s", board, 6, [m for m in board.legal_moves if m != poison])]
if "--quick" in sys.argv:
    cases = [("root_depth5", board, 3, None), ("child_1s", child, 1, None),
             ("child_2s", child, 2, None)]
for name, position, seconds, allowed in cases:
    engine = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=5)
    try:
        engine.configure(output["options"])
        started = time.monotonic()
        info = engine.analyse(position, chess.engine.Limit(time=seconds, depth=5 if name == "root_depth5" else None), root_moves=allowed)
        row = {"name": name, "seconds": seconds, "elapsed_seconds": time.monotonic() - started,
               "depth": info.get("depth"), "nodes": info.get("nodes"),
               "cp_white": info["score"].white().score(), "mate_white": info["score"].white().mate(),
               "pv_uci": [m.uci() for m in info.get("pv", [])]}
        output["cases"].append(row)
        print(json.dumps(row), flush=True)
        Path(__file__).with_name("native_probe_quick.json" if "--quick" in sys.argv else "native_probe.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    finally:
        engine.quit()
