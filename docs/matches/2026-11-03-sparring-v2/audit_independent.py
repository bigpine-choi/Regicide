"""Read-only audit of finalized v2 diagnostic records; no engine is started."""
import argparse
from collections import Counter
from contextlib import closing
import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.pgn
import sparring_pacing
import sparring_rating
from game_archive import GameArchive, MAX_RECORD_BYTES

BASE = Path(__file__).resolve().parent
COMMIT = "31715bfa163bd655fc8192f4d7b7ede4cec0b783"
SEED = "sparring-v2-validation-2026-11-03-holdout"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value):
    return type(value) in (float, int) and math.isfinite(value)


def line(board, value, move=None):
    if not isinstance(value, dict) or type(value.get("depth")) is not int or value["depth"] < 1:
        return None
    pv = value.get("pv_uci")
    if not isinstance(pv, list) or not pv or pv[0] != value.get("uci") or move and pv[0] != move:
        return None
    replay = board.copy(stack=True)
    try:
        for uci in pv:
            replay.push_uci(uci)
    except (ValueError, TypeError):
        return None
    if value.get("pov") not in (None, "white" if board.turn else "black"):
        return None
    cp, mate = value.get("cp"), value.get("mate")
    sign = 1 if board.turn else -1
    if number(cp):
        if value.get("cp_white") != cp * sign or replay.is_checkmate():
            return None
        return {"kind": "cp", "cp": cp, "cp_white": cp * sign, "depth": value["depth"]}
    if type(mate) is int and mate != 0 and value.get("mate_white") == mate * sign:
        winner = board.turn if mate > 0 else not board.turn
        if (replay.is_checkmate() and replay.turn != winner
                and len(pv) == 2 * abs(mate) - (1 if mate > 0 else 0)):
            return {"kind": "mate", "mate": mate, "depth": value["depth"]}
    return None


def parse_pgn(path):
    stream = io.StringIO(Path(path).read_text(encoding="utf-8"))
    games = []
    while (game := chess.pgn.read_game(stream)) is not None:
        assert not game.errors, (path, game.errors)
        games.append(game)
    return games


def audit_game(folder, match, summary):
    record = json.loads((folder / summary["record_file"]).read_text(encoding="utf-8"))
    assert all(record[key] == value for key, value in summary.items())
    game_id = record["game"]
    own_white = record["sparring_color"] == "white"
    assert record["stockfish_color"] == ("black" if own_white else "white")
    expected_seed = hashlib.sha256(json.dumps([SEED, "sparring-vs-limited-stockfish", record["stockfish_elo"]]).encode()).hexdigest()[:32]
    assert record["sparring_seed"] == expected_seed
    assert record["initial_fen"] == chess.STARTING_FEN
    pgns = parse_pgn(folder / record["pgn_file"])
    assert len(pgns) == 1
    pgn = pgns[0]
    assert [m.uci() for m in pgn.mainline_moves()] == record["uci_moves"]
    for key, value in {"SparringMode": "1", "SparringPolicy": "regicide-sparring-play-v2",
                       "SparringSeed": expected_seed, "StockfishUCIElo": str(record["stockfish_elo"]),
                       "SparringMoveTimeMS": "1000", "StockfishMoveTimeMS": "1000",
                       "InterfaceSourceCommit": COMMIT, "EngineSHA256": match["binary_sha256"]["regicide"],
                       "Result": record["result"], "Round": str(game_id)}.items():
        assert pgn.headers.get(key) == value, (game_id, key, pgn.headers.get(key), value)
    assert ("Sparring" in pgn.headers["White"]) == own_white
    trace = [json.loads(row) for row in (folder / f"game-{game_id:03d}.moves.jsonl").read_text(encoding="utf-8").splitlines()]
    assert trace == record["moves"]
    host = record["host"]
    assert host["sparring_play"]["enabled"] is True
    assert host["sparring_play"]["seed"] == expected_seed
    assert all(host[key]["enabled"] is False for key in ("boss_play", "dynamic_play", "opening_variety"))
    assert host["model_sha256"] is None
    assert all(e["configured_options"].get("Hash") == 16 and e["configured_options"].get("UsePieceActivity") is True for e in host["engines"])
    options = record["stockfish_engine"]["configured_options"]
    assert options["Threads"] == 1 and options["Hash"] == 16
    assert options["UCI_LimitStrength"] is True and options["UCI_Elo"] == record["stockfish_elo"]
    side_path = folder / host["sidecar_file"]
    assert sha(side_path) == host["sidecar_sha256"]
    side = json.loads(side_path.read_text(encoding="utf-8"))
    assert side["uci_moves"] == record["uci_moves"] and side["sparring_play"] == host["sparring_play"]
    assert side["state"]["engine"]["sparring_mode"] is True
    assert side["purpose"] == "Human practice; undo and user-forced handicaps permitted, not a strength measurement"
    assert side["handicap_events"] == []
    assert side["state"]["engine"]["sparring_rating"]["label"] is None
    rating_meta = side["sparring_rating"]
    assert rating_meta["calibration"]["calibration_games"] == 0 and rating_meta["calibration"]["offset"] == 0
    assert "user_reported" not in side_path.read_text(encoding="utf-8")
    board = chess.Board()
    phases, reasons = Counter(), Counter()
    decisions, observations, alternatives, exceptions, paced, guards, cautions, notes = [], [], [], [], [], [], [], []
    expected_curve, sf_scores = [], 0
    own_latency, sf_latency = [], []
    own_pre40, selected_pre40, sf_pre40 = [], [], []
    for index, turn in enumerate(trace):
        assert turn["ply"] == index + 1 and turn["before_fen"] == board.fen(en_passant="fen")
        assert board.outcome(claim_draw=True) is None, (game_id, "move after terminal", index + 1)
        move = chess.Move.from_uci(turn["uci"])
        assert move in board.legal_moves and turn["san"] == board.san(move)
        own = board.turn == own_white
        assert turn["actor"] == ("sparring" if own else "stockfish")
        assert turn["baseline_budget_ms"] == 1000
        assert all(number(turn[key]) and turn[key] >= 0 for key in ("elapsed_ms", "search_elapsed_ms", "relay_elapsed_ms"))
        if own:
            own_latency.append(turn["search_elapsed_ms"])
            decision = turn["sparring_decision"]
            assert decision["policy"] == "regicide-sparring-play-v2"
            assert decision["history_uci"] == record["uci_moves"][:index]
            assert decision["ply"] == index + 1 and decision["before_fen"] == turn["before_fen"]
            assert decision["selected_uci"] == turn["uci"]
            expected_phase = "finish" if index >= 138 else "ramp" if index >= 80 else "calibration" if index < 6 else "sparring"
            assert decision["phase"] == expected_phase
            phases[expected_phase] += 1
            reasons[decision["reason"]] += 1
            guard = sparring_pacing.resource_guard(board)
            assert decision["resource_guard"] == guard
            analysis_only = index >= 138 or guard["active"]
            assert decision["analysis_only"] == analysis_only
            assert decision["extra_budget_ms"] == (500 if analysis_only else 2000)
            # Frozen harness records the generic ply-based maximum, omitting
            # critical-guard override. Preserve this evidence discrepancy.
            assert turn["advertised_extra_budget_ms"] == (500 if index >= 138 else 2000)
            if turn["advertised_extra_budget_ms"] != decision["extra_budget_ms"]:
                notes.append({"ply": index + 1, "kind": "harness_generic_budget_overstates_guard_only_budget",
                              "trace_ms": turn["advertised_extra_budget_ms"], "actual_receipt_ms": decision["extra_budget_ms"]})
            assert 0 <= decision["quality_budget_ms"] <= 500
            assert decision["extra_elapsed_ms"] <= decision["extra_budget_ms"] + 250, (game_id, index + 1, "extra watchdog overrun")
            if analysis_only:
                assert decision["selected_uci"] == decision["baseline_uci"]
                assert decision["candidate_budget_ms"] == 0 and decision["probes"] == []
            base = line(board, decision.get("baseline"), decision["baseline_uci"])
            selected = base
            changed = decision["selected_uci"] != decision["baseline_uci"]
            if changed:
                selected = line(board, decision.get("candidate_compared"), turn["uci"])
                final = line(board, decision.get("candidate_final"), turn["uci"])
                assert selected and final and selected["kind"] == final["kind"] == "cp"
                assert selected["depth"] >= 4 and final["depth"] >= selected["depth"]
                assert final["depth"] != selected["depth"] or final["cp"] == selected["cp"]
                assert min(selected["cp"], final["cp"]) >= -100
                if decision["comparison_mode"] == "absolute_target_from_winning_mate":
                    assert base and base["kind"] == "mate" and base["mate"] > 0 and index < 80
                    assert selected["depth"] == max(4, base["depth"])
                    assert decision["loss_cp"] is None
                else:
                    assert base and base["kind"] == "cp" and selected["depth"] == base["depth"]
                    assert decision["loss_cp"] == max(0, base["cp"] - selected["cp"])
                assert decision["naturalness"]["admitted"] is True
                alternatives.append({"ply": index + 1, "selected": turn["uci"], "baseline": decision["baseline_uci"],
                    "loss_cp": decision["loss_cp"], "selected_cp_own": selected["cp"], "reason": decision["reason"]})
                if decision["naturalness"].get("material_return_exception"):
                    pacing = decision["pacing"]
                    assert pacing["active"] and pacing["stage"] in (3, 4) and pacing["allow_material_return"]
                    assert guard["material_concessions_blocked"] is False and board.is_capture(move)
                    for vulnerability in decision["naturalness"]["vulnerable_captures"]:
                        assert -vulnerability["net_material_cp"] <= pacing["return_cap_cp"]
                        assert vulnerability["resources_after_reply"]["active"] is False
                    exceptions.append({"ply": index + 1, "event_ply": pacing["event_ply"], "stage": pacing["stage"],
                                       "cap_cp": pacing["return_cap_cp"], "selected": turn["uci"]})
            if base and base["kind"] == "cp":
                expected_pacing = sparring_pacing.derive(board, decisions, base["cp"],
                    ramp_fraction=max(0, min(1, (board.fullmove_number - 40) / 30)))
                assert decision["pacing"] == expected_pacing
            if decision["pacing"].get("active"):
                p = decision["pacing"]
                assert p["stage"] in range(5) and p["debt_cp"] > 0
                if p["start_root_ply"] is not None:
                    assert p["stage"] == (index - p["start_root_ply"]) // 2 + 1
                if p["stage"] < 3:
                    assert p["allow_material_return"] is False
                paced.append({"ply": index + 1, **p})
            if guard["active"]:
                guards.append({"ply": index + 1, "reason": guard["reason"], "trigger_ply": guard["trigger_ply"]})
            elif guard["material_concessions_blocked"]:
                cautions.append({"ply": index + 1, "nonpawn_cp_total": guard["nonpawn_cp_total"],
                                 "own_nonpawn_pieces": guard["own_nonpawn_pieces"]})
            observation = decision["opponent_observation"]
            if observation.get("valid") is True:
                before = board.copy(stack=True)
                actual = before.pop()
                assert observation["forced"] is False
                assert observation["ply"] == index and observation["played_uci"] == actual.uci()
                assert observation["before_fen"] == before.fen(en_passant="fen")
                assert observation["history_before"] == record["uci_moves"][:index - 1]
                reference = line(before, observation["baseline"])
                played = line(before, observation["candidate_compared"], actual.uci())
                assert reference and played and reference["kind"] == played["kind"] == "cp"
                assert reference["depth"] == played["depth"] == observation["depth"] >= 4
                assert reference["cp"] - played["cp"] >= -25
                assert observation["loss_cp"] == max(0, reference["cp"] - played["cp"])
                observations.append({"ply": index, "loss_cp": observation["loss_cp"], "depth": observation["depth"],
                                     "analysis_only": analysis_only, "phase": expected_phase})
            if index < 80:
                own_pre40.append(base["cp_white"] if base and base["kind"] == "cp" else None)
                selected_pre40.append(selected["cp_white"] if selected and selected["kind"] == "cp" else None)
            decisions.append(decision)
        else:
            sf_latency.append(turn["search_elapsed_ms"])
            assert turn["advertised_extra_budget_ms"] == 0
            packet = turn["stockfish_score_packet"]
            assert packet["source"] == "play_merged_info"
            assert packet["score_pov"] == "white"
            if packet["score_pov_valid"]:
                assert packet["reported_score_turn"] == ("white" if board.turn else "black")
            assert packet["is_selected_move_score"] is False
            assert packet["iteration_identity"] == "not_guaranteed_merged_play_info"
            assert packet["before_fen"] == turn["before_fen"] and packet["selected_uci"] == turn["uci"]
            assert packet["configured_options"] == options
            if packet["pv_legal"]:
                test = board.copy(stack=True)
                for uci in packet["pv_uci"]:
                    test.push_uci(uci)
            assert packet["selected_matches_pv"] == bool(packet["pv_uci"] and packet["pv_uci"][0] == turn["uci"])
            sf_scores += int(packet["bound"] == "exact" and packet["score_pov_valid"] and packet["pv_legal"])
            # White's 40th move is ply79; Black's is ply80 and its response is
            # record81. These SF roots are different from own selection roots.
            if index and index <= 80 and trace[index - 1]["actor"] == "sparring":
                valid = packet["bound"] == "exact" and packet["score_pov_valid"] and packet["pv_legal"]
                sf_pre40.append(packet["cp_white"] if valid and number(packet["cp_white"]) else None)
        board.push(move)
        assert turn["after_fen"] == board.fen(en_passant="fen")
    assert len(trace) == record["plies"] and [m.uci() for m in board.move_stack] == record["uci_moves"]
    assert board.fen(en_passant="fen") == record["final_fen"] == side["final_fen"] == side["state"]["fen"]
    assert decisions == host["sparring_play"]["decisions"]
    assert set(side["engine_movetimes_ms"]) == {str(row["ply"]) for row in trace if row["actor"] == "sparring"}
    assert all(value == 1000 for value in side["engine_movetimes_ms"].values())
    rating = sparring_rating.snapshot(board, decisions, not own_white, rating_meta["calibration"])
    assert rating == side["state"]["engine"]["sparring_rating"]
    assert rating["calibration_games"] == 0 and rating["source"] == "heuristic_prior" and rating["label"] is None
    assert rating["observations"] == len(observations)
    assert sparring_rating.metadata(board, decisions, not own_white, rating_meta["calibration"]) == rating_meta
    archive = GameArchive(side_path.parent)
    archive_rows = []
    for saved_path in sorted(side_path.parent.glob("*.json")):
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        assert saved_path.stat().st_size <= MAX_RECORD_BYTES
        replay = archive.get_game(saved["game_id"])
        assert [row["uci"] for row in replay["moves"]] == saved["uci_moves"]
        assert len(replay["frames"]) == len(saved["uci_moves"]) + 1
        assert replay["frames"][-1]["fen"] == saved["final_fen"]
        curve = (saved.get("sparring_rating") or {}).get("curve", [])
        for frame in replay["frames"]:
            if not frame["engine"]["sparring_mode"]:
                continue
            visible = [row for row in curve if row["available_ply"] <= frame["ply"]]
            value = frame["engine"]["sparring_rating"]
            assert value["label"] is None and value["calibration_games"] == 0
            assert value["observations"] == len(visible)
            assert value["estimate"] == (visible[-1]["estimate"] if visible else None)
            assert value["updated_human_ply"] == (visible[-1]["human_ply"] if visible else None)
            assert not visible or visible[-1]["human_ply"] < frame["ply"]
        archive_rows.append({"file": str(saved_path.relative_to(folder)), "bytes": saved_path.stat().st_size,
                             "frames": len(replay["frames"]), "rating_curve_boundaries_checked": True})
    outcome = board.outcome(claim_draw=True)
    if record["error"]:
        assert record["result"] == "*" and not record["legal_result"]
    elif outcome:
        assert record["completed"] and record["legal_result"] and not record["capped"]
        assert record["result"] == outcome.result() and record["termination"] == outcome.termination.name.lower()
    else:
        assert len(trace) == 220 and record["capped"] and not record["completed"] and not record["legal_result"]
        assert record["result"] == "*"
    score = (.5 if record["result"] == "1/2-1/2" else float(record["result"] == ("1-0" if own_white else "0-1"))) if record["legal_result"] else None
    def band(values):
        longest, current, recoveries = 0, 0, 0
        for value in values:
            if not number(value):
                current = 0
            elif abs(value) > 100:
                current += 1
                longest = max(longest, current)
            else:
                recoveries += int(current > 0)
                current = 0
        return {"samples": len(values), "finite_known": sum(number(v) for v in values),
                "inside_100": sum(number(v) and abs(v) <= 100 for v in values),
                "outside_100": sum(number(v) and abs(v) > 100 for v in values),
                "unknown_or_mate": sum(not number(v) for v in values),
                "longest_consecutive_finite_outside": longest, "observed_recoveries": recoveries,
                "continuous_full40_finite_inside": len(trace) >= 80 and len(values) == 40
                    and all(number(value) and abs(value) <= 100 for value in values)}
    return {"game": game_id, "rating_setting": record["stockfish_elo"], "color": record["sparring_color"],
            "seed": record["sparring_seed"], "pair": record["pair"], "result": record["result"], "score": score,
            "plies": len(trace), "termination": record["termination"], "capped": record["capped"], "error": record["error"],
            "phase_counts": dict(phases), "reason_counts": dict(reasons), "observations": observations,
            "different_moves": alternatives, "positive_loss_concessions": sum(number(v["loss_cp"]) and v["loss_cp"] > 0 for v in alternatives),
            "mate_avoidances": sum(v["loss_cp"] is None for v in alternatives),
            "pacing_rows": paced, "delayed_material_exceptions": exceptions, "resource_guards": guards,
            "resource_caution_only": cautions,
            "first_critical_guard": guards[0] if guards else None,
            "archive_replays": archive_rows, "archive_limit_bytes": MAX_RECORD_BYTES,
            "reached_board_ply80": len(trace) >= 80, "reached_board_ply138": len(trace) >= 138,
            "actual_ramp_decisions": phases["ramp"], "actual_finish_decisions": phases["finish"],
            "first_ramp_decision_ply": next((r["ply"] for r in decisions if r["phase"] == "ramp"), None),
            "first_finish_decision_ply": next((r["ply"] for r in decisions if r["phase"] == "finish"), None),
            "pre40_baseline": band(own_pre40), "pre40_selected_root": band(selected_pre40), "pre40_sf_next_root": band(sf_pre40),
            "rating": rating, "sf_exact_packet_count": sf_scores,
            "wall_ms": {"sparring": own_latency, "stockfish": sf_latency}, "notes": notes,
            "files": {record["record_file"]: sha(folder / record["record_file"]), record["pgn_file"]: sha(folder / record["pgn_file"]),
                      f"game-{game_id:03d}.moves.jsonl": sha(folder / f"game-{game_id:03d}.moves.jsonl"), host["sidecar_file"]: sha(side_path)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    reports, shards, failures, private = [], [], [], []
    for folder in sorted(BASE.glob("ratings-*")):
        match = json.loads((folder / "match.json").read_text(encoding="utf-8"))
        assert match["source"] == {"commit": COMMIT, "dirty": False, "label": COMMIT}
        assert match["config"]["seed"] == SEED and match["policy"] == "regicide-sparring-play-v2"
        assert all(sha(ROOT / name) == value for name, value in match["source_sha256"].items())
        assert all(sha(ROOT / name) == value for name, value in match["catalog_sha256"].items())
        assert sha(match["config"]["binary"]) == match["binary_sha256"]["regicide"]
        assert sha(match["config"]["stockfish"]) == match["binary_sha256"]["stockfish"]
        assert match["protocol"]["ponder"] is False and match["config"]["cap_policy"] == "incomplete"
        for summary in match["games"]:
            try:
                reports.append(audit_game(folder, match, summary))
            except Exception as error:
                import traceback
                failures.append({"game": summary["game"], "error": str(error), "traceback": traceback.format_exc()})
        if match["status"] == "completed":
            assert all(match[field] is True for field in ("sources_unchanged", "catalog_unchanged", "binaries_unchanged"))
            combined = parse_pgn(folder / "match.pgn")
            assert len(combined) == len(match["games"])
            for game, summary in zip(combined, match["games"]):
                assert game.headers["Result"] == summary["result"]
                assert [m.uci() for m in game.mainline_moves()] == json.loads((folder / summary["record_file"]).read_text(encoding="utf-8"))["uci_moves"]
        shards.append({"path": str(folder.relative_to(ROOT)), "status": match["status"], "finalized_games": len(match["games"]),
                       "match_sha256": sha(folder / "match.json"), "source_files": len(match["source_sha256"]),
                       "source_catalog_binary_hashes_verified": True})
        for directory in folder.rglob(".sparring-rating-private"):
            count = 0
            if (directory / "labels.sqlite3").exists():
                with closing(sqlite3.connect(f"file:{(directory / 'labels.sqlite3').as_posix()}?mode=ro", uri=True)) as conn:
                    count = conn.execute("SELECT count(*) FROM labels").fetchone()[0]
            assert count == 0, "SF setting was incorrectly used as a personal label"
            private.append({"path": str(directory.relative_to(ROOT)), "rows": count, "exclude_from_publication": True})
    if args.final:
        assert len(shards) == 2 and all(row["status"] == "completed" and row["finalized_games"] == 4 for row in shards)
        assert len(reports) + len(failures) == 8
        for level in (1320, 1700, 2100, 2500):
            pair = [row for row in reports if row["rating_setting"] == level]
            if not (len(pair) == 2 and {r["color"] for r in pair} == {"white", "black"}
                    and len({r["seed"] for r in pair}) == 1):
                failures.append({"rating_setting": level, "error": "pair missing or mismatched after per-game audit"})
    report = {"schema": "regicide.sparring-independent-audit.v2", "audited_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "source_commit": COMMIT, "auditor_script_sha256": sha(__file__), "passed": not failures,
              "final": args.final, "shards": shards, "games": sorted(reports, key=lambda row: row["game"]),
              "failures": failures, "private_paths": private,
              "privacy": "No SF setting was written as a user label; private rating directories must be excluded even when empty",
              "scope": "Independent legal replay, parity, scores, budgets and structural phase/pacing checks; production pacing/rating replay helpers are also compared, not independent implementations",
              "limitations": ["No additional engine search", "Own-engine selected-root estimate differs from actual post-move evaluation",
                              "SF merged play-info packets are not atomic completed iterations or weakened selected-move scores",
                              "Uncalibrated display ratings are not a rating-accuracy experiment", "Move cap is unfinished, never a draw"],
              "totals": {"games": len(reports), "plies": sum(r["plies"] for r in reports),
                  "wins": sum(r["score"] == 1 for r in reports), "draws": sum(r["score"] == .5 for r in reports),
                  "losses": sum(r["score"] == 0 for r in reports), "unfinished": sum(r["score"] is None for r in reports),
                  "quality_observations": sum(len(r["observations"]) for r in reports),
                  "different_moves": sum(len(r["different_moves"]) for r in reports),
                  "positive_loss_concessions": sum(r["positive_loss_concessions"] for r in reports),
                  "mate_avoidances": sum(r["mate_avoidances"] for r in reports),
                  "delayed_material_exceptions": sum(len(r["delayed_material_exceptions"]) for r in reports),
                  "critical_resource_guard_decisions": sum(len(r["resource_guards"]) for r in reports),
                  "caution_only_decisions": sum(len(r["resource_caution_only"]) for r in reports),
                  "archive_sidecars_replayed": sum(len(r["archive_replays"]) for r in reports),
                  "largest_sidecar_bytes": max((v["bytes"] for r in reports for v in r["archive_replays"]), default=0),
                  "generic_budget_overstatements": sum(len(r["notes"]) for r in reports)}}
    if args.final:
        target = BASE / "independent-audit.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    print(json.dumps({"final": args.final, "passed": report["passed"], "totals": report["totals"], "failures": failures,
                      "games": [{k: row[k] for k in ("game", "result", "plies", "phase_counts", "positive_loss_concessions", "mate_avoidances")} for row in reports]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
