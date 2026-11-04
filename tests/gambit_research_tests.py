"""Resumable native research contracts, using no external or native engine."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import chess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import train_gambit_operations as training
import gambit_research_session as research
from neural_search_data.engine import SearchInterrupted


class FakeTeacher:
    """Fresh fixed-node answers with real, flushed raw-log byte ranges."""
    calls = []
    instances = []
    interrupt_at = None
    interrupt_reason = "search_timeout"
    invalid = None
    enter_hook = None
    option_value = True
    exit_code = 0
    exit_observed = True

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.instances = []
        cls.interrupt_at = cls.invalid = cls.enter_hook = None
        cls.interrupt_reason = "search_timeout"
        cls.option_value = True
        cls.exit_code = 0
        cls.exit_observed = True

    def __init__(self, engine_path, raw_path, nodes, deadline, search_timeout=12):
        self.nodes = nodes
        self.raw_path = Path(raw_path)
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_path.touch()
        self.options = {"UsePieceActivity": self.option_value, "Hash": 16}
        self.runtime_defaults = {"UsePieceActivity": False, "Hash": 16}
        self.identity = {"name": "Deterministic own-engine test double"}
        self.closed = True
        self.cleanup_status = {"quit_requested": False, "transport_closed": False,
                               "returncode": None, "returncode_observed": False}
        self.seq = 0
        type(self).instances.append(self)

    def __enter__(self):
        self.closed = False
        hook = type(self).enter_hook
        type(self).enter_hook = None
        if hook:
            hook()
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True
        self.cleanup_status.update(quit_requested=True, transport_closed=True,
                                   returncode=type(self).exit_code, returncode_observed=type(self).exit_observed)

    def query(self, board, query_id, *, root_moves=None):
        cls = type(self)
        cls.calls.append((query_id, board.fen(en_passant="fen"), tuple(move.uci() for move in board.move_stack)))
        replay = board.copy(stack=True)
        pv = []
        for ply in range(4):
            if replay.outcome(claim_draw=False) is not None:
                break
            legal = root_moves if ply == 0 and root_moves is not None else replay.legal_moves
            move = min(legal, key=lambda item: item.uci())
            pv.append(move.uci())
            replay.push(move)
        begin = self.seq + 1
        with self.raw_path.open("a", encoding="utf-8", newline="\n") as stream:
            for direction, line in [("send", f"go nodes {self.nodes}"),
                                    ("recv", "bestmove " + pv[0])]:
                self.seq += 1
                stream.write(json.dumps({"seq": self.seq, "direction": direction, "line": line}) + "\n")
            stream.flush()
        result = {"query_id": query_id, "requested_nodes": self.nodes, "nodes": self.nodes,
            "depth": 4, "seldepth": 4, "engine_time_ms": 0, "wall_seconds": 0.0,
            "bestmove_uci": pv[0], "pv_uci": pv, "score": {"kind": "cp", "stm": 50},
            "lowerbound": False, "upperbound": False, "completed": True,
            "uci_begin_seq": begin, "uci_end_seq": self.seq}
        if root_moves is not None:
            result["root_moves_uci"] = sorted({move.uci() for move in root_moves})
        if cls.interrupt_at == len(cls.calls):
            result["completed"] = False
            if cls.interrupt_reason == "keyboard_interrupt":
                raise KeyboardInterrupt()
            raise SearchInterrupted(cls.interrupt_reason, result)
        if cls.invalid == "incomplete":
            result["completed"] = False
        elif cls.invalid == "bound":
            result["lowerbound"] = True
        elif cls.invalid == "nodes":
            result["nodes"] += 1
        elif cls.invalid == "pv":
            result["pv_uci"] = ["a1a8"]
        return result


def case(case_id, family, moves, split="train"):
    board = chess.Board()
    for uci in moves.split():
        board.push_uci(uci)
    return {"case_id": case_id, "family_id": family, "split": split,
            "owner": "white" if board.turn else "black", "moves": moves.split(),
            "fen": board.fen(en_passant="fen"), "goal_tags": ["development"],
            "book_status": {"has_continuation": False}}


def semantic(value):
    """Compare decisions/labels, excluding attempt-specific audit provenance."""
    if isinstance(value, dict):
        return {key: semantic(item) for key, item in value.items()
                if key not in {"raw_log", "wall_seconds", "engine_time_ms", "uci_begin_seq", "uci_end_seq"}}
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


class ResearchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="regicide-gambit-research-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.binary = self.folder / "own-engine.exe"
        self.binary.write_bytes(b"not executed: fixed test identity")
        self.curriculum = self.folder / "curriculum.json"
        self.rows = [case("evans-a", "evans", "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4"),
                     case("evans-b", "evans", "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4 c5b4 c2c3 b4a5"),
                     case("kings-a", "kings", "e2e4 e7e5 f2f4 e5f4"),
                     case("from-a", "from", "f2f4 e7e5 f4e5 d7d6 e5d6", "holdout")]
        self.document = {"schema": "regicide.gambit-operations-curriculum.v1", "cases": self.rows,
            "families": {row["family_id"]: {"family_id": row["family_id"], "split": row["split"]} for row in self.rows},
            "split_policy": {"holdout_families": ["from"], "validation_families": []}}
        self.curriculum.write_text(json.dumps(self.document), encoding="utf-8")
        FakeTeacher.reset()
        self.teacher_patch = patch.object(training, "RuntimeTeacher", FakeTeacher)
        self.teacher_patch.start()
        self.addCleanup(self.teacher_patch.stop)

    def run_research(self, name, **kwargs):
        arguments = {"curriculum": self.curriculum, "nodes": 100000, "owner_turns": 2,
                     "per_family": 1, "candidates": 1, "seed": 17, "wall_seconds": 60,
                     "families": ["evans"], "search_timeout": 12}
        arguments.update(kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            return training.run(self.binary, self.folder / name, **arguments)

    def read(self, name, filename):
        return json.loads((self.folder / name / filename).read_text(encoding="utf-8"))

    def test_pause_resume_matches_continuous_decisions_and_reuses_completed_queries(self):
        complete = self.run_research("continuous")
        self.assertEqual(complete["research_status"], "complete")
        expected_calls = len(FakeTeacher.calls)
        self.assertEqual(expected_calls, 4)  # two owner roots and their one selected child each
        FakeTeacher.reset()
        pause = self.run_research("resumed", max_new_queries=1)
        self.assertEqual((pause["research_status"], pause["reason"], pause["new_queries"]), ("paused", "query_budget", 1))
        self.assertEqual(len(FakeTeacher.calls), 1)
        resumed = self.run_research("resumed", resume=True)
        self.assertEqual(resumed["research_status"], "complete")
        self.assertEqual(len(FakeTeacher.calls), expected_calls)
        self.assertEqual(len({entry[0] for entry in FakeTeacher.calls}), expected_calls)
        for filename in ("records.json", "practice.json", "queries.json"):
            self.assertEqual(semantic(self.read("continuous", filename)), semantic(self.read("resumed", filename)), filename)
        self.assertEqual(complete["records"], resumed["records"])
        self.assertEqual(complete["counts"], resumed["counts"])
        self.assertTrue(all(item.closed for item in FakeTeacher.instances))

    def test_repeated_query_budget_pauses_finish_without_duplicate_queries(self):
        last = None
        for attempt in range(12):
            last = self.run_research("chunks", resume=attempt > 0, max_new_queries=1)
            if last["research_status"] == "complete":
                break
            self.assertEqual(last["new_queries"], 1)
        self.assertEqual(last["research_status"], "complete")
        self.assertEqual(len({entry[0] for entry in FakeTeacher.calls}), len(FakeTeacher.calls))

    def test_family_and_case_filters_keep_requested_roots_and_holdout_unexported(self):
        value = self.run_research("filtered", families=["evans", "from"], case_ids=["evans-b", "from-a"])
        selected = self.read("filtered", "predeclared.json")["cases"]
        self.assertEqual({row["case_id"] for row in selected}, {"evans-b", "from-a"})
        self.assertEqual({row["case_id"] for row in value["records"]}, {"evans-b"})
        self.assertIn("from", value["heldout_family_ids"])
        self.assertGreater(value["counts"]["heldout_practice_records"], 0)

    def test_holdout_only_research_completes_without_exporting_active_records(self):
        value = self.run_research("holdout-only", families=["from"])
        self.assertEqual(value["research_status"], "complete")
        self.assertEqual(value["status"], "experimental")
        self.assertEqual(value["records"], [])
        self.assertFalse(value["acceptance"]["passed"])
        self.assertGreater(value["counts"]["heldout_practice_records"], 0)

    def test_unknown_selection_is_rejected_before_teacher_query(self):
        for kwargs in ({"families": ["missing"]}, {"case_ids": ["missing"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_research("unknown-" + str(len(FakeTeacher.calls)), **kwargs)
        self.assertFalse(FakeTeacher.calls)

    def test_changed_config_and_binary_cannot_resume(self):
        self.run_research("strict", max_new_queries=1)
        calls = len(FakeTeacher.calls)
        for changed in ({"nodes": 100001}, {"seed": 18}, {"candidates": 2},
                        {"case_ids": ["evans-b"]}, {"owner_turns": 3}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.run_research("strict", resume=True, **changed)
        self.binary.write_bytes(b"different own-engine binary")
        with self.assertRaises(ValueError):
            self.run_research("strict", resume=True)
        self.assertEqual(len(FakeTeacher.calls), calls)

    def test_changed_curriculum_and_runtime_options_cannot_reuse_cache(self):
        self.run_research("curriculum", max_new_queries=1)
        changed = copy.deepcopy(self.document)
        changed["cases"][0]["goal_tags"] = ["different"]
        self.curriculum.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_research("curriculum", resume=True)
        self.curriculum.write_text(json.dumps(self.document), encoding="utf-8")
        FakeTeacher.option_value = False
        with self.assertRaises(ValueError):
            self.run_research("curriculum", resume=True)

    def test_changed_source_hash_cannot_resume_a_completed_query(self):
        self.run_research("source", max_new_queries=1)
        changed = training.source_hashes()
        changed[next(iter(changed))] = "0" * 64
        before = len(FakeTeacher.calls)
        with patch.object(training, "source_hashes", return_value=changed), self.assertRaises(ValueError):
            self.run_research("source", resume=True)
        self.assertEqual(len(FakeTeacher.calls), before)

    def test_existing_output_requires_resume(self):
        self.run_research("existing", max_new_queries=1)
        with self.assertRaises(FileExistsError):
            self.run_research("existing")

    def test_interrupt_is_paused_and_incomplete_answer_is_queried_again(self):
        for reason in ("search_timeout", "wall_budget", "keyboard_interrupt"):
            with self.subTest(reason=reason):
                FakeTeacher.reset()
                FakeTeacher.interrupt_at = 2
                FakeTeacher.interrupt_reason = reason
                paused = self.run_research(reason)
                self.assertEqual((paused["research_status"], paused["reason"]), ("paused", reason))
                interrupted_id = FakeTeacher.calls[-1][0]
                self.assertTrue(all(item.closed for item in FakeTeacher.instances))
                FakeTeacher.interrupt_at = None
                completed = self.run_research(reason, resume=True)
                self.assertEqual(completed["research_status"], "complete")
                self.assertEqual(sum(row[0] == interrupted_id for row in FakeTeacher.calls), 2)

    def test_default_active_memory_export_is_forbidden_and_original_bytes_preserved(self):
        active = training.memory.DEFAULT_MEMORY
        before = active.read_bytes()
        self.run_research("protected")
        with self.assertRaises(ValueError):
            training.compile_memory(self.folder / "protected", memory_path=active)
        self.assertEqual(active.read_bytes(), before)

    def test_cli_rejects_active_memory_export_before_starting_research(self):
        active = training.memory.DEFAULT_MEMORY
        before = active.read_bytes()
        with patch.object(training, "run", side_effect=AssertionError("Research must not start")) as run:
            with self.assertRaises(ValueError):
                training.main(["--binary", str(self.binary), "--output", str(self.folder / "cli-protected"),
                               "--memory-path", str(active)])
            run.assert_not_called()
        self.assertEqual(active.read_bytes(), before)

    def test_export_race_cannot_replace_a_concurrently_created_file(self):
        target = self.folder / "race-export.json"
        competing_bytes = b'{"owner":"another completed export"}\n'
        def competing_writer(source, destination):
            Path(destination).write_bytes(competing_bytes)
            raise FileExistsError("Concurrent publication already created destination")
        with patch.object(training.os, "link", side_effect=competing_writer) as link:
            with self.assertRaises(FileExistsError):
                training._write_new_export(target, {"owner": "this export"})
            link.assert_called_once()
        self.assertEqual(target.read_bytes(), competing_bytes)
        self.assertEqual(list(self.folder.glob(".gambit-export-*.tmp")), [])

    def test_run_external_export_holds_the_session_lock_until_publication(self):
        output = self.folder / "export-locked"
        target = self.folder / "external-memory.json"
        publish = training._write_new_export
        checked = []
        def locked_publish(path, artifact):
            config = json.loads((output / "session.json").read_text(encoding="utf-8"))["configuration"]
            with self.assertRaisesRegex(RuntimeError, "lock"):
                with research.ResearchSession(output, config, resume=True):
                    self.fail("Export released the session lock before publication")
            checked.append(True)
            publish(path, artifact)
        with patch.object(training, "_write_new_export", side_effect=locked_publish):
            value = self.run_research("export-locked", memory_path=target)
        self.assertEqual(value["research_status"], "complete")
        self.assertEqual(checked, [True])
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["memory_id"], value["memory_id"])

    def test_direct_compile_external_export_also_holds_the_session_lock(self):
        output = self.folder / "compile-locked"
        self.run_research("compile-locked")
        target = self.folder / "compiled-memory.json"
        publish = training._write_new_export
        checked = []
        def locked_publish(path, artifact):
            config = json.loads((output / "session.json").read_text(encoding="utf-8"))["configuration"]
            with self.assertRaisesRegex(RuntimeError, "lock"):
                with research.ResearchSession(output, config, resume=True):
                    self.fail("Direct compiler published outside the session lock")
            checked.append(True)
            publish(path, artifact)
        with patch.object(training, "_write_new_export", side_effect=locked_publish):
            training.compile_memory(output, memory_path=target)
        self.assertEqual(checked, [True])
        self.assertTrue(target.is_file())

    def test_unknown_or_nonzero_native_exit_cannot_export_completed_memory(self):
        for observed, code in ((False, None), (True, 7)):
            with self.subTest(observed=observed, code=code):
                FakeTeacher.reset()
                FakeTeacher.exit_observed, FakeTeacher.exit_code = observed, code
                name = "bad-exit-" + str(code)
                target = self.folder / (name + "-export.json")
                with self.assertRaises(ValueError):
                    self.run_research(name, memory_path=target)
                self.assertFalse(target.exists())
                self.assertFalse((self.folder / name / "operations_memory.json").exists())
                # Even if a caller supplies a misleading closed=True flag, the
                # public compiler still requires an observed successful exit.
                manifest = self.read(name, "run.json")
                manifest["all_native_processes_closed"] = True
                (self.folder / name / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    training.compile_memory(self.folder / name, memory_path=target)
                self.assertFalse(target.exists())

    def test_bad_completed_answer_is_not_cached_or_reused(self):
        for invalid in ("incomplete", "bound", "nodes", "pv"):
            with self.subTest(invalid=invalid):
                FakeTeacher.reset()
                FakeTeacher.invalid = invalid
                try:
                    result = self.run_research("bad-" + invalid, max_new_queries=1)
                except ValueError:
                    pass
                else:
                    self.assertNotEqual(result.get("research_status"), "complete")
                first_id = FakeTeacher.calls[0][0]
                FakeTeacher.invalid = None
                completed = self.run_research("bad-" + invalid, resume=True)
                self.assertEqual(completed["research_status"], "complete")
                self.assertEqual(sum(row[0] == first_id for row in FakeTeacher.calls), 2)

    def test_corrupt_completed_cache_and_raw_log_are_rejected(self):
        for kind in ("cache", "raw"):
            with self.subTest(kind=kind):
                name = "corrupt-" + kind
                self.run_research(name, max_new_queries=1)
                before = len(FakeTeacher.calls)
                if kind == "cache":
                    files = list((self.folder / name / "completed-queries").glob("*.json"))
                    self.assertEqual(len(files), 1)
                    files[0].write_text("{", encoding="utf-8")
                else:
                    raw = FakeTeacher.instances[-1].raw_path
                    raw.write_bytes(raw.read_bytes().replace(b"bestmove", b"bad-move"))
                with self.assertRaises(ValueError):
                    self.run_research(name, resume=True)
                self.assertEqual(len(FakeTeacher.calls), before)

    def test_valid_checksum_cannot_hide_a_changed_full_history(self):
        self.run_research("history", max_new_queries=1)
        cached = next((self.folder / "history/completed-queries").glob("*.json"))
        envelope = json.loads(cached.read_text(encoding="utf-8"))
        envelope["request"]["history_uci"] = []
        payload = {key: value for key, value in envelope.items() if key != "checksum"}
        envelope["checksum"] = research._digest(payload)
        cached.write_text(json.dumps(envelope), encoding="utf-8")
        before = len(FakeTeacher.calls)
        with self.assertRaisesRegex(ValueError, "position|history|request"):
            self.run_research("history", resume=True)
        self.assertEqual(len(FakeTeacher.calls), before)

    def test_active_output_lock_rejects_a_second_writer(self):
        observed = []
        def second_writer():
            try:
                self.run_research("locked", resume=True)
            except (ValueError, RuntimeError, OSError) as error:
                observed.append(str(error))
            else:
                self.fail("Concurrent writer entered the same research output")
        FakeTeacher.enter_hook = second_writer
        result = self.run_research("locked", max_new_queries=1)
        self.assertEqual(result["research_status"], "paused")
        self.assertEqual(len(observed), 1)
        self.assertRegex(observed[0].lower(), "lock|busy|running|writer")

    def test_list_lines_needs_no_engine_and_supports_repeated_family_case_flags(self):
        output = io.StringIO()
        argv = ["train_gambit_operations.py", "--list-lines", "--curriculum", str(self.curriculum),
                "--family", "evans", "--family", "from", "--case", "evans-b", "--case", "from-a"]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            training.main()
        self.assertIn("evans-b", output.getvalue())
        self.assertIn("from-a", output.getvalue())
        self.assertFalse(FakeTeacher.calls)
        self.assertFalse(FakeTeacher.instances)


if __name__ == "__main__":
    unittest.main()
