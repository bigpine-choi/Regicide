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


def configure(engine, **options):
    for name, value in options.items():
        text = str(value).lower() if isinstance(value, bool) else str(value)
        engine.send(f"setoption name {name} value {text}")
        output = engine.ready()
        assert not any(line.startswith("info string ") for line in output), (name, value, output)


def tt_statistics(output):
    records = []
    for line in output:
        if line.startswith("info string tt "):
            fields = line.split()[3:]
            assert len(fields) % 2 == 0, line
            record = dict(zip(fields[::2], map(int, fields[1::2])))
            assert len(record) * 2 == len(fields), line
            assert all(value >= 0 for value in record.values()), line
            probes, hits, cutoffs, stores = (record[key] for key in ["probes", "hits", "cutoffs", "stores"])
            assert 0 <= cutoffs <= hits <= probes and 0 <= stores <= probes, line
            records.append(record)
    assert records, output
    return records


def assert_table_memory(output, allocations=0, reuses=0, hash_mb=None):
    """These are per-search work counters, rather than process RSS estimates."""
    records = tt_statistics(output)
    keys = ["allocations", "reuses", "clears", "bytes"]
    memory = [tuple(record[key] for key in keys) for record in records]
    assert all(item == memory[0] for item in memory), memory
    allocated, reused, cleared, size = memory[-1]
    assert (allocated, reused, cleared) == (allocations, reuses, 0), memory
    if hash_mb is None:
        assert size == 0, memory
    else:
        assert 0 < size <= hash_mb * 1024 * 1024, memory
    return size


def search_fingerprint(move, output):
    """Ignore wall time/allocation lifecycle, retain the exact search graph."""
    progress = [line.split() for line in output
                if line.startswith("info depth ")]
    assert progress, output
    final = progress[-1]
    score_at = final.index("score")
    depth = int(final[final.index("depth") + 1])
    nodes = int(final[final.index("nodes") + 1])
    pv = tuple(final[final.index("pv") + 1:]) if "pv" in final else ()
    counters = [tuple(record[key] for key in ["probes", "hits", "cutoffs", "stores"])
                for record in tt_statistics(output)]
    for earlier, later in zip(counters, counters[1:]):
        assert all(a <= b for a, b in zip(earlier, later)), counters
    return (move, tuple(final[score_at + 1:score_at + 3]), depth,
            nodes, pv, counters[-1])


def search_history(engine, board, go="go depth 3"):
    """Send full known history, unlike a FEN-only analysis request."""
    command = "position fen " + board.root().fen(en_passant="fen")
    if board.move_stack:
        command += " moves " + " ".join(move.uci() for move in board.move_stack)
    engine.send(command)
    engine.send(go)
    move, output = engine.bestmove()
    assert chess.Move.from_uci(move) in board.legal_moves, (board.fen(), output)
    assert_legal_pvs(board, output)
    engine.ready()
    return move, output


def test_python_chess(binary):
    """A separate, established UCI client must be able to use this engine."""
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        assert engine.id.get("name") == "Regicide 0.5.0", engine.id
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
        for option in ["UsePositionalEvaluation", "UseQuiescence", "UseQuietMoveOrdering", "UseQuiescenceChecks"]:
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


def test_transposition_options(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        for name in ["UseTranspositionTable", "UseFastMoves"]:
            option = engine.options.get(name)
            assert option and option.type == "check" and option.default is True, engine.options
        option = engine.options.get("Hash")
        assert option and option.type == "spin", engine.options
        assert (option.default, option.min, option.max) == (16, 1, 256), option
        clear_hash = engine.options.get("Clear Hash")
        assert clear_hash and clear_hash.type == "button", engine.options
        engine.configure({"UseTranspositionTable": False, "UseFastMoves": False, "Hash": 1})
        result = engine.play(chess.Board(), chess.engine.Limit(depth=2))
        assert result.move in chess.Board().legal_moves, result

    with UciProcess(binary) as engine:
        # Both booleans and the advertised memory endpoints must parse. The
        # maximum is reset before searching, so this is not a 256 MiB load test.
        for enabled in [False, True]:
            configure(engine, UseTranspositionTable=enabled, UseFastMoves=enabled)
        for amount in [1, 16, 256]:
            configure(engine, Hash=amount)
        configure(engine, Hash=1, UseTranspositionTable=True, UseFastMoves=True)
        board = chess.Board()
        original = search_fingerprint(*engine.search(board, "go depth 3"))
        for command in [
            "setoption name UseTranspositionTable value yes",
            "setoption name UseTranspositionTable",
            "setoption name UseTranspositionTable value true extra",
            "setoption name UseFastMoves value 1",
            "setoption name UseFastMoves value",
            "setoption name Hash value 0",
            "setoption name Hash value 257",
            "setoption name Hash value -1",
            "setoption name Hash value nope",
            "setoption name Hash",
            "setoption name Hash value 16 extra",
        ]:
            engine.send(command)
            output = engine.ready()
            assert any(line.startswith("info string ") for line in output), (command, output)
            assert not any(line.startswith("bestmove ") for line in output), (command, output)
        after_rejections = search_fingerprint(*engine.search(board, "go depth 3"))
        assert original == after_rejections, (original, after_rejections)


def test_transposition_search(binary):
    fixtures = [
        chess.Board(),
        chess.Board("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"),
        chess.Board("4k3/8/8/3pP3/8/8/8/R3K3 w - d6 0 2"),
        chess.Board(MATE_FEN),
    ]
    saw_hit = False
    with UciProcess(binary) as engine:
        configure(engine, Hash=1)
        for board in fixtures:
            configure(engine, UseTranspositionTable=False, UseFastMoves=True)
            uncached = search_fingerprint(*engine.search(board, "go depth 3"))
            configure(engine, UseTranspositionTable=True)
            cached = search_fingerprint(*engine.search(board, "go depth 3"))
            assert uncached[1] == cached[1], (board.fen(), uncached, cached)
            assert uncached[2] == cached[2] == 3, (uncached, cached)
            assert uncached[-1] == (0, 0, 0, 0), uncached
            assert cached[-1][0] > 0 and cached[-1][3] > 0, cached
            saw_hit |= cached[-1][1] > 0
            configure(engine, UseFastMoves=False)
            checked_moves = search_fingerprint(*engine.search(board, "go depth 3"))
            assert checked_moves == cached, (board.fen(), cached, checked_moves)
    assert saw_hit, "TT was enabled but never reused any entry"


def test_transposition_isolation(binary):
    # Each go starts a fresh logical generation. Repeatability includes node
    # counts and TT counters: stale entries accidentally reused change these.
    board = chess.Board()
    with UciProcess(binary) as engine:
        configure(engine, Hash=1, UseTranspositionTable=True, UseFastMoves=True)
        original = search_fingerprint(*engine.search(board, "go depth 4"))
        repeated = search_fingerprint(*engine.search(board, "go depth 4"))
        assert repeated == original, (original, repeated)
        engine.search(chess.Board(CAPTURE_FEN), "go depth 3")
        configure(engine, UsePositionalEvaluation=False, UseQuiescence=False,
                  UseTranspositionTable=False, UseFastMoves=False)
        altered = search_fingerprint(*engine.search(board, "go depth 3"))
        assert altered[-1] == (0, 0, 0, 0), altered
        configure(engine, UsePositionalEvaluation=True, UseQuiescence=True,
                  UseTranspositionTable=True, UseFastMoves=True, Hash=1)
        restored = search_fingerprint(*engine.search(board, "go depth 4"))
        assert restored == original, (original, restored)
        engine.send("ucinewgame")
        engine.ready()
        new_game = search_fingerprint(*engine.search(board, "go depth 4"))
        assert new_game == original, (original, new_game)
        configure(engine, Hash=16)
        engine.search(chess.Board(CAPTURE_FEN), "go depth 3")
        configure(engine, Hash=1)
        resized = search_fingerprint(*engine.search(board, "go depth 4"))
        assert resized == original, (original, resized)


def test_table_allocation_reuse(binary):
    board = chess.Board()
    terminal = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
    with UciProcess(binary) as engine:
        configure(engine, Hash=1)
        first = engine.search(board, "go depth 2")
        original = search_fingerprint(*first)
        small_size = assert_table_memory(first[1], allocations=1, hash_mb=1)
        for _ in range(12):
            repeated = engine.search(board, "go depth 2")
            assert search_fingerprint(*repeated) == original, (original, repeated)
            assert assert_table_memory(repeated[1], reuses=1, hash_mb=1) == small_size

        # Clear Hash and a new game invalidate entries without reallocating or
        # physically sweeping the buffer. Search results remain cold-equivalent.
        for command in ["setoption name Clear Hash", "ucinewgame"]:
            engine.send(command)
            output = engine.ready()
            assert not any(line.startswith("info string ") for line in output), output
            repeated = engine.search(board, "go depth 2")
            assert search_fingerprint(*repeated) == original, (command, repeated)
            assert assert_table_memory(repeated[1], reuses=1, hash_mb=1) == small_size
        engine.send("setoption name Clear Hash value ignored")
        rejected = engine.ready()
        assert any(line.startswith("info string ") for line in rejected), rejected
        repeated = engine.search(board, "go depth 2")
        assert search_fingerprint(*repeated) == original
        assert_table_memory(repeated[1], reuses=1, hash_mb=1)

        engine.position(terminal)
        engine.send("go depth 2")
        move, output = engine.bestmove()
        assert move == "0000", output
        assert_table_memory(output)
        engine.ready()
        configure(engine, UseTranspositionTable=False)
        _, output = engine.search(board, "go depth 2")
        assert_table_memory(output)
        configure(engine, UseTranspositionTable=True)
        _, output = engine.search(board, "go depth 2")
        assert_table_memory(output, reuses=1, hash_mb=1)

        configure(engine, Hash=16)
        _, output = engine.search(board, "go depth 2")
        large_size = assert_table_memory(output, allocations=1, hash_mb=16)
        assert large_size > small_size, (small_size, large_size)
        _, output = engine.search(board, "go depth 2")
        assert assert_table_memory(output, reuses=1, hash_mb=16) == large_size
        configure(engine, Hash=1)
        resized = engine.search(board, "go depth 2")
        assert search_fingerprint(*resized) == original
        assert assert_table_memory(resized[1], allocations=1, hash_mb=1) == small_size


def test_reused_table_context_isolation(binary):
    cycles = [["g1f3", "g8f6", "f3g1", "f6g8"],
              ["b1c3", "b8c6", "c3b1", "c6b8"]]
    histories = []
    for cycle in cycles:
        board = chess.Board()
        for move in cycle:
            board.push_uci(move)
        histories.append(board)
    assert histories[0].fen() == histories[1].fen()
    assert histories[0].is_repetition(2) and histories[1].is_repetition(2)
    imported = chess.Board(histories[0].fen())
    assert not imported.is_repetition(2)
    old_clock = imported.copy()
    old_clock.halfmove_clock = 99
    later_fullmove = imported.copy()
    later_fullmove.fullmove_number = 42
    fixtures = [chess.Board(), *histories, imported, old_clock, later_fullmove,
                chess.Board(CAPTURE_FEN)]
    defaults = {"Hash": 1, "UsePositionalEvaluation": True, "UseQuiescence": True,
                "UseTranspositionTable": True, "UseFastMoves": True,
                "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True}
    with UciProcess(binary) as reused:
        configure(reused, **defaults)
        for index, board in enumerate(fixtures):
            with UciProcess(binary) as cold:
                configure(cold, **defaults)
                expected = search_history(cold, board)
                assert_table_memory(expected[1], allocations=1, hash_mb=1)
            actual = search_history(reused, board)
            assert search_fingerprint(*actual) == search_fingerprint(*expected), (index, actual, expected)
            assert_table_memory(actual[1], allocations=int(index == 0),
                                reuses=int(index != 0), hash_mb=1)
        for change in [{"UsePositionalEvaluation": False}, {"UseQuiescence": False},
                       {"UsePositionalEvaluation": False, "UseQuiescence": False},
                       {"UseTranspositionTable": False}, {"UseFastMoves": False},
                       {"UseQuietMoveOrdering": False}, {"UseQuiescenceChecks": False},
                       {"UseQuietMoveOrdering": False, "UseQuiescenceChecks": False}, {}]:
            options = defaults | change
            board = chess.Board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1")
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = search_history(cold, board)
            configure(reused, **options)
            actual = search_history(reused, board)
            assert search_fingerprint(*actual) == search_fingerprint(*expected), (options, actual, expected)
            if options["UseTranspositionTable"]:
                assert_table_memory(actual[1], reuses=1, hash_mb=1)
            else:
                assert_table_memory(actual[1])


def test_table_options_during_search(binary):
    board = chess.Board()
    with UciProcess(binary) as engine:
        configure(engine, Hash=1)
        original = engine.search(board, "go depth 3")
        assert_table_memory(original[1], allocations=1, hash_mb=1)
        for command, allocated, enabled, size in [
            ("setoption name Hash value 16", 1, True, 16),
            ("setoption name Hash value 1", 1, True, 1),
            ("setoption name Clear Hash", 0, True, 1),
            ("setoption name UseTranspositionTable value false", 0, False, None),
            ("setoption name UseTranspositionTable value true", 0, True, 1),
            ("ucinewgame", 0, True, 1),
        ]:
            engine.position(board)
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(command)
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (command, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in board.legal_moves, output
            assert_legal_pvs(board, output)
            engine.ready()  # Joined search cannot emit another result after readyok.
            completed = engine.search(board, "go depth 3")
            if enabled:
                assert_table_memory(completed[1], allocations=allocated,
                                    reuses=int(not allocated), hash_mb=size)
                if size == 1:
                    assert search_fingerprint(*completed) == search_fingerprint(*original), (command, completed)
            else:
                assert_table_memory(completed[1])

        # A stopped infinite search must release exclusive use of the retained
        # buffer before the next search starts, without releasing its allocation.
        engine.send("go infinite")
        engine.await_search_started()
        engine.send("stop")
        move, output = engine.bestmove()
        assert chess.Move.from_uci(move) in board.legal_moves, output
        assert_legal_pvs(board, output)
        assert_table_memory(output, reuses=1, hash_mb=1)
        engine.ready()
        repeated = engine.search(board, "go depth 3")
        assert_table_memory(repeated[1], reuses=1, hash_mb=1)
        assert search_fingerprint(*repeated) == search_fingerprint(*original)


def test_search_quality_option_lifecycle(binary):
    """New heuristics are strict options and cannot leak through retained TT memory."""
    defaults = {"Hash": 1, "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True}
    board = chess.Board(CAPTURE_FEN)
    with UciProcess(binary) as engine:
        configure(engine, **defaults)
        original = search_fingerprint(*engine.search(board, "go depth 2"))
        for name in ("UseQuietMoveOrdering", "UseQuiescenceChecks"):
            for suffix in ("value yes", "", "value", "value false extra"):
                command = f"setoption name {name} {suffix}".rstrip()
                engine.send(command)
                rejected = engine.ready()
                assert any(line.startswith("info string ") for line in rejected), (command, rejected)
                assert not any(line.startswith("bestmove ") for line in rejected), (command, rejected)
        after_rejections = search_fingerprint(*engine.search(board, "go depth 2"))
        assert after_rejections == original, (original, after_rejections)

        options = dict(defaults)
        for name, enabled in [("UseQuietMoveOrdering", False), ("UseQuiescenceChecks", False),
                              ("UseQuietMoveOrdering", True), ("UseQuiescenceChecks", True)]:
            # Changes arrive while a worker owns the retained table. readyok
            # must follow its single final result and the option assignment.
            start = chess.Board()
            engine.position(start)
            engine.send("go infinite")
            engine.await_search_started()
            command = f"setoption name {name} value {str(enabled).lower()}"
            engine.send(command)
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (command, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in start.legal_moves, output
            assert_legal_pvs(start, output)
            engine.ready()
            options[name] = enabled
            actual = engine.search(board, "go depth 2")
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = cold.search(board, "go depth 2")
            assert search_fingerprint(*actual) == search_fingerprint(*expected), (options, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)

        # The same disabled feature configuration survives new-game invalidation
        # and a resized buffer; a fresh process supplies the independent query.
        configure(engine, UseQuietMoveOrdering=False, UseQuiescenceChecks=False)
        options.update(UseQuietMoveOrdering=False, UseQuiescenceChecks=False)
        for command, size, allocated in [("ucinewgame", 1, 0),
                                         ("setoption name Hash value 16", 16, 1),
                                         ("setoption name Hash value 1", 1, 1)]:
            engine.send(command)
            engine.ready()
            options["Hash"] = size
            actual = engine.search(board, "go depth 2")
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = cold.search(board, "go depth 2")
            assert search_fingerprint(*actual) == search_fingerprint(*expected), (command, actual, expected)
            assert_table_memory(actual[1], allocations=allocated, reuses=int(not allocated), hash_mb=size)


def test_search_quality_option_behavior(binary):
    # A capture-only horizon misses the opponent's quiet ...Qd1#. Independent
    # replay verifies the checking continuation, rather than trusting a CP shift.
    trap = chess.Board("7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1")
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        for name in ("UseQuietMoveOrdering", "UseQuiescenceChecks"):
            option = engine.options[name]
            assert option.type == "check" and option.default is True, option
        engine.configure({"Hash": 1, "UseQuiescence": True, "UseQuietMoveOrdering": True})
        for board in (trap, trap.mirror()):
            engine.configure({"UseQuiescenceChecks": False})
            old_horizon = engine.play(board, chess.engine.Limit(depth=1), info=chess.engine.INFO_ALL)
            assert old_horizon.move in board.legal_moves
            assert old_horizon.info["score"].pov(board.turn).mate() is None, old_horizon
            engine.configure({"UseQuiescenceChecks": True})
            checked = engine.play(board, chess.engine.Limit(depth=1), info=chess.engine.INFO_ALL)
            assert checked.move in board.legal_moves
            assert checked.info["score"].pov(board.turn).mate() == -1, checked
            continuation = board.copy()
            for move in checked.info["pv"]:
                assert move in continuation.legal_moves, checked
                continuation.push(move)
            assert continuation.is_checkmate(), checked
            assert checked.info["nodes"] > old_horizon.info["nodes"], (old_horizon, checked)

        # With extensions and TT disabled this is the same full-width minimax
        # query. Ordering must be applied, but need not change its exact score.
        engine.configure({"UseQuiescence": False, "UseQuiescenceChecks": False,
                          "UseTranspositionTable": False, "UseQuietMoveOrdering": False})
        board = chess.Board()
        original_order = engine.play(board, chess.engine.Limit(depth=4), info=chess.engine.INFO_ALL)
        engine.configure({"UseQuietMoveOrdering": True})
        learned_order = engine.play(board, chess.engine.Limit(depth=4), info=chess.engine.INFO_ALL)
        assert original_order.move in board.legal_moves and learned_order.move in board.legal_moves
        assert original_order.info["score"] == learned_order.info["score"], (original_order, learned_order)
        assert original_order.info["nodes"] != learned_order.info["nodes"], (original_order, learned_order)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    checks = [test_python_chess, test_history_and_terminal, test_limits,
              test_interleaving, test_bad_commands, test_shutdown, test_evaluation_options,
              test_transposition_options, test_transposition_search, test_transposition_isolation,
              test_table_allocation_reuse, test_reused_table_context_isolation,
              test_table_options_during_search, test_search_quality_option_lifecycle,
              test_search_quality_option_behavior]
    for check in checks:
        check(binary)
        print("PASS UCI: " + check.__name__.removeprefix("test_"), flush=True)
    print(f"PASS engine integration: {len(checks)} groups; chess={chess.__version__}; "
          "legal moves, tactical choices, special history, limits, concurrency, shutdown, TT isolation/reuse")


if __name__ == "__main__":
    main()
