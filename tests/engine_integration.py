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


def pvs_statistics(output):
    records = []
    for line in output:
        if line.startswith("info string pvs "):
            fields = line.split()[3:]
            assert len(fields) == 4 and fields[::2] == ["probes", "researches"], line
            probes, researches = map(int, fields[1::2])
            assert 0 <= researches <= probes, line
            records.append((probes, researches))
    assert records, output
    for earlier, later in zip(records, records[1:]):
        assert all(a <= b for a, b in zip(earlier, later)), records
    return records


def pvs_fingerprint(move, output):
    return search_fingerprint(move, output), pvs_statistics(output)[-1]


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
        engine.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        assert engine.id.get("name") == "Regicide 0.7.0-dev", engine.id
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        for command in ["go searchmoves g6g6", "go ponder", "go mate 2", "go depth banana",
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


def test_searchmoves(binary):
    """A forced root tests an actual alternative while descendants stay unrestricted."""
    with UciProcess(binary) as engine:
        start = chess.Board()
        for command, allowed in (
                ("go searchmoves a2a3 depth 3", {"a2a3"}),
                ("go depth 3 searchmoves h2h3", {"h2h3"}),
                ("go searchmoves e2e4 d2d4 depth 3 nodes 30000", {"e2e4", "d2d4"})):
            engine.position(start)
            engine.send(command)
            best, output = engine.bestmove()
            assert best in allowed, (command, output)
            assert_legal_pvs(start, output)
            engine.ready()
        for command in ("go searchmoves", "go searchmoves depth 2", "go searchmoves e2e5 depth 2",
                        "go depth 2 searchmoves e2e4 e2e4", "go searchmoves e2e4 bad"):
            engine.position(start)
            engine.send(command)
            best, output = engine.bestmove()
            assert best == "0000" and any(line.startswith("info string ") for line in output), output
            engine.ready()
        for fen, forced in (
                ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
                ("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6"),
                ("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8n")):
            board = chess.Board(fen)
            engine.position(board)
            engine.send("go depth 2 searchmoves " + forced)
            best, output = engine.bestmove()
            assert best == forced, (fen, output)
            assert_legal_pvs(board, output)
            engine.ready()
        # Root restrictions must disappear on the next ordinary UCI request.
        board = chess.Board(MATE_FEN)
        engine.position(board)
        engine.send("go depth 2")
        best, output = engine.bestmove()
        board.push_uci(best)
        assert board.is_checkmate(), output
        engine.ready()
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        board = chess.Board()
        reply = client.play(board, chess.engine.Limit(depth=3), root_moves=[chess.Move.from_uci("a2a3")])
        assert reply.move == chess.Move.from_uci("a2a3"), reply


def test_shutdown(binary):
    for end in ["quit", "eof"]:
        with UciProcess(binary) as engine:
            configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        engine.configure({"UseLMR": False})  # Keep the established fixture unchanged.
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
        engine.configure({"UseLMR": False})  # Keep the established fixture unchanged.
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

        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
                "UseQuietMoveOrdering": True, "UseQuiescenceChecks": True, "PVS": True}
    with UciProcess(binary) as reused:
        configure(reused, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(reused, **defaults)
        for index, board in enumerate(fixtures):
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
                       {"PVS": False},
                       {"UseQuietMoveOrdering": False, "UseQuiescenceChecks": False}, {}]:
            options = defaults | change
            board = chess.Board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
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
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = cold.search(board, "go depth 2")
            assert search_fingerprint(*actual) == search_fingerprint(*expected), (command, actual, expected)
            assert_table_memory(actual[1], allocations=allocated, reuses=int(not allocated), hash_mb=size)


def test_search_quality_option_behavior(binary):
    # A capture-only horizon misses the opponent's quiet ...Qd1#. Independent
    # replay verifies the checking continuation, rather than trusting a CP shift.
    trap = chess.Board("7k/8/2b5/8/8/5q2/r4P1P/R5K1 w - - 0 1")
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as engine:
        engine.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        for name in ("UseQuietMoveOrdering", "UseQuiescenceChecks"):
            option = engine.options[name]
            assert option.type == "check" and option.default is True, option
        engine.configure({"Hash": 1, "UseQuiescence": True, "UseQuietMoveOrdering": True,
                          "UseSEE": False, "UsePawnStructure": False})
        # Preserve the original ordering/horizon fixture. The new evaluation's
        # default and search lifecycle have independent coverage below.
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


def test_pvs_protocol_and_fixed_depth(binary):
    # Validate the advertised default using a separate UCI client. On/off
    # comparisons below demand equal exact values, not identical tied moves.
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        client.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        option = client.options.get("PVS")
        assert option and option.type == "check" and option.default is True, client.options
    capture = chess.Board("7k/8/8/8/8/8/q7/R5K1 w - - 0 1")
    fixtures = [(capture, "a1a2"), (capture.mirror(), "a8a7"),
                (chess.Board(MATE_FEN), None),
                (chess.Board("7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1"), None)]
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, Hash=1, UseQuiescence=False, UsePositionalEvaluation=False, UseSEE=False)
        for board, unique in fixtures:
            expected_score = None
            before = board.copy(stack=True)
            for table in (False, True):
                configure(engine, UseTranspositionTable=table)
                for enabled in (False, True):
                    configure(engine, PVS=enabled)
                    move, output = engine.search(board, "go depth 2")
                    fingerprint = search_fingerprint(move, output)
                    if expected_score is None:
                        expected_score = fingerprint[1]
                    assert fingerprint[1] == expected_score and fingerprint[2] == 2, (board.fen(), output)
                    counters = pvs_statistics(output)[-1]
                    if enabled:
                        assert counters[0] > 0, (board.fen(), output)
                    else:
                        assert counters == (0, 0), output
                    if unique:
                        assert move == unique, (board.fen(), output)
                    if board.fen() == MATE_FEN:
                        after = board.copy()
                        after.push_uci(move)
                        assert after.is_checkmate() and expected_score == ("mate", "1"), output
                    assert board == before and board.move_stack == before.move_stack

        # The 99th quiet halfmove permits an intended draw even though the
        # earlier ordered capture resets its clock and leaves a losing score.
        draw = chess.Board("7k/8/3q4/3p4/8/8/8/3R2K1 w - - 99 1")
        assert draw.can_claim_fifty_moves()
        configure(engine, PVS=True, UseQuietMoveOrdering=False)
        move, output = engine.search(draw, "go depth 1")
        assert move != "d1d5" and search_fingerprint(move, output)[1] == ("cp", "0"), output
        assert pvs_statistics(output)[-1][1] > 0, output


def test_pvs_option_lifecycle(binary):
    board = chess.Board()
    options = {"Hash": 1, "PVS": True, "UseQuiescence": False, "UseSEE": False}
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, **options)
        original = pvs_fingerprint(*engine.search(board, "go depth 3"))
        assert original[1][0] > 0, original
        for command in ["setoption name PVS value yes", "setoption name PVS",
                        "setoption name PVS value", "setoption name PVS value false extra"]:
            engine.send(command)
            rejected = engine.ready()
            assert any(line.startswith("info string ") for line in rejected), (command, rejected)
            assert not any(line.startswith("bestmove ") for line in rejected), (command, rejected)
        assert pvs_fingerprint(*engine.search(board, "go depth 3")) == original

        for enabled in (False, True):
            # An option change must first join the active worker and emit its
            # single final move. The next query must match a cold process.
            engine.position(board)
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(f"setoption name PVS value {str(enabled).lower()}")
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (enabled, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in board.legal_moves, output
            assert_legal_pvs(board, output)
            pvs_statistics(output)
            engine.ready()  # No late worker output may leak past readyok.
            options["PVS"] = enabled
            actual = engine.search(board, "go depth 3")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = cold.search(board, "go depth 3")
            assert pvs_fingerprint(*actual) == pvs_fingerprint(*expected), (enabled, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)
            if enabled:
                assert pvs_fingerprint(*actual) == original
            else:
                assert pvs_statistics(actual[1])[-1] == (0, 0)

        # New-game invalidation, explicit clear and resizing preserve a chosen
        # disabled setting while retaining the table's allocation semantics.
        configure(engine, PVS=False)
        options["PVS"] = False
        for command, size, allocation in [("ucinewgame", 1, 0),
                                          ("setoption name Clear Hash", 1, 0),
                                          ("setoption name Hash value 16", 16, 1),
                                          ("setoption name Hash value 1", 1, 1)]:
            engine.send(command)
            engine.ready()
            options["Hash"] = size
            actual = engine.search(board, "go depth 3")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = cold.search(board, "go depth 3")
            assert pvs_fingerprint(*actual) == pvs_fingerprint(*expected), (command, actual, expected)
            assert pvs_statistics(actual[1])[-1] == (0, 0)
            assert_table_memory(actual[1], allocations=allocation,
                                reuses=int(not allocation), hash_mb=size)


def see_statistics(output):
    records = []
    for line in output:
        if line.startswith("info string see "):
            fields = line.split()[3:]
            assert len(fields) == 2 and fields[0] == "calls", line
            value = int(fields[1])
            assert value >= 0, line
            records.append(value)
    assert records, output
    assert all(a <= b for a, b in zip(records, records[1:])), records
    return records


def see_fingerprint(move, output):
    return pvs_fingerprint(move, output), see_statistics(output)[-1]


def test_see_protocol_and_fixed_depth(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        client.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        option = client.options.get("UseSEE")
        assert option and option.type == "check" and option.default is True, client.options
    fixtures = [chess.Board(CAPTURE_FEN),
                chess.Board("7k/8/4p3/3r4/8/8/8/3Q2K1 w - - 0 1"),
                chess.Board("3r3k/8/8/3pP3/8/8/8/3R2K1 w - d6 0 1"),
                chess.Board("1rr5/P6k/8/8/8/8/8/7K w - - 0 1")]
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, Hash=1, UseQuiescence=False, UsePositionalEvaluation=False)
        for board in fixtures:
            expected = None
            before = board.copy(stack=True)
            for table in (False, True):
                for pvs in (False, True):
                    for enabled in (False, True):
                        configure(engine, UseTranspositionTable=table, PVS=pvs, UseSEE=enabled)
                        move, output = engine.search(board, "go depth 2")
                        fingerprint = search_fingerprint(move, output)
                        if expected is None:
                            expected = fingerprint[1]
                        assert fingerprint[1] == expected and fingerprint[2] == 2, (board.fen(), output)
                        calls = see_statistics(output)[-1]
                        assert (calls > 0) if enabled else (calls == 0), (enabled, output)
                        assert board == before and board.move_stack == before.move_stack

        # The queen sacrifice is a losing local exchange, followed by a quiet
        # checking knight mate. SEE must retain it in normal and tactical search.
        sacrifice = chess.Board("5rnk/6pp/7N/3Q4/8/8/8/K7 w - - 0 1")
        for quiet, depth in ((False, 3), (True, 1)):
            configure(engine, UseSEE=True, PVS=True, UseTranspositionTable=True,
                      UseQuiescence=quiet, UseQuiescenceChecks=quiet)
            move, output = engine.search(sacrifice, f"go depth {depth}")
            assert move == "d5g8" and search_fingerprint(move, output)[1] == ("mate", "2"), output
            after = sacrifice.copy()
            for token in ["d5g8", "f8g8", "h6f7"]:
                assert chess.Move.from_uci(token) in after.legal_moves
                after.push_uci(token)
            assert after.is_checkmate()


def test_see_option_lifecycle(binary):
    board = chess.Board("7k/8/4p3/3r4/p7/8/8/R2Q2K1 w - - 0 1")
    options = {"Hash": 1, "PVS": True, "UseQuiescence": False, "UseSEE": True}
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, **options)
        original = see_fingerprint(*engine.search(board, "go depth 2"))
        assert original[-1] > 0, original
        for command in ["setoption name UseSEE value yes", "setoption name UseSEE",
                        "setoption name UseSEE value", "setoption name UseSEE value false extra"]:
            engine.send(command)
            rejected = engine.ready()
            assert any(line.startswith("info string ") for line in rejected), (command, rejected)
            assert not any(line.startswith("bestmove ") for line in rejected), (command, rejected)
        assert see_fingerprint(*engine.search(board, "go depth 2")) == original

        for enabled in (False, True, False):
            start = chess.Board()
            engine.position(start)
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(f"setoption name UseSEE value {str(enabled).lower()}")
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (enabled, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in start.legal_moves, output
            assert_legal_pvs(start, output)
            see_statistics(output)
            engine.ready()
            options["UseSEE"] = enabled
            actual = engine.search(board, "go depth 2")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = cold.search(board, "go depth 2")
            assert see_fingerprint(*actual) == see_fingerprint(*expected), (enabled, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)
            if enabled:
                assert see_fingerprint(*actual) == original
            else:
                assert see_statistics(actual[1])[-1] == 0

        for command, size, allocation in [("ucinewgame", 1, 0),
                                          ("setoption name Clear Hash", 1, 0),
                                          ("setoption name Hash value 16", 16, 1),
                                          ("setoption name Hash value 1", 1, 1)]:
            engine.send(command)
            engine.ready()
            options["Hash"] = size
            actual = engine.search(board, "go depth 2")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = cold.search(board, "go depth 2")
            assert see_fingerprint(*actual) == see_fingerprint(*expected), (command, actual, expected)
            assert see_statistics(actual[1])[-1] == 0
            assert_table_memory(actual[1], allocations=allocation,
                                reuses=int(not allocation), hash_mb=size)


def test_pawn_shield_protocol_and_cli(binary):
    # An exposed g-file gives the moving side less cover in either color.
    # A one-node budget independently selects the root fallback path, so the
    # on/off score comparison cannot accidentally test only leaf evaluation.
    exposed = chess.Board("r2q1rk1/ppp2ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPP2P1P/R2Q1RK1 w - - 0 10")
    assert exposed.is_valid() and not exposed.is_game_over()
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        client.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        option = client.options.get("UsePawnShield")
        assert option and option.type == "check" and option.default is True, client.options

    def cli_query(board, go, enabled, positional=True):
        command = [str(binary), "search", "2", "--hash", "1", "--no-quiescence", "--no-lmr",
                   "--fen", board.fen(en_passant="fen")]
        if go == "go nodes 1":
            command += ["--nodes", "1"]
        if not enabled:
            command.append("--no-pawn-shield")
        if not positional:
            command.append("--no-position")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=TIMEOUT, check=True)
        assert not result.stderr, result.stderr
        fields = dict(line.split(":", 1) for line in result.stdout.splitlines())
        fields = {name: value.strip() for name, value in fields.items()}
        score_name = "score_mate" if "score_mate" in fields else "score_cp"
        return (fields["bestmove"], ("mate" if score_name == "score_mate" else "cp",
                                     fields[score_name]), int(fields["depth"]),
                int(fields["nodes"]), tuple(fields["pv"].split()),
                tuple(int(fields["tt_" + key]) for key in ["probes", "hits", "cutoffs", "stores"]))

    with UciProcess(binary) as engine:

        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, Hash=1, UseQuiescence=False)
        default = search_fingerprint(*engine.search(exposed, "go depth 2"))
        configure(engine, UsePawnShield=True)
        explicit = search_fingerprint(*engine.search(exposed, "go depth 2"))
        assert default == explicit, (default, explicit)
        for board in (exposed, exposed.mirror()):
            before = board.copy(stack=True)
            for go in ("go depth 2", "go nodes 1"):
                scores = {}
                for enabled in (False, True):
                    configure(engine, UsePawnShield=enabled)
                    actual = search_fingerprint(*engine.search(board, go))
                    assert actual == cli_query(board, go, enabled), (board.fen(), go, enabled, actual)
                    scores[enabled] = actual[1]
                    if go == "go nodes 1":
                        assert actual[2] == 0 and actual[3] <= 1, actual
                if go == "go nodes 1":
                    assert scores[False][0] == scores[True][0] == "cp", scores
                    assert int(scores[True][1]) < int(scores[False][1]), scores

            # Material-only analysis must ignore the new positional feature,
            # including the no-completed-iteration cancellation fallback.
            configure(engine, UsePositionalEvaluation=False)
            for go in ("go depth 2", "go nodes 1"):
                configure(engine, UsePawnShield=False)
                off = search_fingerprint(*engine.search(board, go))
                configure(engine, UsePawnShield=True)
                on = search_fingerprint(*engine.search(board, go))
                assert off == on == cli_query(board, go, True, positional=False), (go, off, on)
            configure(engine, UsePositionalEvaluation=True)
            assert board == before and board.move_stack == before.move_stack


def test_pawn_shield_option_lifecycle(binary):
    # Keep an actual repetition history live while options change. Matching a
    # cold process with that same history verifies worker/TT isolation without
    # replacing the tested position after a toggle.
    board = chess.Board("r2q1rk1/ppp2ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPP2P1P/R2Q1RK1 w - - 0 10")
    for move in ("g1h1", "g8h8", "h1g1", "h8g8"):
        board.push_uci(move)
    assert board.is_repetition(2) and not board.is_game_over()
    options = {"Hash": 1, "UseQuiescence": False, "UsePawnShield": False}
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, **options)
        first = search_history(engine, board, "go depth 2")
        original = see_fingerprint(*first)
        assert_table_memory(first[1], allocations=1, hash_mb=1)
        for command in ["setoption name UsePawnShield value yes", "setoption name UsePawnShield",
                        "setoption name UsePawnShield value", "setoption name UsePawnShield value false extra"]:
            engine.send(command)
            rejected = engine.ready()
            assert any(line.startswith("info string ") for line in rejected), (command, rejected)
            assert not any(line.startswith("bestmove ") for line in rejected), (command, rejected)
        engine.send("go depth 2")
        assert see_fingerprint(*engine.bestmove()) == original
        engine.ready()

        for enabled in (True, False, True, False):
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(f"setoption name UsePawnShield value {str(enabled).lower()}")
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (enabled, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in board.legal_moves, output
            assert_legal_pvs(board, output)
            engine.ready()  # A joined worker must not emit any late bestmove.
            options["UsePawnShield"] = enabled
            engine.send("go depth 2")
            actual = engine.bestmove()
            assert_legal_pvs(board, actual[1])
            engine.ready()
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = search_history(cold, board, "go depth 2")
            assert see_fingerprint(*actual) == see_fingerprint(*expected), (enabled, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)
            if not enabled:
                assert see_fingerprint(*actual) == original, (original, actual)


def test_pawn_structure_protocol_and_cli(binary):
    # The pawn on d6 is passed in either color. Its advancement bonus exceeds
    # the isolated-pawn cost; a one-node query independently reaches the root
    # fallback without completing a normal evaluation iteration.
    passed = chess.Board("4k3/8/3P4/8/8/8/8/4K3 w - - 0 1")
    assert passed.is_valid() and not passed.is_game_over()
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        client.configure({"UseLMR": False})  # Keep the established fixture unchanged.
        option = client.options.get("UsePawnStructure")
        assert option and option.type == "check" and option.default is True, client.options

    def cli_query(board, go, enabled, positional=True, shield=True):
        command = [str(binary), "search", "2", "--hash", "1", "--no-quiescence", "--no-lmr",
                   "--fen", board.fen(en_passant="fen")]
        if go == "go nodes 1":
            command += ["--nodes", "1"]
        if not enabled:
            command.append("--no-pawn-structure")
        if not positional:
            command.append("--no-position")
        if not shield:
            command.append("--no-pawn-shield")
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=TIMEOUT, check=True)
        assert not result.stderr, result.stderr
        fields = dict(line.split(":", 1) for line in result.stdout.splitlines())
        fields = {name: value.strip() for name, value in fields.items()}
        score_name = "score_mate" if "score_mate" in fields else "score_cp"
        return (fields["bestmove"], ("mate" if score_name == "score_mate" else "cp",
                                     fields[score_name]), int(fields["depth"]),
                int(fields["nodes"]), tuple(fields["pv"].split()),
                tuple(int(fields["tt_" + key]) for key in ["probes", "hits", "cutoffs", "stores"]))

    with UciProcess(binary) as engine:

        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, Hash=1, UseQuiescence=False)
        default = see_fingerprint(*engine.search(passed, "go depth 2"))
        configure(engine, UsePawnStructure=True)
        explicit = see_fingerprint(*engine.search(passed, "go depth 2"))
        assert default == explicit, (default, explicit)
        for board in (passed, passed.mirror()):
            before = board.copy(stack=True)
            for shield in (False, True):
                configure(engine, UsePawnShield=shield)
                for go in ("go depth 2", "go nodes 1"):
                    scores = {}
                    for enabled in (False, True):
                        configure(engine, UsePawnStructure=enabled)
                        actual = search_fingerprint(*engine.search(board, go))
                        expected = cli_query(board, go, enabled, shield=shield)
                        assert actual == expected, (board.fen(), go, shield, enabled, actual, expected)
                        scores[enabled] = actual[1]
                        if go == "go nodes 1":
                            assert actual[2] == 0 and actual[3] <= 1, actual
                    if go == "go nodes 1":
                        assert scores[False][0] == scores[True][0] == "cp", scores
                        assert int(scores[True][1]) > int(scores[False][1]), scores

            # Positional evaluation is the containing switch. Its OFF state
            # ignores both new-feature options even on a cancelled root.
            configure(engine, UsePositionalEvaluation=False)
            for shield in (False, True):
                configure(engine, UsePawnShield=shield)
                for go in ("go depth 2", "go nodes 1"):
                    configure(engine, UsePawnStructure=False)
                    off = search_fingerprint(*engine.search(board, go))
                    configure(engine, UsePawnStructure=True)
                    on = search_fingerprint(*engine.search(board, go))
                    assert off == on == cli_query(board, go, True, positional=False,
                                                  shield=shield), (go, shield, off, on)
            configure(engine, UsePositionalEvaluation=True)
            assert board == before and board.move_stack == before.move_stack


def test_pawn_structure_option_lifecycle(binary):
    # Toggle while a worker searches an actual repetition history, retaining
    # that position across changes. Cold processes receive the identical full
    # history; graph equality catches stale TT values and dropped move history.
    board = chess.Board("4k3/8/3P4/8/8/8/8/4K3 w - - 0 1")
    for move in ("e1f1", "e8f8", "f1e1", "f8e8"):
        board.push_uci(move)
    assert board.is_repetition(2) and not board.is_game_over()
    options = {"Hash": 1, "UseQuiescence": False, "UsePawnStructure": False}
    with UciProcess(binary) as engine:
        configure(engine, UseLMR=False)  # Preserve the pre-LMR regression graph.
        configure(engine, **options)
        first = search_history(engine, board, "go depth 2")
        original = see_fingerprint(*first)
        assert_table_memory(first[1], allocations=1, hash_mb=1)
        for command in ["setoption name UsePawnStructure value yes",
                        "setoption name UsePawnStructure",
                        "setoption name UsePawnStructure value",
                        "setoption name UsePawnStructure value false extra"]:
            engine.send(command)
            rejected = engine.ready()
            assert any(line.startswith("info string ") for line in rejected), (command, rejected)
            assert not any(line.startswith("bestmove ") for line in rejected), (command, rejected)
        engine.send("go depth 2")
        assert see_fingerprint(*engine.bestmove()) == original
        engine.ready()

        for enabled in (True, False, True, False):
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(f"setoption name UsePawnStructure value {str(enabled).lower()}")
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1, (enabled, output)
            assert chess.Move.from_uci(bestmoves[0].split()[1]) in board.legal_moves, output
            assert_legal_pvs(board, output)
            engine.ready()  # No late worker output may cross this barrier.
            options["UsePawnStructure"] = enabled
            engine.send("go depth 2")
            actual = engine.bestmove()
            assert_legal_pvs(board, actual[1])
            engine.ready()
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = search_history(cold, board, "go depth 2")
            assert see_fingerprint(*actual) == see_fingerprint(*expected), (enabled, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)
            if not enabled:
                assert see_fingerprint(*actual) == original, (original, actual)

        # Administrative TT lifecycle operations preserve the chosen OFF
        # setting and retain lazy allocation/reuse behavior.
        for command, size, allocation in [("ucinewgame", 1, 0),
                                          ("setoption name Clear Hash", 1, 0),
                                          ("setoption name Hash value 16", 16, 1),
                                          ("setoption name Hash value 1", 1, 1)]:
            engine.send(command)
            engine.ready()
            options["Hash"] = size
            actual = search_history(engine, board, "go depth 2")
            with UciProcess(binary) as cold:
                configure(cold, UseLMR=False)  # Preserve the pre-LMR regression graph.
                configure(cold, **options)
                expected = search_history(cold, board, "go depth 2")
            assert see_fingerprint(*actual) == see_fingerprint(*expected), (command, actual, expected)
            assert_table_memory(actual[1], allocations=allocation,
                                reuses=int(not allocation), hash_mb=size)


def lmr_statistics(output):
    records = []
    for line in output:
        if line.startswith("info string lmr "):
            fields = line.split()[3:]
            assert len(fields) == 4 and fields[::2] == ["reductions", "researches"], line
            reductions, researches = map(int, fields[1::2])
            assert 0 <= researches <= reductions, line
            records.append((reductions, researches))
    assert records, output
    for earlier, later in zip(records, records[1:]):
        assert all(a <= b for a, b in zip(earlier, later)), records
    return records


def lmr_fingerprint(move, output):
    return search_fingerprint(move, output), see_statistics(output)[-1], lmr_statistics(output)[-1]


def lmr_cli_query(binary, board, depth, enabled, nodes=None):
    command = [str(binary), "search", str(depth), "--hash", "1", "--no-quiescence",
               "--fen", board.fen(en_passant="fen")]
    if not enabled:
        command.append("--no-lmr")
    if nodes is not None:
        command += ["--nodes", str(nodes)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            timeout=TIMEOUT, check=True)
    assert not result.stderr, result.stderr
    fields = {key: value.strip() for key, value in
              (line.split(":", 1) for line in result.stdout.splitlines())}
    score_name = "score_mate" if "score_mate" in fields else "score_cp"
    graph = (fields["bestmove"], ("mate" if score_name == "score_mate" else "cp",
                                fields[score_name]), int(fields["depth"]),
             int(fields["nodes"]), tuple(fields["pv"].split()),
             tuple(int(fields["tt_" + key]) for key in ["probes", "hits", "cutoffs", "stores"]))
    return graph, int(fields["see_calls"]), (int(fields["lmr_reductions"]),
                                            int(fields["lmr_researches"]))


def test_lmr_protocol_cli_and_gates(binary):
    # These are fixed positions, not generated or selected from strength data.
    # ON changes selective search, so only legality/completed limits are
    # required between modes; CLI and UCI with the same mode must agree fully.
    fixtures = [chess.Board("4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1"),
                chess.Board("r2q1rk1/ppp2ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPP2PPP/R2Q1RK1 w - - 0 10")]
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        option = client.options.get("UseLMR")
        assert option and option.type == "check" and option.default is True, client.options
    saw_reduction = False
    saw_research = False
    with UciProcess(binary) as engine:
        configure(engine, Hash=1, UseQuiescence=False)
        default = lmr_fingerprint(*engine.search(fixtures[0], "go depth 5"))
        configure(engine, UseLMR=True)
        assert lmr_fingerprint(*engine.search(fixtures[0], "go depth 5")) == default
        for original in fixtures:
            for board in (original, original.mirror()):
                before = board.copy(stack=True)
                for enabled in (False, True):
                    configure(engine, UseLMR=enabled)
                    actual = lmr_fingerprint(*engine.search(board, "go depth 5"))
                    assert actual == lmr_cli_query(binary, board, 5, enabled), (board.fen(), enabled, actual)
                    assert actual[0][2] == 5, actual
                    if enabled:
                        saw_reduction |= actual[-1][0] > 0
                        saw_research |= actual[-1][1] > 0
                    else:
                        assert actual[-1] == (0, 0), actual
                # At depth four the root is protected and every child is too
                # shallow. At one node no iteration can reduce a late move.
                for go, depth, nodes in [("go depth 4", 4, None), ("go nodes 1", 5, 1)]:
                    configure(engine, UseLMR=False)
                    off = lmr_fingerprint(*engine.search(board, go))
                    configure(engine, UseLMR=True)
                    on = lmr_fingerprint(*engine.search(board, go))
                    assert off == on == lmr_cli_query(binary, board, depth, True, nodes), (go, off, on)
                    assert on[-1] == (0, 0), on
                # PVS and learned quiet ordering are both required. Disabling
                # either is a strict no-op, not a different reduction policy.
                for required in ("PVS", "UseQuietMoveOrdering"):
                    configure(engine, **{required: False, "UseLMR": False})
                    off = lmr_fingerprint(*engine.search(board, "go depth 5"))
                    configure(engine, UseLMR=True)
                    on = lmr_fingerprint(*engine.search(board, "go depth 5"))
                    assert off == on and on[-1] == (0, 0), (board.fen(), required, off, on)
                    configure(engine, **{required: True})
                assert board == before and board.move_stack == before.move_stack
    assert saw_reduction, "fixed depth-five corpus never exercised an actual LMR reduction"
    assert saw_research, "fixed corpus never promoted an improving reduced result to full depth"


def test_lmr_option_lifecycle(binary):
    board = chess.Board("4k3/8/2n2n2/3p4/3P4/2N2N2/8/4K3 w - - 0 1")
    for move in ("e1f1", "e8f8", "f1e1", "f8e8"):
        board.push_uci(move)
    assert board.is_repetition(2) and not board.is_game_over()
    options = {"Hash": 1, "UseQuiescence": False, "UseLMR": False}
    with UciProcess(binary) as engine:
        configure(engine, **options)
        first = search_history(engine, board, "go depth 5")
        original = lmr_fingerprint(*first)
        assert_table_memory(first[1], allocations=1, hash_mb=1)
        for command in ["setoption name UseLMR value yes", "setoption name UseLMR value 1",
                        "setoption name UseLMR", "setoption name UseLMR value",
                        "setoption name UseLMR value TRUE", "setoption name UseLMR value false extra"]:
            engine.send(command)
            output = engine.ready()
            assert any(line.startswith("info string ") for line in output), (command, output)
        engine.send("go depth 5")
        assert lmr_fingerprint(*engine.bestmove()) == original
        engine.ready()
        for enabled in (True, False, True, False):
            engine.send("go infinite")
            engine.await_search_started()
            engine.send(f"setoption name UseLMR value {str(enabled).lower()}")
            output = engine.ready(allow_bestmove=True)
            bestmoves = [line for line in output if line.startswith("bestmove ")]
            assert len(bestmoves) == 1 and chess.Move.from_uci(bestmoves[0].split()[1]) in board.legal_moves, output
            assert_legal_pvs(board, output)
            lmr_statistics(output)
            engine.ready()  # Joined workers cannot leak late output.
            options["UseLMR"] = enabled
            engine.send("go depth 5")
            actual = engine.bestmove()
            assert_legal_pvs(board, actual[1])
            engine.ready()
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = search_history(cold, board, "go depth 5")
            assert lmr_fingerprint(*actual) == lmr_fingerprint(*expected), (enabled, actual, expected)
            assert_table_memory(actual[1], reuses=1, hash_mb=1)
            if not enabled:
                assert lmr_fingerprint(*actual) == original
        # Keep ON across administrative actions, then compare to fresh workers.
        configure(engine, UseLMR=True)
        options["UseLMR"] = True
        for command, size, allocation in [("ucinewgame", 1, 0),
                                          ("setoption name Clear Hash", 1, 0),
                                          ("setoption name Hash value 16", 16, 1),
                                          ("setoption name Hash value 1", 1, 1)]:
            engine.send(command)
            engine.ready()
            options["Hash"] = size
            actual = search_history(engine, board, "go depth 5")
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = search_history(cold, board, "go depth 5")
            assert lmr_fingerprint(*actual) == lmr_fingerprint(*expected), (command, actual, expected)
            assert_table_memory(actual[1], allocations=allocation, reuses=int(not allocation), hash_mb=size)


def test_lmr_default_client_compatibility(binary):
    # The desktop game and human-turn hint helper both use python-chess UCI
    # clients. Default ON must remain usable with short budgets and both colors.
    original = chess.Board("r2q1rk1/ppp2ppp/2n1bn2/3pp3/3PP3/2N1BN2/PPP2PPP/R2Q1RK1 w - - 0 10")
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        assert client.options["UseLMR"].default is True
        for board in (original, original.mirror()):
            before = board.copy(stack=True)
            result = client.play(board, chess.engine.Limit(time=0.1), info=chess.engine.INFO_ALL)
            assert result.move in board.legal_moves, result
            continuation = board.copy(stack=True)
            for move in result.info.get("pv", []):
                assert move in continuation.legal_moves, result
                continuation.push(move)
            assert board == before and board.move_stack == before.move_stack
            client.ping()


def test_quiescence_threat_option_lifecycle(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        option = client.options["UseQuiescenceThreats"]
        assert option.type == "check" and option.default is False
    board = chess.Board("r2qkb1r/1pp2ppp/2n2P2/8/p1Bp4/P4N2/RP3PPP/1bBQ1RK1 w kq - 0 11")
    with UciProcess(binary) as engine:
        for value in ("yes", "1", "TRUE", "false extra"):
            engine.send("setoption name UseQuiescenceThreats value " + value)
            output = engine.ready()
            assert any("check option requires" in line or "missing or extra" in line for line in output), output
        original = None
        for enabled in (False, True, False):
            configure(engine, UseQuiescenceThreats=enabled)
            actual = engine.search(board, "go nodes 2000")
            with UciProcess(binary) as cold:
                configure(cold, UseQuiescenceThreats=enabled)
                expected = cold.search(board, "go nodes 2000")
            assert search_fingerprint(*actual) == search_fingerprint(*expected)
            if not enabled:
                current = search_fingerprint(*actual)
                assert original is None or original == current
                original = current
            assert any(line.startswith("info string qthreat ") for line in actual[1]), actual
        configure(engine, UseQuiescenceThreats=True)
        engine.position(board)
        engine.send("go infinite")
        engine.await_search_started()
        engine.send("setoption name UseQuiescenceThreats value false")
        output = engine.ready(allow_bestmove=True)
        assert sum(line.startswith("bestmove ") for line in output) == 1, output
        engine.ready()


def test_quiescence_cache_option_lifecycle(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        option = client.options["UseQuiescenceThreatCache"]
        assert option.type == "check" and option.default is False
    board = chess.Board("r2qkb1r/1pp2ppp/2n2P2/8/p1Bp4/P4N2/RP3PPP/1bBQ1RK1 w kq - 0 11")
    with UciProcess(binary) as engine:
        configure(engine, UseQuiescenceThreats=True)
        for value in ("yes", "1", "TRUE", "false extra"):
            engine.send("setoption name UseQuiescenceThreatCache value " + value)
            output = engine.ready()
            assert any("check option requires" in line or "missing or extra" in line for line in output), output
        original = None
        for enabled in (False, True, False):
            configure(engine, UseQuiescenceThreatCache=enabled)
            actual = engine.search(board, "go nodes 2000")
            with UciProcess(binary) as cold:
                configure(cold, UseQuiescenceThreats=True, UseQuiescenceThreatCache=enabled)
                expected = cold.search(board, "go nodes 2000")
            assert search_fingerprint(*actual) == search_fingerprint(*expected)
            if not enabled:
                current = search_fingerprint(*actual)
                assert original is None or original == current
                original = current
            assert any(line.startswith("info string qcache ") for line in actual[1]), actual
        configure(engine, UseQuiescenceThreatCache=True)
        engine.position(board)
        engine.send("go infinite")
        engine.await_search_started()
        engine.send("setoption name UseQuiescenceThreatCache value false")
        output = engine.ready(allow_bestmove=True)
        assert sum(line.startswith("bestmove ") for line in output) == 1, output
        engine.ready()


def test_tactical_threat_ordering_protocol(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        option = client.options["UseTacticalThreatOrdering"]
        assert option.type == "check" and option.default is False
    board = chess.Board("r2qkb1r/1pp2ppp/2n2P2/8/p1Bp4/P4N2/RP3PPP/1bBQ1RK1 w kq - 0 11")
    with UciProcess(binary) as engine:
        configure(engine, UseQuiescenceThreats=True, UseQuiescenceThreatCache=True)
        for enabled in (False, True, False):
            configure(engine, UseTacticalThreatOrdering=enabled)
            actual = engine.search(board, "go nodes 2000")
            with UciProcess(binary) as cold:
                configure(cold, UseQuiescenceThreats=True, UseQuiescenceThreatCache=True,
                          UseTacticalThreatOrdering=enabled)
                expected = cold.search(board, "go nodes 2000")
            assert search_fingerprint(*actual) == search_fingerprint(*expected)
            assert any(line.startswith("info string rootthreat ") for line in actual[1])


def test_quiescence_reply_budget_protocol(binary):
    with chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=TIMEOUT) as client:
        option = client.options["UseQuiescenceReplyBudget"]
        assert option.type == "check" and option.default is False
    board = chess.Board("r2qkb1r/1pp2ppp/2n2P2/8/p1Bp4/P4N2/RP3PPP/1bBQ1RK1 w kq - 0 11")
    with UciProcess(binary) as engine:
        configure(engine, UseQuiescenceThreats=True, UseQuiescenceThreatCache=True)
        engine.send("setoption name UseQuiescenceReplyBudget value yes")
        assert any("check option requires" in line for line in engine.ready())
        for enabled in (False, True, False):
            options = dict(UseQuiescenceThreats=True, UseQuiescenceThreatCache=True,
                           UseTacticalThreatOrdering=not enabled, UseQuiescenceReplyBudget=enabled)
            configure(engine, **options)
            actual = engine.search(board, "go nodes 2000")
            with UciProcess(binary) as cold:
                configure(cold, **options)
                expected = cold.search(board, "go nodes 2000")
            assert search_fingerprint(*actual) == search_fingerprint(*expected)
        configure(engine, UseQuiescenceReplyBudget=True)
        engine.position(board)
        engine.send("go infinite")
        engine.await_search_started()
        engine.send("setoption name UseQuiescenceReplyBudget value false")
        output = engine.ready(allow_bestmove=True)
        assert sum(line.startswith("bestmove ") for line in output) == 1
        engine.ready()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    checks = [test_python_chess, test_history_and_terminal, test_limits,
              test_interleaving, test_bad_commands, test_searchmoves, test_shutdown, test_evaluation_options,
              test_transposition_options, test_transposition_search, test_transposition_isolation,
              test_table_allocation_reuse, test_reused_table_context_isolation,
              test_table_options_during_search, test_search_quality_option_lifecycle,
              test_search_quality_option_behavior, test_pvs_protocol_and_fixed_depth,
              test_pvs_option_lifecycle, test_see_protocol_and_fixed_depth,
              test_see_option_lifecycle, test_pawn_shield_protocol_and_cli,
              test_pawn_shield_option_lifecycle, test_pawn_structure_protocol_and_cli,
              test_pawn_structure_option_lifecycle, test_lmr_protocol_cli_and_gates,
              test_lmr_option_lifecycle, test_lmr_default_client_compatibility,
              test_quiescence_threat_option_lifecycle, test_quiescence_cache_option_lifecycle,
              test_tactical_threat_ordering_protocol, test_quiescence_reply_budget_protocol]
    for check in checks:
        check(binary)
        print("PASS UCI: " + check.__name__.removeprefix("test_"), flush=True)
    print(f"PASS engine integration: {len(checks)} groups; chess={chess.__version__}; "
          "legal moves, tactical choices, special history, limits, concurrency, shutdown, TT/PVS/shield/structure/LMR isolation/reuse")


if __name__ == "__main__":
    main()
