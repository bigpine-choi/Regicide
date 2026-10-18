"""Diagnose repeated endgame contexts in completed TRAIN self-play losses.

This reads no arena archive and exports no training labels. Themes are objective
position descriptions, not proven causes. A finite own-engine teacher remains
fallible even when its different budgets agree. All comparisons replay the full
history, clear the hash and start a fresh logical game for every query.
"""

import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import sys

import chess
import chess.engine

import analyze_learning_losses as LOSS
import collect_endgame_data as ENDATA
import collect_learning_data as DATA

SCHEMA = "regicide.endgame.diagnosis.v1"
PHASE = {chess.KNIGHT: 1, chess.BISHOP: 1, chess.ROOK: 2, chess.QUEEN: 4}
THEME_NAMES = ("pawn_only", "passed_pawns", "advanced_passed_pawns", "pawn_race",
               "attacked_undefended_nonpawn", "rook_endgame", "king_pawn_activity")


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def endgame_context(board):
    """Classify without inspecting the result, teacher, or played move.

    Attacks are geometric python-chess attacks, so pinned attackers/defenders
    still count. They do not prove a legal profitable capture or a hanging piece.
    A passed pawn has no enemy pawn ahead on its own or adjacent files.
    """
    pieces = len(board.piece_map())
    phase = sum(weight * len(board.pieces(kind, color))
                for kind, weight in PHASE.items() for color in chess.COLORS)
    queens = sum(len(board.pieces(chess.QUEEN, color)) for color in chess.COLORS)
    is_endgame = (queens == 0 and pieces <= 12) or (phase <= 8 and pieces <= 16)
    passed, advanced, loose = {}, {}, {}
    kings = {}
    for color in chess.COLORS:
        side = "w" if color else "b"
        passed[side], advanced[side], loose[side] = [], [], []
        for square in board.pieces(chess.PAWN, color):
            enemy_in_front = any(
                abs(chess.square_file(enemy) - chess.square_file(square)) <= 1 and
                ((chess.square_rank(enemy) > chess.square_rank(square)) if color else
                 (chess.square_rank(enemy) < chess.square_rank(square)))
                for enemy in board.pieces(chess.PAWN, not color))
            if not enemy_in_front:
                passed[side].append(chess.square_name(square))
                relative_rank = chess.square_rank(square) if color else 7 - chess.square_rank(square)
                if relative_rank >= 4:
                    advanced[side].append(chess.square_name(square))
        for square, piece in board.piece_map().items():
            if piece.color == color and piece.piece_type not in (chess.KING, chess.PAWN):
                if board.is_attacked_by(not color, square) and not board.is_attacked_by(color, square):
                    loose[side].append(chess.square_name(square))
        square = board.king(color)
        pawns = list(board.pieces(chess.PAWN, chess.WHITE) | board.pieces(chess.PAWN, chess.BLACK))
        kings[side] = {"square": chess.square_name(square),
                       "nearest_pawn_distance": min((chess.square_distance(square, pawn) for pawn in pawns),
                                                    default=None),
                       "center_distance": min(chess.square_distance(square, center)
                                              for center in (chess.D4, chess.E4, chess.D5, chess.E5))}
    nonpawn = any(board.pieces(kind, color) for kind in PHASE for color in chess.COLORS)
    pawns_exist = any(board.pieces(chess.PAWN, color) for color in chess.COLORS)
    themes = []
    if pawns_exist and not nonpawn:
        themes.append("pawn_only")
    if any(passed.values()):
        themes.append("passed_pawns")
    if any(advanced.values()):
        themes.append("advanced_passed_pawns")
    if all(advanced.values()):
        themes.append("pawn_race")
    if any(loose.values()):
        themes.append("attacked_undefended_nonpawn")
    if any(board.pieces(chess.ROOK, color) for color in chess.COLORS):
        themes.append("rook_endgame")
    if pawns_exist:
        themes.append("king_pawn_activity")
    return {"is_endgame": is_endgame, "piece_count": pieces, "phase": phase,
            "queens": queens, "themes": themes, "passed_pawns": passed,
            "advanced_passed_pawns": advanced, "attacked_undefended_nonpawns": loose,
            "king_activity": kings, "themes_are_causes": False}


def source_games(datasets):
    """Whitelist original train families; never consume reserved arena records.

    Exact complete game histories duplicated through cumulative corpora count
    once. Sources are recorded by content hash, not unstable local path names.
    """
    registry = DATA.load_openings(DATA.DEFAULT_OPENINGS)
    train_families = {item["family"] for item in registry["openings"] if item["split"] == "train"}
    forbidden_families = {item["family"] for item in registry["openings"] if item["split"] != "train"}
    sources, games, seen, excluded = [], [], set(), Counter()
    for dataset in datasets:
        path, manifest, games_path, hashes, rows = LOSS.load_source(dataset)
        allowed_endgames, seed_sha = None, None
        if "endgame" in manifest:
            declaration = manifest["endgame"]
            if (not isinstance(declaration, dict) or
                    declaration.get("schema") != "regicide.endgame.collection.v1" or
                    declaration.get("selected_split") != "train"):
                raise ValueError("diagnostic endgame source must declare the training collection role")
            seed_sha = DATA.sha256_file(ENDATA.DEFAULT_SEEDS)
            seeds, openings = ENDATA.load_seeds(ENDATA.DEFAULT_SEEDS, "train")
            if (DATA.sha256_file(ENDATA.DEFAULT_SEEDS) != seed_sha or
                    declaration.get("seed_file_sha256") != seed_sha or
                    declaration.get("seed_definition") != seeds):
                raise ValueError("diagnostic endgame seed definition or SHA256 changed")
            allowed_endgames = {(item["family"], item["initial_fen"]) for item in openings["openings"]}
            if any(game["split"] != "train" or (game["family"], game["initial_fen"]) not in allowed_endgames
                   for game in rows):
                raise ValueError("diagnostic endgame game family or initial FEN is not a declared training seed")
        source = {"manifest": str(path), "manifest_sha256": DATA.sha256_file(path),
                  "files_sha256": hashes, "selfplay_nodes": manifest.get("limits", {}).get("selfplay_nodes")}
        if seed_sha is not None:
            source["validated_endgame_seed_sha256"] = seed_sha
        sources.append(source)
        for game in rows:
            if game["split"] != "train":
                excluded["held_out_game"] += 1
                continue
            if allowed_endgames is None and (game["family"] in forbidden_families or game["family"] not in train_families):
                raise ValueError("train game family is absent from the frozen training whitelist")
            if not game["completed"] or game["result"] not in ("1-0", "0-1"):
                excluded["not_completed_decisive"] += 1
                continue
            key = hashlib.sha256(json.dumps([game["initial_fen"], game["history"], game["result"]],
                                           separators=(",", ":")).encode()).hexdigest()
            if key in seen:
                excluded["duplicate_complete_game"] += 1
                continue
            seen.add(key)
            games.append({"game": game, "source": source, "unique_game_sha256": key})
    return sources, games, dict(excluded)


def endgame_decisions(game):
    """Replay once; shallow scores select positions but never certify an error."""
    by_ply = {}
    for record in game.get("selfplay_searches", []):
        ply = record.get("ply")
        if type(ply) is not int or not 0 <= ply < len(game["history"]):
            raise ValueError("invalid archived search ply")
        if ply in by_ply or record.get("move") != game["history"][ply]:
            raise ValueError("duplicate search or archived move/history mismatch")
        by_ply[ply] = record
    loser = chess.BLACK if game["result"] == "1-0" else chess.WHITE
    board, selected = DATA.replay_position(game["initial_fen"], []), []
    for ply, text in enumerate(game["history"]):
        record = by_ply.get(ply)
        if record is not None and board.turn == loser:
            context = endgame_context(board)
            if context["is_endgame"]:
                current, following = record.get("cp_stm"), by_ply.get(ply + 1, {}).get("cp_stm")
                estimate = current + following if type(current) is int and type(following) is int else None
                event = by_ply.get(ply + 1, {}).get("raw_score_event") or {}
                selected.append({"ply": ply, "played_move": text, "rough_loss_cp": estimate,
                                 "mate_threat_after_played": event.get("kind") == "mate" and event.get("value", 0) > 0,
                                 "archived_search": record, "context": context})
        board.push(chess.Move.from_uci(text))
    swings = sorted((row for row in selected if row["rough_loss_cp"] is not None and row["rough_loss_cp"] > 0),
                    key=lambda row: (-row["rough_loss_cp"], row["ply"]))
    mates = sorted((row for row in selected if row["mate_threat_after_played"]), key=lambda row: row["ply"])
    fallback = sorted(selected, key=lambda row: (-(row["rough_loss_cp"] if row["rough_loss_cp"] is not None else -1000000), row["ply"]))
    result, seen = [], set()
    for row in swings[:1] + mates[:1] + swings[1:] + fallback:
        if row["ply"] not in seen:
            seen.add(row["ply"])
            result.append(row)
    return result


def select_cases(datasets, max_games=12, max_positions_per_game=2):
    positive(max_games, "max_games")
    positive(max_positions_per_game, "max_positions_per_game")
    sources, games, excluded = source_games(datasets)
    candidates = []
    inventory = []
    for item in games:
        game = item["game"]
        endgames = endgame_decisions(game)
        inventory.append({"game_id": game["game_id"], "family": game["family"],
                          "unique_game_sha256": item["unique_game_sha256"],
                          "endgame_loser_decisions": len(endgames),
                          "context_counts": dict(Counter(theme for row in endgames for theme in row["context"]["themes"]))})
        if endgames:
            candidates.append({**item, "selected": endgames[:max_positions_per_game]})
    # Selection/order uses only archived shallow evidence, fixed before teacher calls.
    candidates.sort(key=lambda item: (
        -(item["selected"][0]["rough_loss_cp"] if item["selected"][0]["rough_loss_cp"] is not None else -1000000),
        item["unique_game_sha256"]))
    cases = []
    for item in candidates[:max_games]:
        for selected in item["selected"]:
            game, ply = item["game"], selected["ply"]
            board = DATA.replay_position(game["initial_fen"], game["history"][:ply])
            cases.append({"game_id": game["game_id"], "family": game["family"], "split": "train",
                          "unique_game_sha256": item["unique_game_sha256"],
                          "source_manifest_sha256": item["source"]["manifest_sha256"],
                          "selfplay_nodes": item["source"]["selfplay_nodes"],
                          "initial_fen": game["initial_fen"], "history": game["history"][:ply],
                          "fen": board.fen(en_passant="fen"), "ply": ply,
                          "played_move": selected["played_move"], "selection": selected,
                          "context": selected["context"]})
    return {"sources": sources, "excluded_games": excluded, "inventory": inventory,
            "available_endgame_games": len(candidates), "selected_cases": cases}


def search(engine, board, nodes, depth=None, forced=None):
    positive(nodes, "nodes")
    if depth is not None:
        positive(depth, "depth")
    if forced is not None and forced not in board.legal_moves:
        raise ValueError("forced move is illegal")
    if "Clear Hash" not in engine.options:
        raise ValueError("diagnosis requires explicit Clear Hash support")
    engine.configure({"Clear Hash": None})
    result = engine.play(board, chess.engine.Limit(nodes=nodes, depth=depth), game=object(),
                         root_moves=[forced] if forced is not None else None,
                         info=chess.engine.INFO_ALL)
    event = copy.deepcopy(engine.protocol.last_score_event)
    if result.move is None or result.move not in board.legal_moves:
        raise ValueError("teacher returned no legal move")
    if forced is not None and result.move != forced:
        raise ValueError("teacher ignored forced root move")
    info = result.info
    if type(info.get("nodes")) is not int or not 1 <= info["nodes"] <= nodes:
        raise ValueError("teacher exceeded or omitted node count")
    if type(info.get("depth")) is not int or info["depth"] < 0 or (depth is not None and info["depth"] > depth):
        raise ValueError("invalid completed teacher depth")
    label, exclusion = DATA.exact_cp_info(board, info, event)
    if info.get("pv") and info["pv"][0] != result.move:
        raise ValueError("teacher bestmove and PV disagree")
    return {"move": result.move.uci(), "cp_stm": label["teacher_cp_stm"] if label else None,
            "depth": info["depth"], "nodes": info["nodes"], "requested_nodes": nodes,
            "requested_depth": depth, "depth_completed": depth is None or info["depth"] == depth,
            "pv": [move.uci() for move in info.get("pv", [])],
            "raw_score_event": event, "label_exclusion": exclusion}


def assess(comparisons, case, common_depth, min_loss_cp=100, stability_cp=30):
    reasons = []
    common = comparisons[0]
    for item in comparisons:
        for side in ("best", "played"):
            result = item[side]
            if result["cp_stm"] is None:
                reasons.append("nonfinite_bound_or_missing_score")
            if result["depth"] < common_depth or not result["depth_completed"]:
                reasons.append("incomplete_or_shallow_search")
        item["cp_loss"] = (item["best"]["cp_stm"] - item["played"]["cp_stm"]
                           if item["best"]["cp_stm"] is not None and item["played"]["cp_stm"] is not None else None)
    deeper = comparisons[1:]
    archived_nodes = case.get("selfplay_nodes")
    if type(archived_nodes) is not int or any(item["requested_nodes"] <= archived_nodes for item in deeper):
        reasons.append("teacher_budget_not_greater_than_selfplay")
    if len(deeper) != 2:
        reasons.append("two_deeper_budgets_required")
    else:
        for side in ("best", "played"):
            left, right = deeper[0][side], deeper[1][side]
            if side == "best" and left["move"] != right["move"]:
                reasons.append("deeper_best_move_unstable")
            if left["cp_stm"] is not None and right["cp_stm"] is not None and abs(left["cp_stm"] - right["cp_stm"]) > stability_cp:
                reasons.append(f"deeper_{side}_score_unstable")
    all_different = all(item["best"]["move"] != case["played_move"] for item in comparisons)
    if not all_different:
        reasons.append("played_move_not_consistently_inferior")
    if any(item["cp_loss"] is None or item["cp_loss"] < min_loss_cp for item in comparisons):
        reasons.append("loss_not_consistently_above_threshold")
    best_cp = common["best"]["cp_stm"]
    return {"status": "bounded_teacher_supported_loss" if not reasons else "unconfirmed",
            "reasons": sorted(set(reasons)), "common_depth_loss_cp": common["cp_loss"],
            "common_depth_best_cp_stm": best_cp,
            "already_severely_unfavourable_under_teacher": best_cp is not None and best_cp < -500,
            "already_unfavourable_threshold_cp": -500,
            "contexts_are_proven_causes": False,
            "causal_limit": "finite own-engine agreement cannot prove optimal play or separate evaluation error from selective-search horizon"}


def theme_summary(records):
    result = {}
    for theme in THEME_NAMES:
        selected = [record for record in records if theme in record["context"]["themes"]]
        supported = [record for record in selected if record.get("assessment", {}).get("status") == "bounded_teacher_supported_loss"]
        result[theme] = {"selected_positions": len(selected),
                         "selected_unique_games": len({row["unique_game_sha256"] for row in selected}),
                         "supported_positions": len(supported),
                         "supported_unique_games": len({row["unique_game_sha256"] for row in supported}),
                         "supported_families": sorted({row["family"] for row in supported})}
    return result


def run_diagnosis(datasets, binary, output, *, max_games=12, max_positions_per_game=2,
                  common_depth=4, depth_nodes=200000, teacher_nodes=(50000, 200000),
                  min_loss_cp=100, stability_cp=30, inventory_only=False):
    for name, value in (("common_depth", common_depth), ("depth_nodes", depth_nodes),
                        ("min_loss_cp", min_loss_cp), ("stability_cp", stability_cp)):
        positive(value, name)
    if common_depth > 64 or len(teacher_nodes) != 2 or any(type(value) is not int or value < 1000 for value in teacher_nodes) or teacher_nodes[0] >= teacher_nodes[1]:
        raise ValueError("require common_depth<=64 and two increasing teacher budgets >=1000")
    selection = select_cases(datasets, max_games, max_positions_per_game)
    binary = Path(binary).expanduser().resolve(strict=True)
    output = DATA.reserve_output(output)
    cases = selection.pop("selected_cases")
    report = {"schema": SCHEMA, "status": "running", "started_utc": DATA.utc_now(),
              **selection, "engine": {"sha256": DATA.sha256_file(binary), "options": {**DATA.OPTIONS, "UseNeuralEvaluation": False}},
              "source_sha256": DATA.sha256_file(__file__), "queries_completed": 0,
              "limits": {"max_games": max_games, "max_positions_per_game": max_positions_per_game,
                         "common_depth": common_depth, "depth_nodes": depth_nodes,
                         "teacher_nodes": list(teacher_nodes), "min_loss_cp": min_loss_cp,
                         "stability_cp": stability_cp},
              "policy": {"endgame": "queen_count=0 and pieces<=12; OR phase<=8 and pieces<=16; N/B=1,R=2,Q=4",
                         "selection": "train-whitelisted completed decisive games; loser decisions only; archived swing rank before teacher; duplicate whole histories count once",
                         "comparison": "same full-history root, unrestricted versus forced played move; cold hash/fresh logical game per query",
                         "evidence": "common-depth finite regret plus two greater node budgets with stable best move and scores; themes describe context, never proven causes",
                         "training": "no labels exported; no arena archive read; held-out games excluded before diagnostic selection"},
              "records": [], "errors": []}

    def save():
        report["theme_summary"] = theme_summary(report["records"])
        report["checkpoint_utc"] = DATA.utc_now()
        DATA.checkpoint_json(output / "endgame-report.json", report)

    save()
    try:
        if inventory_only:
            report["selected_cases"] = cases
        else:
            with DATA.LearningEngine.popen_uci([str(binary), "uci"], timeout=600) as teacher:
                if not teacher.id.get("name", "").startswith("Regicide ") or not set(DATA.OPTIONS).issubset(teacher.options):
                    raise ValueError("requires own Regicide engine with all required options")
                teacher.configure(DATA.OPTIONS)
                teacher.configure({"UseNeuralEvaluation": False})
                report["engine"]["uci_id"] = dict(teacher.id)
                for case in cases:
                    board = DATA.replay_position(case["initial_fen"], case["history"])
                    record = {**case, "comparisons": []}
                    report["records"].append(record)
                    for mode, nodes, depth in (("common_depth", depth_nodes, common_depth),
                                                ("deeper_nodes_1", teacher_nodes[0], None),
                                                ("deeper_nodes_2", teacher_nodes[1], None)):
                        pair = {"mode": mode, "requested_nodes": nodes}
                        record["comparisons"].append(pair)
                        for side, forced in (("best", None), ("played", chess.Move.from_uci(case["played_move"]))):
                            report["active_query"] = {"game": case["unique_game_sha256"], "ply": case["ply"], "mode": mode, "side": side}
                            save()
                            pair[side] = search(teacher, board, nodes, depth, forced)
                            report["queries_completed"] += 1
                            save()
                    record["assessment"] = assess(record["comparisons"], case, common_depth, min_loss_cp, stability_cp)
                    report.pop("active_query", None)
                    save()
                    print(f"endgame {case['game_id']} ply {case['ply']}: {record['assessment']['status']}", flush=True)
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
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-games", type=DATA.positive_int, default=12)
    parser.add_argument("--max-positions-per-game", type=DATA.positive_int, default=2)
    parser.add_argument("--common-depth", type=DATA.positive_int, default=4)
    parser.add_argument("--depth-nodes", type=DATA.positive_int, default=200000)
    parser.add_argument("--teacher-nodes", type=DATA.positive_int, nargs=2, default=[50000, 200000])
    parser.add_argument("--min-loss-cp", type=DATA.positive_int, default=100)
    parser.add_argument("--stability-cp", type=DATA.positive_int, default=30)
    parser.add_argument("--inventory-only", action="store_true")
    args = vars(parser.parse_args(argv))
    args["datasets"] = args.pop("dataset")
    try:
        report = run_diagnosis(**args)
        print(f"complete: {report['available_endgame_games']} endgame games; {report['queries_completed']} queries")
        return 0
    except Exception as exc:
        print(f"endgame diagnosis failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
