"""Diagnose finite Regicide self-play losses and export conservative train labels.

This is a bounded teacher diagnosis, not proof of a chess mistake or its cause.
Best and played moves are searched from the SAME full-history root. Only exact
finite cp labels, completed depth >= 2, and train-family positions can augment
learning. Mate/bound/incomplete/low-budget cases remain diagnostic evidence.
"""

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import sys

import chess
import chess.engine

import collect_learning_data as DATA

SCHEMA = "regicide.learning.loss-analysis.v1"
MIN_RELIABLE_NODES = 1000


def json_rows(path):
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"blank JSONL row {number}")
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"nonobject JSONL row {number}")
        rows.append(row)
    return rows


def local_path(manifest_path, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError("dataset paths must be nonempty relative paths")
    path = (manifest_path.parent / value).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(manifest_path.parent.resolve()):
        raise ValueError("dataset path escapes its directory")
    return path


def load_source(dataset):
    path = Path(dataset).expanduser().resolve(strict=True)
    if path.is_dir():
        path = path / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(manifest, dict) or manifest.get("schema") != DATA.SCHEMA or
            manifest.get("status") != "complete"):
        raise ValueError("analysis requires a complete v1 self-play dataset")
    games_path = local_path(path, manifest.get("games_path"))
    positions_path = local_path(path, manifest.get("positions_path", manifest.get("samples_path")))
    source_hashes = {games_path.name: DATA.sha256_file(games_path),
                     positions_path.name: DATA.sha256_file(positions_path)}
    for name, digest in source_hashes.items():
        recorded = manifest.get("files_sha256", {}).get(name)
        if recorded is not None and recorded != digest:
            raise ValueError(f"source dataset hash mismatch: {name}")
    games, ids, families = json_rows(games_path), set(), {}
    for game in games:
        identifier = game.get("game_id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("game ids must be unique nonempty strings")
        ids.add(identifier)
        split, family = game.get("split"), game.get("family")
        if split not in DATA.SPLITS or not isinstance(family, str) or not family:
            raise ValueError("invalid game split/family")
        if families.setdefault(family, split) != split:
            raise ValueError("opening family spans splits")
        if not isinstance(game.get("completed"), bool):
            raise ValueError("game completed must be boolean")
        if game["completed"]:
            board = DATA.replay_position(game.get("initial_fen"), game.get("history"))
            outcome = board.outcome(claim_draw=True)
            if outcome is None or outcome.result() != game.get("result"):
                raise ValueError("completed game result disagrees with replayed rules")
    return path, manifest, games_path, source_hashes, games


def select_positions(game, maximum):
    """Preselect bounded loser decisions using archived shallow score swings.

    No new teacher query is used for this ordering. A score swing merely ranks
    what to inspect and is never treated as the final loss label.
    """
    if not game.get("completed") or game.get("result") not in ("1-0", "0-1"):
        return []
    loser = chess.BLACK if game["result"] == "1-0" else chess.WHITE
    searches = game.get("selfplay_searches", [])
    if not isinstance(searches, list):
        raise ValueError("selfplay_searches must be a list")
    by_ply = {}
    for record in searches:
        ply = record.get("ply")
        if isinstance(ply, bool) or not isinstance(ply, int) or ply < 0 or ply >= len(game["history"]):
            raise ValueError("invalid archived search ply")
        if ply in by_ply or record.get("move") != game["history"][ply]:
            raise ValueError("duplicate search or archived move/history mismatch")
        by_ply[ply] = record
    selected = []
    for ply, record in by_ply.items():
        board = DATA.replay_position(game["initial_fen"], game["history"][:ply])
        if board.turn != loser:
            continue
        current, following = record.get("cp_stm"), by_ply.get(ply + 1, {}).get("cp_stm")
        estimate = current + following if (isinstance(current, int) and not isinstance(current, bool)
                                          and isinstance(following, int) and not isinstance(following, bool)) else None
        next_event = by_ply.get(ply + 1, {}).get("raw_score_event") or {}
        threatened_mate = next_event.get("kind") == "mate" and next_event.get("value", 0) > 0
        selected.append({"ply": ply, "played_move": game["history"][ply],
                         "rough_loss_cp": estimate, "mate_threat_after_played": threatened_mate,
                         "archived_search": record})
    # Reserve a slot for a finite swing before inspecting the mate sequence.
    # Otherwise a tiny diagnostic budget is exhausted by consecutive moves in
    # an already forced mate, leaving the earlier deterioration uninspected.
    swings = sorted((item for item in selected if item["rough_loss_cp"] is not None and
                     item["rough_loss_cp"] > 0), key=lambda item: (-item["rough_loss_cp"], item["ply"]))
    mates = sorted((item for item in selected if item["mate_threat_after_played"]),
                   key=lambda item: item["ply"])
    fallback = sorted(selected, key=lambda item: (
        -(item["rough_loss_cp"] if item["rough_loss_cp"] is not None else -1000000), item["ply"]))
    priority = swings[:1] + mates[:1] + swings[1:] + fallback
    ordered, seen = [], set()
    for item in priority:
        if item["ply"] not in seen:
            seen.add(item["ply"])
            ordered.append(item)
    return ordered[:maximum]


def search_root(engine, board, nodes, forced=None):
    if forced is not None and forced not in board.legal_moves:
        raise ValueError("forced played move is illegal")
    if "Clear Hash" in engine.options:
        engine.configure({"Clear Hash": None})
    result = engine.play(board, chess.engine.Limit(nodes=nodes), game=object(),
                         root_moves=[forced] if forced is not None else None,
                         info=chess.engine.INFO_ALL)
    event = copy.deepcopy(engine.protocol.last_score_event)
    if result.move is None or result.move not in board.legal_moves:
        raise ValueError("analysis engine returned no legal move; searchmoves support is required")
    if forced is not None and result.move != forced:
        raise ValueError("analysis engine ignored its forced root move")
    info = result.info
    pv = info.get("pv", [])
    if pv and pv[0] != result.move:
        raise ValueError("analysis bestmove and PV differ")
    probe = board.copy(stack=True)
    for move in pv:
        if probe.outcome(claim_draw=False) is not None or move not in probe.legal_moves:
            raise ValueError("illegal or postterminal analysis PV")
        probe.push(move)
    label, exclusion = DATA.exact_cp_info(board, info, event)
    score = info.get("score")
    cp = score.pov(board.turn).score() if score is not None else None
    mate = score.pov(board.turn).mate() if score is not None else None
    exact = event is not None and not event["upperbound"] and not event["lowerbound"]
    return {"move": result.move.uci(), "cp_stm": cp, "mate_stm": mate,
            "depth": info.get("depth"), "nodes": info.get("nodes"),
            "requested_nodes": nodes, "pv": [move.uci() for move in pv],
            "raw_score_event": event, "exact": exact,
            "label": label, "label_exclusion": exclusion}


def diagnose(best, played, selected, *, neural_player=False):
    exact = best["exact"] and played["exact"]
    cp_loss = (best["cp_stm"] - played["cp_stm"]
               if exact and best["cp_stm"] is not None and played["cp_stm"] is not None else None)
    mate_change = None
    if exact and (best["mate_stm"] is not None or played["mate_stm"] is not None):
        mate_change = {"best_mate_stm": best["mate_stm"], "played_mate_stm": played["mate_stm"],
                       "note": "mate distance is diagnostic only; never converted into a cp label"}
    saved = selected["archived_search"]
    shallow_cp = saved.get("cp_stm")
    shift = (best["cp_stm"] - shallow_cp if isinstance(shallow_cp, int) and
             not isinstance(shallow_cp, bool) and best["cp_stm"] is not None else None)
    if neural_player:
        classification = "learned_evaluation_or_search_difference_possible"
    elif best["move"] != selected["played_move"]:
        classification = "limited_search_or_evaluation_miss_possible"
    else:
        classification = "no_better_move_found_under_this_budget"
    return {"cp_loss": cp_loss, "mate_transition": mate_change,
            "shallow_to_deeper_best_cp_shift": shift,
            "completed_depths_differ": best["depth"] != played["depth"],
            "classification": classification, "classification_is_inference": True,
            "causal_limit": "bounded teacher searches cannot separate evaluation error from search horizon or prove optimal play"}


def run_analysis(dataset, binary, output, analysis_nodes=100000,
                 max_positions_per_game=8, min_loss_cp=100):
    for name, value in (("analysis_nodes", analysis_nodes),
                        ("max_positions_per_game", max_positions_per_game), ("min_loss_cp", min_loss_cp)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    manifest_path, source, games_path, source_hashes, games = load_source(dataset)
    binary = Path(binary).expanduser().resolve(strict=True)
    output = DATA.reserve_output(output)
    records, positions, exclusions = [], [], Counter()
    selfplay_nodes = source.get("limits", {}).get("selfplay_nodes")
    budget_reliable = (analysis_nodes >= MIN_RELIABLE_NODES and
                       isinstance(selfplay_nodes, int) and not isinstance(selfplay_nodes, bool) and
                       analysis_nodes > selfplay_nodes)
    report = {"schema": SCHEMA, "status": "running", "started_utc": DATA.utc_now(),
              "source_dataset_manifest": str(manifest_path),
              "source_dataset_manifest_sha256": DATA.sha256_file(manifest_path),
              "source_files_sha256": source_hashes, "source_games_path": str(games_path),
              "analyzer": {"sha256": DATA.sha256_file(__file__),
                           "selfplay_parser_sha256": DATA.sha256_file(DATA.__file__)},
              "positions_path": "positions.jsonl",
              "engine": {"name": binary.name, "sha256": DATA.sha256_file(binary),
                         "configured_options": {**DATA.OPTIONS, "UseNeuralEvaluation": False}},
              "limits": {"analysis_nodes_per_query": analysis_nodes,
                         "max_positions_per_game": max_positions_per_game,
                         "min_loss_cp": min_loss_cp, "minimum_completed_depth": 2,
                         "minimum_nodes": MIN_RELIABLE_NODES,
                         "budget_greater_than_selfplay": budget_reliable},
              "policy": {"selection": "complete decisive games; loser engine decisions only; largest positive archived finite swing first, earliest mate threat second, then remaining finite swings/fallback; no extra screening search",
                         "comparison": "same full-history root; unrestricted best versus searchmoves=[actual played move]; fresh game and hash per query",
                         "labels": "own classical Regicide exact finite cp; full history; no mate conversion; train families only",
                         "uncertainty": "a larger finite search is a fallible teacher, not a proof; classification is inference, not causal diagnosis",
                         "holdout": "validation/test losses may be diagnosed but never exported as training corrections"},
              "loss_games": sum(game["completed"] and game["result"] in ("1-0", "0-1") for game in games),
              "queries_completed": 0, "records": records, "errors": []}

    def save():
        DATA.write_jsonl(output / "positions.jsonl", positions)
        report.update({"checkpoint_utc": DATA.utc_now(),
                       "positions": len(positions), "exclusions": dict(exclusions),
                       "positions_sha256": DATA.sha256_file(output / "positions.jsonl")})
        DATA.checkpoint_json(output / "loss-report.json", report)

    save()
    try:
        with DATA.LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as teacher:
            if not teacher.id.get("name", "").startswith("Regicide "):
                raise ValueError("loss analysis requires the own Regicide engine")
            if not set(DATA.OPTIONS).issubset(teacher.options):
                raise ValueError("analysis engine omitted required options")
            teacher.configure(DATA.OPTIONS)
            if "UseNeuralEvaluation" in teacher.options:
                teacher.configure({"UseNeuralEvaluation": False})
            report["engine"]["uci_id"] = dict(teacher.id)
            for game in games:
                selected_rows = select_positions(game, max_positions_per_game)
                for selected in selected_rows:
                    ply = selected["ply"]
                    board = DATA.replay_position(game["initial_fen"], game["history"][:ply])
                    report["active_query"] = {"game_id": game["game_id"], "ply": ply,
                                              "phase": "unrestricted_best", "history": game["history"][:ply]}
                    save()
                    best = search_root(teacher, board, analysis_nodes)
                    report["queries_completed"] += 1
                    report["active_query"].update(phase="forced_played", best=best)
                    save()
                    played = search_root(teacher, board, analysis_nodes,
                                         chess.Move.from_uci(selected["played_move"]))
                    report["queries_completed"] += 1
                    diagnosis = diagnose(best, played, selected,
                                         neural_player=bool(game.get("selfplay_model_sha256")))
                    reason = DATA.sample_exclusion(board)
                    reliable = (budget_reliable and best["label"] is not None and
                                played["label"] is not None and
                                best["depth"] >= 2 and played["depth"] >= 2)
                    if reason is None and game["split"] != "train":
                        reason = "held_out_family"
                    if reason is None and not reliable:
                        reason = "unreliable_or_nonfinite_comparison"
                    if reason is None and best["move"] == selected["played_move"]:
                        reason = "no_better_alternative_found"
                    if reason is None and (diagnosis["cp_loss"] is None or diagnosis["cp_loss"] < min_loss_cp):
                        reason = "below_loss_threshold"
                    record = {"game_id": game["game_id"], "family": game["family"],
                              "split": game["split"], "initial_fen": game["initial_fen"],
                              "history": game["history"][:ply], "fen": board.fen(en_passant="fen"),
                              "ply": ply, "turn": "w" if board.turn else "b",
                              "played_move": selected["played_move"],
                              "selection": selected, "best": best, "played": played,
                              **diagnosis, "reliable_for_training": reliable,
                              "training_exclusion": reason}
                    records.append(record)
                    report.pop("active_query", None)
                    if reason:
                        exclusions[reason] += 1
                    else:
                        positions.append({"game_id": game["game_id"], "opening_id": game.get("opening_id"),
                                          "family": game["family"], "split": game["split"],
                                          "initial_fen": game["initial_fen"], "history": record["history"],
                                          "fen": record["fen"], "ply": ply, "turn": record["turn"],
                                          "material_cp_stm": DATA.material_cp_stm(board),
                                          **best["label"],
                                          "outcome_stm": DATA.outcome_stm(game["result"], record["turn"]),
                                          "source": "loss_analysis",
                                          "loss_analysis": {"cp_loss": diagnosis["cp_loss"],
                                                            "method": "same_root_forced_move",
                                                            "reliable": True,
                                                            "classification": diagnosis["classification"],
                                                            "classification_is_inference": True}})
                    save()
                    print(f"loss {game['game_id']} ply {ply}: cp_loss={diagnosis['cp_loss']} training={reason or 'accepted'}",
                          flush=True)
        report.update({"status": "complete", "finished_utc": DATA.utc_now()})
        save()
        return report
    except BaseException as exc:
        report.update({"status": "failed", "finished_utc": DATA.utc_now()})
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        save()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="v1 dataset manifest or directory")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory only")
    parser.add_argument("--analysis-nodes", type=DATA.positive_int, default=100000)
    parser.add_argument("--max-positions-per-game", type=DATA.positive_int, default=8)
    parser.add_argument("--min-loss-cp", type=DATA.positive_int, default=100)
    args = parser.parse_args(argv)
    try:
        result = run_analysis(args.dataset, args.binary, args.output, args.analysis_nodes,
                              args.max_positions_per_game, args.min_loss_cp)
        print(f"complete: {len(result['records'])} diagnosed positions, {result['positions']} train corrections")
        return 0
    except Exception as exc:
        print(f"loss analysis failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
