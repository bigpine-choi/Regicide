"""Independent raw-record replay audit; never launches an engine or edits study inputs."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import collect_learning_data as DATA


def need(test, message):
    global checks
    checks += 1
    if not test:
        raise AssertionError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def board_at(fen, history):
    board = chess.Board(fen)
    need(board.is_valid(), "invalid initial FEN")
    for text in history:
        move = chess.Move.from_uci(text)
        need(board.outcome(claim_draw=True) is None and move in board.legal_moves, "illegal/postterminal history")
        board.push(move)
    return board


checks, pv_plies = 0, 0
latest = json.loads((ROOT / "build/learning-cycle/latest.json").read_text())
run = ROOT / "build/learning-cycle" / latest["run"]
path = run / "diagnosis/endgame-report.json"
report = json.loads(path.read_text())
need(report["status"] == "complete" and report["schema"] == "regicide.endgame.diagnosis.v1", "report not complete")
need(report["engine"]["sha256"] == latest["binary_sha256"], "engine source identity")
need(report["source_sha256"] == sha(ROOT / "scripts/diagnose_endgames.py"), "source hash changed")
games = {}
for source in report["sources"]:
    manifest_path = Path(source["manifest"])
    need("arena" not in manifest_path.parts and ("bootstrap" in manifest_path.parts or manifest_path.parent.name == "data"), "nontraining source")
    need(sha(manifest_path) == source["manifest_sha256"], "manifest digest")
    manifest = json.loads(manifest_path.read_text())
    for name, digest in source["files_sha256"].items():
        need(sha(manifest_path.parent / name) == digest, "source bytes changed")
    for line in (manifest_path.parent / manifest["games_path"]).read_text().splitlines():
        game = json.loads(line)
        if game["split"] == "train" and game["completed"] and game["result"] in ("1-0", "0-1"):
            key = hashlib.sha256(json.dumps([game["initial_fen"], game["history"], game["result"]], separators=(",", ":")).encode()).hexdigest()
            games[key] = (game, source["manifest_sha256"], manifest["limits"]["selfplay_nodes"])
need(len(report["inventory"]) == len(games), "game inventory count")
need(len(report["records"]) * 6 == report["queries_completed"], "six queries per position")
by_game, themes, supported, exclusions = Counter(), {}, [], Counter()
themes_all = ("pawn_only", "passed_pawns", "advanced_passed_pawns", "pawn_race",
              "attacked_undefended_nonpawn", "rook_endgame", "king_pawn_activity")
for record in report["records"]:
    key = record["unique_game_sha256"]
    game, source_hash, archived_nodes = games[key]
    by_game[key] += 1
    need(record["split"] == "train" and record["family"] == game["family"], "heldout feedback/family")
    need(record["source_manifest_sha256"] == source_hash, "source identity")
    need(record["history"] == game["history"][:record["ply"]], "not exact source prefix")
    need(record["played_move"] == game["history"][record["ply"]], "not actual played move")
    board = board_at(record["initial_fen"], record["history"])
    need(board.fen(en_passant="fen") == record["fen"], "FEN history mismatch")
    need(board.turn == (chess.BLACK if game["result"] == "1-0" else chess.WHITE), "not losing side")
    pieces = len(board.piece_map())
    phase = sum(len(board.pieces(kind, color)) * weight for kind, weight in ((chess.KNIGHT, 1), (chess.BISHOP, 1), (chess.ROOK, 2), (chess.QUEEN, 4)) for color in chess.COLORS)
    queens = sum(len(board.pieces(chess.QUEEN, color)) for color in chess.COLORS)
    context = record["context"]
    need(context["is_endgame"] and ((queens == 0 and pieces <= 12) or (phase <= 8 and pieces <= 16)), "not endgame")
    need((context["piece_count"], context["phase"], context["queens"]) == (pieces, phase, queens), "context counts")
    expected_themes = []
    passed, advanced, attacked = {}, {}, {}
    for color, side in ((chess.WHITE, "w"), (chess.BLACK, "b")):
        passed[side], advanced[side], attacked[side] = [], [], []
        for square in board.pieces(chess.PAWN, color):
            ahead = [enemy for enemy in board.pieces(chess.PAWN, not color)
                     if abs(chess.square_file(enemy) - chess.square_file(square)) <= 1 and
                     ((chess.square_rank(enemy) > chess.square_rank(square)) if color else
                      (chess.square_rank(enemy) < chess.square_rank(square)))]
            if not ahead:
                passed[side].append(chess.square_name(square))
                if (chess.square_rank(square) if color else 7 - chess.square_rank(square)) >= 4:
                    advanced[side].append(chess.square_name(square))
        for square, piece in board.piece_map().items():
            if piece.color == color and piece.piece_type not in (chess.KING, chess.PAWN) and board.is_attacked_by(not color, square) and not board.is_attacked_by(color, square):
                attacked[side].append(chess.square_name(square))
    need(context["passed_pawns"] == passed and context["advanced_passed_pawns"] == advanced and context["attacked_undefended_nonpawns"] == attacked, "context square evidence")
    pawns = bool(board.pawns)
    if pawns and not (board.knights | board.bishops | board.rooks | board.queens):
        expected_themes.append("pawn_only")
    if any(passed.values()): expected_themes.append("passed_pawns")
    if any(advanced.values()): expected_themes.append("advanced_passed_pawns")
    if all(advanced.values()): expected_themes.append("pawn_race")
    if any(attacked.values()): expected_themes.append("attacked_undefended_nonpawn")
    if board.rooks: expected_themes.append("rook_endgame")
    if pawns: expected_themes.append("king_pawn_activity")
    need(context["themes"] == expected_themes and context["themes_are_causes"] is False, "theme causes conflation")
    comparisons = record["comparisons"]
    reasons = set()
    for index, pair in enumerate(comparisons):
        nodes = 200000 if index in (0, 2) else 50000
        requested_depth = 4 if index == 0 else None
        need(pair["requested_nodes"] == nodes, "query budget changed")
        for side in ("best", "played"):
            result = pair[side]
            need(result["requested_nodes"] == nodes and result["requested_depth"] == requested_depth, "query request")
            need(type(result["nodes"]) is int and 1 <= result["nodes"] <= nodes, "node cap")
            need(type(result["depth"]) is int and result["depth"] >= 0, "depth")
            need(result["depth_completed"] == (requested_depth is None or result["depth"] == 4), "depth completion")
            event = result["raw_score_event"]
            if event is not None:
                need(DATA.parse_score_event(event["raw_info"]) == event, "raw event flags/score")
                tokens = event["raw_info"].split()
                need(int(tokens[tokens.index("depth") + 1]) == result["depth"], "raw depth")
                need(int(tokens[tokens.index("nodes") + 1]) == result["nodes"], "raw nodes")
                need(tokens[tokens.index("pv") + 1:] == result["pv"], "raw PV")
            expected_cp = event["value"] if event and event["kind"] == "cp" and not event["lowerbound"] and not event["upperbound"] and result["depth"] >= 1 and result["pv"] else None
            need(result["cp_stm"] == expected_cp, "nonfinite/bound converted to cp")
            probe = board.copy(stack=True)
            need(result["move"] in [move.uci() for move in probe.legal_moves], "illegal bestmove")
            if side == "played": need(result["move"] == record["played_move"], "forced root ignored")
            need(not result["pv"] or result["pv"][0] == result["move"], "PV firstmove")
            for text in result["pv"]:
                move = chess.Move.from_uci(text)
                need(probe.outcome(claim_draw=False) is None and move in probe.legal_moves, "illegal/postterminal PV")
                probe.push(move)
                pv_plies += 1
            if result["cp_stm"] is None: reasons.add("nonfinite_bound_or_missing_score")
            if result["depth"] < 4 or not result["depth_completed"]: reasons.add("incomplete_or_shallow_search")
        loss = pair["best"]["cp_stm"] - pair["played"]["cp_stm"] if pair["best"]["cp_stm"] is not None and pair["played"]["cp_stm"] is not None else None
        need(pair["cp_loss"] == loss, "cp loss arithmetic")
        if pair["best"]["move"] == record["played_move"]: reasons.add("played_move_not_consistently_inferior")
        if loss is None or loss < 100: reasons.add("loss_not_consistently_above_threshold")
    for side in ("best", "played"):
        left, right = comparisons[1][side], comparisons[2][side]
        if side == "best" and left["move"] != right["move"]: reasons.add("deeper_best_move_unstable")
        if left["cp_stm"] is not None and right["cp_stm"] is not None and abs(left["cp_stm"] - right["cp_stm"]) > 30: reasons.add(f"deeper_{side}_score_unstable")
    if not (type(archived_nodes) is int and archived_nodes < 50000): reasons.add("teacher_budget_not_greater_than_selfplay")
    assessment = record["assessment"]
    need(assessment["reasons"] == sorted(reasons), "support reasons differ")
    status = "unconfirmed" if reasons else "bounded_teacher_supported_loss"
    need(assessment["status"] == status and assessment["contexts_are_proven_causes"] is False, "unsupported cause claim")
    cp = comparisons[0]["best"]["cp_stm"]
    need(assessment["already_severely_unfavourable_under_teacher"] == (cp is not None and cp < -500), "already bad metadata")
    exclusions.update(reasons)
    if not reasons:
        supported.append({"game_id": record["game_id"], "unique_game_sha256": key, "family": record["family"],
                          "ply": record["ply"], "played": record["played_move"], "same_depth_alternative": comparisons[0]["best"]["move"],
                          "loss_cp_by_budget": [pair["cp_loss"] for pair in comparisons], "already_severely_unfavourable_under_teacher": assessment["already_severely_unfavourable_under_teacher"]})
need(len(by_game) <= 12 and max(by_game.values()) <= 2, "position cap exceeded")
for theme in themes_all:
    selected = [record for record in report["records"] if theme in record["context"]["themes"]]
    yes = [record for record in selected if record["assessment"]["status"] == "bounded_teacher_supported_loss"]
    expected = {"selected_positions": len(selected), "selected_unique_games": len({r["unique_game_sha256"] for r in selected}),
                "supported_positions": len(yes), "supported_unique_games": len({r["unique_game_sha256"] for r in yes}),
                "supported_families": sorted({r["family"] for r in yes})}
    need(report["theme_summary"][theme] == expected, "theme summary mismatch")
summary = {"status": "passed", "source_commit": latest["checkout"]["commit"], "run": latest["run"],
           "report_sha256": sha(path), "audit_script_sha256": sha(__file__), "conditions_checked": checks,
           "queries": report["queries_completed"], "positions": len(report["records"]), "games": len(by_game),
           "legal_pv_plies": pv_plies, "bounded_teacher_supported_positions": len(supported), "supported": supported,
           "failure_reasons_position_counts": dict(exclusions),
           "claim_limit": "Two bounded teacher-supported losses, both French training games and both rook/pawn contexts; one already below -500cp. No causal or broad strength conclusion."}
out = ROOT / "build/endgame-learning-scale/diagnosis-audit.json"
out.write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
