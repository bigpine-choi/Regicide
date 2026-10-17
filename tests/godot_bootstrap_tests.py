"""Exercise desktop bootstrap with a real isolated manager/Regicide process."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BINARY = None


class BootstrapTests(unittest.TestCase):
    def test_desktop_mode_requires_a_private_startup_file(self):
        reply = subprocess.run([sys.executable, str(ROOT / "scripts/play_human.py"),
                                "--desktop-client", "--no-browser", "--binary", "not-an-engine"],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(reply.returncode, 2)
        self.assertIn("--desktop-client requires --startup-file", reply.stderr)

    def exercise(self, desktop):
        if BINARY is None:
            self.skipTest("supply the Regicide binary for isolated real-process checks")
        with tempfile.TemporaryDirectory(prefix="regicide-bootstrap-") as directory:
            folder = Path(directory)
            startup_file = folder / "session.json"
            stdout_file = folder / "stdout.txt"
            stderr_file = folder / "stderr.txt"
            command = [sys.executable, str(ROOT / "scripts/play_human.py"), "--binary", str(BINARY),
                       "--save-dir", str(folder / "games"), "--startup-file", str(startup_file), "--no-browser"]
            if desktop:
                command.append("--desktop-client")
            with stdout_file.open("w", encoding="utf-8") as stdout, stderr_file.open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
                startup = None
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if startup_file.is_file():
                            startup = json.loads(startup_file.read_text(encoding="utf-8"))
                            break
                        if process.poll() is not None:
                            self.fail("manager exited: " + stderr_file.read_text(encoding="utf-8"))
                        time.sleep(0.02)
                    self.assertIsNotNone(startup, "startup deadline")
                    self.assertTrue(startup["url"].startswith("http://127.0.0.1:"))
                    self.assertIn("Regicide", startup["engine_name"])
                    public = json.loads(stdout_file.read_text(encoding="utf-8").strip())
                    self.assertNotIn("token", public)
                    with urllib.request.urlopen(startup["url"] + "/", timeout=5) as reply:
                        html = reply.read().decode("utf-8")
                    token = html.split('name="regicide-token" content="', 1)[1].split('"', 1)[0]
                    if desktop:
                        self.assertEqual(startup["token"], token)
                        self.assertNotIn(token, stdout_file.read_text(encoding="utf-8"))
                        if os.name == "posix":
                            self.assertEqual(startup_file.stat().st_mode & 0o077, 0)
                    else:
                        self.assertNotIn("token", startup)
                    request = urllib.request.Request(startup["url"] + "/api/state")
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        urllib.request.urlopen(request, timeout=5)
                    self.assertEqual(missing.exception.code, 403)
                    missing.exception.close()
                    request.add_header("X-Regicide-Token", token)
                    with urllib.request.urlopen(request, timeout=5) as reply:
                        state = json.load(reply)
                    self.assertTrue(state["human_to_move"])
                    self.assertEqual(len(state["legal_moves"]), 20)
                    stop = urllib.request.Request(startup["url"] + "/api/shutdown",
                                                  data=json.dumps({"revision": state["revision"]}).encode(),
                                                  headers={"Content-Type": "application/json",
                                                           "X-Regicide-Token": token}, method="POST")
                    with urllib.request.urlopen(stop, timeout=5) as reply:
                        self.assertTrue(json.load(reply)["closed"])
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertEqual(stderr_file.read_text(encoding="utf-8"), "")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)

    def test_desktop_secret_file_authenticates_without_stdout_disclosure(self):
        self.exercise(True)

    def test_browser_startup_contract_stays_compatible(self):
        self.exercise(False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?")
    args, rest = parser.parse_known_args()
    BINARY = args.binary.resolve() if args.binary else None
    unittest.main(argv=[str(Path(__file__)), *rest], verbosity=2)
