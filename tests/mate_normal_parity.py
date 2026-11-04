"""Compare unchanged normal UCI search. Usage: SCRIPT BASELINE PROTOTYPE [REPORT]."""
import json
import queue
import subprocess
import sys
import threading
import time

CASES = [
    ("start_depth3", "position startpos", "go depth 3"),
    ("ruy_depth3", "position startpos moves e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6", "go depth 3"),
    ("castling_depth3", "position fen r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1", "go depth 3"),
    ("endgame_depth4", "position fen 8/8/4k3/8/4K3/8/4P3/8 w - - 0 1", "go depth 4"),
    ("start_nodes2000", "position startpos", "go nodes 2000"),
    ("normal_after_mate", "position startpos", "go depth 3"),
]

class Engine:
    def __init__(self, binary):
        self.process = subprocess.Popen([binary, "uci"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
        self.lines = queue.Queue()
        self.thread = threading.Thread(target=self.reader, daemon=True)
        self.thread.start()
        self.send("uci")
        self.until(lambda line: line == "uciok")
        self.send("setoption name Move Overhead value 0")
        self.send("isready")
        self.until(lambda line: line == "readyok")

    def reader(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\r\n"))
        self.lines.put(None)

    def send(self, line):
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        result = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(result[-10:])
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(result[-10:]) from None
            if line is None:
                raise RuntimeError(f"Engine exited: {result[-10:]}")
            result.append(line)
            if predicate(line):
                return result

    def search(self, position, command):
        self.send(position)
        started = time.perf_counter()
        self.send(command)
        lines = self.until(lambda line: line.startswith("bestmove "))
        elapsed_ms = (time.perf_counter() - started)*1000
        info = [l for l in lines if l.startswith("info depth ") and " score " in l]
        if not info:
            raise RuntimeError(f"No normal-search score: {lines}")
        tokens = info[-1].split()
        score = tokens.index("score")
        value = {"depth": int(tokens[tokens.index("depth")+1]),
                 "score_type": tokens[score+1], "score": int(tokens[score+2]),
                 "nodes": int(tokens[tokens.index("nodes")+1]),
                 "pv": tokens[tokens.index("pv")+1:] if "pv" in tokens else [],
                 "bestmove": lines[-1].split()[1]}
        return {"result": value, "wall_ms": round(elapsed_ms, 3)}

    def prime_mate(self):
        self.send("position fen 7k/5Q2/6K1/8/8/8/8/8 w - - 0 1")
        self.send("go mate 1 nodes 10000")
        return self.until(lambda line: line.startswith("bestmove "))

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=5)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        self.thread.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()

def main():
    baseline, prototype = sys.argv[1:3]
    report = {"baseline": baseline, "prototype": prototype,
              "timing_note": "Single-run wall times are diagnostic, not a speed or Elo claim.",
              "cases": []}
    for name, position, command in CASES:
        engines = [Engine(baseline), Engine(prototype)]
        try:
            priming = None
            if name == "normal_after_mate":
                priming = {"baseline": engines[0].prime_mate(),
                           "prototype": engines[1].prime_mate()}
            before = engines[0].search(position, command)
            after = engines[1].search(position, command)
            case = {"name": name, "position": position, "command": command,
                    "baseline": before, "prototype": after,
                    "equal": before["result"] == after["result"]}
            if priming is not None:
                case["mate_priming"] = priming
            report["cases"].append(case)
            print("PARITY", name, case["equal"], flush=True)
        finally:
            for engine in engines:
                engine.close()
    report["all_equal"] = all(case["equal"] for case in report["cases"])
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
    print("normal-search parity: %d/%d" %
          (sum(case["equal"] for case in report["cases"]), len(report["cases"])), flush=True)
    return 0 if report["all_equal"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
