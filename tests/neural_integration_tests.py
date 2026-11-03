"""Usage: python tests/neural_integration_tests.py candidate [HEAD-baseline]."""
import json
from itertools import product
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
parity_pairs = 0

def analyse(engine, board):
 info = engine.analyse(board, chess.engine.Limit(depth=2))
 assert info["depth"] == 2 and info["pv"][0] in board.legal_moves
 return {"score": str(info["score"]), "pv": [m.uci() for m in info["pv"]],
         "nodes": info["nodes"], "depth": info["depth"], "seldepth": info["seldepth"]}

def run(exe, extra, fen, depth=3):
 output = subprocess.check_output([str(exe), "search", str(depth), "--fen", fen, *extra],
                                  text=True, timeout=30)
 return {k: v for line in output.splitlines() if ": " in line
         for k, v in [line.split(": ", 1)] if k != "time_ms"}

with chess.engine.SimpleEngine.popen_uci([str(candidate), "uci"]) as engine:
 assert engine.options["UseNeuralEvaluation"].default is False
 assert engine.options["UseIncrementalNeuralEvaluation"].default is True
 for fen in positions:
  board = chess.Board(fen)
  for neural, incremental in ((False, True), (True, True), (True, False), (True, True), (False, False)):
   engine.configure({"UseNeuralEvaluation": neural,
                     "UseIncrementalNeuralEvaluation": incremental})
   rows.append({"fen": fen, "neural": neural, "incremental": incremental,
                "result": analyse(engine, board)})
  assert rows[-1]["result"] == rows[-5]["result"], "classical results changed after neural search"
  assert rows[-2]["result"] == rows[-3]["result"] == rows[-4]["result"], "neural refresh mode changed result"
  parity_pairs += 2
 # Exercise all quiescence/PVS/validated-move combinations through the public
 # protocol. Reusing one process also checks option changes and table generations.
 engine.configure({"UseNeuralEvaluation": True, "Hash": 1})
 for quiescence, pvs, fast in product((False, True), repeat=3):
  engine.configure({"UseQuiescence": quiescence, "PVS": pvs, "UseFastMoves": fast})
  for fen in positions:
   board = chess.Board(fen)
   engine.configure({"UseIncrementalNeuralEvaluation": True})
   incremental = analyse(engine, board)
   engine.configure({"UseIncrementalNeuralEvaluation": False})
   full = analyse(engine, board)
   assert incremental == full, ("UCI search graph mismatch", fen, quiescence, pvs, fast)
   parity_pairs += 1
reply = subprocess.run([str(candidate), "uci"], input=("uci\n"
 "setoption name UseNeuralEvaluation value invalid\n"
 "setoption name UseIncrementalNeuralEvaluation value invalid\nisready\nquit\n"),
 capture_output=True, text=True, timeout=10, check=True).stdout
assert reply.count("check option requires true or false") == 2 and "readyok" in reply
cli_pairs = 0
for fen in positions:
 assert run(candidate, ["--neural"], fen) == run(candidate, ["--neural", "--neural-full-refresh"], fen), "CLI neural parity"
 assert run(candidate, [], fen) == run(candidate, ["--neural-full-refresh"], fen), "inactive CLI flag changed classical search"
 cli_pairs += 2
if len(sys.argv) > 2:
 baseline = Path(sys.argv[2]).resolve()
 for fen in positions:
  for flags in ([], ["--no-position"]):
   assert run(candidate, flags, fen) == run(baseline, flags, fen), "default/material regression"
print(json.dumps({"uci_rows": rows, "neural_parity_pairs": parity_pairs,
                  "cli_pairs": cli_pairs, "baseline_pairs": 8 if len(sys.argv) > 2 else 0}, indent=2))
