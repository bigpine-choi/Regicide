"""Reply-supported attacks after authored gambit preparation or an offer.

Geometry orders native proposals; it never replaces centipawns or proves an
attack. Fresh substantial material offers belong to the separate trap proof.
"""
import copy
import math
import threading
import time

import chess
import chess.engine

import boss_openings
import dynamic_play
import opening_variety as opening
import sparring_material

POLICY = "regicide-gambit-initiative-v2"
SOURCE_POLICY = "regicide-boss-play-v5"
SOURCE_POLICIES = (SOURCE_POLICY, "regicide-boss-play-v6", "regicide-boss-play-v7", "regicide-boss-play-v8")
MAX_WINDOW_PLIES = 20
MAX_EXTRA_MS = 1000
MAX_PROBES = 2
MIN_DEPTH = 4
MIN_PROBE_MS = 50
TOLERANCE_CP = 35
CONVERSION_CP = 300
VALUES = sparring_material.VALUES


def _material(board, color):
    return sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
               for kind, value in VALUES.items())


def _positions(board):
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN):
        return None
    position, result = board.root(), [board.root()]
    for move in board.move_stack:
        if move not in position.legal_moves:
            return None
        position.push(move)
        result.append(position.copy(stack=False))
    return result if position.fen(en_passant="fen") == board.fen(en_passant="fen") else None


def context(board, decisions):
    """Recognize an actual v5 signature or surviving authored preparation.

    Offered/accepted values are gross signature-square exposure/capture values.
    Debt is the current net balance; a recaptured exchange is not a sacrifice.
    """
    result = {"active": False, "reason": "no_authored_context", "line_id": None,
              "stage": None, "signature_complete": False, "signature_ply": None,
              "signature_uci": None, "signature_fen": None, "source_ply": None,
              "entry_ply": None, "material_debt_cp": 0, "material_offered_cp": 0,
              "material_accepted_cp": 0, "accepted": False, "policy": POLICY}
    positions = _positions(board)
    if positions is None:
        return {**result, "reason": "invalid_or_custom_history"}
    history = [move.uci() for move in board.move_stack]
    result.update(fen=board.fen(en_passant="fen"), history_uci=history,
                  turn="white" if board.turn else "black")
    rows = decisions.values() if isinstance(decisions, dict) else decisions
    rows = rows if isinstance(rows, (list, tuple)) or hasattr(rows, "__iter__") else ()
    catalog = {row["line_id"]: row for row in boss_openings.repertoire()}
    signatures, preparations, legacy = [], [], False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("policy") not in SOURCE_POLICIES:
            legacy |= row.get("policy") in ("regicide-boss-play-v3", "regicide-boss-play-v4")
            continue
        line_id = row.get("line_id")
        source = catalog.get(line_id) if isinstance(line_id, str) else None
        if source is None:
            continue
        ply = row.get("ply")
        if type(ply) is not int or not 1 <= ply <= min(len(history), len(source["moves"])):
            continue
        prefix = source["moves"][:ply]
        owner = source["color"] == "white"
        complete = ply == len(source["moves"])
        if (history[:ply] != prefix or positions[ply - 1].turn != owner
                or row.get("line_complete") is not complete or row.get("history_uci") != prefix[:-1]
                or row.get("before_fen") != positions[ply - 1].fen(en_passant="fen")
                or row.get("selected_uci") != prefix[-1] or row.get("candidate_uci") != prefix[-1]
                or not isinstance(row.get("seed"), str) or not row["seed"]):
            continue
        item = (ply, source["line_id"], row, source)
        (signatures if complete else preparations).append(item)
    valid = signatures or preparations
    if not valid:
        return {**result, "reason": "legacy_policy" if legacy else result["reason"]}
    ply, line_id, row, source = max(valid, key=lambda item: (item[0], item[1]))
    owner = source["color"] == "white"
    complete = bool(signatures)
    anchor_move = chess.Move.from_uci(source["moves"][ply - 1])
    anchor = positions[ply]
    offered = anchor.piece_at(anchor_move.to_square) if complete else None
    exposed = ([move.uci() for move in anchor.legal_moves
                if move.to_square == anchor_move.to_square and anchor.is_capture(move)] if complete else [])
    capture = None
    if complete and len(history) > ply:
        reply = chess.Move.from_uci(history[ply])
        if reply.to_square == anchor_move.to_square and anchor.is_capture(reply):
            capture = {"ply": ply + 1, "uci": reply.uci(),
                       "value_cp": VALUES[offered.piece_type] if offered else 0}
    offset = min(len(history), len(source["moves"]))
    still_in_repertoire = history[:offset] == source["moves"][:offset]
    result.update(line_id=line_id, opening_name=source["name"],
                  stage="offered" if complete else "preparation", signature_complete=complete,
                  source_ply=ply, entry_ply=ply, source_uci=anchor_move.uci(),
                  source_fen=anchor.fen(en_passant="fen"),
                  source_before_fen=positions[ply - 1].fen(en_passant="fen"),
                  signature_ply=ply if complete else None,
                  source_policy=row["policy"],
                  owner="white" if owner else "black",
                  signature_uci=anchor_move.uci() if complete else None, seed=row["seed"],
                  signature_fen=anchor.fen(en_passant="fen") if complete else None,
                  elapsed_plies=len(history) - ply,
                  material_debt_cp=max(0, -_material(board, owner)),
                  source_material_balance_cp=_material(anchor, owner),
                  signature_material_balance_cp=_material(anchor, owner) if complete else None,
                  material_offered_cp=VALUES[offered.piece_type] if exposed and offered else 0,
                  material_accepted_cp=capture["value_cp"] if capture else 0,
                  accepted=capture is not None, signature_capture=capture,
                  preparation_in_repertoire=not complete and still_in_repertoire,
                  material_basis=("gross signature-square exposure/capture; debt is current net balance, not final sacrifice cost"
                                  if complete else "authored preparation only; no claimed offer/acceptance; debt is observed net balance"))
    reason = ("window_finished" if len(history) - ply > MAX_WINDOW_PLIES else
              "terminal" if board.is_game_over(claim_draw=True) else
              "in_check" if board.is_check() else
              "repetition" if opening.has_repetition(board) else
              "forced_reply" if board.legal_moves.count() <= 1 else
              "simplified_position" if dynamic_play._phase(board) < 8 or dynamic_play._nonpawns(board) < 5 else
              "preparation_in_repertoire" if not complete and still_in_repertoire else
              "opponent_to_move" if board.turn != owner else None)
    return {**result, "active": reason is None,
            "reason": reason or ("completed_authored_gambit" if complete else "authored_preparation_offbook")}


def _line(board, info, required):
    if (not isinstance(info, dict) or not isinstance(info.get("score"), chess.engine.PovScore)
            or info["score"].turn != board.turn or info.get("multipv", 1) != 1):
        return None
    return opening.score_line(board, info, required)


def _style(board, pv):
    value = dynamic_play._style(board, pv)
    if value is None:
        return None
    components = value["components"]
    weighted = {key: points * (1 if key == "activity" else 2)
                for key, points in components.items()}
    return {**value, "points": sum(weighted.values()), "gambit_components": weighted,
            "basis": "native two-ply line; retained development/open lines/coordinated king pressure; geometric points, not CP"}


def _proposal_points(board, move):
    after = board.copy(stack=False)
    after.push(move)
    before_features = dynamic_play._features(board, board.turn)
    after_features = dynamic_play._features(after, board.turn)
    return (dynamic_play._proposal_points(board, move.uci())
            + 3 * (after_features["developed_minors"] - before_features["developed_minors"])
            + 3 * (after_features["rook_files"] - before_features["rook_files"])
            + 4 * (after_features["coordinated_pressure"] - before_features["coordinated_pressure"]))


def _proposals(board, baseline, seed):
    return sorted((move for move in board.legal_moves if move != baseline),
                  key=lambda move: (-_proposal_points(board, move),
                      opening._digest([POLICY, seed, opening.history_key(board), move.uci()]), move.uci()))[:MAX_PROBES]


def _material_screen(board, move, deadline):
    """No new substantial loss after any immediate legal capture/re-capture.

    Compensation credit requires a complete bounded legal exchange tree.
    Tactical compensation beyond that screen belongs to the trap policy.
    """
    gain = (100 if board.is_en_passant(move) else
            VALUES[board.piece_type_at(move.to_square)] if board.is_capture(move) else 0)
    gain += VALUES[move.promotion] - 100 if move.promotion else 0
    after = board.copy(stack=True)
    after.push(move)
    if after.is_game_over(claim_draw=True):
        return {"admitted": False, "reason": "terminal_alternative", "captures": []}
    rows = []
    for reply in list(after.legal_moves):
        if time.monotonic() >= deadline:
            return {"admitted": False, "reason": "material_screen_timeout", "captures": rows}
        victim = after.piece_at(reply.to_square)
        after.push(reply)
        try:
            if after.is_checkmate():
                return {"admitted": False, "reason": "immediate_mate_reply", "captures": rows}
            if victim is None or victim.color != board.turn or VALUES[victim.piece_type] < 300:
                continue
            remaining = (deadline - time.monotonic()) * 1000
            if remaining < 1:
                return {"admitted": False, "reason": "material_screen_timeout", "captures": rows}
            exchange = sparring_material.exchange_gain(after, reply.to_square,
                                                       max_time_ms=max(1, min(25, int(remaining))))
            net = gain - VALUES[victim.piece_type] + exchange["gain_cp"]
            net -= VALUES[reply.promotion] - 100 if reply.promotion else 0
            row = {"reply_uci": reply.uci(), "victim_cp": VALUES[victim.piece_type],
                   "candidate_gain_cp": gain, "recapture_exchange": exchange, "net_material_cp": net}
            rows.append(row)
            if not exchange["complete"] or net < -100:
                return {"admitted": False, "reason": "new_piece_donation", "captures": rows}
        finally:
            after.pop()
    return {"admitted": True, "reason": "no_new_substantial_material_offer", "captures": rows}


def _anchor_matches(ctx, source, positions, history):
    if source is None or positions is None:
        return False
    ply = ctx.get("entry_ply")
    if (type(ply) is not int or not 1 <= ply <= min(len(history), len(source["moves"]))
            or ctx.get("source_ply") != ply or ctx.get("source_uci") != source["moves"][ply - 1]
            or history[:ply] != source["moves"][:ply]
            or positions[ply - 1].turn != (source["color"] == "white")
            or ctx.get("source_before_fen") != positions[ply - 1].fen(en_passant="fen")
            or ctx.get("source_fen") != positions[ply].fen(en_passant="fen")
            or len(history) - ply > MAX_WINDOW_PLIES):
        return False
    if ctx.get("stage") == "offered":
        return (ctx.get("signature_complete") is True and ply == len(source["moves"])
                and ctx.get("signature_ply") == ply and ctx.get("signature_uci") == source["moves"][-1]
                and ctx.get("signature_fen") == positions[ply].fen(en_passant="fen"))
    if ctx.get("stage") == "preparation":
        offset = min(len(history), len(source["moves"]))
        return (ctx.get("signature_complete") is False and ply < len(source["moves"])
                and ctx.get("signature_ply") is None and ctx.get("signature_uci") is None
                and ctx.get("signature_fen") is None
                and history[:offset] != source["moves"][:offset]
                and ctx.get("material_offered_cp") == 0 and ctx.get("material_accepted_cp") == 0
                and ctx.get("accepted") is False and ctx.get("signature_capture") is None)
    return False


def choose(board, baseline_move, baseline_info, baseline_packets, search, close_engine, *,
           seed, budget_ms, context):
    """Choose at most two native root probes within the caller's shared budget."""
    started = time.monotonic()
    ctx = copy.deepcopy(context) if isinstance(context, dict) else {}
    baseline = _line(board, baseline_info, baseline_move)
    result = {"policy": POLICY, "context": ctx, "selected_uci": baseline_move.uci(),
              "applied": False, "reason": "inactive_context", "loss_cp": 0,
              "baseline": baseline, "attempts": [], "evidence": None,
              "budget_ms": 0, "extra_elapsed_ms": 0, "tolerance_cp": TOLERANCE_CP}
    line_id = ctx.get("line_id")
    source = (next((row for row in boss_openings.repertoire() if row["line_id"] == line_id), None)
              if isinstance(line_id, str) else None)
    history = [move.uci() for move in board.move_stack]
    positions = _positions(board)
    if (ctx.get("active") is not True or ctx.get("policy") != POLICY
            or ctx.get("source_policy") not in SOURCE_POLICIES or source is None
            or not _anchor_matches(ctx, source, positions, history)
            or (source["color"] == "white") != board.turn
            or board.is_check() or board.is_game_over(claim_draw=True) or opening.has_repetition(board)
            or board.legal_moves.count() <= 1 or dynamic_play._phase(board) < 8 or dynamic_play._nonpawns(board) < 5
            or ctx.get("fen") != board.fen(en_passant="fen")
            or ctx.get("history_uci") != [move.uci() for move in board.move_stack]
            or ctx.get("turn") != ("white" if board.turn else "black") or ctx.get("seed") != seed):
        return result, False
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms < MIN_PROBE_MS:
        result["reason"] = "short_budget"
        return result, False
    if baseline is None or baseline["depth"] < MIN_DEPTH:
        result["reason"] = "baseline_unreliable"
        return result, False
    if (baseline["cp"] >= CONVERSION_CP or dynamic_play._pv_has_terminal(board, baseline["pv_uci"])):
        result["reason"] = "forcing_or_conversion_baseline"
        return result, False
    base_style = _style(board, baseline["pv_uci"])
    if base_style is None:
        result["reason"] = "insufficient_reply_evidence"
        return result, False
    packet = baseline_packets.get(baseline["depth"]) if isinstance(baseline_packets, dict) else None
    if packet is not None and _line(board, packet, baseline_move) != baseline:
        result["reason"] = "baseline_packet_conflict"
        return result, False
    result.update(reason="no_verified_initiative_alternative", budget_ms=min(budget_ms, MAX_EXTRA_MS),
                  baseline_evidence=base_style)
    deadline = started + result["budget_ms"] / 1000
    done, expired = threading.Event(), threading.Event()
    def expire():
        if not done.is_set():
            expired.set()
            try:
                close_engine()
            except Exception:
                pass
    watchdog = threading.Timer(max(0, deadline - time.monotonic()) + .1, expire)
    watchdog.daemon = True
    watchdog.start()
    discard, chosen = False, None
    try:
        proposals = _proposals(board, baseline_move, seed)
        for index, move in enumerate(proposals):
            if expired.is_set() or time.monotonic() >= deadline:
                break
            attempt = {"uci": move.uci(), "admitted": False, "reason": "unverified"}
            result["attempts"].append(attempt)
            screen = _material_screen(board, move, deadline)
            attempt["material_screen"] = screen
            if not screen["admitted"]:
                attempt["reason"] = screen["reason"]
                continue
            amount = ((deadline - time.monotonic()) * 1000 - 10) / max(1, len(proposals) - index)
            if amount < MIN_PROBE_MS:
                attempt["reason"] = "insufficient_probe_budget"
                break
            attempt["budget_ms"] = round(amount, 3)
            reply, final_info, packets = search(amount, root_move=move, publish=False,
                                                fresh=True, depth=baseline["depth"])
            common = _line(board, (packets or {}).get(baseline["depth"]), move)
            final = _line(board, final_info, move)
            attempt.update(candidate_compared=common, candidate_final=final)
            if reply.move != move or common is None or final is None or final["depth"] < baseline["depth"]:
                attempt["reason"] = "candidate_unreliable_or_incomplete"
                continue
            if (final["depth"] == baseline["depth"] and final["cp"] != common["cp"]):
                attempt["reason"] = "candidate_score_conflict"
                continue
            if min(common["cp"], final["cp"]) < baseline["cp"] - TOLERANCE_CP:
                attempt["reason"] = "outside_quality_floor"
                continue
            if dynamic_play._pv_has_terminal(board, final["pv_uci"]):
                attempt["reason"] = "terminal_continuation"
                continue
            style = _style(board, final["pv_uci"])
            attempt["style"] = style
            if style is None or style["points"] <= base_style["points"]:
                attempt["reason"] = "no_retained_attacking_gain"
                continue
            attempt.update(admitted=True, reason="quality_gated_reply_supported_initiative",
                           loss_cp=max(0, baseline["cp"] - common["cp"]))
            if chosen is None or (style["points"], -attempt["loss_cp"], move.uci()) > (
                    chosen["style"]["points"], -chosen["loss_cp"], chosen["uci"]):
                chosen = attempt
    except Exception as error:
        result.update(reason="initiative_probe_failed", error=type(error).__name__)
        discard, chosen = True, None
    finally:
        done.set()
        watchdog.cancel()
        watchdog.join(timeout=.2)
    elapsed = (time.monotonic() - started) * 1000
    result["extra_elapsed_ms"] = round(elapsed, 3)
    if expired.is_set() or time.monotonic() > deadline:
        result["reason"] = "initiative_probe_timeout"
        return result, True
    if chosen is not None:
        result.update(selected_uci=chosen["uci"], applied=True, reason=chosen["reason"],
                      loss_cp=chosen["loss_cp"], evidence=copy.deepcopy(chosen))
    return result, discard
