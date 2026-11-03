"""Independent pre-search opening validation; never imports an engine client."""
import datetime
import hashlib
import json
import sys
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "tests/data/see_cost_openings_2026-10-18.json"
OUTPUT = Path(__file__).with_name("preflight-opening-validation.json")
prior = {}
sources = []
occurrences = 0


def canonical(board):
    # Ignore move counters, and normalize an uncapturable EP square away.
    return " ".join(board.fen(en_passant="legal").split()[:4])


def remember(board, source):
    global occurrences
    if not board.is_valid():
        raise AssertionError(f"Invalid prior position: {source}: {board.fen()}")
    key = canonical(board)
    prior.setdefault(key, []).append(source)
    occurrences += 1


def from_line(item, source):
    board = chess.Board(item.get("fen", chess.STARTING_FEN))
    for text in item["moves"]:
        move = chess.Move.from_uci(text)
        if move not in board.legal_moves:
            raise AssertionError(f"Illegal prior line at {source}: {text}")
        board.push(move)
    remember(board, source)


def walk(value, source):
    if isinstance(value, dict):
        if "name" in value and isinstance(value.get("moves"), list):
            from_line(value, source + ":line:" + value["name"])
        for key, item in value.items():
            if key in {"fen", "search_fen", "initial_fen", "opening_fen", "fen_after_opening"} and isinstance(item, str):
                remember(chess.Board(item), source + ":" + key)
            if isinstance(item, (dict, list)):
                walk(item, source + ":" + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk(item, source + f"[{index}]")


json_files = sorted((ROOT / "tests/data").glob("*.json")) + sorted((ROOT / "docs/matches").rglob("*.json"))
for path in json_files:
    if path == TARGET:
        continue
    relative = path.relative_to(ROOT).as_posix()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    walk(data, relative)
    sources.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

pgn_games = 0
for path in sorted((ROOT / "docs/matches").rglob("*.pgn")):
    relative = path.relative_to(ROOT).as_posix()
    with path.open(encoding="utf-8-sig") as stream:
        while (game := chess.pgn.read_game(stream)) is not None:
            pgn_games += 1
            assert not game.errors, (relative, game.errors)
            board = game.board()
            count = int(game.headers.get("OpeningPlies", "0"))
            moves = list(game.mainline_moves())
            assert count <= len(moves), (relative, count, len(moves))
            for move in moves[:count]:
                assert move in board.legal_moves, (relative, move)
                board.push(move)
            remember(board, f"{relative}:game:{pgn_games}:OpeningPlies:{count}")
    sources.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

document = json.loads(TARGET.read_text(encoding="utf-8"))
assert document["schema_version"] == 1
assert len(document["openings"]) == 8
new = set()
turns = {"white": 0, "black": 0}
validated = []
for entry in document["openings"]:
    board = chess.Board()
    san = []
    castling_plies = []
    assert 10 <= len(entry["moves"]) <= 12, entry["name"]
    for ply, text in enumerate(entry["moves"], 1):
        assert board.outcome(claim_draw=True) is None, (entry["name"], ply)
        move = chess.Move.from_uci(text)
        assert move in board.legal_moves, (entry["name"], text)
        if board.is_castling(move):
            castling_plies.append(ply)
        san.append(board.san(move))
        board.push(move)
        assert board.is_valid(), (entry["name"], ply)
    assert board.outcome(claim_draw=True) is None, entry["name"]
    assert not board.is_game_over(claim_draw=True), entry["name"]
    key = canonical(board)
    assert key not in new, (entry["name"], "duplicate fresh position")
    assert key not in prior, (entry["name"], "used prior position", prior.get(key))
    new.add(key)
    turn = "white" if board.turn == chess.WHITE else "black"
    turns[turn] += 1
    validated.append({"name": entry["name"], "uci_moves": entry["moves"], "san_moves": san,
                      "plies": len(san), "turn": turn, "fen": board.fen(en_passant="fen"),
                      "canonical_position": key, "legal_castling_plies": castling_plies,
                      "legal_move_count": board.legal_moves.count(),
                      "is_valid": board.is_valid(), "claimable_or_terminal_outcome": None,
                      "matches_any_prior_start": False})
assert turns == {"white": 4, "black": 4}, turns

# Check compatibility with the existing match loader without spawning an engine.
sys.path.insert(0, str(ROOT / "scripts"))
import compare_engines
_, loaded = compare_engines.load_openings(TARGET)
assert len(loaded) == 8
assert [entry["fen_after_opening"] for entry in loaded] == [entry["fen"] for entry in validated]

result = {"schema_version": 1, "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "python_version": sys.version, "python_chess_version": chess.__version__,
          "method": "Hand-authored lines; python-chess legality/state/terminal/uniqueness checks only. No candidate or baseline engine process, search, evaluation, ranking, tuning or game result used.",
          "canonicalization": "First four FEN fields; legal en-passant only; omit halfmove/fullmove counters.",
          "source": TARGET.relative_to(ROOT).as_posix(),
          "source_sha256": hashlib.sha256(TARGET.read_bytes()).hexdigest(),
          "prior_json_files_checked": len(json_files) - 1, "prior_pgn_games_checked": pgn_games,
          "prior_position_occurrences": occurrences, "prior_unique_canonical_positions": len(prior),
          "historical_input_sha256": sources, "side_to_move_counts": turns,
          "selected_positions": validated, "existing_loader_compatibility": True, "passed": True}
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({key: value for key, value in result.items()
                  if key not in {"historical_input_sha256", "selected_positions"}}, indent=2))
for position in validated:
    print(position["name"] + ": " + " ".join(position["san_moves"]) + " | " + position["turn"])
print("Saved: " + str(OUTPUT))
