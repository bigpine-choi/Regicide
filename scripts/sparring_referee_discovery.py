"""Bounded native-only shallow discovery for an external referee candidate set.

The caller supplies its existing Regicide search and close callbacks. This
module never starts an engine, asks Stockfish to invent a move, or subtracts a
depth-four score from a deeper baseline. Its output is separate shallow native
evidence, not an admitted full-depth concession or a tactical safety proof.
"""

import copy
import threading
import time

import chess
import chess.engine

import boss_play
import sparring_play


ORIGIN = "native_root_exclusion_depth4"
SOURCE = "regicide_native_depth4_discovery"
DEPTH = 4
MAX_BUDGET_MS = 600
MAX_CANDIDATES = 3
MAX_PACKET_DEPTH = 256
MAX_CP = 100_000
TIMEOUT_GRACE_SECONDS = .1


def _line(board, packet, move):
    if (not isinstance(packet, dict) or type(packet.get("depth")) is not int
            or not DEPTH <= packet["depth"] <= MAX_PACKET_DEPTH
            or type(packet.get("multipv", 1)) is not int or packet.get("multipv", 1) != 1
            or packet.get("bound", "exact") != "exact"
            or packet.get("score_bound", "exact") != "exact"
            or packet.get("mate") is not None or packet.get("mate_white") is not None):
        return None
    fen = board.fen(en_passant="fen")
    if any(key in packet and packet[key] != fen for key in ("fen", "before_fen", "source_fen")):
        return None
    line = boss_play.score_line(board, packet, move)
    if (line is None or type(line["cp"]) is not int or abs(line["cp"]) > MAX_CP
            or type(line["cp_white"]) is not int
            or line["cp_white"] != line["cp"] * (1 if board.turn else -1)):
        return None
    for key in ("cp", "cp_white"):
        if key in packet and (type(packet[key]) is not int or packet[key] != line[key]):
            return None
    if "pov" in packet and packet["pov"] != line["pov"]:
        return None
    replay = board.copy(stack=True)
    for uci in line["pv_uci"]:
        replay.push_uci(uci)
    if replay.is_checkmate():
        return None
    return {**line, "bound": "exact", "mate": None}


def _excluded(board, values):
    if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 256:
        return None
    result = set()
    for uci in values:
        if not isinstance(uci, str) or len(uci) not in (4, 5):
            return None
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            return None
        if move.uci() != uci or move not in board.legal_moves:
            return None
        result.add(move)
    return result


def discover(board, search, close_engine, *, excluded_uci, budget_ms=MAX_BUDGET_MS,
             cancelled=None, max_candidates=MAX_CANDIDATES):
    """Discover up to three legal native alternatives at completed depth four.

    At most ``max_candidates`` searches share an allocation of at most 600ms.
    A rejected naturalness screen still consumes one search slot. Every search
    excludes previous returned roots as well as the caller's exclusions. The
    whole-run watchdog closes only the supplied engine after budget + 100ms;
    cancellation also closes it once a search has begun. The close callback
    must interrupt a blocked search, as it does in the host.

    Cancellation, illegal output, unreliable packets, search errors, or a
    deadline overrun discard the entire result, including earlier candidates.
    ``discard_engine`` tells the host that its close callback was invoked.
    """
    if type(budget_ms) is not int or budget_ms < 1:
        raise ValueError("Discovery budget must be positive integer milliseconds")
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError("Discovery candidate limit must be between one and three")
    if not callable(search) or not callable(close_engine) or cancelled is not None and not callable(cancelled):
        raise ValueError("Discovery requires search/close callbacks and an optional cancellation callback")
    budget_ms = min(budget_ms, MAX_BUDGET_MS)
    started = time.monotonic()
    deadline = started + budget_ms / 1000
    result = {"context": None, "origin": ORIGIN, "native_candidates": [],
              "elapsed_ms": 0, "discard_engine": False, "reason": "invalid_position",
              "budget_ms": budget_ms, "searches": 0, "allocated_ms": 0}
    if not isinstance(board, chess.Board) or board.chess960 or not board.is_valid():
        return result
    position = board.copy(stack=True)
    result["context"] = {"before_fen": position.fen(en_passant="fen"),
                         "history_uci": [move.uci() for move in position.move_stack],
                         "pov": "white" if position.turn else "black"}
    excluded = _excluded(position, excluded_uci)
    if excluded is None:
        result["reason"] = "invalid_exclusions"
        return result
    if position.is_game_over(claim_draw=True):
        result["reason"] = "terminal_position"
        return result
    if all(move in excluded for move in position.legal_moves):
        result["reason"] = "no_remaining_legal_candidates"
        return result

    done, lock = threading.Event(), threading.Lock()
    failure = [None]
    engine_closed = [False]
    search_begun = [False]

    def stop(reason, discard=False):
        close = False
        with lock:
            if done.is_set():
                return
            if failure[0] is None:
                failure[0] = reason
            if discard and not engine_closed[0]:
                engine_closed[0] = True
                close = True
        if close:
            try:
                close_engine()
            except Exception:
                pass

    def check_cancelled():
        if cancelled is None:
            return False
        try:
            return bool(cancelled())
        except Exception:
            return True

    def stopped():
        if check_cancelled():
            stop("cancelled", search_begun[0])
        if time.monotonic() > deadline:
            stop("timeout", search_begun[0])
        with lock:
            return failure[0] is not None

    def monitor_cancellation():
        while not done.wait(.005):
            if check_cancelled():
                stop("cancelled", search_begun[0])
                return

    timer = threading.Timer(max(0, deadline - time.monotonic()) + TIMEOUT_GRACE_SECONDS,
                            lambda: stop("timeout", search_begun[0]))
    timer.daemon = True
    watcher = threading.Thread(target=monitor_cancellation, daemon=True) if cancelled is not None else None
    try:
        if not stopped():
            timer.start()
            if watcher is not None:
                watcher.start()
            for _attempt in range(max_candidates):
                if stopped():
                    break
                allowed = [move for move in position.legal_moves if move not in excluded]
                if not allowed:
                    break
                allocation = min(budget_ms / max_candidates, max(0, (deadline - time.monotonic()) * 1000),
                                 budget_ms - result["allocated_ms"])
                if allocation < 1:
                    stop("timeout", search_begun[0])
                    break
                with lock:
                    if failure[0] is not None:
                        break
                    search_begun[0] = True
                result["searches"] += 1
                result["allocated_ms"] += allocation
                reply, final, packets = search(allocation, root_moves=allowed, publish=False,
                    fresh=True, depth=DEPTH, position=position.copy(stack=True))
                if stopped():
                    break
                move = getattr(reply, "move", None)
                if not isinstance(move, chess.Move) or move not in allowed:
                    stop("illegal_or_foreign_candidate", True)
                    break
                excluded.add(move)
                common = _line(position, packets.get(DEPTH), move) if isinstance(packets, dict) else None
                latest = _line(position, final, move)
                completed = (_line(position, packets.get(latest["depth"]), move)
                             if latest is not None and isinstance(packets, dict) else None)
                if common is None or common["depth"] != DEPTH or latest is None or completed != latest:
                    stop("unreliable_candidate")
                    break
                natural = sparring_play.naturalness(position.copy(stack=True), move)
                if stopped():
                    break
                if not isinstance(natural, dict) or natural.get("admitted") is not True:
                    continue
                result["native_candidates"].append({"uci": move.uci(), "origin": "shallow_discovery",
                    "line": common, "completed_final_line": latest,
                    "naturalness": copy.deepcopy(natural), "source": SOURCE})
            stopped()
    except InterruptedError:
        stop("cancelled", True)
    except TimeoutError:
        stop("timeout", True)
    except Exception:
        stop("search_failed", True)
    finally:
        with lock:
            done.set()
        timer.cancel()
        if timer.ident is not None:
            timer.join(timeout=.2)
        if watcher is not None and watcher.ident is not None:
            watcher.join(timeout=.2)
    result["discard_engine"] = engine_closed[0]
    result["reason"] = failure[0] or ("native_candidates_discovered" if result["native_candidates"] else "no_natural_candidates")
    if failure[0] is not None:
        result["native_candidates"] = []
    result["allocated_ms"] = round(result["allocated_ms"], 3)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return copy.deepcopy(result)
