"""Resolve isolated referee paths using fixture bytes, without starting engines."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import referee_engine


class RefereeEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / referee_engine.DEFAULT_RELATIVE_PATHS[0]
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"fixture official engine")
        self.license = self.binary.parent / "Copying.txt"
        self.license.write_bytes(b"fixture GPL license text")
        self.sha = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.license_sha = hashlib.sha256(self.license.read_bytes()).hexdigest()
        patches = [mock.patch.object(referee_engine, "STOCKFISH_SHA256", self.sha),
                   mock.patch.object(referee_engine, "STOCKFISH_LICENSE_SHA256", self.license_sha),
                   mock.patch.object(referee_engine.sys, "platform", "win32"),
                   mock.patch.dict(os.environ, {referee_engine.ENVIRONMENT_VARIABLE: ""}),
                   mock.patch.object(subprocess, "Popen", side_effect=AssertionError("resolver must never start a process"))]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)

    def resolve(self, explicit=None):
        return referee_engine.resolve_referee_engine(self.root, explicit)

    def test_reuses_only_verified_default_and_reports_provenance_without_writes(self):
        original = {path: path.stat().st_mtime_ns for path in [self.binary,self.license]}
        result = self.resolve()
        self.assertTrue(result["available"])
        self.assertEqual(result["binary"],str(self.binary.resolve()))
        self.assertEqual(result["sha256"],self.sha)
        self.assertEqual(result["license_sha256"],self.license_sha)
        self.assertEqual(result["name"],"Stockfish 18")
        self.assertEqual(result["selection"],"local_package")
        self.assertEqual(result["license"],"GPL-3.0-or-later")
        self.assertTrue(result["verified_official"])
        self.assertEqual(result["source_revision"],"cb3d4ee9b47d0c5aae855b12379378ea1439675c")
        self.assertEqual({path:path.stat().st_mtime_ns for path in original},original)
        json.dumps(result)

    def test_modified_default_is_unavailable_without_silent_fallback(self):
        fallback=self.root/referee_engine.DEFAULT_RELATIVE_PATHS[1]
        fallback.parent.mkdir(parents=True);fallback.write_bytes(self.binary.read_bytes())
        (fallback.parent/"Copying.txt").write_bytes(self.license.read_bytes())
        self.binary.write_bytes(b"changed engine")
        result=self.resolve()
        self.assertFalse(result["available"]);self.assertIn("해시",result["reason"])
        self.assertEqual(result["binary"],str(self.binary.resolve()))

    def test_missing_or_changed_default_license_is_unavailable(self):
        self.license.unlink()
        self.assertFalse(self.resolve()["available"])
        self.license.write_bytes(b"not the expected license")
        self.assertIn("라이선스",self.resolve()["reason"])

    def test_no_default_does_not_scan_path_or_choose_regicide(self):
        self.binary.unlink();(self.root/"build").mkdir(exist_ok=True)
        (self.root/"build/regicide.exe").write_bytes(b"main playing engine")
        with mock.patch.object(referee_engine.os,"get_exec_path",side_effect=AssertionError("no PATH scan")):
            result=self.resolve()
        self.assertFalse(result["available"]);self.assertEqual(result["selection"],"none")

    def test_second_known_local_package_is_available_if_first_does_not_exist(self):
        fallback=self.root/referee_engine.DEFAULT_RELATIVE_PATHS[1]
        fallback.parent.mkdir(parents=True);fallback.write_bytes(self.binary.read_bytes())
        (fallback.parent/"Copying.txt").write_bytes(self.license.read_bytes());self.binary.unlink()
        self.assertEqual(self.resolve()["binary"],str(fallback.resolve()))

    def test_explicit_selection_precedes_environment_and_default(self):
        explicit=self.root/"chosen engine.exe";explicit.write_bytes(b"custom user-selected engine")
        os.environ[referee_engine.ENVIRONMENT_VARIABLE]=str(self.binary)
        result=self.resolve(explicit)
        self.assertTrue(result["available"]);self.assertEqual(result["selection"],"explicit")
        self.assertEqual(result["binary"],str(explicit.resolve()));self.assertFalse(result["verified_official"])
        self.assertIsNone(result["source_url"]);self.assertIsNone(result["name"]);self.assertIsNone(result["license"])
        self.assertIn("신원",result["reason"])

    def test_environment_selection_supports_repo_relative_path_and_does_not_change_env(self):
        os.environ[referee_engine.ENVIRONMENT_VARIABLE]=referee_engine.DEFAULT_RELATIVE_PATHS[0]
        result=self.resolve()
        self.assertTrue(result["available"]);self.assertEqual(result["selection"],"environment")
        self.assertEqual(os.environ[referee_engine.ENVIRONMENT_VARIABLE],referee_engine.DEFAULT_RELATIVE_PATHS[0])

    def test_missing_or_invalid_explicit_selection_never_falls_back(self):
        for path in [self.root/"missing.exe","","bad\nengine.exe","bad\0engine.exe",42]:
            with self.subTest(path=path):
                result=self.resolve(path)
                self.assertFalse(result["available"]);self.assertEqual(result["selection"],"explicit")
        os.environ[referee_engine.ENVIRONMENT_VARIABLE]="missing-environment.exe"
        self.assertFalse(self.resolve()["available"])
        self.assertEqual(self.resolve()["selection"],"environment")

    def test_non_binary_directory_and_empty_file_are_rejected(self):
        for path in [self.binary.parent,self.root/"empty.exe",self.root/"engine.cmd"]:
            if path.suffix:path.write_bytes(b"" if path.name=="empty.exe" else b"echo bad")
            self.assertFalse(self.resolve(path)["available"])

    def test_non_windows_needs_explicit_native_executable(self):
        with mock.patch.object(referee_engine.sys,"platform","linux"):
            self.assertFalse(self.resolve()["available"])
            self.assertFalse(self.resolve(self.binary)["available"])
            native=self.root/"stockfish";native.write_bytes(b"native fixture");native.chmod(0o755)
            with mock.patch.object(referee_engine.os,"access",return_value=True):
                result=self.resolve(native)
            self.assertTrue(result["available"]);self.assertFalse(result["verified_official"])
            with mock.patch.object(referee_engine.os,"access",return_value=False):
                self.assertFalse(self.resolve(native)["available"])

    def test_unreadable_binary_and_invalid_root_return_unavailable(self):
        with mock.patch.object(referee_engine,"_digest",side_effect=PermissionError("denied")):
            result=self.resolve()
            self.assertFalse(result["available"]);self.assertIn("PermissionError",result["reason"])
        self.assertFalse(referee_engine.resolve_referee_engine(self.root/"missing-root")["available"])

    def test_cli_only_reports_json_and_never_opens_a_process(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=referee_engine.main(["--repo-root",str(self.root)])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())["sha256"],self.sha)


if __name__=="__main__":
    unittest.main()
