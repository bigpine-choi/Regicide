"""Validate native hint presentation and real Regicide helper HTTP integration.

Inputs are synthesized. --rendered captures real native rendered screenshots.
Usage: python tests/hint_godot_tests.py --godot GODOT --binary REGICIDE
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import godot_integration_tests as integration


ROOT = integration.ROOT
backend = integration.backend


class DelayedHintHandler(integration.AuditedHandler):
    """Deliver a captured ready hint late to exercise the native callback barrier."""

    def do_GET(self):
        if self.path != "/api/hint":
            return super().do_GET()
        if not self._allowed(True):
            return
        snapshot = self.server.game.hint()
        delayed = snapshot["status"] == "ready"
        self.server.audit.append({"method": "GET", "path": self.path,
                                  "snapshot_status": snapshot["status"],
                                  "snapshot_revision": snapshot["revision"],
                                  "delayed_ready": delayed})
        if delayed:
            time.sleep(.35)
        self._send(200, snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "build/hint-godot-validation")
    parser.add_argument("--rendered", action="store_true")
    args = parser.parse_args()
    args.godot = args.godot.resolve(strict=True)
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    imported = subprocess.run([str(args.godot), "--headless", "--path", str(ROOT / "godot"),
                               "--editor", "--import"], cwd=ROOT, text=True,
                              encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=90)
    (args.output / "import.log").write_text(imported.stdout, encoding="utf-8")
    if imported.returncode or "SCRIPT ERROR:" in imported.stdout or "ERROR:" in imported.stdout:
        raise AssertionError(f"Godot import failed:\n{imported.stdout}")
    integration.run_godot(args, "hint_tests.gd", ["--evidence-dir", args.output], label="hint-synthetic")
    with tempfile.TemporaryDirectory(prefix="regicide-hint-godot-") as temporary:
        temporary = Path(temporary)
        game = backend.HumanGame(args.binary, temporary / "games")
        server = backend.HumanServer(game)
        server.RequestHandlerClass = DelayedHintHandler
        server.audit = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = temporary / "private-backend.json"
        config.write_text(json.dumps({"url": server.origin, "token": server.token}), encoding="utf-8")
        try:
            integration.run_godot(args, "hint_tests.gd",
                                  ["--backend-file", config, "--evidence-dir", args.output],
                                  label="hint-live")
            routes = [entry["path"] for entry in server.audit if entry["method"] == "POST"]
            expected_game_routes = ["/api/move", "/api/engine", "/api/undo", "/api/new",
                                    "/api/engine", "/api/handicap", "/api/handicap-cancel", "/api/resign"]
            assert [route for route in routes if route != "/api/hint"] == expected_game_routes, routes
            assert routes.count("/api/hint") >= 8, routes
            assert any(entry.get("delayed_ready") for entry in server.audit), server.audit
            assert not game.hint()["enabled"] and game.hint()["move"] is None
            assert not game.error
            result = {"mode": "native rendered synthetic input" if args.rendered else "headless synthetic input",
                      "engine": game.engine_name, "binary_sha256": game.binary_sha256,
                      "routes": server.audit,
                      "saved_games_independently_replayed": integration.verify_saved_games(temporary / "games"),
                      "note": "Actual Regicide helper in both human colors. No manual-play validation claimed."}
            (args.output / "hint-integration-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Godot recommendation OFF/ON, both colors, cancellation, independent controls and lifecycle passed.")
        finally:
            (args.output / "http-routes.json").write_text(
                json.dumps(server.audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            game.close()


if __name__ == "__main__":
    main()
