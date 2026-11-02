"""Server-owned monotonic Fischer clocks for local practice games.

Historical entries are balances after a completed move and its increment.
Polling never resets an anchor. Wall-clock/date changes cannot add time.
"""

import copy
import time


SCHEMA = "regicide.game-clock.v1"
CONTROLS = {
    "10+0": {"initial_ms": 600_000, "increment_ms": 0, "label": "10분 래피드"},
    "10+5": {"initial_ms": 600_000, "increment_ms": 5_000, "label": "10+5 래피드"},
    "15+10": {"initial_ms": 900_000, "increment_ms": 10_000, "label": "15+10 래피드"},
}


def validate_control(control_id):
    if control_id is not None and (not isinstance(control_id, str) or control_id not in CONTROLS):
        raise ValueError("10분, 10+5 또는 15+10 래피드를 선택해 주세요.")
    return control_id


class GameClock:
    def __init__(self, control_id=None, *, monotonic=None, timeout_policy="both", soft_timeout_color=None):
        self.control_id = validate_control(control_id)
        if timeout_policy not in ("both", "engine_only"):
            raise ValueError("Unknown clock timeout policy.")
        if (timeout_policy == "engine_only" and soft_timeout_color not in ("white", "black")
                or timeout_policy == "both" and soft_timeout_color is not None):
            raise ValueError("Clock timeout policy and practice color do not match.")
        self.timeout_policy = timeout_policy
        self.soft_timeout_color = soft_timeout_color
        self._now = monotonic or time.monotonic
        self.config = dict(CONTROLS[control_id]) if control_id else {
            "initial_ms": 0, "increment_ms": 0, "label": "시간 제한 없음"}
        self.enabled = control_id is not None
        self._balances = {"white": float(self.config["initial_ms"]), "black": float(self.config["initial_ms"])}
        self._running = "white" if self.enabled else None
        self._turn_color = self._running
        self._anchor = self._now()
        self._elapsed_before_pause = 0.0
        self._status = "running" if self.enabled else "disabled"
        self.flagged_color = None
        self.pause_reason = None
        self.ply = 0
        self.history = [self._entry(None, 0)] if self.enabled else []

    def _entry(self, mover, elapsed_ms):
        return {"ply": self.ply, "white_ms": round(self._balances["white"], 3),
                "black_ms": round(self._balances["black"], 3), "mover": mover,
                "elapsed_ms": round(elapsed_ms, 3)}

    def _elapsed(self, now=None):
        if self._running is None:
            return 0.0
        return max(0.0, ((self._now() if now is None else now) - self._anchor) * 1000)

    def snapshot(self):
        now = self._now()
        elapsed = self._elapsed(now)
        balances = dict(self._balances)
        if self._running:
            balances[self._running] = max(0.0, balances[self._running] - elapsed)
        overtime_color = (self.soft_timeout_color if self.enabled and self.soft_timeout_color
                          and balances[self.soft_timeout_color] <= 0 else None)
        turn_elapsed = self._elapsed_before_pause + elapsed
        overtime_ms = (max(0, turn_elapsed - self.history[-1][self.soft_timeout_color + "_ms"])
                       if overtime_color and self._turn_color == self.soft_timeout_color else 0)
        return {"schema": SCHEMA, "enabled": self.enabled, "control_id": self.control_id,
                "timeout_policy": self.timeout_policy, "soft_timeout_color": self.soft_timeout_color,
                "overtime_color": overtime_color, "overtime_ms": round(overtime_ms, 3),
                **self.config, "white_ms": round(balances["white"], 3) if self.enabled else None,
                "black_ms": round(balances["black"], 3) if self.enabled else None,
                "running_color": self._running, "status": self._status,
                "turn_elapsed_ms": round(turn_elapsed, 3),
                "flagged_color": self.flagged_color, "pause_reason": self.pause_reason, "ply": self.ply,
                "sampled_monotonic_ms": round(now * 1000, 3) if self.enabled else None}

    def _expire_elapsed(self, elapsed):
        if (self._running and self._running != self.soft_timeout_color
                and elapsed >= self._balances[self._running]):
            color = self._running
            self._elapsed_before_pause += self._balances[color]
            self._balances[color] = 0.0
            self.flagged_color = color
            self._running = None
            self._status = "finished"
            return color
        return None

    def expire(self):
        return self._expire_elapsed(self._elapsed())

    def complete_move(self, color, ply):
        if not self.enabled:
            self.ply = ply
            return True
        if self.expire() or self.flagged_color:
            return False
        if self._status != "running" or color != self._running or ply != self.ply + 1:
            raise ValueError("Clock and board turn do not match.")
        now = self._now()
        elapsed = self._elapsed(now)
        # Recheck the exact completion instant, not the earlier UI snapshot.
        if color != self.soft_timeout_color and elapsed >= self._balances[color]:
            self._expire_elapsed(elapsed)
            return False
        self._balances[color] = max(0.0, self._balances[color] - elapsed) + self.config["increment_ms"]
        self.ply = ply
        self.history.append(self._entry(color, self._elapsed_before_pause + elapsed))
        self._running = "black" if color == "white" else "white"
        self._turn_color = self._running
        self._anchor = now
        self._elapsed_before_pause = 0.0
        return True

    def pause(self, reason="paused"):
        if not self.enabled or self._status != "running":
            return
        # Use exactly one instant for both the flag check and debit. A
        # second read could cross zero and produce a paused zero clock with
        # no flag, incorrectly letting an editor pause evade timeout.
        elapsed = self._elapsed()
        if self._expire_elapsed(elapsed):
            return
        self._balances[self._running] = max(0.0, self._balances[self._running] - elapsed)
        self._elapsed_before_pause += elapsed
        self._running = None
        self._status = "paused"
        self.pause_reason = reason

    def resume(self, color):
        if self.enabled and self._status == "paused" and not self.flagged_color:
            self._running = color
            self._turn_color = color
            self._anchor = self._now()
            self._status = "running"
            self.pause_reason = None

    def finish(self):
        if self.enabled and self._status != "finished":
            # An already established board result has precedence over a
            # later poll. Stopping must not manufacture a new flag fall.
            if self._running:
                elapsed = self._elapsed()
                if self._running != self.soft_timeout_color:
                    elapsed = min(self._balances[self._running], elapsed)
                self._balances[self._running] = max(0.0, self._balances[self._running] - elapsed)
                self._elapsed_before_pause += elapsed
            self._running = None
            self._status = "finished"
            self.pause_reason = None

    def restore_ply(self, ply, color):
        if not self.enabled:
            self.ply = ply
            return
        entry = next((r for r in self.history if r["ply"] == ply), None)
        if entry is None:
            raise ValueError("Missing historical clock balance.")
        self.history = [r for r in self.history if r["ply"] <= ply]
        self._balances = {name: float(entry[name + "_ms"]) for name in ("white", "black")}
        self.ply = ply
        self.flagged_color = None
        self.pause_reason = None
        self._elapsed_before_pause = 0.0
        self._running = color
        self._turn_color = color
        self._anchor = self._now()
        self._status = "running"

    def metadata(self):
        return {"schema": SCHEMA, "enabled": self.enabled, "control_id": self.control_id, **self.config,
                "timeout_policy": self.timeout_policy, "soft_timeout_color": self.soft_timeout_color,
                "history": copy.deepcopy(self.history), "snapshot": self.snapshot(),
                "timeout_rule": ("human clock zero permits play; engine clock zero loses regardless of mating material"
                                 if self.timeout_policy == "engine_only" else
                                 "opponent has_insufficient_material => draw; conservative material-only detection"),
                "undo_policy": "restore balances after retained ply; no retroactive extra increment"}
