"""Usage: python tests/neural_integration_tests.py candidate [HEAD-baseline]."""
import json
from pathlib import Path
import subprocess
import sys
import chess
import chess.engine

candidate = Path(sys.argv[1]).resolve()
positions = [chess.STARTING_FEN,
 "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
 "7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1",
 "7k/P7/8/8/8/8/8/6K1 w - - 0 1"]
rows = []
with chess.engine.SimpleEngine.popen_uci([str(candidate), "uci"]) as engine:
 assert engine.options["UseNeuralEvaluation"].default is False
 for fen in positions:
  board = chess.Board(fen)
  for neural in (False, True, False):
   engine.configure({"UseNeuralEvaluation": neural})
   info = engine.analyse(board, chess.engine.Limit(depth=2))
   assert info["depth"] == 2 and info["pv"][0] in board.legal_moves
   rows.append({"fen": fen, "neural": neural, "score": str(info["score"]),
                "pv": [m.uci() for m in info["pv"]], "nodes": info["nodes"]})
  assert rows[-1] == rows[-3], "classical results changed after neural search"
reply = subprocess.run([str(candidate), "uci"], input="uci\nsetoption name UseNeuralEvaluation value invalid\nisready\nquit\n", capture_output=True, text=True, timeout=10, check=True).stdout
assert "check option requires true or false" in reply and "readyok" in reply
if len(sys.argv) > 2:
 baseline = Path(sys.argv[2]).resolve()
 for fen in positions:
  for flags in ([], ["--no-position"]):
   def run(exe, extra):
    output = subprocess.check_output([str(exe), "search", "3", "--fen", fen, *extra], text=True, timeout=30)
    return {k: v for line in output.splitlines() if ": " in line
            for k, v in [line.split(": ", 1)] if k != "time_ms"}
   assert run(candidate, flags) == run(baseline, flags), "default/material regression"
print(json.dumps({"uci_rows": rows, "baseline_pairs": 8 if len(sys.argv) > 2 else 0}, indent=2))
