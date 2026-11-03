"""Learned ordering for gambit proposals; never a move or centipawn authority.

The small linear policy is trained on fixed-node Regicide child searches.
It only orders already legal proposals.  The caller must independently search
and verify every selected proposal with its existing risk and time limits.
Missing, stale, experimental or malformed models leave the supplied order alone.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import chess

import dynamic_play

POLICY = "regicide-gambit-ranking-v1"
SCHEMA = "regicide.gambit-policy.v1"
FEATURE_SCHEMA = "regicide-gambit-features-v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "data/gambits/policy.json"
MAX_MODEL_BYTES = 131072
MAX_CANDIDATES = 32
VALUES = {chess.PAWN: 1., chess.KNIGHT: 3., chess.BISHOP: 3.2,
          chess.ROOK: 5., chess.QUEEN: 9., chess.KING: 0.}
FEATURE_NAMES = (
    "capture_gain", "moving_piece", "check", "castling", "promotion_gain",
    "mobility_delta", "development_delta", "rook_files_delta", "pressure_delta",
    "opponent_mobility_delta", "opponent_pressure_delta", "center_control_delta",
    "king_ring_danger_delta", "pawn_advance", "destination_defended",
    "destination_attacked", "worst_capture_net", "reply_min_mobility_delta",
    "reply_min_development_delta", "reply_min_pressure_delta", "reply_check_fraction",
    "reply_mate", "own_material_balance", "opponent_castling_rights",
)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def position_key(board):
    """A transposition identity; real repetition still needs the move stack."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _capture(board, move):
    if board.is_en_passant(move):
        return 1.
    piece = board.piece_at(move.to_square)
    return VALUES[piece.piece_type] if piece else 0.


def _control(board, color):
    return sum(len(board.attackers(color, square)) for square in
               (chess.D4, chess.E4, chess.D5, chess.E5))


def _danger(board, color):
    square = board.king(color)
    if square is None:
        return 16.
    ring = chess.SquareSet(chess.BB_KING_ATTACKS[square] | chess.BB_SQUARES[square])
    return float(sum(len(board.attackers(not color, target)) for target in ring))


def features(board, move):
    """Cheap geometry and exhaustive immediate-reply minima, not an evaluation.

    Captures, checks and recaptures are deliberately included.  Immediate reply
    geometry cannot prove tactical safety; the caller's native probes do that.
    No caller-owned board is ever pushed or modified.
    """
    if isinstance(move, str):
        move = chess.Move.from_uci(move)
    if not isinstance(board, chess.Board) or move not in board.legal_moves:
        raise ValueError("A legal candidate and valid board are required")
    color = board.turn
    piece = board.piece_at(move.from_square)
    before = dynamic_play._features(board, color)
    opponent = dynamic_play._features(board, not color)
    danger, center = _danger(board, color), _control(board, color)
    gain = _capture(board, move)
    promotion_gain = VALUES.get(move.promotion, 0.) - (1. if move.promotion else 0.)
    castling = board.is_castling(move)
    after = board.copy(stack=False)
    after.push(move)
    own_after = dynamic_play._features(after, color)
    opponent_after = dynamic_play._features(after, not color)
    replies = list(after.legal_moves)
    minima = {key: float(own_after[key]) for key in
              ("mobility", "developed_minors", "coordinated_pressure")}
    worst_capture = 0.
    checks, mate = 0, 0.
    for reply in replies:
        lost = _capture(after, reply)
        after.push(reply)
        try:
            checks += int(after.is_check())
            mate = max(mate, float(after.is_checkmate()))
            reply_values = dynamic_play._features(after, color)
            for key in minima:
                minima[key] = min(minima[key], reply_values[key])
            if lost:
                # The largest legal recapture is a geometric feature only;
                # it does not claim that the recapture is tactically safe.
                recaptures = [_capture(after, capture) for capture in after.legal_moves
                              if capture.to_square == reply.to_square and after.is_capture(capture)]
                worst_capture = min(worst_capture, gain + promotion_gain - lost + max(recaptures, default=0.))
        finally:
            after.pop()
    pawn_advance = (chess.square_rank(move.to_square) - chess.square_rank(move.from_square)) * (1 if color else -1)
    material = sum(value * (len(after.pieces(kind, color)) - len(after.pieces(kind, not color)))
                   for kind, value in VALUES.items())
    result = [
        gain, VALUES[piece.piece_type], float(after.is_check()), float(castling), promotion_gain,
        (own_after["mobility"] - before["mobility"]) / 8.,
        own_after["developed_minors"] - before["developed_minors"],
        own_after["rook_files"] - before["rook_files"],
        (own_after["coordinated_pressure"] - before["coordinated_pressure"]) / 4.,
        (opponent_after["mobility"] - opponent["mobility"]) / 8.,
        (opponent_after["coordinated_pressure"] - opponent["coordinated_pressure"]) / 4.,
        (_control(after, color) - center) / 4., (_danger(after, color) - danger) / 4.,
        float(pawn_advance if piece.piece_type == chess.PAWN else 0),
        float(after.is_attacked_by(color, move.to_square)),
        float(after.is_attacked_by(not color, move.to_square)), worst_capture,
        (minima["mobility"] - before["mobility"]) / 8.,
        minima["developed_minors"] - before["developed_minors"],
        (minima["coordinated_pressure"] - before["coordinated_pressure"]) / 4.,
        checks / max(1, len(replies)), mate, material / 3.,
        float(after.has_kingside_castling_rights(not color)) + float(after.has_queenside_castling_rights(not color)),
    ]
    return [round(max(-32., min(32., value)), 8) for value in result]


def feature_sources():
    paths = (Path(__file__), Path(dynamic_play.__file__))
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def model_digest(model):
    value = {key: item for key, item in model.items() if key != "model_id"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _validate(model):
    if not isinstance(model, dict):
        raise ValueError("invalid_model")
    if (model.get("schema") != SCHEMA or model.get("policy") != POLICY
            or model.get("feature_schema") != FEATURE_SCHEMA
            or model.get("feature_names") != list(FEATURE_NAMES)):
        raise ValueError("unsupported_model")
    if model.get("feature_sources") != feature_sources():
        raise ValueError("stale_features")
    for key in ("weights", "means", "scales"):
        values = model.get(key)
        if (not isinstance(values, list) or len(values) != len(FEATURE_NAMES)
                or not all(_finite(value) and abs(value) <= 10000 for value in values)
                or key == "scales" and any(value <= 0 for value in values)):
            raise ValueError("invalid_weights")
    identity = model.get("engine_identity")
    if (not isinstance(identity, dict) or set(identity) != {"binary_sha256", "evaluation_id"}
            or not isinstance(identity["binary_sha256"], str) or len(identity["binary_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in identity["binary_sha256"])
            or not isinstance(identity["evaluation_id"], str) or not identity["evaluation_id"]):
        raise ValueError("invalid_engine_identity")
    if model.get("model_id") != model_digest(model):
        raise ValueError("model_checksum_mismatch")
    return model


@lru_cache(maxsize=8)
def _read(path, modified_ns, size):
    if size <= 0 or size > MAX_MODEL_BYTES:
        raise ValueError("invalid_model_size")
    raw = Path(path).read_bytes()
    if len(raw) != size:
        raise ValueError("model_changed_while_reading")
    return _validate(json.loads(raw))


def _model(path=None):
    path = Path(path) if path is not None else DEFAULT_MODEL
    info = path.stat()
    return _read(str(path.resolve()), info.st_mtime_ns, info.st_size)


def model_identity(model_path=None):
    """Small immutable identifier for host memo keys; no implicit activation."""
    try:
        model = _model(model_path)
        return {"policy": POLICY, "model_id": model["model_id"], "status": model.get("status")}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return {"policy": POLICY, "model_id": None, "status": "unavailable"}


def _score(vector, model):
    return sum(weight * (value - mean) / scale for value, weight, mean, scale in
               zip(vector, model["weights"], model["means"], model["scales"]))


def rank_with_metadata(board, candidate_rows, *, model_path=None, engine_identity=None):
    """Return (rows, receipt); active rows are only reordered, never added.

    An explicit runtime binary/evaluator identity is mandatory.  Scores are
    dimensionless model outputs, never centipawns, safety proof or probabilities.
    """
    rows = copy.deepcopy(candidate_rows)
    receipt = {"policy": POLICY, "active": False, "applied": False,
               "reason": "model_unavailable", "model_id": None,
               "units": "learned ordering score, not centipawns or probability", "ranking": []}
    try:
        model = _model(model_path)
        receipt["model_id"] = model["model_id"]
        if model.get("status") != "promoted" or model.get("acceptance", {}).get("passed") is not True:
            receipt["reason"] = "experimental_model"
            return rows, receipt
        if engine_identity != model["engine_identity"]:
            receipt["reason"] = "runtime_identity_mismatch"
            return rows, receipt
        if (not isinstance(board, chess.Board) or not board.is_valid() or board.chess960
                or board.is_game_over(claim_draw=False)
                or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CANDIDATES
                or any(not isinstance(row, dict) or not isinstance(row.get("uci"), str) for row in rows)):
            receipt["reason"] = "invalid_proposals"
            return rows, receipt
        moves = [chess.Move.from_uci(row["uci"]) for row in rows]
        if len(set(moves)) != len(moves) or any(move not in board.legal_moves for move in moves):
            receipt["reason"] = "invalid_proposals"
            return rows, receipt
        scores = [_score(features(board, move), model) for move in moves]
        order = sorted(range(len(rows)), key=lambda index: (-scores[index], index))
        receipt.update(active=True, applied=order != list(range(len(rows))), reason="learned_proposal_order",
                       ranking=[{"uci": rows[index]["uci"], "score": round(scores[index], 6)} for index in order])
        return [rows[index] for index in order], receipt
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return rows, receipt


def rank(board, candidate_rows, *, model_path=None, engine_identity=None):
    return rank_with_metadata(board, candidate_rows, model_path=model_path,
                              engine_identity=engine_identity)[0]
