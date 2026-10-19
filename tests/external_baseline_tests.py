"""External-baseline unit checks; optionally pass a Regicide executable."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("compare_engines", ROOT / "scripts/compare_engines.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)
REGICIDE = None


def uci_fixture():
    import chess
    board = chess.Board()
    settings = {"Hash": 8, "Threads": 2}
    for line in sys.stdin:
        words = line.split()
        if not words:
            continue
        command = words[0]
        if command == "uci":
            print("id name External baseline fixture")
            print("id author Regicide tests")
            print("option name Hash type spin default 8 min 1 max 1024")
            print("option name Threads type spin default 2 min 1 max 8")
            print("uciok", flush=True)
        elif command == "isready":
            print("readyok", flush=True)
        elif command == "setoption":
            value_at = words.index("value")
            settings[" ".join(words[2:value_at])] = int(words[value_at + 1])
        elif command == "ucinewgame":
            board.reset()
        elif command == "position":
            index = words.index("moves") if "moves" in words else len(words)
            board = chess.Board() if words[1] == "startpos" else chess.Board(" ".join(words[2:index]))
            for move in words[index + 1:]:
                board.push_uci(move)
        elif command == "go":
            if settings != {"Hash": 16, "Threads": 1}:
                raise RuntimeError(f"baseline options were not applied: {settings}")
            move = next(iter(board.legal_moves), None)
            print("info depth 1 nodes 1 time 0", flush=True)
            print("bestmove " + (move.uci() if move else "0000"), flush=True)
        elif command == "quit":
            return


class ExternalBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.binary = Path(sys.executable).resolve()
        self.source = self.directory / "external-source.json"
        self.openings = self.directory / "openings.json"
        self.openings.write_text(json.dumps({"openings": [
            {"name": "fixture-opening", "moves": ["e2e4", "e7e5"]}]}), encoding="utf-8")
        self.manifest = {"name": "Fixture", "version": "1", "source_url":
                         "https://github.com/bigpine-choi/Regicide", "revision": "test-fixture",
                         "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                         "args": [str(Path(__file__).resolve()), "--uci-fixture"]}
        self.write_manifest()

    def write_manifest(self):
        self.source.write_text(json.dumps(self.manifest), encoding="utf-8")

    def command(self):
        return [str(self.binary), str(self.binary), "--baseline-source", str(self.source),
                "--baseline-options", '{"Threads":1,"Hash":16}', "--candidate-commit", "a" * 40,
                "--pgn", str(self.directory / "match.pgn"), "--openings", str(self.openings),
                "--opening-count", "1", "--nodes", "10", "--max-plies", "6"]

    def rejects(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            compare.arguments(argv)

    def test_manifest_and_commands(self):
        source = compare.load_external_source(self.source, self.binary)
        self.assertEqual(source["manifest_path"], str(self.source.resolve()))
        self.assertEqual(source["manifest_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        item = compare.binary_provenance(self.binary, None, {"Threads": 1}, external_source=source)
        self.assertEqual(item["command"], [str(self.binary)] + self.manifest["args"])
        self.assertFalse(item["source_commit_object_verified"])
        self.assertIsNone(item["provided_source_commit"])
        self.assertEqual(item["external_source"], source)
        self.assertTrue(item["binary_sha256_matches_manifest"])
        self.assertEqual(compare.binary_provenance(self.binary, "a" * 40, {})["command"],
                         [str(self.binary), "uci"])
        del self.manifest["args"]
        self.write_manifest()
        self.assertEqual(compare.load_external_source(self.source, self.binary)["args"], [])
        self.source.write_text(json.dumps(self.manifest), encoding="utf-8-sig")
        self.assertEqual(compare.load_external_source(self.source, self.binary)["args"], [])

    def test_bad_manifest_and_hash(self):
        original = dict(self.manifest)
        invalid = [{key: value for key, value in original.items() if key != missing}
                   for missing in ("name", "version", "source_url", "revision", "binary_sha256")]
        invalid += [None, [], dict(original, version=True), dict(original, revision=None),
                    dict(original, source_url="file:///tmp/source"),
                    dict(original, source_url="https://user:password@example.com/source"),
                    dict(original, args="--uci-fixture"), dict(original, args=["ok", 1]),
                    dict(original, args=["bad\0argument"]), dict(original, binary_sha256="bad"),
                    dict(original, binary_sha256="0" * 64)]
        for manifest in invalid:
            with self.subTest(manifest=manifest):
                self.manifest = manifest
                self.write_manifest()
                with self.assertRaises(ValueError):
                    compare.load_external_source(self.source, self.binary)
        self.source.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            compare.load_external_source(self.source, self.binary)

    def test_cli_sources_options_and_collisions(self):
        with mock.patch.object(compare, "verified_commit", return_value="a" * 40) as verify:
            args = compare.arguments(self.command())
            verify.assert_called_once_with("a" * 40)
            self.assertEqual(args.baseline_options, {"Threads": 1, "Hash": 16})
            self.rejects(self.command() + ["--baseline-commit", "a" * 40])
            for bad in ("[]", "null", "{broken", '{"Hash":[16]}'):
                self.rejects(self.command() + ["--baseline-options", bad])
            argv = self.command()
            argv[argv.index("--pgn") + 1] = str(self.source.with_suffix(".pgn"))
            self.rejects(argv)
            del argv[argv.index("--baseline-source"):argv.index("--baseline-source") + 2]
            self.rejects(argv)

    def test_external_uci_smoke(self):
        if REGICIDE is None:
            self.skipTest("pass a Regicide executable for subprocess and PGN checks")
        import chess
        import chess.pgn
        argv = self.command()
        argv[1] = str(REGICIDE)
        argv[argv.index("--candidate-commit") + 1] = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/compare_engines.py")] + argv,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        metadata = json.loads((self.directory / "match.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["errors"], [])
        baseline = metadata["engines"]["baseline"]
        self.assertFalse(baseline["source_commit_object_verified"])
        self.assertIsNone(baseline["provided_source_commit"])
        self.assertEqual(baseline["command"], [str(self.binary)] + self.manifest["args"])
        self.assertEqual(baseline["validated_options"], {"Threads": 1, "Hash": 16})
        self.assertTrue(baseline["configuration_note"])
        self.assertEqual(len(metadata["games"]), 2)
        with (self.directory / "match.pgn").open(encoding="utf-8") as stream:
            for record in metadata["games"]:
                self.assertEqual((record["result"], record["termination"]), ("*", "ply_cap"))
                self.assertEqual(record["plies"], 6)
                game = chess.pgn.read_game(stream)
                self.assertIsNotNone(game)
                self.assertFalse(game.errors)
                self.assertEqual(game.headers["Result"], "*")
                baseline_color = "White" if record["white"] == "baseline" else "Black"
                self.assertEqual(game.headers[baseline_color + "SourceCommit"], "external: see JSON")
                self.assertEqual(game.headers[baseline_color + "SourceURL"], self.manifest["source_url"])
                board = game.board()
                moves = list(game.mainline_moves())
                self.assertEqual([move.uci() for move in moves], record["uci_moves"])
                for move in moves:
                    self.assertIn(move, board.legal_moves)
                    board.push(move)
                self.assertEqual(board.fen(en_passant="fen"), record["final_fen"])
            self.assertIsNone(chess.pgn.read_game(stream))


if __name__ == "__main__":
    if sys.argv[1:] == ["--uci-fixture"]:
        uci_fixture()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("binary", nargs="?", type=Path)
        known, remaining = parser.parse_known_args()
        REGICIDE = known.binary.resolve() if known.binary else None
        unittest.main(argv=[sys.argv[0]] + remaining)
