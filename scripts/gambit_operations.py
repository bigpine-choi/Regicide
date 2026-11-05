"""Post-sacrifice plans anchored to actual accepted material investments.

Goals and geometry order native probes; they are not CP, learned strength, or
proof that an attack succeeds. Every alternate move needs current native proof.
"""
import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import chess

import dynamic_play
from game_outcome import automatic_outcome
import sparring_material

POLICY = "regicide-gambit-operations-v2"
CURRICULUM = Path(__file__).resolve().parents[1] / "data/gambits/operations_curriculum.json"
MAX_AFTER_ACCEPTANCE_PLIES = 48
CONTINUATION_POLICY = "regicide-gambit-continuation-v2"
MAX_CONTINUATION_PLIES = 8
VALUES = sparring_material.VALUES
GOALS = ("development", "attack_maintenance", "material_recovery", "defensive_transition", "consolidation")


def material_balance(board, color):
    return sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
               for kind, value in VALUES.items())


def position_key(board):
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


@lru_cache(maxsize=4)
def _read(path, modified_ns, size):
    if not 0 < size <= 4 * 1024 * 1024:
        raise ValueError("invalid curriculum size")
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("schema") != "regicide.gambit-operations-curriculum.v1"
            or not isinstance(data.get("cases"), list)
            or not all(isinstance(case, dict) for case in data["cases"])):
        raise ValueError("invalid curriculum schema")
    payload = {key: value for key, value in data.items() if key != "curriculum_sha256"}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if data.get("curriculum_sha256") != expected:
        raise ValueError("curriculum checksum mismatch")
    anchors = []
    for case in data.get("cases", []):
        try:
            prefix = case.get("recognition_prefix_uci") or case["moves"][:max(
                case["acceptance"]["capture_ply"], case.get("family_confirmation_ply", 0))]
            if not prefix or case["owner"] not in ("white", "black"):
                continue
            board = chess.Board()
            before_capture, captured = None, None
            signature = case["signature"]
            signature_square = chess.parse_square(signature["offered_square"])
            offered_piece = None
            if not 0 < signature["ply"] < case["acceptance"]["capture_ply"] <= len(prefix):
                continue
            for ply, uci in enumerate(prefix, 1):
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise ValueError("illegal curriculum prefix")
                if ply == signature["ply"] and (
                        move.uci() != signature["uci"] or move.to_square != signature_square
                        or board.fen(en_passant="fen") != signature["before_fen"]):
                    raise ValueError("incorrect signature")
                if signature["ply"] < ply < case["acceptance"]["capture_ply"] and (
                        move.from_square == signature_square or move.to_square == signature_square):
                    raise ValueError("offer moved or captured earlier")
                if ply == case["acceptance"]["capture_ply"]:
                    before_capture = board.copy(stack=True)
                    victim_square = (move.to_square - 8 if board.turn else move.to_square + 8) if board.is_en_passant(move) else move.to_square
                    captured = board.piece_at(victim_square)
                    if (not board.is_capture(move) or captured is None
                            or captured.color != (case["owner"] == "white")
                            or victim_square != signature_square or captured != offered_piece
                            or move.uci() != case["acceptance"]["capture_uci"]
                            or board.fen(en_passant="fen") != case["acceptance"]["before_fen"]):
                        raise ValueError("unproven acceptance")
                board.push(move)
                if ply == signature["ply"]:
                    offered_piece = board.piece_at(signature_square)
                    if (offered_piece is None or offered_piece.symbol() != signature["offered_piece"]
                            or offered_piece.color != (case["owner"] == "white")
                            or board.fen(en_passant="fen") != signature["after_fen"]):
                        raise ValueError("incorrect offered piece")
            if before_capture is None or captured is None or material_balance(board, case["owner"] == "white") >= 0:
                continue
            anchors.append({"family_id": case["family_id"], "case_id": case["case_id"],
                "owner": case["owner"], "recognition_prefix_uci": prefix,
                "acceptance": copy.deepcopy(case["acceptance"]),
                "recognition_ply": len(prefix), "recognition_fen": board.fen(en_passant="fen"),
                "initial_material_debt_cp": -material_balance(board, case["owner"] == "white")})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    unique = {(row["family_id"], row["owner"], tuple(row["recognition_prefix_uci"])): row for row in anchors}
    return list(unique.values()), hashlib.sha256(raw).hexdigest()


def metadata():
    try:
        stat = CURRICULUM.stat()
        anchors, sha = _read(str(CURRICULUM), stat.st_mtime_ns, stat.st_size)
        return {"policy": POLICY, "available": bool(anchors), "curriculum_sha256": sha,
                "accepted_anchors": len(anchors), "window_plies": MAX_AFTER_ACCEPTANCE_PLIES,
                "goals": list(GOALS)}
    except (OSError, ValueError, TypeError, UnicodeError):
        return {"policy": POLICY, "available": False, "curriculum_sha256": None, "accepted_anchors": 0}


def _danger(board, color):
    king = board.king(color)
    if king is None:
        return 32
    ring = chess.SquareSet(chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king])
    return sum(len(board.attackers(not color, square)) for square in ring)


def goal(board, owner):
    own, opponent = dynamic_play._features(board, owner), dynamic_play._features(board, not owner)
    debt = max(0, -material_balance(board, owner))
    danger, enemy_danger = _danger(board, owner), _danger(board, not owner)
    # These are descriptive planning signals. Native evaluation remains the
    # authority, including when these geometrical signals misread the position.
    if board.turn == owner and board.is_check() or danger >= max(4, enemy_danger + 2):
        return "defensive_transition"
    if debt == 0:
        return "consolidation"
    if own["developed_minors"] < 3:
        return "development"
    if board.turn == owner:
        for move in board.legal_moves:
            value = 100 if board.is_en_passant(move) else VALUES.get(board.piece_type_at(move.to_square), 0)
            if board.is_capture(move) and value >= min(debt, 300):
                return "material_recovery"
    return "attack_maintenance"


def context(board, decisions=()):
    inactive = {"policy": POLICY, "active": False, "reason": "no_verified_accepted_sacrifice"}
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN
            or board.is_repetition(2) or automatic_outcome(board) is not None):
        return {**inactive, "reason": "invalid_or_terminal_history"}
    replay, history = chess.Board(), []
    try:
        for move in board.move_stack:
            if move not in replay.legal_moves:
                return {**inactive, "reason": "invalid_history"}
            history.append(move.uci())
            replay.push(move)
        if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
            return {**inactive, "reason": "altered_board"}
        stat = CURRICULUM.stat()
        anchors, sha = _read(str(CURRICULUM), stat.st_mtime_ns, stat.st_size)
    except (OSError, TypeError, ValueError, UnicodeError):
        return {**inactive, "reason": "curriculum_unavailable"}
    matching = [row for row in anchors if history[:row["recognition_ply"]] == row["recognition_prefix_uci"]]
    if not matching:
        return inactive
    anchor = max(matching, key=lambda row: (row["recognition_ply"], row["family_id"], row["case_id"]))
    owner = anchor["owner"] == "white"
    age = len(history) - anchor["acceptance"]["capture_ply"]
    purpose = goal(board, owner)
    reason = ("window_finished" if age > MAX_AFTER_ACCEPTANCE_PLIES else
              "opponent_to_move" if board.turn != owner else "in_check" if board.is_check() else
              "forced_reply" if board.legal_moves.count() <= 1 else
              "simplified_position" if len(board.piece_map()) < 8 else "accepted_sacrifice_operations")
    return {**copy.deepcopy(anchor), "policy": POLICY, "active": reason == "accepted_sacrifice_operations",
        "reason": reason, "accepted": True, "goal": purpose, "elapsed_since_acceptance_plies": age,
        "material_debt_cp": max(0, -material_balance(board, owner)),
        "material_balance_cp": material_balance(board, owner),
        "fen": board.fen(en_passant="fen"), "history_uci": history, "position_key": position_key(board),
        "curriculum_sha256": sha,
        "context_id": _digest([POLICY, sha, anchor["family_id"], history, board.fen(en_passant="fen"), purpose]),
        "loss_cap_cp": 15 if purpose in ("defensive_transition", "consolidation") else 35,
        "basis": "actual accepted prefix and current geometry; not a claim of optimal strategy"}


def _points(board, move, purpose):
    owner = board.turn
    before = dynamic_play._features(board, owner)
    danger = _danger(board, owner)
    gain = 100 if board.is_en_passant(move) else VALUES.get(board.piece_type_at(move.to_square), 0)
    after = board.copy(stack=False)
    after.push(move)
    own = dynamic_play._features(after, owner)
    activity = (own["mobility"] - before["mobility"]) / 4
    development = 5 * (own["developed_minors"] - before["developed_minors"]) + 4 * board.is_castling(move)
    safety = 4 * (danger - _danger(after, owner))
    pressure = own["coordinated_pressure"] - before["coordinated_pressure"]
    if purpose == "defensive_transition":
        return safety + activity + 2 * board.is_castling(move) - max(0, -development)
    if purpose == "material_recovery":
        return gain / 50 + safety + activity
    if purpose == "consolidation":
        return safety + development + activity + gain / 100
    if purpose == "development":
        return development + safety + activity + pressure
    return 2 * pressure + development + activity + safety


def proposals(board, seed, *, context_value=None, limit=4):
    ctx = context(board) if context_value is None else context_value
    if not ctx.get("active") or ctx.get("fen") != board.fen(en_passant="fen") or not 1 <= limit <= 8:
        return []
    moves = sorted(board.legal_moves, key=lambda move: (-_points(board, move, ctx["goal"]),
                         _digest([POLICY, seed, ctx["context_id"], move.uci()])))[:limit]
    return [{"uci": move.uci(), "source": "native_operations_geometry", "policy": POLICY,
             "context_id": ctx["context_id"], "family_id": ctx["family_id"], "goal": ctx["goal"],
             "fen": ctx["fen"], "history_uci": list(ctx["history_uci"]),
             "curriculum_sha256": ctx["curriculum_sha256"], "loss_cap_cp": 0,
             "candidate_only": True} for move in moves]


def continuation_evidence(board, pv, purpose):
    """Measure a bounded settled native continuation; never change CP.

    Start with the candidate and actual opponent reply. Extend only actual
    PV recaptures on pending exchange squares, or intervening checks/evasions.
    An available recapture needs a next PV move to establish whether it was
    taken or declined. A truncated exchange/check supplies no plan credit.
    """
    if (not isinstance(board, chess.Board) or not board.is_valid()
            or purpose not in GOALS or not isinstance(pv, (list, tuple)) or len(pv) < 2):
        return None
    replay = board.copy(stack=True)
    prefix, extensions = [], []
    capture_squares = set()

    def push(move):
        evasion = replay.is_check()
        captured = replay.is_capture(move)
        replay.push(move)
        if captured:
            capture_squares.add(move.to_square)
        elif not evasion and not replay.is_check():
            capture_squares.clear()
        # A checking intermediate move and its evasion can postpone a
        # recapture, even if that evasion captures a different checker.
        # Retain pending squares across the forced interruption.
        prefix.append(move)
        return automatic_outcome(replay) is None

    try:
        for uci in pv[:2]:
            move = chess.Move.from_uci(uci) if isinstance(uci, str) else uci
            if not isinstance(move, chess.Move) or move not in replay.legal_moves:
                return None
            if not push(move):
                return None
        while True:
            checked = replay.is_check()
            recaptures = ([move for move in replay.generate_legal_captures()
                           if move.to_square in capture_squares] if capture_squares else [])
            if not checked and not recaptures:
                break
            if len(prefix) >= len(pv):
                return None
            uci = pv[len(prefix)]
            move = chess.Move.from_uci(uci) if isinstance(uci, str) else uci
            if not isinstance(move, chess.Move) or move not in replay.legal_moves:
                return None
            intermediate_check = not checked and move not in recaptures and replay.gives_check(move)
            if not checked and move not in recaptures and not intermediate_check:
                # The native PV explicitly declines this recapture. Do not
                # accumulate development credit from an unrelated longer PV.
                break
            if len(prefix) >= MAX_CONTINUATION_PLIES:
                return None
            extensions.append({"ply": len(prefix) + 1, "uci": move.uci(),
                               "reason": "check_evasion" if checked else
                                         "intermediate_check" if intermediate_check else "recapture"})
            if not push(move):
                return None
    except (TypeError, ValueError):
        return None
    owner = board.turn
    before, after = dynamic_play._features(board, owner), dynamic_play._features(replay, owner)
    safety = _danger(board, owner) - _danger(replay, owner)
    development = after["developed_minors"] - before["developed_minors"]
    activity = (after["mobility"] - before["mobility"]) / 4
    pressure = after["coordinated_pressure"] - before["coordinated_pressure"]
    material = material_balance(replay, owner) - material_balance(board, owner)
    undeveloped_before = sum(len(board.pieces(kind, owner)) for kind in (chess.KNIGHT, chess.BISHOP)) - before["developed_minors"]
    undeveloped_after = sum(len(replay.pieces(kind, owner)) for kind in (chess.KNIGHT, chess.BISHOP)) - after["developed_minors"]
    # A settled equal exchange of already developed minors is not retreat.
    # Count the remaining development burden only without a material loss;
    # simply losing an undeveloped piece must not earn a development bonus.
    development_progress = undeveloped_before - undeveloped_after if material >= 0 else development
    development_basis = "undeveloped_reduction_without_material_loss" if material >= 0 else "raw_developed_change_after_material_loss"
    castled = int(board.is_castling(prefix[0]))
    points = (4 * safety + activity + 2 * castled if purpose == "defensive_transition" else
              material / 50 + 2 * safety + activity if purpose == "material_recovery" else
              3 * safety + 3 * development_progress + activity + material / 100 if purpose == "consolidation" else
              5 * development_progress + 4 * castled + 2 * safety + activity + pressure if purpose == "development" else
              2 * pressure + 3 * development_progress + activity + 2 * safety)
    return {"goal": purpose, "points": points, "material_change_cp": material,
            "king_danger_reduction": safety, "developed_minors_change": development,
            "development_progress": development_progress, "development_change_basis": development_basis,
            "undeveloped_minors_before": undeveloped_before, "undeveloped_minors_after": undeveloped_after,
            "pressure_change": pressure, "reply_fen": replay.fen(en_passant="fen"),
            "pv_prefix_uci": [move.uci() for move in prefix],
            "continuation_policy": CONTINUATION_POLICY, "measured_plies": len(prefix),
            "extensions": extensions, "settled": True,
            "basis": "observed geometry after bounded native reply exchanges/check evasions; not CP or a forced plan"}


def valid_proposal(board, row):
    ctx = context(board)
    if not isinstance(row, dict) or not ctx.get("active"):
        return False
    try:
        return (row.get("source") == "native_operations_geometry" and row.get("policy") == POLICY
                and row.get("context_id") == ctx["context_id"] and row.get("family_id") == ctx["family_id"]
                and row.get("goal") == ctx["goal"] and row.get("fen") == ctx["fen"]
                and row.get("history_uci") == ctx["history_uci"]
                and row.get("curriculum_sha256") == ctx["curriculum_sha256"]
                and chess.Move.from_uci(row["uci"]) in board.legal_moves)
    except (ValueError, TypeError, KeyError):
        return False
