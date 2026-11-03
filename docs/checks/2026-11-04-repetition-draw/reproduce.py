"""Read-only diagnosis of claimable vs completed-position draws.

Run from repository root with .venv-tests/Scripts/python.exe.  No engine,
HTTP server, user game, archive file or production source is modified.
"""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import inspect
import json
import datetime
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
from game_archive import GameArchive
from game_outcome import automatic_outcome
from play_human import HumanGame

CYCLE = ["g1f3", "g8f6", "f3g1", "f6g8"]
checks = []


def expect(label, condition):
    checks.append({"label": label, "passed": bool(condition)})
    assert condition, label


def replay(moves, fen=chess.STARTING_FEN):
    board = chess.Board(fen)
    expect("valid initial FEN: " + fen, board.is_valid())
    for uci in moves:
        move = chess.Move.from_uci(uci)
        expect("legal move: " + uci, move in board.legal_moves)
        board.push(move)
    return board


def encoded(outcome):
    return None if outcome is None else {"result": outcome.result(), "reason": outcome.termination.name.lower()}


def proposed_completed_position_outcome(board):
    """Proof-only proposed online policy, not a production implementation."""
    outcome = board.outcome(claim_draw=False)
    if outcome is not None:
        return outcome
    if board.is_fifty_moves():
        return chess.Outcome(chess.Termination.FIFTY_MOVES, None)
    if board.is_repetition(3):
        return chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None)
    return None


def observed(label, board):
    before = (board.fen(en_passant="fen"), tuple(board.move_stack))
    actual = HumanGame._outcome(SimpleNamespace(board=board, _override=None))
    row = {"label": label, "fen": board.fen(en_passant="fen"),
           "moves": [move.uci() for move in board.move_stack],
           "current_twofold": board.is_repetition(2),
           "current_threefold": board.is_repetition(3),
           "current_fivefold": board.is_fivefold_repetition(),
           "current_fifty_moves": board.is_fifty_moves(),
           "can_claim_threefold": board.can_claim_threefold_repetition(),
           "can_claim_fifty_moves": board.can_claim_fifty_moves(),
           "compulsory_outcome": encoded(board.outcome(claim_draw=False)),
           "legacy_claim_outcome": encoded(board.outcome(claim_draw=True)),
           "actual_HumanGame_outcome": actual,
           "proposed_online_outcome": encoded(proposed_completed_position_outcome(board))}
    expect("production helper agrees with independent completed-position oracle: " + label,
           automatic_outcome(board) == proposed_completed_position_outcome(board))
    expect("inspection preserves full board and history: " + label,
           before == (board.fen(en_passant="fen"), tuple(board.move_stack)))
    return row


def main():
    rows = []
    before_third = replay((CYCLE * 2)[:7])
    rows.append(observed("7 plies: only next Ng8 creates the third occurrence", before_third))
    expect("7 plies: actual position is not threefold", not before_third.is_repetition(3))
    expect("7 plies: a repetition is claimable by intended move", before_third.can_claim_threefold_repetition())
    expect("7 plies: legacy claim policy would end one ply before occurrence", rows[-1]["legacy_claim_outcome"]["reason"] == "threefold_repetition")
    expect("7 plies: repaired host continues", rows[-1]["actual_HumanGame_outcome"] == ("*", None))
    expect("7 plies: proposed policy continues", rows[-1]["proposed_online_outcome"] is None)
    alternate = before_third.copy(stack=True)
    alternate.push_uci("e7e5")
    expect("7 plies: side to move can avoid repetition with e5", not alternate.can_claim_threefold_repetition())

    record = {"game_id": "0123456789abcdef0123456789abcdef",
              "uci_moves": [move.uci() for move in before_third.move_stack],
              "state": {"result": "*", "human_color": "white"}}
    prepared = GameArchive._prepare(record, record["game_id"])
    archive = {"input_result": "*", "prepared_result": prepared["result"],
               "prepared_termination": prepared["termination"]}
    expect("repaired archive keeps an uncompleted third repetition open", archive["prepared_result"] == "*")

    third = replay(CYCLE * 2)
    rows.append(observed("8 plies: actual third occurrence", third))
    expect("third occurrence must end under proposed online policy", rows[-1]["proposed_online_outcome"]["reason"] == "threefold_repetition")
    for plies in (15, 16):
        board = replay((CYCLE * 4)[:plies])
        rows.append(observed(str(plies) + " plies: compulsory fivefold boundary", board))
        expect("fivefold only after actual fifth occurrence: " + str(plies),
               (board.outcome(claim_draw=False) is not None) == (plies == 16))

    identity = []
    castle_fen = "4k1n1/8/8/8/8/8/8/4K1NR w K - 0 1"
    rook_cycle = ["h1h2", "g8f6", "h2h1", "f6g8"]
    rights_lost = replay(rook_cycle * 2, castle_fen)
    expect("same placement with lost castling rights does not count original", not rights_lost.is_repetition(3))
    expect("lost-rights positions themselves can repeat", rights_lost.is_repetition(2))
    identity.append({"label": "castling rights distinguish positions", "initial_fen": castle_fen,
                     "final_fen": rights_lost.fen(en_passant="fen"),
                     "same_piece_placement": rights_lost.board_fen() == chess.Board(castle_fen).board_fen(),
                     "current_threefold": rights_lost.is_repetition(3)})

    for label, fen, expected in [
        ("legal en passant right distinguishes initial position", "4k1n1/8/8/3pP3/8/8/8/4K1NR w K d6 0 1", False),
        ("unusable en passant square does not distinguish position", "4k1n1/8/8/3p4/8/8/8/4K1NR w K d6 0 1", True),
        ("pinned en passant is not a legal capture and does not distinguish position", "k3r1n1/8/8/3pP3/8/8/8/4K1NR w K d6 0 1", True),
    ]:
        initial = chess.Board(fen)
        board = replay(CYCLE * 2, fen)
        expect(label, board.is_repetition(3) == expected)
        identity.append({"label": label, "initial_fen": fen,
                         "initial_legal_en_passant": initial.has_legal_en_passant(),
                         "initial_pseudo_legal_en_passant": initial.has_pseudo_legal_en_passant(),
                         "final_fen": board.fen(en_passant="fen"), "current_threefold": board.is_repetition(3)})

    turn_fen = "7k/8/8/8/3R4/8/8/K7 w - - 0 1"
    different_turn = replay(["a1b1", "h8g8", "b1b2", "g8h8", "b2a1"], turn_fen)
    expect("same placement with opposite side to move is not a repetition", not different_turn.is_repetition(2))
    identity.append({"label": "side to move distinguishes positions", "initial_fen": turn_fen,
                     "final_fen": different_turn.fen(),
                     "same_piece_placement": different_turn.board_fen() == chess.Board(turn_fen).board_fen(),
                     "current_twofold": different_turn.is_repetition(2)})

    fifty_base = "8/8/8/8/8/2k5/8/R5K1 w - - {} 51"
    for halfmoves in (99, 100, 149, 150):
        board = chess.Board(fifty_base.format(halfmoves))
        rows.append(observed(str(halfmoves) + " reversible halfmoves", board))
        expect("50-move current boundary: " + str(halfmoves), board.is_fifty_moves() == (halfmoves >= 100))
        expect("75-move compulsory boundary: " + str(halfmoves),
               (board.outcome(claim_draw=False) is not None) == (halfmoves >= 150))
    expect("at 99 reversible halfmoves legacy policy would end before 100", rows[-4]["legacy_claim_outcome"]["reason"] == "fifty_moves")
    expect("at 99 reversible halfmoves repaired host continues", rows[-4]["actual_HumanGame_outcome"] == ("*", None))
    expect("at 99 proposed policy continues", rows[-4]["proposed_online_outcome"] is None)
    pawn = replay(["e2e4"], "8/8/8/8/8/2k5/4P3/R5K1 w - - 99 51")
    capture = replay(["a1a2"], "8/8/8/8/8/2k5/r7/R5K1 w - - 99 51")
    expect("pawn move resets fifty-move clock", pawn.halfmove_clock == 0 and not pawn.is_fifty_moves())
    expect("capture resets fifty-move clock", capture.halfmove_clock == 0 and not capture.is_fifty_moves())

    mate = replay(["g6g7"], "7k/8/5KQ1/8/8/8/8/8 w - - 149 75")
    rows.append(observed("checkmate at halfmove 150 has precedence over 75 moves", mate))
    expect("checkmate has priority over all draw thresholds", rows[-1]["proposed_online_outcome"] == {"result": "1-0", "reason": "checkmate"})

    output = {"run_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "python_version": platform.python_version(),
              "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "python_chess_version": chess.__version__, "assertions_passed": len(checks),
              "production_function_snapshot": inspect.getsource(HumanGame._outcome),
              "production_sources_sha256": {name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
                                             for name in ("play_human.py", "game_archive.py", "game_outcome.py")},
              "archive_reproduction": archive, "positions": rows, "position_identity": identity,
              "checks": checks}
    target = Path(__file__).with_name("after-fix-diagnosis.json")
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"assertions_passed": len(checks), "output": str(target),
                      "seven_ply_outcome": rows[0]["actual_HumanGame_outcome"],
                      "seven_ply_current_threefold": rows[0]["current_threefold"],
                      "archive": archive}, ensure_ascii=False))


if __name__ == "__main__":
    main()
