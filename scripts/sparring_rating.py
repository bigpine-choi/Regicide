"""Experimental local Chess.com Rapid estimate, separate from move selection.

No rating relationship is claimed before user-reported completed-game labels.
Contextual evidence reduces the weight of simple successes and shallow search.
Local labels fit only stage-specific offsets, never a slope. Legacy archives
retain their original formula; legacy labels are reinterpreted only in memory.
There are no engine calls, network calls, or calibrated confidence intervals.
"""
import copy
import datetime
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

import chess
import decision_support

METHOD = "regicide-sparring-rating-v2"
LEGACY_METHOD = "regicide-sparring-rating-v1"
METHODS = (LEGACY_METHOD, METHOD)
EVIDENCE_METHOD = "regicide-rating-evidence-v1"
TARGET = "chess_com_rapid"
POLICIES = ("regicide-sparring-play-v1", "regicide-sparring-play-v2", "regicide-sparring-play-v3",
            "regicide-sparring-play-v4", "regicide-sparring-play-v5")
PRIOR_LOSS = 80.0
PRIOR_WEIGHT = 8.0
CALIBRATION_PRIOR_GAMES = .25
MAX_CALIBRATION_OFFSET = 3000.0
MAX_LOSS_CP = 1200.0
MATE_PENALTIES = {"allowed_mate": 400., "missed_mate": 250.}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def prior(evaluation_key="", *, method=METHOD):
    result = {"method": method, "evaluation_key": evaluation_key,
              "calibration_games": 0, "offset": 0.0}
    if method == METHOD:
        result.update(offset_by_observation=[], spread_by_observation=[],
                      calibration_spread=None, calibration_weight=0.)
    return result


def _model(value, *, method=METHOD):
    if (not isinstance(value, dict) or value.get("method") != method
            or type(value.get("calibration_games")) is not int
            or value["calibration_games"] < 0 or not _number(value.get("offset"))
            or abs(value["offset"]) > MAX_CALIBRATION_OFFSET
            or not isinstance(value.get("evaluation_key", ""), str)):
        return prior(method=method)
    result = copy.deepcopy(value)
    if method == METHOD:
        for name, bound in (("offset_by_observation", MAX_CALIBRATION_OFFSET),
                            ("spread_by_observation", 2 * MAX_CALIBRATION_OFFSET)):
            items = value.get(name, [])
            if (not isinstance(items, list) or len(items) not in (0, 40)
                    or any(not _number(item) or abs(item) > bound
                           or name.startswith("spread") and item < 0 for item in items)):
                return prior()
            result[name] = list(items)
        spread = value.get("calibration_spread")
        if spread is not None and (not _number(spread) or not 0 <= spread <= 2 * MAX_CALIBRATION_OFFSET):
            return prior()
        result["calibration_spread"] = spread
    return result


def _evidence(value, board, played):
    """Invalid new evidence never falls back to the legacy loss channel."""
    if "rating_evidence" not in value:
        loss, depth = value.get("loss_cp"), value.get("depth")
        if (value.get("valid", True) is not True or type(depth) is not int or depth < 4
                or not _number(loss) or loss < 0):
            return None
        return {"kind": "cp_loss", "loss_cp": loss, "depth": depth, "reliability": "legacy",
                "best_cp": None, "played_cp": None}
    evidence = value["rating_evidence"]
    if (not isinstance(evidence, dict) or evidence.get("method") != EVIDENCE_METHOD
            or evidence.get("valid") is not True or evidence.get("played_uci") != played.uci()
            or type(evidence.get("depth")) is not int or evidence["depth"] < 4
            or evidence.get("reliability") not in ("stable", "limited")):
        return None
    try:
        best = chess.Move.from_uci(evidence.get("best_uci", ""))
    except (ValueError, TypeError):
        return None
    if best not in board.legal_moves:
        return None
    bc, pc, bm, pm = (evidence.get(key) for key in ("best_cp", "played_cp", "best_mate", "played_mate"))
    if any(not ((type(cp) is int and mate is None) or
                (cp is None and type(mate) is int and mate != 0)) for cp, mate in ((bc, bm), (pc, pm))):
        return None
    if best == played and (bc, bm) != (pc, pm):
        return None
    kind = evidence.get("kind")
    valid = {"cp_loss": bm is None and pm is None and pc <= bc + 25,
             "allowed_mate": pm is not None and pm < 0 and (bm is None or bm > 0),
             "missed_mate": bm is not None and bm > 0 and pm is None,
             "mate_preserved": bm is not None and bm > 0 and pm is not None and pm > 0,
             "already_lost": bm is not None and bm < 0 and pm is not None and pm < 0}.get(kind, False)
    return ({**copy.deepcopy(evidence), "loss_cp": max(0, bc - pc) if kind == "cp_loss" else None}
            if valid else None)


def _item(before, core, value):
    try:
        played = chess.Move.from_uci(core["played_uci"])
    except (ValueError, TypeError, KeyError):
        return None
    if played not in before.legal_moves or before.legal_moves.count() <= 1:
        return None
    evidence = _evidence(value, before, played)
    if evidence is None:
        return None
    depth, kind, loss = evidence["depth"], evidence["kind"], evidence["loss_cp"]
    weight = {4: .4, 5: .6, 6: .8, 7: .9}.get(depth, 1.)
    if evidence["reliability"] in ("limited", "legacy"):
        weight *= .5
    complexity = decision_support._position_complexity(before.fen(en_passant="fen"), before.chess960)
    clarity, f = complexity["clarity"]["kind"], complexity["features"]
    reasons = ["depth_and_reliability"]
    after = before.copy(stack=False)
    after.push(played)
    simple = (clarity == "familiar_opening" and
              decision_support._position_key(after) in decision_support._familiar_positions())
    simple = simple or (clarity == "clear_queen_gain" and before.piece_type_at(played.to_square) == chess.QUEEN)
    if (before.move_stack and not before.is_check() and not before.gives_check(played)
            and not f["own_loose_pieces"] and not f["own_pinned_pieces"] and not f["overloaded_defenders"]
            and f["attacked_king_neighbors"] < 3 and f["checking_choices"] <= 1 and f["capture_choices"] <= 3):
        previous = before.copy(stack=True)
        last = previous.pop()
        simple = simple or (previous.is_capture(last) and before.is_capture(played)
                            and last.to_square == played.to_square)
    if kind == "cp_loss" and loss <= 20 and simple:
        weight *= .2
        reasons.append("simple_success")
    bc, pc = evidence.get("best_cp"), evidence.get("played_cp")
    if (type(bc) is int and type(pc) is int and abs(bc) >= 600 and abs(pc) >= 600 and (bc > 0) == (pc > 0)):
        weight *= .25
        reasons.append("already_decided")
    if kind in ("mate_preserved", "already_lost"):
        # These events stay in the source receipt, but cannot advance the
        # calibration stage or age earlier informative observations.
        return None
    item = {**core, "loss_cp": loss, "depth": depth, "evidence_kind": kind,
            "evidence_reliability": evidence["reliability"], "information_weight": round(weight, 6),
            "weight_reasons": reasons, "complexity_score": complexity["score"], "clarity_kind": clarity}
    if "rating_evidence" in value:
        item["rating_evidence"] = copy.deepcopy(value["rating_evidence"])
    return item


def observations(board, decisions, human_color):
    """Only actual, unforced, same-history quality receipts become evidence."""
    root = board.root()
    history, fens, turns, positions = [], [root.fen(en_passant="fen")], [], []
    for move in board.move_stack:
        if move not in root.legal_moves:
            return []
        positions.append(root.copy(stack=True))
        turns.append(root.turn)
        history.append(move.uci())
        root.push(move)
        fens.append(root.fen(en_passant="fen"))
    if root.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return []
    found = {}
    rows = decisions.values() if isinstance(decisions, dict) else decisions if isinstance(decisions, (list, tuple)) else []
    for row in rows:
        if not isinstance(row, dict) or row.get("policy") not in POLICIES:
            continue
        value = row.get("opponent_observation")
        if not isinstance(value, dict):
            continue
        if "rating_evidence" not in value and value.get("valid") is not True:
            continue
        prefix, ply = value.get("history_before"), value.get("ply")
        if (type(ply) is not int or not 1 <= ply < len(history)
                or turns[ply - 1] != human_color or prefix != history[:ply - 1]
                or value.get("played_uci") != history[ply - 1]
                or value.get("before_fen") != fens[ply - 1]
                or value.get("forced") is not False
                or row.get("history_uci") != history[:ply]
                or row.get("before_fen") != fens[ply]
                or row.get("ply") != ply + 1 or row.get("selected_uci") != history[ply]):
            continue
        core = {"human_ply": ply, "available_ply": ply + 1,
                "history_before": prefix, "played_uci": history[ply - 1], "before_fen": fens[ply - 1]}
        item = _item(positions[ply - 1], core, value)
        if item is None:
            continue
        if ply in found and found[ply] != item:
            found[ply] = None
        elif ply not in found:
            found[ply] = item
    return [item for _, item in sorted(found.items()) if item is not None]


def _legacy_curve(rows, calibration):
    """Frozen v1 arithmetic for archived games, not new calibration."""
    curve, recent = [], []
    offset = _model(calibration, method=LEGACY_METHOD)["offset"]
    for row in rows:
        recent = [*recent, row][-40:]
        weights = [.94 ** age for age in reversed(range(len(recent)))]
        mean = (PRIOR_WEIGHT * PRIOR_LOSS + sum(
            weight * min(400., item["loss_cp"]) for weight, item in zip(weights, recent)))
        mean /= PRIOR_WEIGHT + sum(weights)
        raw = max(100., min(4000., 2400 - 400 * math.log1p(mean / 20)))
        estimate = int(round(max(100., min(4000., raw + offset))))
        curve.append({**row, "prior_estimate": round(raw, 3), "estimate": estimate,
                      "delta": estimate - curve[-1]["estimate"] if curve else None,
                      "smoothed_loss_cp": round(mean, 3)})
    return curve


def _stage_value(model, name, count, fallback):
    values = model.get(name, [])
    return values[min(40, max(1, count)) - 1] if values else fallback


def _curve(rows, calibration):
    curve, recent, effective = [], [], 0.
    model = _model(calibration)
    for row in rows:
        recent = [*recent, row][-40:]
        effective += row["information_weight"]
        weights = [.94 ** age * item["information_weight"]
                   for age, item in zip(reversed(range(len(recent))), recent)]
        cp_pairs = [(item, weight) for item, weight in zip(recent, weights) if item["evidence_kind"] == "cp_loss"]
        prior_weight = PRIOR_WEIGHT * math.exp(-effective / 12.)
        denominator = prior_weight + sum(weight for _item, weight in cp_pairs)
        mean = ((prior_weight * PRIOR_LOSS + sum(weight * min(MAX_LOSS_CP, item["loss_cp"])
                 for item, weight in cp_pairs)) / denominator if denominator else PRIOR_LOSS)
        # Mate failures are categorical, never fabricated centipawn scores.
        # These bounded rating penalties are authored, not empirically fitted.
        mate_penalty = min(600., sum(weight * MATE_PENALTIES.get(item["evidence_kind"], 0.)
                                    for item, weight in zip(recent, weights)))
        raw = max(100., min(4000., 2400 - 400 * math.log1p(mean / 20) - mate_penalty))
        offset = _stage_value(model, "offset_by_observation", len(curve) + 1, model["offset"])
        estimate = int(round(max(100., min(4000., raw + offset))))
        curve.append({**row, "prior_estimate": round(raw, 3), "estimate": estimate,
                      "delta": estimate - curve[-1]["estimate"] if curve else None,
                      "smoothed_loss_cp": round(mean, 3), "effective_observations": round(effective, 4),
                      "remaining_prior_weight": round(prior_weight, 6),
                      "mate_penalty": round(mate_penalty, 3), "calibration_offset": offset})
    return curve


def metadata(board, decisions, human_color, calibration):
    return {"method": METHOD, "target": TARGET,
            "human_color": "white" if human_color else "black",
            "initial_fen": board.root().fen(en_passant="fen"),
            "calibration": _model(calibration),
            "curve": _curve(observations(board, decisions, human_color), calibration)}


def _snapshot(curve, calibration, *, method=METHOD):
    model = _model(calibration, method=method)
    point = curve[-1] if curve else {}
    result = {"method": method, "target": TARGET, "estimate": point.get("estimate"),
            "delta": point.get("delta"), "observations": len(curve),
            "updated_human_ply": point.get("human_ply"),
            "calibration_games": model["calibration_games"],
            "source": "local_regularized_calibration" if model["calibration_games"] else "heuristic_prior",
            "status": ("awaiting_observations" if not curve else "provisional_calibrated"
                       if model["calibration_games"] else "provisional_uncalibrated"),
            "label": None}
    if method == METHOD:
        effective = point.get("effective_observations", 0.)
        result.update(effective_observations=effective,
                      evidence_level="low" if effective < 4 else "developing" if effective < 12 else "supported",
                      calibration_spread=_stage_value(model, "spread_by_observation", len(curve),
                                                       model.get("calibration_spread")))
    return result


def snapshot(board, decisions, human_color, calibration):
    data = metadata(board, decisions, human_color, calibration)
    return _snapshot(data["curve"], data["calibration"])


def archive_snapshot(board, data):
    """Replay the available prefix using its recorded method, without future data."""
    if (not isinstance(data, dict) or data.get("method") not in METHODS or data.get("target") != TARGET
            or data.get("initial_fen") != board.root().fen(en_passant="fen")
            or data.get("human_color") not in ("white", "black")
            or not isinstance(data.get("curve"), list)):
        return None
    method, color = data["method"], data["human_color"] == "white"
    history = [move.uci() for move in board.move_stack]
    position, fens, turns, positions = board.root(), [], [], []
    for move in board.move_stack:
        if move not in position.legal_moves:
            return None
        positions.append(position.copy(stack=True))
        turns.append(position.turn)
        fens.append(position.fen(en_passant="fen"))
        position.push(move)
    if position.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return None
    rows = []
    for row in data.get("curve", []):
        if not isinstance(row, dict):
            return None
        ply = row.get("human_ply")
        if type(ply) is not int or ply < 1:
            return None
        if row.get("available_ply") != ply + 1:
            return None
        if row["available_ply"] > len(history):
            continue
        if (turns[ply - 1] != color or row.get("history_before") != history[:ply - 1]
                or row.get("played_uci") != history[ply - 1]
                or row.get("before_fen") != fens[ply - 1]
                or (rows and ply <= rows[-1]["human_ply"])):
            return None
        if method == LEGACY_METHOD:
            if (not _number(row.get("loss_cp")) or row["loss_cp"] < 0
                    or type(row.get("depth")) is not int or row["depth"] < 4):
                return None
            rows.append(row)
        else:
            item = _item(positions[ply - 1], {key: row[key] for key in
                         ("human_ply", "available_ply", "history_before", "played_uci", "before_fen")}, row)
            if item is None:
                return None
            rows.append(item)
    # Recompute the transparent method; never trust an arbitrary saved estimate.
    curve = _legacy_curve(rows, data.get("calibration")) if method == LEGACY_METHOD else _curve(rows, data.get("calibration"))
    return _snapshot(curve, data.get("calibration"), method=method)


def _label_rows(features):
    """Validate saved input histories; derive v2 context without saved estimates."""
    if (not isinstance(features, dict) or features.get("method") not in METHODS
            or features.get("target") != TARGET or features.get("human_color") not in ("white", "black")
            or not isinstance(features.get("curve"), list) or not features["curve"]
            or not isinstance(features.get("initial_fen"), str)):
        return None
    try:
        root = chess.Board(features["initial_fen"])
    except (ValueError, TypeError):
        return None
    if not root.is_valid():
        return None
    rows, previous = [], []
    color = features["human_color"] == "white"
    for row in features["curve"]:
        if not isinstance(row, dict):
            return None
        ply, prefix = row.get("human_ply"), row.get("history_before")
        if (type(ply) is not int or ply < 1 or row.get("available_ply") != ply + 1
                or not isinstance(prefix, list) or len(prefix) != ply - 1
                or prefix[:len(previous)] != previous or rows and ply <= rows[-1]["human_ply"]):
            return None
        before = root.copy(stack=True)
        try:
            for uci in prefix:
                move = chess.Move.from_uci(uci)
                if move not in before.legal_moves:
                    return None
                before.push(move)
        except (ValueError, TypeError):
            return None
        if before.turn != color or row.get("before_fen") != before.fen(en_passant="fen"):
            return None
        item = _item(before, {key: row.get(key) for key in
                     ("human_ply", "available_ply", "history_before", "played_uci", "before_fen")}, row)
        if item is None:
            return None
        rows.append(item)
        previous = prefix + [item["played_uci"]]
    return rows


def _fit_offset(values):
    """One bounded contribution per game; robust location, never a slope."""
    residuals = [value for value, _weight in values]
    center = statistics.median(residuals)
    mad = statistics.median(abs(value - center) for value in residuals)
    radius = max(150., 3 * mad)
    bounded = [(max(center - radius, min(center + radius, value)) if len(values) >= 3 else value, weight)
               for value, weight in values]
    total = sum(weight for _value, weight in bounded)
    offset = sum(value * weight for value, weight in bounded) / (CALIBRATION_PRIOR_GAMES + total)
    # Descriptive residual spread is not predictive uncertainty or a CI.
    return round(max(-MAX_CALIBRATION_OFFSET, min(MAX_CALIBRATION_OFFSET, offset)), 3), round(mad, 3)


def fingerprint(board, result, termination):
    value = [board.root().fen(en_passant="fen"), [m.uci() for m in board.move_stack],
             board.fen(en_passant="fen"), result, termination]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


class RatingStore:
    """Private local supervised data. SQLite transactions serialize corrections."""
    def __init__(self, save_dir):
        self.directory = Path(save_dir) / ".sparring-rating-private"
        self.path = self.directory / "labels.sqlite3"

    def _connect(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        # Even a custom save directory inside a checkout must not upload labels.
        (self.directory / ".gitignore").write_text("*\n", encoding="utf-8")
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS labels (game_id TEXT PRIMARY KEY, "
                               "method TEXT NOT NULL, evaluation_key TEXT NOT NULL, rating INTEGER NOT NULL, "
                               "eligible INTEGER NOT NULL, sample_json TEXT NOT NULL, recorded_at TEXT NOT NULL)")
        except BaseException:
            connection.close()
            raise
        return connection

    def calibration(self, evaluation_key):
        model = prior(evaluation_key)
        if not self.path.exists():
            return model
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
                saved = connection.execute("SELECT method,rating,sample_json FROM labels WHERE "
                    "method IN (?,?) AND evaluation_key=? AND eligible=1", (*METHODS, evaluation_key)).fetchall()
            games = []
            for method, rating, encoded in saved:
                try:
                    sample = json.loads(encoded)
                except (TypeError, ValueError):
                    continue
                if (not isinstance(sample, dict) or sample.get("completed") is not True
                        or sample.get("handicap") is not False or type(rating) is not int or not 100 <= rating <= 4000):
                    continue
                features = sample.get("features")
                if (not isinstance(features, dict) or features.get("method") != method
                        or not isinstance(features.get("calibration"), dict)
                        or features["calibration"].get("evaluation_key") != evaluation_key):
                    continue
                rows = _label_rows(features)
                if not rows or not any(row["information_weight"] > 0 for row in rows):
                    continue
                curve = _curve(rows, prior(evaluation_key))
                games.append((rating, curve, min(1., len(curve) / 8.)))
            if games:
                offsets, spreads = [], []
                for stage in range(1, 41):
                    values = [(max(-MAX_CALIBRATION_OFFSET, min(MAX_CALIBRATION_OFFSET,
                               rating - curve[min(stage, len(curve)) - 1]["prior_estimate"])), weight)
                              for rating, curve, weight in games]
                    offset, spread = _fit_offset(values)
                    offsets.append(offset)
                    spreads.append(spread)
                model.update(calibration_games=len(games), offset=offsets[-1], offset_by_observation=offsets,
                             spread_by_observation=spreads, calibration_spread=spreads[-1],
                             calibration_weight=round(sum(weight for _rating, _curve, weight in games), 4))
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            # Unavailable labels cannot prevent chess or become invented data.
            model["storage_warning"] = "local_calibration_unavailable"
        return model

    def upsert(self, game_id, rating, sample):
        if type(rating) is not int or not 100 <= rating <= 4000:
            raise ValueError("실제 Chess.com 래피드 레이팅은 100~4000 정수로 입력해 주세요.")
        if not isinstance(sample, dict) or sample.get("completed") is not True:
            raise ValueError("완료된 대국만 레이팅을 저장할 수 있습니다.")
        features = sample.get("features", {})
        method = features.get("method", METHOD) if isinstance(features, dict) else METHOD
        model = _model(features.get("calibration") if isinstance(features, dict) else None, method=method)
        rows = _label_rows(features)
        eligible = bool(rows and any(row["information_weight"] > 0 for row in rows)) and sample.get("handicap") is False
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        encoded = json.dumps({**sample, "source": "user_reported"}, ensure_ascii=False, allow_nan=False)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?) ON CONFLICT(game_id) "
                    "DO UPDATE SET method=excluded.method,evaluation_key=excluded.evaluation_key,"
                    "rating=excluded.rating,eligible=excluded.eligible,sample_json=excluded.sample_json,"
                    "recorded_at=excluded.recorded_at", (game_id, method, model.get("evaluation_key", ""),
                        rating, int(eligible), encoded, timestamp))
        except (sqlite3.Error, OSError) as error:
            raise ValueError("개인 레이팅을 저장하지 못했습니다. 저장 위치를 확인해 주세요.") from error
        return {"rating": rating, "source": "user_reported", "recorded_at": timestamp,
                "eligible_for_calibration": eligible}

    def invalidate(self, game_id):
        if not self.path.exists():
            return
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("UPDATE labels SET eligible=0 WHERE game_id=?", (game_id,))
        except (sqlite3.Error, OSError) as error:
            raise ValueError("개인 레이팅의 대국 연결을 갱신하지 못했습니다.") from error
