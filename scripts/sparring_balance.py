"""Bounded early-game balance discovery using only native move proposals.

The external engine checks positions, never supplies a root move. Elective
concessions have a strict legal-capture screen with no material-return exception.
"""
import copy
import time

import chess

import sparring_play
import pending_exchange

POLICY = "regicide-sparring-balance-v2"
TARGET_CP = 30
BAND_CP = 100
BUFFER_CP = 80
PREVENTIVE_START_PLY = 8
PREVENTIVE_TRIGGER_CP = 60
TARGET_TOLERANCE_CP = 20
MIN_PREVENTIVE_PROGRESS_CP = 20
MAX_DISCOVERY_MS = 1800
MIN_DISCOVERY_MS = 12
MAX_DISCOVERY_DEPTH = 3
PROMOTION_MS = 1200
MAX_AUDITS = 5


def trigger_cp(board):
    """Start correcting a modest advantage after both sides develop four moves."""
    return PREVENTIVE_TRIGGER_CP if PREVENTIVE_START_PLY <= len(board.move_stack) < 40 else BUFFER_CP


def required(board, verdict, assessment):
    if (len(board.move_stack) >= 40 or not verdict.get("verified")
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN):
        return False
    own = "white" if board.turn else "black"
    child = assessment.get("child") or {}
    cp = verdict.get("child_cp_own")
    if child.get("winner") == own:
        return True
    if type(cp) not in (int, float) or not cp > trigger_cp(board):
        return False
    if cp > BUFFER_CP:
        return True
    # The new preventive range must not reopen a concession that native play
    # suppressed to preserve scarce resources or a delayed recovery schedule.
    # Existing outside-buffer and mate recovery retain their historical policy.
    return (not sparring_play.sparring_pacing.resource_guard(board).get("active")
            and not sparring_play.sparring_pacing.derive(board, [], cp).get("active"))


def recover(board, move, initial, initial_verdict, *, native_search, native_evidence,
            audit, check, deadline, minimum_native_depth, completed=None):
    """Find a safe native candidate closer to the ordinary pre-20 band.

Shallow singleton searches order candidates only. Application needs a matching
completed native search of at least ``minimum_native_depth`` and independent
root/child evidence. A forced existing defensive move is never filtered here.
"""
    own = "white" if board.turn else "black"
    original_cp = initial_verdict.get("child_cp_own")
    preventive = type(original_cp) in (int, float) and abs(original_cp) <= BUFFER_CP
    result = {"policy": POLICY, "active": True, "status": "unmet", "reason": "no_verified_natural_balance_candidate",
              "target_cp": TARGET_CP, "band_cp": BAND_CP, "buffer_cp": BUFFER_CP,
              "trigger_cp": trigger_cp(board), "target_tolerance_cp": TARGET_TOLERANCE_CP,
              "preventive": preventive, "minimum_progress_cp": MIN_PREVENTIVE_PROGRESS_CP if preventive else 0,
              "center_reached": False, "original_child_cp_own": original_cp,
              "discovery": [], "attempts": [], "selected_child_cp_own": None,
              "scope": "elective natural native concessions; no material-return exception"}
    legal = []
    for candidate in sorted(board.legal_moves, key=lambda item: (
            board.is_capture(item), board.gives_check(item), item.uci())):
        check()
        if candidate == move:
            continue
        natural = pending_exchange.naturalness(board, candidate)
        if natural["admitted"]:
            legal.append((candidate, natural))
    remaining_ms = (deadline - time.monotonic()) * 1000
    discovery_ms = min(MAX_DISCOVERY_MS, max(0, remaining_ms - 2700), remaining_ms * .30)
    discovery_deadline = time.monotonic() + discovery_ms / 1000
    leads = []
    for index, (candidate, natural) in enumerate(legal):
        check()
        left = (discovery_deadline - time.monotonic()) * 1000
        amount = min(100, left / max(1, len(legal) - index))
        if amount < MIN_DISCOVERY_MS:
            break
        response = native_search(amount, [candidate], depth=MAX_DISCOVERY_DEPTH)
        evidence = native_evidence(response, [candidate])
        row = {"uci": candidate.uci(), "naturalness": natural,
               "budget_ms": round(amount, 3), "native_evidence": copy.deepcopy(evidence)}
        result["discovery"].append(row)
        if evidence is None:
            continue
        cp = evidence.get("cp")
        if cp is not None:
            cp *= 1 if board.turn else -1
        row["cp_own"] = cp
        if cp is not None and cp >= -BAND_CP:
            leads.append((response, row))
    # Native scores only establish the order; every accepted candidate is
    # independently audited below. Quiet moves are preferred in close ties.
    leads.sort(key=lambda item: (abs(item[1]["cp_own"]) > BAND_CP,
                                  abs(item[1]["cp_own"] - TARGET_CP),
                                  board.is_capture(item[0][0].move), item[1]["uci"]))
    best = None
    root = initial.get("root") or {}
    for response, discovery in leads[:MAX_AUDITS]:
        check()
        if (deadline - time.monotonic()) * 1000 < 850:
            break
        candidate = response[0].move
        checked, verdict = audit(candidate)
        row = {"uci": candidate.uci(), "naturalness": discovery["naturalness"],
               "discovery_evidence": discovery["native_evidence"],
               "assessment": checked, "verdict": verdict, "native_promotion": None,
               "native_evidence": None}
        result["attempts"].append(row)
        if not verdict.get("verified") or not verdict.get("acceptable"):
            continue
        other_root = checked.get("root") or {}
        root_cp, other_cp = root.get("cp_own"), other_root.get("cp_own")
        if (root.get("winner") != other_root.get("winner") or
                root_cp is not None and other_cp is not None and abs(root_cp - other_cp) > 100):
            row["reason"] = "reference_root_disagreement"
            continue
        cp = verdict.get("child_cp_own")
        # A winning mate remains legal, but does not restore an early close game.
        if checked.get("child", {}).get("winner") is not None or cp is None or cp < -BAND_CP:
            continue
        inside = abs(cp) <= BUFFER_CP
        central = abs(cp - TARGET_CP) <= TARGET_TOLERANCE_CP
        if original_cp is not None:
            progress = abs(original_cp - TARGET_CP) - abs(cp - TARGET_CP)
            minimum_progress = MIN_PREVENTIVE_PROGRESS_CP if preventive else 0 if inside else 30
            row.update(progress_cp=progress, minimum_progress_cp=minimum_progress)
            if progress <= 0 or progress < minimum_progress:
                row["reason"] = "no_clear_balance_progress"
                continue
        if best is not None and abs(cp - TARGET_CP) >= abs(best[1]["child_cp_own"] - TARGET_CP):
            continue
        evidence = discovery["native_evidence"]
        if evidence["depth"] < minimum_native_depth:
            amount = min(PROMOTION_MS, (deadline - time.monotonic()) * 1000 - 100)
            if amount < 100:
                break
            response = native_search(amount, [candidate])
            evidence = native_evidence(response, [candidate])
            row["native_promotion"] = {"budget_ms": round(amount, 3), "evidence": copy.deepcopy(evidence),
                                       "allowed_uci": [candidate.uci()]}
            if evidence is None or evidence["depth"] < minimum_native_depth:
                row["reason"] = "incomplete_native_promotion"
                continue
        row["native_evidence"] = copy.deepcopy(evidence)
        best = (response, copy.deepcopy(verdict), checked)
        result.update(status="met" if inside else "improved",
                      reason="verified_natural_balance" if inside else "verified_natural_balance_progress",
                      selected_child_cp_own=cp, center_reached=central)
        if completed is not None:
            completed(best, copy.deepcopy(result))
        # A boundary-level success is retained while later verified candidates
        # can still reach the center. Completed evidence survives optional expiry.
        if central:
            break
    if best is not None:
        cp = best[1]["child_cp_own"]
        result.update(status="met" if abs(cp) <= BUFFER_CP else "improved",
                      reason="verified_natural_balance" if abs(cp) <= BUFFER_CP else "verified_natural_balance_progress",
                      selected_child_cp_own=cp, center_reached=abs(cp - TARGET_CP) <= TARGET_TOLERANCE_CP)
    return best, result

