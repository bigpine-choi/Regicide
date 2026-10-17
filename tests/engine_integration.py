"""Independently exercise Regicide's UCI process; python-chess is test-only.

Run: python tests/engine_integration.py path/to/regicide[.exe]
The generous timeouts detect hung protocol operations, not playing strength or NPS.
"""

import argparse
import queue
import subprocess
import threading
import time
from pathlib import Path

import chess
import chess.engine


TIMEOUT = 6.0
MATE_FEN = "7k/5K2/6Q1/8/8/8/8/8 w - - 0 1"
CAPTURE_FEN = "7k/8/8/3q4/8/8/8/3RK3 w - - 0 1"


class UciProcess:
    """A real pipe transport, including interleaved output and process shutdown."""

    def __init__(self, binary):
        self.process = subprocess.Popen(
            [str(binary), "uci"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )
        self.lines = queue.Queue()
        self.transcript = []
        self.errors = []
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.lines.put(None)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.errors.append(line.rstrip("\r\n"))

    def __enter__(self):
        try:
            self.send("uci")
            output = self.until(lambda line: line == "uciok")
            assert any(line.startswith("id name ") for line in output), output
            self.ready()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=TIMEOUT)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=TIMEOUT)
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        self.stdout_thread.join(timeout=1)
        self.stderr_thread.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()

    def send(self, command):
        self.transcript.append("> " + command)
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def until(self, predicate, timeout=TIMEOUT):
        output = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"UCI response timeout: {self.transcript[-20:]} "
                                     f"stderr={self.errors[-10:]}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as error:
                raise AssertionError(f"UCI response timeout: {self.transcript[-20:]} "
                                     f"stderr={self.errors[-10:]}") from error
            if line is None:
                raise AssertionError(f"unexpected UCI EOF: {self.transcript[-20:]} "
                                     f"stderr={self.errors[-10:]}")
            self.transcript.append("< " + line)
            output.append(line)
            if predicate(line):
                return output

    def ready(self, allow_bestmove=False):
        self.send("isready")
        output = self.until(lambda line: line == "readyok")
        if not allow_bestmove:
            assert not any(line.startswith("bestmove ") for line in output), output
        return output

    def position(self, board):
        self.send("position fen " + board.fen(en_passant="fen"))

    def bestmove(self):
        output = self.until(lambda line: line.startswith("bestmove "))
        fields = output[-1].split()
        assert len(fields) in (2, 4), output[-1]
        return fields[1], output

    def search(self, board, go):
        self.position(board)
        self.send(go)
        move, output = self.bestmove()
        assert chess.Move.from_uci(move) in board.legal_moves, (board.fen(), go, output)
        assert_legal_pvs(board, output)
        self.ready()
        return move, output

    def await_search_started(self):
        output = self.until(lambda line: line.startswith("info ") and " depth " in line)
        assert not any(line.startswith("bestmove ") for line in output), output


def info_values(output, key):
    values = []
    for line in output:
        if line.startswith("info ") and not line.startswith("info string "):
            fields = line.split()
            if key in fields:
                values.append(int(fields[fields.index(key) + 1]))
    return values


def assert_legal_pvs(board, output):
    for line in output:
        if line.startswith("info ") and " pv " in line:
            continuation = board.copy()
            for token in line.split(" pv ", 1)[1].split():
                move = chess.Move.from_uci(token)
                assert move in continuation.legal_moves, (board.fen(), line, token)
                continuation.push(move)


def test_python_chess(binary):
    """A separate, established UCI client must be able to use this engine."""
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        assert "Regicide" in engine.id.get("name", ""), engine.id
        engine.ping()
        for root, expect_mate in [(chess.Board(MATE_FEN), True),
                                  (chess.Board(CAPTURE_FEN), False)]:
            for board in [root, root.mirror()]:
                before = board.fen()
                result = engine.play(board, chess.engine.Limit(depth=2), info=chess.engine.INFO_ALL)
                assert result.move in board.legal_moves, (before, result)
                assert board.fen() == before, "client board was unexpectedly changed"
                if expect_mate:
                    after = board.copy()
                    after.push(result.move)
                    assert after.is_checkmate(), (before, result)
                else:
                    victim = board.piece_at(result.move.to_square)
                    assert victim and victim.piece_type == chess.QUEEN, (before, result)
        board = chess.Board()
        for move in ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]:
            board.push_uci(move)
        result = engine.play(board, chess.engine.Limit(depth=2))
        assert result.move in board.legal_moves, result
        engine.ping()


def test_history_and_terminal(binary):
    special = [
        ("8/8/8/8/8/5Q2/7R/2k1K2R w K - 0 1", "e1g1", chess.Termination.CHECKMATE),
        ("k7/2Q5/6K1/3pP3/8/8/8/8 w - d6 0 1", "e5d6", chess.Termination.STALEMATE),
        ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n", chess.Termination.INSUFFICIENT_MATERIAL),
    ]
    with UciProcess(binary) as engine:
        for fen, move_uci, expected in special:
            original = chess.Board(fen)
            move = chess.Move.from_uci(move_uci)
            for mirror in [False, True]:
                board = original.mirror() if mirror else original.copy()
                played = (chess.Move(chess.square_mirror(move.from_square),
                                     chess.square_mirror(move.to_square), promotion=move.promotion)
                          if mirror else move)
                assert board.is_valid() and board.outcome() is None, board.fen()
                root = board.fen(en_passant="fen")
                board.push(played)
                assert board.outcome().termination == expected, board.fen()
                engine.send(f"position fen {root} moves {played.uci()}")
                engine.send("go depth 2")
                best, output = engine.bestmove()
                assert best == "0000", (root, played, output)
                engine.ready()

        # This is an automatic fivefold draw only when the complete move history
        # survives position parsing. Its final piece placement is the start board.
        cycle = ["g1f3", "g8f6", "f3g1", "f6g8"] * 4
        board = chess.Board()
        for move in cycle:
            board.push_uci(move)
        assert board.outcome().termination == chess.Termination.FIVEFOLD_REPETITION
        engine.send("position startpos moves " + " ".join(cycle))
        engine.send("go depth 2")
        best, output = engine.bestmove()
        assert best == "0000", output
        engine.ready()

        # Terminal roots without history also require the UCI null move.
        for fen in ["7k/6Q1/5K2/8/8/8/8/8 b - - 0 1",
                    "7k/5K2/6Q1/8/8/8/8/8 b - - 0 1",
                    "4k3/8/8/8/8/8/8/4K3 w - - 0 1"]:
            board = chess.Board(fen)
            assert board.is_valid() and board.is_game_over(), fen
            engine.position(board)
            engine.send("go depth 2")
            best, output = engine.bestmove()
            assert best == "0000", output
            engine.ready()


def test_limits(binary):
    board = chess.Board()
    with UciProcess(binary) as engine:
        _, output = engine.search(board, "go depth 2")
        depths = info_values(output, "depth")
        assert depths and max(depths) == 2, output

        for command, max_nodes in [("go nodes 100", 100),
                                   ("go depth 5 nodes 60 movetime 5000", 60)]:
            _, output = engine.search(board, command)
            nodes = info_values(output, "nodes")
            assert nodes and max(nodes) <= max_nodes, (command, output)

        for position, command in [
            (board, "go movetime 80"),
            (board, "go wtime 160 btime 160 winc 0 binc 0 movestogo 1"),
            (board, "go wtime 0 btime 0 winc 0 binc 0"),
            (chess.Board("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"),
             "go wtime 160 btime 160 winc 20 binc 20 movestogo 20"),
        ]:
            start = time.monotonic()
            engine.search(position, command)
            assert time.monotonic() - start < TIMEOUT, command


def test_interleaving(binary):
    board = chess.Board()
    with UciProcess(binary) as engine:
        engine.position(board)
        engine.send("go infinite")
        engine.await_search_started()
        engine.ready()  # isready must remain responsive while search is running.
        engine.send("stop")
        best, output = engine.bestmove()
        assert chess.Move.from_uci(best) in board.legal_moves, output
        assert_legal_pvs(board, output)
        engine.ready()  # No duplicate bestmove may follow the stop result.

        # A stop that arrives before a completed iteration still has a legal fallback.
        engine.send("go infinite")
        engine.send("stop")
        best, output = engine.bestmove()
        assert chess.Move.from_uci(best) in board.legal_moves, output
        assert_legal_pvs(board, output)
        engine.ready()

        # Replacing an active search may finish its old bestmove before readyok;
        # after that barrier, only the replacement position may produce results.
        engine.send("go infinite")
        engine.await_search_started()
        terminal = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
        engine.position(terminal)
        engine.ready(allow_bestmove=True)
        engine.send("go depth 2")
        best, output = engine.bestmove()
        assert best == "0000", output
        engine.ready()

        # ucinewgame must also safely interrupt and clear the prior search.
        engine.position(board)
        engine.send("go infinite")
        engine.await_search_started()
        engine.send("ucinewgame")
        engine.ready(allow_bestmove=True)
        engine.search(chess.Board(MATE_FEN), "go depth 2")

        # Terminal searches finish computation immediately and then wait for
        # stop. Repeated immediate stops exercise that condition-variable edge.
        engine.position(terminal)
        for _ in range(30):
            engine.send("go infinite")
            engine.send("stop")
            best, output = engine.bestmove()
            assert best == "0000", output
            engine.ready()


def test_bad_commands(binary):
    board = chess.Board(MATE_FEN)
    with UciProcess(binary) as engine:
        engine.position(board)
        for command in ["position fen not-a-fen",
                        "position startpos moves e2e4 e7e5 g1f3 invalid",
                        "position startpos moves e2e5",
                        "position startpos moves e2e4 e7e5 e1g1"]:
            engine.send(command)
            engine.ready()
            engine.send("go depth 2")
            best, output = engine.bestmove()
            move = chess.Move.from_uci(best)
            assert move in board.legal_moves, (command, output)
            after = board.copy()
            after.push(move)
            assert after.is_checkmate(), (command, output)
            engine.ready()

        engine.send("setoption name UnknownTestOption value ignored")
        engine.send("unknown_command")
        engine.ready()
        for command in ["go searchmoves g6g7", "go ponder", "go mate 2", "go depth banana",
                        "go depth", "go depth 65", "go nodes -1", "go movetime nope"]:
            engine.send(command)
            best, output = engine.bestmove()
            assert best == "0000", (command, output)
            assert any(line.startswith("info string ") for line in output), (command, output)
            engine.ready()
        engine.send("go depth 2")
        best, output = engine.bestmove()
        move = chess.Move.from_uci(best)
        assert move in board.legal_moves, output
        board.push(move)
        assert board.is_checkmate(), output
        engine.ready()


def test_shutdown(binary):
    for end in ["quit", "eof"]:
        with UciProcess(binary) as engine:
            engine.send("position startpos")
            engine.send("go infinite")
            engine.await_search_started()
            start = time.monotonic()
            if end == "quit":
                engine.send("quit")
            else:
                engine.process.stdin.close()
            try:
                code = engine.process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise AssertionError(f"{end} failed to interrupt infinite search") from error
            assert code == 0, (end, code, engine.errors)
            assert time.monotonic() - start < TIMEOUT, end


def test_evaluation_options(binary):
    # The same independently verified poisoned capture must change with qsearch,
    # not with a position-table adjustment or an arbitrary move blacklist.
    board = chess.Board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1")
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        for option in ["UsePositionalEvaluation", "UseQuiescence"]:
            assert option in engine.options and engine.options[option].default is True, engine.options
        engine.configure({"UsePositionalEvaluation": False, "UseQuiescence": False})
        shallow = engine.play(board, chess.engine.Limit(depth=1))
        assert shallow.move == chess.Move.from_uci("d1d5"), shallow
        engine.configure({"UseQuiescence": True})
        stable = engine.play(board, chess.engine.Limit(depth=1), info=chess.engine.INFO_ALL)
        assert stable.move in board.legal_moves and stable.move != shallow.move, stable
        assert stable.info.get("seldepth", 0) >= 2, stable
        engine.configure({"UsePositionalEvaluation": True, "UseQuiescence": False})
        developed = engine.play(chess.Board(), chess.engine.Limit(depth=1))
        assert developed.move in {chess.Move.from_uci(m) for m in ["b1c3", "g1f3"]}, developed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    checks = [test_python_chess, test_history_and_terminal, test_limits,
              test_interleaving, test_bad_commands, test_shutdown, test_evaluation_options]
    for check in checks:
        check(binary)
        print("PASS UCI: " + check.__name__.removeprefix("test_"), flush=True)
    print(f"PASS engine integration: {len(checks)} groups; chess={chess.__version__}; "
          "legal moves, tactical choices, special history, limits, concurrency, shutdown")


if __name__ == "__main__":
    main()
