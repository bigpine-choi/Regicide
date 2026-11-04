"""Independent exhaustive python-chess oracle; test-only chess==1.11.2.
Run: python tests/mate_oracle_tests.py path/to/regicide
"""
import argparse
import json
import queue
import random
import re
import subprocess
import threading
import time
from pathlib import Path
import chess

def force_mate(board, attacker, plies):
    outcome = board.outcome(claim_draw=False)
    if outcome is not None:
        return outcome.termination == chess.Termination.CHECKMATE and outcome.winner == attacker
    defender = board.turn != attacker
    if defender and (board.can_claim_fifty_moves() or board.can_claim_threefold_repetition()):
        return False
    if plies == 0:
        return False
    for move in list(board.legal_moves):
        board.push(move)
        try:
            child = force_mate(board, attacker, plies - 1)
        finally:
            board.pop()
        if child != defender:
            return child
    return defender

def distance(board, bound, root_moves=None):
    attacker = board.turn
    if root_moves is None:
        for n in range(1, bound + 1):
            if force_mate(board, attacker, 2*n-1):
                return n
    elif board.outcome(claim_draw=False) is None:
        for n in range(1, bound + 1):
            for token in root_moves:
                move = chess.Move.from_uci(token)
                assert move in board.legal_moves
                board.push(move)
                try:
                    won = force_mate(board, attacker, 2*n-2)
                finally:
                    board.pop()
                if won:
                    return n
    return None

class Uci:
    def __init__(self, binary):
        self.proc = subprocess.Popen([binary, "uci"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self.reader, daemon=True)
        self.thread.start()
        self.send("uci")
        self.until("uciok")
        self.send("isready")
        self.until("readyok")

    def reader(self):
        for line in self.proc.stdout:
            self.queue.put(line.rstrip())
        self.queue.put(None)

    def send(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def until(self, prefix, timeout=40):
        deadline = time.monotonic() + timeout
        lines = []
        while True:
            try:
                line = self.queue.get(timeout=max(0.001, deadline-time.monotonic()))
            except queue.Empty as error:
                raise AssertionError("UCI timeout waiting for " + prefix) from error
            assert line is not None, ("early EOF", lines)
            lines.append(line)
            if line.startswith(prefix):
                return lines
            assert time.monotonic() < deadline, lines

    def position(self, board):
        command = "position fen " + board.root().fen(en_passant="fen")
        if board.move_stack:
            command += " moves " + " ".join(move.uci() for move in board.move_stack)
        self.send(command)

    def close(self):
        if self.proc.poll() is None:
            self.send("quit")
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.thread.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()

def status(lines):
    values = [re.search(r"\bmate_status ([a-z_]+)", line) for line in lines]
    values = [match.group(1) for match in values if match]
    assert values, ("missing explicit proof status", lines)
    return values[-1]

def validate(uci, board, bound, label, root_moves=None):
    assert board.is_valid(), (label, board.status())
    expected = distance(board, bound, root_moves)
    uci.position(board)
    command = "go mate %d nodes 2000000 movetime 30000" % bound
    if root_moves is not None:
        command += " searchmoves " + " ".join(root_moves)
    uci.send(command)
    lines = uci.until("bestmove ")
    actual = status(lines)
    wanted = "proven" if expected is not None else "no_mate_within_bound"
    assert actual == wanted, (label, board.fen(), expected, actual, lines)
    scores = [re.search(r"\bscore mate (-?\d+)", line) for line in lines]
    scores = [int(match.group(1)) for match in scores if match]
    pv = []
    if expected is None:
        assert not scores and not any(" pv " in line for line in lines), (label, lines)
    else:
        assert scores and scores[-1] == expected, (label, expected, lines)
        info = [line for line in lines if " score mate " in line and " pv " in line]
        assert info, (label, "missing proof PV")
        pv = info[-1].split(" pv ", 1)[1].split()
        witness = board.copy(stack=True)
        attacker = witness.turn
        assert len(pv) == 2*expected-1, (label, expected, pv)
        for token in pv:
            assert witness.outcome(claim_draw=False) is None, (label, "PV past terminal")
            if witness.turn != attacker:
                assert not witness.can_claim_fifty_moves(), (label, "fifty claim")
                assert not witness.can_claim_threefold_repetition(), (label, "repetition claim")
            move = chess.Move.from_uci(token)
            assert move in witness.legal_moves, (label, token, witness.fen())
            witness.push(move)
        assert witness.is_checkmate() and witness.turn != attacker, (label, witness.fen(), pv)
    summary = next(line for line in lines if line.startswith("info string mate_status "))
    return {"label": label, "fen": board.fen(en_passant="fen"), "history": [m.uci() for m in board.move_stack],
            "bound": bound, "root_moves": root_moves, "expected_mate": expected, "status": actual, "pv": pv, "summary": summary}

def cases():
    result = [
        ("M1", chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1"), 2),
        ("stalemate", chess.Board("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1"), 2),
        ("already_mated", chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1"), 2),
        ("quiet_M2", chess.Board("8/7k/5K2/8/8/8/Q7/8 w - - 0 1"), 2),
        ("en_passant", chess.Board("1N1rkr2/5p2/8/3pP3/8/8/8/K3R3 w - d6 0 1"), 1),
        ("castling", chess.Board("4rkr1/4p1p1/8/8/8/8/8/4K2R w K - 0 1"), 1),
        ("promotion", chess.Board("7k/5P2/6K1/8/8/8/8/8 w - - 0 1"), 1)]
    rng = random.Random(0x4D16)
    quotas = {1: 3, 2: 3, None: 6}
    found = {1: 0, 2: 0, None: 0}
    attempts = 0
    while any(found[key] < quotas[key] for key in quotas):
        attempts += 1
        assert attempts <= 50000, ("fixture quota not met", found)
        board = chess.Board.empty()
        bk = rng.choice([chess.A1, chess.H1, chess.A8, chess.H8])
        wk = rng.choice([sq for sq in chess.SQUARES if 2 <= chess.square_distance(sq, bk) <= 3])
        major = rng.choice([sq for sq in chess.SQUARES if sq not in (wk, bk)])
        board.set_piece_at(wk, chess.Piece(chess.KING, chess.WHITE))
        board.set_piece_at(bk, chess.Piece(chess.KING, chess.BLACK))
        board.set_piece_at(major, chess.Piece(rng.choice([chess.QUEEN, chess.ROOK]), chess.WHITE))
        board.turn = chess.WHITE
        if not board.is_valid() or board.outcome(claim_draw=False) is not None:
            continue
        n = distance(board, 2)
        if found[n] < quotas[n]:
            found[n] += 1
            result.append(("seeded_%s_%d" % (n, found[n]), board, 2))
    m1 = result[0][1]
    m2 = result[3][1]
    for halfmove in (100, 149, 150):
        board = m1.copy()
        board.halfmove_clock = halfmove
        result.append(("M1_clock_%d" % halfmove, board, 2))
    for halfmove in (98, 99):
        board = m2.copy()
        board.halfmove_clock = halfmove
        result.append(("M2_clock_%d" % halfmove, board, 2))
    repeated = chess.Board("7k/8/5K2/8/8/8/Q7/8 w - - 0 1")
    for move in ["a2a3", "h8h7", "a3a2", "h7h8"]*2:
        repeated.push_uci(move)
    result.append(("attacker_threefold_claim_optional", repeated, 2))
    branch3 = chess.Board("8/7k/8/5K2/8/8/Q7/8 w - - 0 1")
    result.append(("cooperative_M2_counterexample", branch3.copy(), 2, ["a2g2"]))
    result.append(("M3_longest_defense", branch3.copy(), 3, ["a2g2"]))
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    uci = Uci(str(Path(args.binary).resolve()))
    records = []
    try:
        for case in cases():
            label, board, bound = case[:3]
            root_moves = case[3] if len(case) == 4 else None
            record = validate(uci, board, bound, label, root_moves)
            records.append(record)
            print("PASS oracle:", label, record["status"], flush=True)
        uci.position(chess.Board())
        uci.send("go mate 16 nodes 1")
        limited = uci.until("bestmove ")
        assert status(limited) == "unknown" and not any(" score mate " in line for line in limited)
        uci.send("go mate 16 nodes 1000000000")
        uci.send("isready")
        ready = uci.until("readyok", timeout=3)
        uci.send("stop")
        stopped = ready + uci.until("bestmove ", timeout=3)
        assert status(stopped) == "unknown" and not any(" score mate " in line for line in stopped)
        uci.position(chess.Board())
        uci.send("go depth 2")
        normal = uci.until("bestmove ")
        assert chess.Move.from_uci(normal[-1].split()[1]) in chess.Board().legal_moves
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"seed": 0x4D16, "chess_version": chess.__version__,
                "positions": records, "checks": ["node_limit", "stop", "normal_reuse"]}, indent=2), encoding="utf-8")
        print("PASS independent oracle: %d positions + node/stop/normal checks; chess=%s" %
              (len(records), chess.__version__))
    finally:
        uci.close()

if __name__ == "__main__":
    main()
