"""Persistent human-game library, replay, migration and read-only HTTP checks.

All games and migrations use temporary folders, never the user's game library.
python-chess independently verifies saved PGN and every replay frame.
Usage: python tests/game_archive_tests.py [path/to/regicide]
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import chess
import chess.pgn

import human_play_tests as baseline

from game_archive import GameArchive


backend = baseline.play_human
REAL_BINARY = None


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="regicide-archive-tests-")
        self.root = Path(self.temporary.name)
        self.binary = self.root / "test-peer.exe"
        self.binary.write_bytes(b"fake peer; not executed")
        self.factory = baseline.FakeFactory()
        self.folder = self.root / "games"
        self.game = backend.HumanGame(self.binary, self.folder, self.factory)
        self.archive = GameArchive(self.folder)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.game.close)

    def revision(self):
        return self.game.state()["revision"]

    def human(self, uci):
        return self.game.human_move(uci, self.revision())

    def engine(self, uci):
        self.factory.instances[-1].replies.append(uci)
        return self.game.engine_move(self.revision())

    def opening(self):
        for human, engine in (("e2e4", "e7e5"), ("g1f3", "b8c6")):
            self.human(human)
            self.engine(engine)

    def record(self, game_id=None):
        return json.loads((self.folder / ((game_id or self.game.game_id) + ".json")).read_text(encoding="utf-8"))

    def summary(self, game_id=None):
        return self.archive.get_game(game_id or self.game.game_id)["summary"]

    def assert_replay(self, details):
        parsed, pgn_board = baseline.read_pgn(details["pgn"])
        moves = list(parsed.mainline_moves())
        self.assertEqual(len(details["frames"]), len(moves) + 1)
        self.assertEqual(details["summary"]["ply_count"], len(moves))
        board = parsed.board()
        for ply, frame in enumerate(details["frames"]):
            with self.subTest(ply=ply):
                self.assertEqual(frame["ply"], ply)
                self.assertEqual(frame["board"], {chess.square_name(square): piece.symbol()
                                                for square, piece in board.piece_map().items()})
                self.assertEqual(frame["turn"], "white" if board.turn else "black")
                self.assertEqual(frame["in_check"], board.is_check())
                self.assertEqual(frame["last_move"], moves[ply - 1].uci() if ply else None)
                self.assertFalse(frame.get("human_to_move", False))
                self.assertFalse(frame.get("engine_to_move", False))
                self.assertEqual(frame.get("legal_moves", []), [])
                if ply < len(moves):
                    self.assertIn(moves[ply], board.legal_moves)
                    self.assertEqual(details["moves"][ply]["san"], board.san(moves[ply]))
                    self.assertEqual(details["moves"][ply]["uci"], moves[ply].uci())
                    board.push(moves[ply])
        self.assertEqual(board.fen(), pgn_board.fen())

    def test_accumulates_across_new_games_and_backend_restarts(self):
        self.opening()
        first = self.game.game_id
        self.game.new_game("black", 100, self.revision())
        self.engine("d2d4")
        second = self.game.game_id
        self.game.close()
        resumed = backend.HumanGame(self.binary, self.folder, self.factory)
        self.addCleanup(resumed.close)
        third = resumed.game_id
        resumed.human_move("e2e4", resumed.state()["revision"])
        listed = self.archive.list_games(third)
        by_id = {entry["game_id"]: entry for entry in listed["games"]}
        self.assertTrue({first, second, third}.issubset(by_id))
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(by_id[first]["record_status"], "unfinished")
        self.assertEqual(by_id[second]["record_status"], "unfinished")
        self.assertEqual(by_id[first]["human_color"], "white")
        self.assertEqual(by_id[second]["human_color"], "black")
        self.assertTrue(by_id[third]["current"])
        self.assertFalse(by_id[first]["current"])
        self.assertEqual(by_id[third]["record_status"], "in_progress")
        self.assertEqual(by_id[first]["ply_count"], 4)
        self.assertEqual(by_id[second]["ply_count"], 1)
        self.assert_replay(self.archive.get_game(first))
        self.assert_replay(self.archive.get_game(second))

    def test_replay_and_engine_provenance_survive_without_pgn_sidecar(self):
        self.opening()
        before = self.game.state()
        (self.folder / (self.game.game_id + ".pgn")).unlink()
        details = self.archive.get_game(self.game.game_id, self.game.game_id)
        self.assert_replay(details)
        self.assertEqual(self.game.state(), before)
        parsed, _ = baseline.read_pgn(details["pgn"])
        self.assertEqual(parsed.headers["EngineSHA256"], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual(details["summary"]["engine_name"], self.game.engine_name)
        self.assertEqual(details["summary"]["movetime_ms"], 1000)
        self.assertEqual(parsed.headers["Result"], "*")
        self.assertEqual(self.archive.get_pgn(self.game.game_id), details["pgn"])

    def test_completed_result_and_end_timestamp_remain_stable_on_close(self):
        self.opening()
        self.game.resign(self.revision())
        before = self.record()
        self.assertEqual(before["record_status"], "completed")
        self.assertTrue(before["ended_at"])
        self.assertTrue(before["updated_at"])
        self.game.close()
        self.game.close()
        after = self.record()
        self.assertEqual(after["ended_at"], before["ended_at"])
        self.assertEqual(after["record_status"], "completed")
        self.assertEqual(self.summary()["result"], "0-1")
        self.assertEqual(self.summary()["termination"], "resignation")
        self.assert_replay(self.archive.get_game(self.game.game_id))

    def test_terminal_checkmate_and_draw_replay_without_extra_engine_move(self):
        positions = (("7k/5K2/6Q1/8/8/8/8/8 w - - 0 1", "g6g7", "1-0", "checkmate"),
                     ("7k/8/8/8/8/8/8/4K2N w - - 0 1", None, "1/2-1/2", "insufficient_material"))
        for fen, uci, result, termination in positions:
            with self.subTest(termination=termination):
                self.game.new_game("white", 100, self.revision())
                self.game.board = chess.Board(fen)
                if uci:
                    self.human(uci)
                else:
                    self.game._save()
                details = self.archive.get_game(self.game.game_id)
                self.assertEqual(details["summary"]["result"], result)
                self.assertEqual(details["summary"]["record_status"], "completed")
                self.assertEqual(details["summary"]["termination"], termination)
                self.assert_replay(details)

    def test_finished_game_reopened_by_undo_clears_end_timestamp(self):
        self.opening()
        self.game.resign(self.revision())
        self.assertTrue(self.record()["ended_at"])
        self.game.undo(self.revision())
        record = self.record()
        self.assertEqual(record["record_status"], "in_progress")
        self.assertIsNone(record["ended_at"])
        self.assertEqual(self.summary()["result"], "*")
        self.assert_replay(self.archive.get_game(self.game.game_id))

    def test_engine_error_is_archived_without_fabricating_a_result(self):
        self.human("e2e4")
        self.factory.instances[-1].replies.append(chess.engine.EngineTerminatedError("test peer stopped"))
        self.game.engine_move(self.revision())
        self.game.close()
        summary = self.summary()
        self.assertEqual(summary["record_status"], "error")
        self.assertEqual(summary["result"], "*")
        self.assertEqual(summary["ply_count"], 1)
        self.assert_replay(self.archive.get_game(self.game.game_id))

    def test_undo_updates_current_record_without_making_another_game(self):
        self.opening()
        before_ids = set(path.stem for path in self.folder.glob("*.json"))
        self.game.undo(self.revision())
        after_ids = set(path.stem for path in self.folder.glob("*.json"))
        self.assertEqual(before_ids, after_ids)
        details = self.archive.get_game(self.game.game_id)
        self.assertEqual([move["uci"] for move in details["moves"]], ["e2e4", "e7e5"])
        self.assert_replay(details)

    def test_handicap_preview_preserves_original_and_forced_move_is_attributed(self):
        self.opening()
        original = (self.folder / (self.game.game_id + ".json")).read_bytes()
        self.game.begin_handicap(2, self.revision())
        self.assertEqual((self.folder / (self.game.game_id + ".json")).read_bytes(), original)
        self.assertEqual(self.summary()["ply_count"], 4)
        self.game.forced_move("f7f6", self.revision())
        details = self.archive.get_game(self.game.game_id)
        self.assertEqual([move["uci"] for move in details["moves"]], ["e2e4", "f7f6"])
        self.assertEqual(details["moves"][1]["actor"], "engine")
        self.assertTrue(details["moves"][1]["forced"])
        self.assertEqual(details["summary"]["handicap_moves"], 1)
        self.assertIn("User-forced Regicide move", details["pgn"])
        self.assert_replay(details)

    def test_close_during_handicap_preserves_original_full_history(self):
        self.opening()
        self.game.begin_handicap(2, self.revision())
        self.game.close()
        details = self.archive.get_game(self.game.game_id)
        self.assertEqual([move["uci"] for move in details["moves"]],
                         ["e2e4", "e7e5", "g1f3", "b8c6"])
        self.assertEqual(details["summary"]["record_status"], "unfinished")
        self.assert_replay(details)

    def test_old_valid_json_remains_readable_without_new_metadata(self):
        self.opening()
        record = self.record()
        for key in ("record_status", "updated_at", "ended_at"):
            record.pop(key, None)
        (self.folder / (self.game.game_id + ".json")).write_text(json.dumps(record), encoding="utf-8")
        details = self.archive.get_game(self.game.game_id)
        self.assertEqual(details["summary"]["result"], "*")
        self.assert_replay(details)

    def test_offset_timestamp_is_normalized_to_utc_for_the_viewer(self):
        self.opening()
        record = self.record()
        record["started_at"] = "2026-10-18T00:30:00+09:00"
        (self.folder / (self.game.game_id + ".json")).write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.summary()["started_at"], "2026-10-17T15:30:00+00:00")

    def test_bad_files_do_not_hide_other_games(self):
        self.opening()
        (self.folder / "corrupt.json").write_text("{bad JSON", encoding="utf-8")
        illegal = self.record()
        illegal["game_id"] = "illegal"
        illegal["uci_moves"] = ["e2e5"]
        (self.folder / "illegal.json").write_text(json.dumps(illegal), encoding="utf-8")
        oversized = self.folder / "oversized.json"
        oversized.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        result = self.archive.list_games()
        self.assertEqual(result["total"], 1)
        self.assertGreaterEqual(result["invalid_count"], 3)
        self.assertEqual(result["games"][0]["game_id"], self.game.game_id)
        self.assert_replay(self.archive.get_game(self.game.game_id))

    def test_invalid_results_initial_positions_and_excessive_history_are_rejected(self):
        self.opening()
        for field, value in (("uci_moves", ["g1f3"] * 10_001),
                             ("initial_fen", "8/8/8/8/8/8/8/8 w - - 0 1"),
                             ("game_id", "mismatched")):
            with self.subTest(field=field):
                record = self.record()
                record[field] = value
                (self.folder / (self.game.game_id + ".json")).write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.archive.get_game(self.game.game_id)
                self.game._save()
        record = self.record()
        record["state"]["result"] = "1-0"
        record["state"]["termination"] = None
        (self.folder / (self.game.game_id + ".json")).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.archive.get_game(self.game.game_id)

    def test_unsafe_ids_cannot_access_files_outside_library(self):
        outside = self.root / "secret.json"
        outside.write_text('{"secret":"not a game"}', encoding="utf-8")
        for unsafe in ("../secret", "..", "/secret", "a/b", "a\\b", "a%2fb", "", "a" * 101):
            with self.subTest(game_id=unsafe):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.archive.get_game(unsafe)
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"secret":"not a game"}')

    def test_symlinked_record_cannot_escape_library(self):
        outside = self.root / "outside.json"
        record = self.record()
        record["game_id"] = "linked"
        outside.write_text(json.dumps(record), encoding="utf-8")
        linked = self.folder / "linked.json"
        try:
            linked.symlink_to(outside)
        except OSError:
            self.skipTest("This OS account cannot create file symlinks")
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.archive.get_game("linked")
        self.assertEqual(self.archive.list_games()["total"], 1)

    def test_migration_is_idempotent_and_preserves_source_bytes(self):
        self.opening()
        self.game.close()
        legacy = self.folder
        source = {path.name: path.read_bytes() for path in legacy.iterdir() if path.is_file()}
        target = GameArchive(self.root / "permanent")
        first = target.import_legacy([legacy])
        second = target.import_legacy([legacy])
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertGreaterEqual(second["skipped"], 1)
        self.assertEqual(target.list_games()["total"], 1)
        self.assertEqual(source, {path.name: path.read_bytes() for path in legacy.iterdir() if path.is_file()})
        imported_id = target.list_games()["games"][0]["game_id"]
        self.assert_replay(target.get_game(imported_id))
        pgn_path = self.root / "permanent" / (imported_id + ".pgn")
        self.assertTrue(pgn_path.is_file())
        imported, _ = baseline.read_pgn(pgn_path.read_text(encoding="utf-8"))
        self.assertEqual([move.uci() for move in imported.mainline_moves()],
                         ["e2e4", "e7e5", "g1f3", "b8c6"])

    def test_migration_deduplicates_copies_but_keeps_conflicting_histories(self):
        self.opening()
        self.game.close()
        second_source = self.root / "legacy-copy"
        shutil.copytree(self.folder, second_source)
        archive = GameArchive(self.root / "permanent")
        archive.import_legacy([self.folder, second_source])
        self.assertEqual(archive.list_games()["total"], 1)
        original_id = archive.list_games()["games"][0]["game_id"]
        original_path = self.root / "permanent" / (original_id + ".json")
        original = original_path.read_bytes()
        conflict = self.record()
        conflict["uci_moves"] = ["d2d4", "d7d5"]
        replay = chess.Board()
        entries = []
        for ply, uci in enumerate(conflict["uci_moves"], 1):
            move = chess.Move.from_uci(uci)
            entries.append({"ply": ply, "uci": uci, "san": replay.san(move),
                            "actor": "human" if replay.turn else "engine"})
            replay.push(move)
        conflict["final_fen"] = replay.fen(en_passant="fen")
        conflict["state"]["moves"] = entries
        conflict["state"]["last_move"] = "d7d5"
        conflict["state"]["board"] = {chess.square_name(square): piece.symbol()
                                           for square, piece in replay.piece_map().items()}
        (second_source / (self.game.game_id + ".json")).write_text(json.dumps(conflict), encoding="utf-8")
        result = archive.import_legacy([second_source])
        self.assertEqual(result["imported"], 1)
        self.assertGreaterEqual(result["conflicts"], 1)
        self.assertEqual(archive.list_games()["total"], 2)
        self.assertEqual(original_path.read_bytes(), original)
        archive.import_legacy([second_source])
        self.assertEqual(archive.list_games()["total"], 2)


class ArchiveHTTPTests(baseline.HumanHTTPTests):
    # Reuse the existing authenticated host fixture without copying baseline tests.
    def test_archive_reads_require_token_and_allowed_origin(self):
        self.game.human_move("e2e4", self.game.state()["revision"])
        paths = ("/api/games", f"/api/games/{self.game.game_id}",
                 f"/api/games/{self.game.game_id}/pgn")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token=False)[0], 403)
                self.assertEqual(self.request(path, headers={"Origin": "https://attacker.invalid"})[0], 403)
                self.assertEqual(self.request(path)[0], 200)

    def test_browsing_is_read_only_and_validates_paths(self):
        self.game.human_move("e2e4", self.game.state()["revision"])
        before = self.game.state()
        calls = len(self.factory.instances[-1].calls)
        saved = {path.name: path.read_bytes() for path in self.game.save_dir.iterdir() if path.is_file()}
        code, _, payload = self.request("/api/games")
        self.assertEqual(code, 200)
        listed = json.loads(payload)
        self.assertIn(self.game.game_id, [entry["game_id"] for entry in listed["games"]])
        code, _, payload = self.request(f"/api/games/{self.game.game_id}")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(payload)["summary"]["ply_count"], 1)
        code, _, payload = self.request(f"/api/games/{self.game.game_id}/pgn")
        self.assertEqual(code, 200)
        baseline.read_pgn(payload.decode("utf-8"))
        for path in ("/api/games/../secret", "/api/games/%2e%2e%2fsecret", "/api/games/unknown",
                     "/api/games/a%5cb", "/api/games/a/b", "/api/games/unknown/pgn"):
            self.assertIn(self.request(path)[0], (400, 404))
        self.assertEqual(self.game.state(), before)
        self.assertEqual(len(self.factory.instances[-1].calls), calls)
        self.assertEqual({path.name: path.read_bytes() for path in self.game.save_dir.iterdir() if path.is_file()}, saved)


# Avoid rerunning inherited baseline methods when this file is called directly.
for _name in tuple(dir(ArchiveHTTPTests)):
    if _name.startswith("test_") and _name not in ArchiveHTTPTests.__dict__:
        setattr(ArchiveHTTPTests, _name, None)


class RealArchiveTests(unittest.TestCase):
    def test_actual_regicide_library_replays_both_colors_after_restart(self):
        if REAL_BINARY is None:
            self.skipTest("Supply the Regicide binary for real-engine archive validation")
        with tempfile.TemporaryDirectory(prefix="regicide-real-archive-") as folder:
            game = backend.HumanGame(REAL_BINARY, Path(folder))
            try:
                game.new_game("white", 100, game.state()["revision"])
                game.human_move("e2e4", game.state()["revision"])
                game.engine_move(game.state()["revision"])
                first = game.game_id
                game.new_game("black", 100, game.state()["revision"])
                game.engine_move(game.state()["revision"])
                second = game.game_id
                game.close()
                archive = GameArchive(folder)
                for game_id, count in ((first, 2), (second, 1)):
                    details = archive.get_game(game_id)
                    parsed, _ = baseline.read_pgn(details["pgn"])
                    self.assertEqual(len(list(parsed.mainline_moves())), count)
                    self.assertEqual(details["summary"]["record_status"], "unfinished")
                    self.assertTrue(details["summary"]["engine_name"].startswith("Regicide"))
            finally:
                game.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", nargs="?", type=Path)
    args, remaining = parser.parse_known_args()
    REAL_BINARY = args.binary.resolve(strict=True) if args.binary else None
    unittest.main(argv=[__file__, *remaining], verbosity=2)
