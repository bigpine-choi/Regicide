"""Exercise native analysis scene against an isolated real Regicide host."""

import argparse
import json
from pathlib import Path
import tempfile
import threading
import time

import godot_integration_tests as integration


class DelayedAnalysisHandler(integration.AuditedHandler):
    """Deliver an old GET after clicks and mutations have already happened."""

    def do_GET(self):
        if self.path != "/api/analysis":
            return super().do_GET()
        if not self._allowed(True):
            return
        snapshot = self.server.game.analysis.snapshot()
        entry = {"method": "GET", "path": self.path,
                 "snapshot_revision": snapshot["revision"],
                 "delayed_snapshot": True, "captured_at": time.monotonic()}
        self.server.audit.append(entry)
        time.sleep(.35)
        entry["delivered_at"] = time.monotonic()
        self._send(200, snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=integration.ROOT / "build/analysis-godot")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    integration.verify_fresh_checkout(args)
    integration.run_godot(args, "analysis_tests.gd", ["--evidence-dir", args.output], "analysis-synthetic")
    integration.run_godot(args, "analysis_drag_input_tests.gd", label="analysis-drag-input")
    with tempfile.TemporaryDirectory(prefix="regicide-analysis-godot-") as temporary:
        temporary = Path(temporary)
        game = integration.backend.HumanGame(args.binary, temporary / "games")
        server = integration.backend.HumanServer(game)
        server.RequestHandlerClass = DelayedAnalysisHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        initial = game.state()
        archives = {path.name: path.read_bytes() for path in (temporary / "games").rglob("*") if path.is_file()}
        try:
            integration.run_godot(args, "analysis_tests.gd", ["--backend-file", config, "--evidence-dir", args.output], "analysis-live")
            integration.run_godot(args, "analysis_castling_flow_tests.gd", ["--backend-file", config], "analysis-castling-flow")
            assert game.state() == initial, "analysis changed the live game"
            assert archives == {path.name: path.read_bytes() for path in (temporary / "games").rglob("*") if path.is_file()}, "analysis changed live archives"
            mutations = [entry["path"] for entry in server.audit if entry["method"] == "POST"]
            assert mutations and all(route == "/api/analysis" for route in mutations), mutations
            posts = [entry for entry in server.audit if entry["method"] == "POST"]
            assert any(poll.get("captured_at", 0) < post["started"]
                       and poll.get("delivered_at", 0) > post["started"] + post.get("elapsed_seconds", 0)
                       for poll in server.audit if poll.get("delayed_snapshot")
                       for post in posts), "no mutation completed while an older GET was delayed"
            assert not game.analysis.snapshot()["active"]
            (args.output / "analysis-integration.json").write_text(json.dumps({"engine": game.engine_name, "binary_sha256": game.binary_sha256, "routes": server.audit, "live_state_unchanged": True, "archives_unchanged": True, "mode": "native rendered synthetic input" if args.rendered else "headless synthetic input"}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()
    print("Godot free placement, real recommendations, manual replies, undo, promotion and live isolation passed.")


if __name__ == "__main__":
    main()
