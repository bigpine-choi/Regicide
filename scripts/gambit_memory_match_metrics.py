"""Descriptive move metrics for a memory A/B match, without engine searches.

Prefix moves are not replayed as observations. A receipt-supported proposal,
an actually played offer, and an actual capture are separate events. Activity
is one-ply geometric activity, not native evaluation, safety or a site grade.
"""
from __future__ import annotations

import chess

import boss_openings
import boss_play
import dynamic_play
import gambit_operations
import gambit_practice_memory
import sound_sacrifice
from sparring_material import VALUES

SCHEMA = "regicide.gambit-memory-move-metrics.v1"


def _identity(board, row):
    return (isinstance(row, dict) and row.get("before_fen", row.get("fen")) == board.fen(en_passant="fen")
            and row.get("history_uci") == [move.uci() for move in board.move_stack])


def _material(board, color):
    return sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
               for kind, value in VALUES.items())


def _operation(context):
    return {key: context.get(key) for key in ("active", "accepted", "reason", "family_id", "owner", "goal",
        "material_debt_cp", "material_balance_cp", "elapsed_since_acceptance_plies")}


def _rows(value):
    return value if isinstance(value, (list, tuple)) else []


def _memory_row(board, row):
    try:
        return (isinstance(row, dict) and row.get("source") == gambit_practice_memory.SOURCE
                and _identity(board, row) and chess.Move.from_uci(row["uci"]) in board.legal_moves)
    except (KeyError, TypeError, ValueError):
        return False


def _line(board, row, uci):
    if (not isinstance(row, dict) or type(row.get("depth")) is not int or row["depth"] < 1
            or type(row.get("cp")) is not int or not isinstance(row.get("pv_uci"), list)
            or not row["pv_uci"] or row["pv_uci"][0] != uci):
        return False
    replay = board.copy(stack=True)
    try:
        for word in row["pv_uci"]:
            move = chess.Move.from_uci(word)
            if replay.outcome(claim_draw=False) is not None or move not in replay.legal_moves:
                return False
            replay.push(move)
    except (TypeError, ValueError):
        return False
    return True


def move_metrics(board, move, decision=None, operation=None, proposals=None):
    """Describe this played move; do not adjudicate strength or recompute search.

    ``board`` includes the complete pre-move history. ``decision`` is the host's
    current boss receipt, ``operation`` optionally its pre-move operations
    context, and ``proposals`` the pre-move memory/proposal rows. The function
    never mutates those objects. ``memory_probed_uci`` counts attempts with a
    legal native comparison packet; raw search logs remain the authority for
    calls that failed before returning a packet.
    """
    if not isinstance(board, chess.Board) or board.chess960 or not board.is_valid():
        raise ValueError("A valid standard-chess board is required")
    move = chess.Move.from_uci(move) if isinstance(move, str) else move
    if not isinstance(move, chess.Move) or move not in board.legal_moves:
        raise ValueError("Metrics require an actually legal move")
    before_fen, history = board.fen(en_passant="fen"), [m.uci() for m in board.move_stack]
    uci, actor, ply = move.uci(), board.turn, len(board.move_stack) + 1
    after = board.copy(stack=True)
    after.push(move)
    decision = decision if isinstance(decision, dict) else {}
    receipt_valid = (_identity(board, decision) and decision.get("policy") in boss_play.SUPPORTED_POLICIES
                     and decision.get("selected_uci") == uci and decision.get("ply") == ply)

    catalog = {row["line_id"]: row for row in boss_openings.repertoire()}
    def signature(line_id):
        row = catalog.get(line_id)
        return row if (row is not None and board.root().fen(en_passant="fen") == chess.STARTING_FEN
            and (row["color"] == "white") == actor and len(row["moves"]) == ply
            and row["moves"][:-1] == history) else None
    planned = signature(decision.get("proposed_line_id")) if receipt_valid else None
    proposed = bool(planned and decision.get("mode") == "opening"
                    and decision.get("candidate_uci") == planned["moves"][-1])
    selected = signature(decision.get("line_id")) if receipt_valid else None
    signature_selected = bool(selected and decision.get("line_complete") is True
                              and decision.get("candidate_uci") == uci == selected["moves"][-1])
    actual_signature = next((row for line_id in catalog
        if (row := signature(line_id)) and row["moves"][-1] == uci), None)
    capture_options = []
    if actual_signature:
        capture_options = [reply.uci() for reply in after.legal_moves
                           if reply.to_square == move.to_square and after.is_capture(reply)]

    before_context = operation if _identity(board, operation) else gambit_operations.context(board)
    after_context = gambit_operations.context(after)
    acceptance = after_context.get("acceptance") or {}
    accepted = bool(after_context.get("accepted") is True and acceptance.get("capture_ply") == ply
                    and acceptance.get("capture_uci") == uci and board.is_capture(move)
                    and board.fen(en_passant="fen") == acceptance.get("before_fen"))
    captured_square = (move.to_square - 8 if actor else move.to_square + 8) if board.is_en_passant(move) else move.to_square
    victim = board.piece_at(captured_square) if board.is_capture(move) else None

    detail = decision.get("tactics") if receipt_valid else None
    detail = detail if isinstance(detail, dict) else {}
    evidence = detail.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    geometry = evidence.get("sacrifice")
    geometry = geometry if isinstance(geometry, dict) else {}
    kinds = _rows(detail.get("kinds")) or [detail.get("kind")]
    sacrifice_selected = bool(detail.get("qualified") is True and detail.get("selected_uci") == uci
        and "sound_sacrifice_candidate" in kinds
        and isinstance(geometry, dict) and geometry.get("policy") == sound_sacrifice.POLICY
        and geometry.get("eligible") is True and geometry.get("complete") is True
        and geometry.get("uci") == uci and _identity(board, geometry))

    offered = []
    for row in _rows(proposals):
        if _memory_row(board, row) and row["uci"] not in offered:
            offered.append(row["uci"])
    knowledge = decision.get("knowledge") if receipt_valid else None
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    attempted, probed, admitted = [], [], []
    for attempt in _rows(knowledge.get("attempts")):
        row = attempt.get("source") if isinstance(attempt, dict) else None
        if not _memory_row(board, row) or row["uci"] != attempt.get("uci"):
            continue
        word = row["uci"]
        if word not in attempted:
            attempted.append(word)
        if _line(board, attempt.get("candidate_compared"), word) and word not in probed:
            probed.append(word)
        if attempt.get("admitted") is True and word in probed and word not in admitted:
            admitted.append(word)
    memory_applied = bool(knowledge.get("applied") is True and knowledge.get("selected_uci") == uci
                          and decision.get("baseline_uci") != uci and uci in admitted)
    own_before, own_after = dynamic_play._features(board, actor), dynamic_play._features(after, actor)
    material_before, material_after = _material(board, actor), _material(after, actor)
    return {"schema": SCHEMA, "ply": ply, "uci": uci, "actor_color": "white" if actor else "black",
        "receipt_valid": receipt_valid, "mode": decision.get("mode") if receipt_valid else None,
        "authored_signature_proposed": proposed, "authored_signature_selected": signature_selected,
        "signature_line_id": selected["line_id"] if signature_selected else None,
        "catalog_signature_played_this_move": actual_signature is not None,
        "catalog_signature_line_id": actual_signature["line_id"] if actual_signature else None,
        "signature_immediate_capture_available": bool(capture_options), "signature_capture_options_uci": capture_options,
        "catalog_signature_accepted_this_move": accepted,
        "accepted_signature_owner": after_context.get("owner") if accepted else None,
        "accepted_signature_family_id": after_context.get("family_id") if accepted else None,
        "accepted_signature_capture_ply": ply if accepted else None,
        "actual_capture": victim is not None, "captured_piece": victim.symbol() if victim else None,
        "captured_material_units": VALUES[victim.piece_type] if victim else 0,
        "selected_sacrifice_candidate_from_receipt": sacrifice_selected,
        "sacrifice_candidate_changed_move": sacrifice_selected and detail.get("applied") is True
            and decision.get("baseline_uci") != uci,
        "local_sacrifice_investment_cp": geometry.get("investment_cp") if sacrifice_selected else None,
        "memory_proposed_uci": offered, "memory_attempted_uci": attempted,
        "memory_probed_uci": probed, "memory_admitted_uci": admitted,
        "selected_matches_memory_proposal": uci in offered, "memory_changed_move": memory_applied,
        "operations_before": _operation(before_context), "operations_after": _operation(after_context),
        "activity": {"before": own_before, "after_one_ply": own_after,
            "delta": {key: own_after[key] - own_before[key] for key in own_before},
            "basis": "one-ply pseudo activity of mover; not safe mobility, native CP or a retained two-ply attack"},
        "material": {"before_own": material_before, "after_own": material_after,
            "delta_own": material_after - material_before,
            "basis": "P100 N300 B300 R500 Q900; material units, not engine evaluation"},
        "position_unchanged": before_fen == board.fen(en_passant="fen") and history == [m.uci() for m in board.move_stack],
        "scope": "played continuation only; receipt proposals, played offers and actual captures are separate; no Chess.com grade or strength claim"}
