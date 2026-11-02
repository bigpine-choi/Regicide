"""Experimental local Chess.com Rapid estimate, separate from move selection.

No rating relationship is claimed before user-reported completed-game labels.
The transparent prior maps shrunk native centipawn losses to a display scale;
local supervision fits only a strongly regularized offset (ten prior games).
There are no engine calls, network calls, or calibrated confidence intervals.
"""
import datetime
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import chess

METHOD = "regicide-sparring-rating-v1"
TARGET = "chess_com_rapid"
POLICIES = ("regicide-sparring-play-v1", "regicide-sparring-play-v2")
PRIOR_LOSS = 80.0
PRIOR_WEIGHT = 8.0
CALIBRATION_PRIOR_GAMES = 10.0
MAX_CALIBRATION_OFFSET = 3000.0


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def prior(evaluation_key=""):
    return {"method": METHOD, "evaluation_key": evaluation_key,
            "calibration_games": 0, "offset": 0.0}


def _model(value):
    if (not isinstance(value, dict) or value.get("method") != METHOD
            or type(value.get("calibration_games")) is not int
            or value["calibration_games"] < 0 or not _number(value.get("offset"))
            or abs(value["offset"]) > MAX_CALIBRATION_OFFSET):
        return prior()
    return dict(value)


def observations(board, decisions, human_color):
    """Only actual, unforced, same-history quality receipts become evidence."""
    root = board.root()
    history, fens, turns = [], [root.fen(en_passant="fen")], []
    for move in board.move_stack:
        if move not in root.legal_moves:
            return []
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
        prefix, ply = value.get("history_before"), value.get("ply")
        depth, loss = value.get("depth"), value.get("loss_cp")
        if (type(ply) is not int or not 1 <= ply < len(history)
                or turns[ply - 1] != human_color or prefix != history[:ply - 1]
                or value.get("played_uci") != history[ply - 1]
                or value.get("before_fen") != fens[ply - 1]
                or value.get("valid") is not True or value.get("forced") is not False
                or type(depth) is not int or depth < 4 or not _number(loss) or loss < 0
                or row.get("history_uci") != history[:ply]
                or row.get("before_fen") != fens[ply]
                or row.get("ply") != ply + 1 or row.get("selected_uci") != history[ply]):
            continue
        item = {"human_ply": ply, "available_ply": ply + 1,
                "history_before": prefix, "played_uci": history[ply - 1],
                "before_fen": fens[ply - 1], "loss_cp": loss, "depth": depth}
        if ply in found and found[ply] != item:
            found[ply] = None
        elif ply not in found:
            found[ply] = item
    return [item for _, item in sorted(found.items()) if item is not None]


def _curve(rows, calibration):
    curve, recent = [], []
    offset = _model(calibration)["offset"]
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


def metadata(board, decisions, human_color, calibration):
    return {"method": METHOD, "target": TARGET,
            "human_color": "white" if human_color else "black",
            "initial_fen": board.root().fen(en_passant="fen"),
            "calibration": _model(calibration),
            "curve": _curve(observations(board, decisions, human_color), calibration)}


def _snapshot(curve, calibration):
    model = _model(calibration)
    point = curve[-1] if curve else {}
    return {"method": METHOD, "target": TARGET, "estimate": point.get("estimate"),
            "delta": point.get("delta"), "observations": len(curve),
            "updated_human_ply": point.get("human_ply"),
            "calibration_games": model["calibration_games"],
            "source": "local_regularized_calibration" if model["calibration_games"] else "heuristic_prior",
            "status": ("awaiting_observations" if not curve else "provisional_calibrated"
                       if model["calibration_games"] else "provisional_uncalibrated"),
            "label": None}


def snapshot(board, decisions, human_color, calibration):
    data = metadata(board, decisions, human_color, calibration)
    return _snapshot(data["curve"], data["calibration"])


def archive_snapshot(board, data):
    """Replay the recorded curve prefix; never reveal a future observation."""
    if (not isinstance(data, dict) or data.get("method") != METHOD or data.get("target") != TARGET
            or data.get("initial_fen") != board.root().fen(en_passant="fen")
            or data.get("human_color") not in ("white", "black")
            or not isinstance(data.get("curve"), list)):
        return None
    color = data["human_color"] == "white"
    history = [move.uci() for move in board.move_stack]
    position, fens, turns = board.root(), [], []
    for move in board.move_stack:
        if move not in position.legal_moves:
            return None
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
                or not _number(row.get("loss_cp")) or row["loss_cp"] < 0
                or type(row.get("depth")) is not int or row["depth"] < 4
                or (rows and ply <= rows[-1]["human_ply"])):
            return None
        rows.append(row)
    # Recompute the transparent method; never trust an arbitrary saved estimate.
    return _snapshot(_curve(rows, data.get("calibration")), data.get("calibration"))


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
            with closing(sqlite3.connect(self.path, timeout=3)) as connection:
                rows = connection.execute("SELECT rating, sample_json FROM labels WHERE "
                    "method=? AND evaluation_key=? AND eligible=1", (METHOD, evaluation_key)).fetchall()
            residuals = []
            for rating, encoded in rows:
                sample = json.loads(encoded)
                if not isinstance(sample, dict) or not isinstance(sample.get("features"), dict):
                    continue
                features = sample["features"]
                curve = features.get("curve", [])
                if (sample.get("completed") is True and sample.get("handicap") is False
                        and features.get("method") == METHOD and features.get("target") == TARGET
                        and isinstance(curve, list) and curve and isinstance(curve[-1], dict) and type(rating) is int
                        and 100 <= rating <= 4000 and _number(curve[-1].get("prior_estimate"))):
                    residuals.append(max(-MAX_CALIBRATION_OFFSET, min(MAX_CALIBRATION_OFFSET,
                        rating - curve[-1]["prior_estimate"])))
            model.update(calibration_games=len(residuals), offset=round(max(-MAX_CALIBRATION_OFFSET, min(MAX_CALIBRATION_OFFSET,
                sum(residuals) / (CALIBRATION_PRIOR_GAMES + len(residuals)))), 3))
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            # Unavailable labels cannot prevent chess or become invented data.
            model["storage_warning"] = "local_calibration_unavailable"
        return model

    def upsert(self, game_id, rating, sample):
        if type(rating) is not int or not 100 <= rating <= 4000:
            raise ValueError("실제 Chess.com 래피드 레이팅은 100~4000 정수로 입력해 주세요.")
        if sample.get("completed") is not True:
            raise ValueError("완료된 대국만 레이팅을 저장할 수 있습니다.")
        features = sample.get("features", {})
        model = _model(features.get("calibration"))
        eligible = bool(features.get("curve")) and sample.get("handicap") is False
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        encoded = json.dumps({**sample, "source": "user_reported"}, ensure_ascii=False, allow_nan=False)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("INSERT INTO labels VALUES (?,?,?,?,?,?,?) ON CONFLICT(game_id) "
                    "DO UPDATE SET method=excluded.method,evaluation_key=excluded.evaluation_key,"
                    "rating=excluded.rating,eligible=excluded.eligible,sample_json=excluded.sample_json,"
                    "recorded_at=excluded.recorded_at", (game_id, METHOD, model.get("evaluation_key", ""),
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
