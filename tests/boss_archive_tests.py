"""Boss-mode provenance and per-position replay, using isolated game files."""

import json
from pathlib import Path
import tempfile
import unittest

import chess

import human_play_tests as baseline
import boss_play
from game_archive import GameArchive


class BossArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="regicide-boss-archive-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        binary = self.root / "fake.exe"
        binary.write_bytes(b"test peer, not executed")
        self.factory = baseline.FakeFactory()
        self.game = baseline.play_human.HumanGame(binary, self.root / "games", self.factory)
        self.addCleanup(self.game.close)
        self.game.human_move("d2d4", self.game.revision)
        self.factory.instances[-1].replies.append("e7e5")
        self.game.engine_move(self.game.revision)
        self.path = self.root / "games" / (self.game.game_id + ".json")
        self.record = json.loads(self.path.read_text(encoding="utf-8"))
        self.archive = GameArchive(self.path.parent)

    def save(self, metadata):
        self.record["boss_play"] = metadata
        self.path.write_text(json.dumps(self.record), encoding="utf-8")

    def metadata(self):
        return {"enabled": True, "policy": boss_play.POLICY, "seed": "a" * 32,
                "decisions": [{"ply": 2, "line_id": "englund", "line_complete": True,
                               "candidate_uci": "e7e5", "selected_uci": "e7e5",
                               "history_uci": ["d2d4"]}]}

    def test_replay_uses_past_decisions_not_final_saved_phase(self):
        self.save(self.metadata())
        before = self.path.read_bytes()
        result = self.archive.get_game(self.game.game_id)
        self.assertTrue(result["summary"]["boss_mode"])
        self.assertEqual(result["frames"][0]["engine"]["boss_phase"], "opening")
        self.assertEqual(result["frames"][1]["engine"]["boss_phase"], "opening")
        self.assertNotEqual(result["frames"][2]["engine"]["boss_phase"], "opening")
        parsed, _ = baseline.read_pgn(result["pgn"])
        self.assertEqual(parsed.headers["BossMode"], "1")
        self.assertEqual(parsed.headers["BossPolicy"], boss_play.POLICY)
        self.assertEqual(self.path.read_bytes(), before)

    def test_untrusted_or_disabled_mode_does_not_change_old_game(self):
        invalid = [None, [], {"enabled": "true", "policy": boss_play.POLICY, "seed": "a" * 32},
                   {"enabled": True, "policy": "unknown", "seed": "a" * 32},
                   {"enabled": True, "policy": boss_play.POLICY, "seed": '\"\\n[Result \"1-0\"]'}]
        for value in invalid:
            with self.subTest(value=value):
                self.save(value)
                result = self.archive.get_game(self.game.game_id)
                self.assertFalse(result["summary"]["boss_mode"])
                self.assertTrue(all(not frame["engine"]["boss_mode"] for frame in result["frames"]))
                parsed, _ = baseline.read_pgn(result["pgn"])
                self.assertNotIn("BossPolicy", parsed.headers)
                self.assertEqual(parsed.headers["Result"], "*")

    def test_legacy_v3_mode_and_policy_are_preserved_without_rewriting_the_game(self):
        metadata = self.metadata()
        metadata['policy'] = 'regicide-boss-play-v3'
        self.save(metadata)
        original = self.path.read_bytes()
        result = self.archive.get_game(self.game.game_id)
        self.assertTrue(result['summary']['boss_mode'])
        self.assertTrue(all(frame['engine']['boss_mode'] for frame in result['frames']))
        parsed, _ = baseline.read_pgn(result['pgn'])
        self.assertEqual(parsed.headers['BossPolicy'], 'regicide-boss-play-v3')
        self.assertEqual(self.path.read_bytes(), original)

    def test_decisions_must_match_actual_engine_move_and_history(self):
        metadata = self.metadata()
        valid = metadata["decisions"][0]
        metadata["decisions"] += [dict(valid, ply=99), dict(valid, ply=True),
                                  dict(valid, history_uci=["e2e4"]),
                                  dict(valid, selected_uci="d7d5"), "malformed"]
        self.save(metadata)
        prepared = self.archive._load(self.game.game_id)
        self.assertEqual(prepared["boss_decisions"], [valid])

    def test_missing_mode_metadata_remains_normal(self):
        self.record.pop("boss_play", None)
        self.path.write_text(json.dumps(self.record), encoding="utf-8")
        result = self.archive.get_game(self.game.game_id)
        self.assertFalse(result["summary"]["boss_mode"])
        self.assertTrue(all(not frame["engine"]["boss_mode"] for frame in result["frames"]))

    def test_opponent_profile_is_replayed_without_future_observation_leak(self):
        metadata = self.metadata()
        after_human = chess.Board()
        after_human.push_uci('d2d4')
        metadata['decisions'][0].update(
            before_fen=after_human.fen(en_passant='fen'),
            opponent_observation={
                'valid': True, 'forced': False, 'history_before': [], 'ply': 1,
                'played_uci': 'd2d4', 'before_fen': chess.STARTING_FEN,
                'loss_cp': 200, 'depth': 4})
        metadata['opponent_profile'] = {'frequency': 1, 'quality_observations': 999}
        self.save(metadata)
        frames = self.archive.get_game(self.game.game_id)['frames']
        self.assertEqual([f['engine']['boss_opponent']['quality_observations'] for f in frames], [0, 0, 1])
        self.assertAlmostEqual(frames[0]['engine']['boss_opponent']['frequency'], .1)
        self.assertGreater(frames[2]['engine']['boss_opponent']['frequency'], .1)
        self.assertLess(frames[2]['engine']['boss_opponent']['frequency'], 1)

    def test_short_search_and_later_setting_have_distinct_replay_phases(self):
        self.record["state"]["engine"]["movetime_ms"] = 1000
        self.record["engine_movetimes_ms"] = {"2": 100}
        self.record["time_settings_events"] = [{"after_ply": 1, "from_ms": 100,
                                                "to_ms": 1000, "active_search_ms": 100}]
        self.save(self.metadata())
        frames = self.archive.get_game(self.game.game_id)["frames"]
        self.assertEqual(frames[0]["engine"]["movetime_ms"], 100)
        self.assertEqual(frames[0]["engine"]["boss_phase"], "full_strength")
        self.assertEqual(frames[1]["engine"]["movetime_ms"], 100)
        self.assertEqual(frames[1]["engine"]["boss_phase"], "full_strength")
        self.assertEqual(frames[2]["engine"]["movetime_ms"], 1000)
        self.assertEqual(frames[2]["engine"]["boss_phase"], "trap_hunt")


if __name__ == "__main__":
    unittest.main()
