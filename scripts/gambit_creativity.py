"""Explore a quiet plan only after the existing gambit choice keeps its baseline.

Motifs determine which alternative earns a search, never an evaluation bonus.
The candidate must meet the baseline in completed root searches, or a small
root deficit must be overturned by paired unrestricted child verification.
The selected motif must survive the actual opponent reply and exchange line.
"""
from collections import defaultdict
import copy
import math
import threading
import time

import chess

import boss_openings
import dynamic_play
import gambit_initiative as attack
import gambit_knowledge
import gambit_operations as operations
import opening_variety as opening

POLICY = "regicide-gambit-creativity-v3"
# Three fixed 24-pair development runs produced no actual creative selections.
# Preserve the verified ordinary policy until both diversity and quality pass.
ENABLED = False
MIN_PROBE_MS = 40
MIN_DEPTH = 4
MAX_EXTRA_MS = 1000
MAX_PLIES = 80
CONVERSION_CP = 300
MAX_SCREENED = 6
TOP_PER_MOTIF = 3
SAMPLED_PER_MOTIF = 2
LOSS_CAP_CP = 0
ROOT_RECHECK_BAND_CP = 10


def _context(board, plan, seed):
    """Revalidate every supplied source context; a plan is not authority."""
    if not isinstance(plan, dict) or plan.get("mode") != "initiative" or not plan.get("eligible"):
        return None
    supplied = False
    purpose = operations.goal(board, board.turn)
    if plan.get("operations_context") is not None:
        supplied = True
        wanted, current = plan["operations_context"], operations.context(board)
        if (not isinstance(wanted, dict) or not current.get("active")
                or current.get("owner") != ("white" if board.turn else "black")
                or wanted != current):
            return None
        purpose = current["goal"]
    if plan.get("knowledge_context") is not None:
        supplied = True
        wanted, current = plan["knowledge_context"], gambit_knowledge.context(board)
        if (not isinstance(wanted, dict) or not current.get("active")
                or current.get("owner") != ("white" if board.turn else "black") or wanted != current):
            return None
    if plan.get("initiative_context") is not None:
        supplied = True
        ctx = plan["initiative_context"]
        if not isinstance(ctx, dict):
            return None
        source = next((row for row in boss_openings.repertoire() if row["line_id"] == ctx.get("line_id")), None)
        history = [move.uci() for move in board.move_stack]
        if (not ctx.get("active") or ctx.get("policy") != attack.POLICY
                or ctx.get("source_policy") not in attack.SOURCE_POLICIES
                or source is None or (source["color"] == "white") != board.turn
                or ctx.get("fen") != board.fen(en_passant="fen")
                or ctx.get("history_uci") != history or ctx.get("seed") != seed
                or ctx.get("turn") != ("white" if board.turn else "black")
                or ctx.get("owner") != ("white" if board.turn else "black")
                or not attack._anchor_matches(ctx, source, attack._positions(board), history)):
            return None
    return purpose if supplied else None


def _piece_reach(board, square, color):
    pawn_attacks = 0
    for pawn in board.pieces(chess.PAWN, not color):
        pawn_attacks |= board.attacks_mask(pawn)
    return board.attacks_mask(square) & ~board.occupied_co[color] & ~pawn_attacks


def _repeated_line(board, pv):
    replay = board.copy(stack=True)
    for uci in pv:
        replay.push_uci(uci)
        if opening.has_repetition(replay):
            return True
    return False


def _parent_line(board, root_move, child_line):
    """Label a fixed root ply plus child search; never invent a root packet."""
    return {"uci": root_move.uci(), "pv_uci": [root_move.uci(), *child_line["pv_uci"]],
            "depth": child_line["depth"] + 1, "child_depth": child_line["depth"],
            "depth_basis": "fixed_root_ply_plus_completed_child_depth",
            "cp": -child_line["cp"], "cp_white": child_line["cp_white"],
            "pov": "white" if board.turn else "black"}


def _paired_recheck(board, baseline_move, candidate, depth, search, deadline, attempt):
    """Re-evaluate both actual children with unrestricted, equal-depth search.

    The original root loss remains on the attempt. A small root deficit only
    authorizes this extra work; it never authorizes playing a weaker child.
    """
    child_depth = depth - 1
    remaining = (deadline - time.monotonic()) * 1000 - 10
    attempt.update(comparison_kind="paired_unrestricted_children", child_depth=child_depth,
                   paired_search_complete=False,
                   loss_basis="paired_completed_child_searches")
    if remaining < 2 * MIN_PROBE_MS:
        attempt["reason"] = "insufficient_paired_recheck_budget"
        return False
    per_child = remaining / 2
    for role, move in (("baseline", baseline_move), ("candidate", candidate)):
        amount = min(per_child, (deadline - time.monotonic()) * 1000 - 10)
        if amount < MIN_PROBE_MS:
            attempt["reason"] = "insufficient_paired_recheck_budget"
            return False
        child = board.copy(stack=True)
        child.push(move)
        history = [item.uci() for item in child.move_stack]
        fen = child.fen(en_passant="fen")
        record = {"fen": fen, "history_uci": history, "budget_ms": round(amount, 3),
                  "target_depth": child_depth, "root_move": move.uci()}
        attempt["paired_child_" + role] = record
        began = time.monotonic()
        reply, info, packets = search(amount, position=child, publish=False, fresh=True, depth=child_depth)
        record["elapsed_ms"] = round((time.monotonic() - began) * 1000, 3)
        returned = reply.move if isinstance(reply.move, chess.Move) else None
        record["returned_uci"] = returned.uci() if returned is not None else None
        if child.fen(en_passant="fen") != fen or [item.uci() for item in child.move_stack] != history:
            attempt["reason"] = "paired_child_position_changed"
            return False
        common = attack._line(child, (packets or {}).get(child_depth), returned) if returned else None
        final = attack._line(child, info, returned) if returned else None
        record.update(common=common, final=final)
        if (common is None or final is None or common["depth"] != child_depth
                or final["depth"] != child_depth or common != final):
            attempt["reason"] = "paired_" + role + "_incomplete_or_conflicting"
            return False
        record["parent_line"] = _parent_line(board, move, final)
    base = attempt["paired_child_baseline"]["parent_line"]
    selected = attempt["paired_child_candidate"]["parent_line"]
    attempt.update(paired_search_complete=True, baseline_compared=base,
                   selection_compared=selected, selection_final=selected,
                   paired_loss_cp=max(0, base["cp"] - selected["cp"]))
    if selected["cp"] < base["cp"]:
        attempt["reason"] = "outside_paired_zero_loss_floor"
        return False
    return True


def _continued_plan(board, move, line, purpose):
    if dynamic_play._pv_has_terminal(board, line["pv_uci"]):
        return None, {}, "terminal_continuation"
    if _repeated_line(board, line["pv_uci"]):
        return None, {}, "repeated_candidate_continuation"
    progress = operations.continuation_evidence(board, line["pv_uci"], purpose)
    if progress is None:
        return None, {}, "unsettled_candidate"
    if progress["king_danger_reduction"] < 0:
        return progress, {}, "king_danger_increased"
    settled = board.copy(stack=True)
    for uci in progress["pv_prefix_uci"]:
        settled.push_uci(uci)
    return progress, _motifs(board, move, settled), None


def _motifs(board, move, settled):
    """Positive geometric changes retained on a supplied, real continuation.

    Calling this with the immediate child only generates a proposal. Admission
    calls it again after the native opponent reply and completed exchanges.
    """
    owner = board.turn
    piece = board.piece_at(move.from_square)
    if piece is None or piece.color != owner:
        return {}
    first = board.copy(stack=True)
    first.push(move)
    before, after = dynamic_play._features(board, owner), dynamic_play._features(settled, owner)
    kept = settled.piece_at(move.to_square) == piece
    motifs = {}
    if piece.piece_type == chess.PAWN:
        opened = dynamic_play._opened_lines(board, first, settled, move, owner)
        contact = 0
        if kept:
            old = board.attacks_mask(move.from_square) & board.pieces_mask(chess.PAWN, not owner)
            new = settled.attacks_mask(move.to_square) & settled.pieces_mask(chess.PAWN, not owner)
            contact = max(0, new.bit_count() - old.bit_count())
        if opened or contact:
            motifs["pawn_break"] = opened * 3 + contact
    if kept and piece.piece_type == chess.ROOK:
        reach_gain = (_piece_reach(settled, move.to_square, owner).bit_count()
                      - _piece_reach(board, move.from_square, owner).bit_count())
        files = after["rook_files"] - before["rook_files"]
        if reach_gain >= 2 or files > 0:
            motifs["rook_activity"] = max(0, reach_gain) + 3 * max(0, files)
    if kept and piece.piece_type in (chess.KNIGHT, chess.BISHOP):
        gain = after["mobility"] - before["mobility"]
        developed = after["developed_minors"] - before["developed_minors"]
        if gain >= 2 or developed > 0:
            motifs["piece_coordination"] = max(0, gain) + 3 * max(0, developed)
    pressure = after["coordinated_pressure"] - before["coordinated_pressure"]
    if pressure > 0:
        motifs["king_pressure"] = pressure
    return motifs


def proposals(board, baseline_move, seed, excluded_uci=(), *, deadline=None):
    """Seeded motif coverage among geometry leaders, not random legal moves."""
    groups = defaultdict(list)
    blocked = set(excluded_uci) | {baseline_move.uci()}
    history = opening.history_key(board)
    for move in board.legal_moves:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if (move.uci() in blocked or move.promotion or board.is_capture(move)
                or board.gives_check(move) or board.is_castling(move)):
            continue
        after = board.copy(stack=True)
        after.push(move)
        if after.is_game_over(claim_draw=True) or opening.has_repetition(after):
            continue
        motifs = _motifs(board, move, after)
        for motif, points in motifs.items():
            groups[motif].append({"uci": move.uci(), "motif": motif,
                                  "proposal_points": points, "proposed_motifs": motifs})
    order = sorted(groups, key=lambda motif: opening._digest([POLICY, seed, history, "motif", motif]))
    sampled = {}
    for motif in order:
        leaders = sorted(groups[motif], key=lambda row: (-row["proposal_points"], row["uci"]))[:TOP_PER_MOTIF]
        sampled[motif] = sorted(leaders, key=lambda row: opening._digest(
            [POLICY, seed, history, "candidate", motif, row["uci"]]))[:SAMPLED_PER_MOTIF]
    result, seen = [], {}
    # Alternate motif families before taking their second member. Sampling
    # two of three leaders changes the allowed set, not just root ordering.
    for index in range(SAMPLED_PER_MOTIF):
        for motif in order:
            if index >= len(sampled[motif]):
                continue
            row = sampled[motif][index]
            if row["uci"] in seen:
                seen[row["uci"]]["eligible_motifs"].append(motif)
            elif len(result) < MAX_SCREENED:
                value = {**row, "eligible_motifs": [motif]}
                result.append(value)
                seen[row["uci"]] = value
    return result


def choose(board, baseline_move, baseline_info, baseline_packets, search, close_engine, *,
           seed, budget_ms, search_plan, excluded_uci=()):
    started = time.monotonic()
    uci = baseline_move.uci() if isinstance(baseline_move, chess.Move) else None
    result = {"policy": POLICY, "selected_uci": uci, "applied": False,
              "reason": "baseline_preserved", "loss_cp": 0, "loss_cap_cp": LOSS_CAP_CP,
              "baseline": None, "attempts": [], "evidence": None,
              "extra_elapsed_ms": 0, "budget_ms": 0,
              "root_recheck_band_cp": ROOT_RECHECK_BAND_CP,
              "budget_basis": "shared wall deadline; unused root search time can fund two equal child ceilings",
              "selection_basis": "seeded native pool; zero loss at root or independently paired equal-depth children"}
    def finish(reason, discard=False):
        result["reason"] = reason
        result["extra_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        return result, discard
    if not ENABLED:
        return finish("creativity_disabled")
    if (not isinstance(board, chess.Board) or board.chess960 or not board.is_valid()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN
            or baseline_move not in board.legal_moves):
        return finish("invalid_baseline_or_history")
    if (board.is_check() or board.is_game_over(claim_draw=True) or opening.has_repetition(board)
            or board.legal_moves.count() <= 1 or len(board.move_stack) >= MAX_PLIES):
        return finish("forced_terminal_or_repeated_position")
    if dynamic_play._phase(board) < 8 or dynamic_play._nonpawns(board) < 5:
        return finish("simplified_position")
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms < MIN_PROBE_MS:
        return finish("short_or_invalid_budget")
    if (not isinstance(excluded_uci, (list, tuple, set, frozenset))
            or any(not isinstance(item, str) for item in excluded_uci)):
        return finish("invalid_exclusions")
    baseline = attack._line(board, baseline_info, baseline_move)
    result["baseline"] = baseline
    if baseline is None or baseline["depth"] < MIN_DEPTH:
        return finish("baseline_unreliable")
    if (not isinstance(baseline_packets, dict)
            or attack._line(board, baseline_packets.get(baseline["depth"]), baseline_move) != baseline):
        return finish("baseline_packet_conflict")
    if abs(baseline["cp"]) >= CONVERSION_CP or dynamic_play._pv_has_terminal(board, baseline["pv_uci"]):
        return finish("forcing_or_decisive_baseline")
    try:
        purpose = _context(board, search_plan, seed)
        if purpose is None:
            return finish("context_changed")
        if purpose == "defensive_transition":
            return finish("defensive_transition_preserved")
        base_progress = operations.continuation_evidence(board, baseline["pv_uci"], purpose)
    except Exception as error:
        result["error"] = type(error).__name__
        return finish("creativity_context_failed", True)
    if base_progress is None:
        return finish("unsettled_baseline")
    result.update(goal=purpose, baseline_operations_evidence=base_progress,
                  budget_ms=min(float(budget_ms), MAX_EXTRA_MS))
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
    chosen, discarded = None, False
    reason = "no_verified_creative_alternative"
    try:
        result["proposals"] = proposals(board, baseline_move, seed, excluded_uci, deadline=deadline)[:MAX_SCREENED]
        allowed = {}
        for proposal in result["proposals"]:
            if expired.is_set() or time.monotonic() >= deadline:
                break
            move = chess.Move.from_uci(proposal["uci"])
            attempt = {**copy.deepcopy(proposal), "admitted": False}
            result["attempts"].append(attempt)
            screen = attack._material_screen(board, move, deadline)
            attempt["material_screen"] = screen
            if not screen["admitted"]:
                attempt["reason"] = screen["reason"]
                continue
            attempt["reason"] = "not_returned_by_native_pool"
            allowed[move.uci()] = attempt
        result["candidate_pool"] = list(allowed)
        amount = (deadline - time.monotonic()) * 1000 - 10
        if allowed and not expired.is_set() and amount >= MIN_PROBE_MS:
            roots = [chess.Move.from_uci(item) for item in allowed]
            options = {"root_move": roots[0]} if len(roots) == 1 else {"root_moves": roots}
            root_search_started = time.monotonic()
            reply, info, packets = search(amount, publish=False, fresh=True,
                                           depth=baseline["depth"], **options)
            move = reply.move
            returned = move.uci() if isinstance(move, chess.Move) else None
            result["search"] = {"root_moves": list(allowed), "budget_ms": round(amount, 3),
                                "elapsed_ms": round((time.monotonic() - root_search_started) * 1000, 3),
                                "target_depth": baseline["depth"], "returned_uci": returned}
            if returned not in allowed:
                result["attempts"].append({"uci": returned, "admitted": False,
                    "reason": "candidate_outside_allowed_pool", "budget_ms": round(amount, 3)})
                reason = "candidate_outside_allowed_pool"
                attempt = None
            else:
                attempt = allowed[returned]
                attempt["budget_ms"] = round(amount, 3)
            # Only the native winner has a score/PV. No loser inherits the
            # pool's scalar score or can be chosen after a failed winner.
            common = attack._line(board, (packets or {}).get(baseline["depth"]), move)
            final = attack._line(board, info, move)
            if attempt is not None:
                attempt.update(candidate_compared=common, candidate_final=final)
            usable = False
            if attempt is None:
                pass
            elif (common is None or final is None
                    or common["depth"] != baseline["depth"] or final["depth"] < baseline["depth"]):
                attempt["reason"] = "candidate_unreliable_or_incomplete"
            elif final["depth"] == common["depth"] and final["cp"] != common["cp"]:
                attempt["reason"] = "candidate_score_conflict"
            elif final["depth"] > baseline["depth"] and final["cp"] < baseline["cp"]:
                # A shorter child pair must not erase an existing deeper warning.
                attempt["reason"] = "outside_zero_loss_floor"
            elif dynamic_play._pv_has_terminal(board, final["pv_uci"]):
                attempt["reason"] = "terminal_continuation"
            elif _repeated_line(board, final["pv_uci"]):
                attempt["reason"] = "repeated_candidate_continuation"
            else:
                root_loss = max(0, baseline["cp"] - min(common["cp"], final["cp"]))
                attempt.update(root_loss_cp=root_loss, baseline_compared=baseline,
                               selection_compared=common, selection_final=final,
                               comparison_kind="native_same_depth_creativity",
                               loss_basis="completed_root_searches")
                if root_loss == 0:
                    usable = True
                elif root_loss > ROOT_RECHECK_BAND_CP:
                    attempt["reason"] = "outside_zero_loss_floor"
                else:
                    usable = _paired_recheck(board, baseline_move, move, baseline["depth"],
                                             search, deadline, attempt)
                    if usable:
                        progress, motifs, failure = _continued_plan(
                            board, baseline_move, attempt["baseline_compared"], purpose)
                        attempt.update(baseline_operations_evidence=progress, baseline_retained_motifs=motifs)
                        if failure is not None:
                            attempt["reason"] = "paired_baseline_" + failure
                            usable = False
                if usable:
                    progress, motifs, failure = _continued_plan(board, move, attempt["selection_final"], purpose)
                    attempt.update(operations_evidence=progress, retained_motifs=motifs)
                    retained = [key for key in attempt.get("eligible_motifs", [attempt["motif"]]) if key in motifs]
                    if failure is not None:
                        attempt["reason"] = failure
                    elif not retained:
                        attempt["reason"] = "motif_not_retained_after_reply"
                    else:
                        attempt.update(admitted=True, reason="native_verified_creative_continuation",
                            motif=retained[0], retained_after_reply=True, loss_cp=0,
                            style={"points": sum(motifs.values()), "motifs": motifs,
                                   "units": "retained geometric motif points; not centipawns"})
                        chosen = attempt
        elif allowed:
            reason = "insufficient_probe_budget"
            for attempt in allowed.values():
                attempt["reason"] = reason
    except Exception as error:
        result["error"] = type(error).__name__
        reason, discarded, chosen = "creativity_probe_failed", True, None
    finally:
        done.set()
        watchdog.cancel()
        watchdog.join(timeout=.2)
    if expired.is_set() or time.monotonic() > deadline:
        return finish("creativity_probe_timeout", True)
    if chosen is not None:
        result.update(selected_uci=chosen["uci"], applied=True, evidence=copy.deepcopy(chosen))
        for key in ("comparison_kind", "loss_basis", "root_loss_cp", "paired_loss_cp", "child_depth",
                    "baseline_compared", "selection_compared", "selection_final"):
            if key in chosen:
                result[key] = copy.deepcopy(chosen[key])
        return finish(chosen["reason"])
    return finish(reason, discarded)
