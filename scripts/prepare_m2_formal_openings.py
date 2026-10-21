"""Freeze evaluator-blind M2 starts and their explicit exposure exclusions.

No engine, evaluator, score, result or external opening database is consulted.
Named descriptions are authoring labels, not certified theory or best play.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import random
import re

import chess

ROOT = Path(__file__).resolve().parents[1]
SEED = 2026102102
PARENT_PLIES = 8
DEFAULT_FORMAL = ROOT / "tests/data/m2_formal_openings_2026-10-21.json"
DEFAULT_PREFLIGHT = ROOT / "tests/data/m2_preflight_openings_2026-10-21.json"
DEFAULT_AUDIT = ROOT / "docs/checks/2026-10-21-m2-formal/openings/exclusion-audit.json"
UCI = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?\Z")
LEGACY_FEN_FIELDS = {"fen", "initial_fen", "parent_initial_fen", "starting_fen", "start_fen",
                     "fen_before", "fen_after", "fen_after_opening", "opening_fen", "final_fen", "board_fen"}

# Written before any evaluation/search/game on these starts. Changes permitted
# during preparation are solely legality or the predeclared exposure filters.
# Odd indices stop one ply earlier to balance the side to move, not engine color.
DECLARED = (
    ("petroff-central-knights", "open-e4-e5", "Petroff central knights", "e4 e5 Nf3 Nf6 Nxe5 d6 Nf3 Nxe4 d4 d5 Bd3 Bd6 O-O O-O"),
    ("philidor-open-d-file", "open-e4-e5", "Philidor open d-file", "e4 e5 Nf3 d6 d4 exd4 Nxd4 Nf6 Nc3 Be7 Bf4 O-O Qd2 Nc6 O-O-O"),
    ("vienna-developed-bishops", "open-e4-e5", "Vienna developed bishops", "e4 e5 Nc3 Nf6 Bc4 Nc6 d3 Bc5 Nf3 d6 O-O O-O"),
    ("bishop-c6-center", "open-e4-e5", "Bishop opening c6 center", "e4 e5 Bc4 Nf6 d3 c6 Nf3 d5 Bb3 Bd6 O-O O-O a3 a6"),
    ("kings-gambit-center", "open-e4-e5", "King pawn offer and central counter", "e4 e5 f4 exf4 Nf3 d5 exd5 Nf6 Bb5 c6 dxc6 bxc6 Be2 Bd6 O-O O-O"),
    ("center-queen-retreat", "open-e4-e5", "Center exchange queen retreat", "e4 e5 d4 exd4 Qxd4 Nc6 Qe3 Nf6 Nc3 Bb4 Bd2 O-O O-O-O d6"),
    ("ponziani-supported-center", "open-e4-e5", "Ponziani supported center", "e4 e5 Nf3 Nc6 c3 d6 d4 Nf6 Bd3 Be7 O-O O-O"),
    ("four-knights-bishop-pin", "open-e4-e5", "Four knights bishop pin", "e4 e5 Nf3 Nc6 Nc3 Nf6 Bb5 Bb4 O-O O-O d3 d6 a3 a6"),
    ("italian-two-knights-quiet", "open-e4-e5", "Italian two knights quiet development", "e4 e5 Nf3 Nc6 Bc4 Nf6 d3 Be7 Nc3 O-O O-O d6 a3 a6"),
    ("spanish-berlin-d3", "open-e4-e5", "Spanish Berlin d3 center", "e4 e5 Nf3 Nc6 Bb5 Nf6 d3 Bc5 O-O O-O Bxc6 dxc6 Nbd2 Re8"),
    ("spanish-black-fianchetto", "open-e4-e5", "Spanish black fianchetto", "e4 e5 Nf3 Nc6 Bb5 g6 c3 a6 Ba4 Bg7 d4 exd4 cxd4 d6 O-O Nge7"),
    ("sicilian-rossolimo", "sicilian", "Sicilian bishop and black fianchetto", "e4 c5 Nf3 Nc6 Bb5 g6 O-O Bg7 Re1 Nf6 e5 Nd5 c3 O-O d4 cxd4"),
    ("sicilian-moscow", "sicilian", "Sicilian early bishop exchange", "e4 c5 Nf3 d6 Bb5+ Bd7 Bxd7+ Qxd7 O-O Nf6 Re1 Nc6 c3 g6 d4 cxd4"),
    ("sicilian-pawn-offer", "sicilian", "Sicilian c-pawn offer development", "e4 c5 d4 cxd4 c3 dxc3 Nxc3 Nc6 Nf3 d6 Bc4 e6 O-O Nf6"),
    ("sicilian-c3-queen-center", "sicilian", "Sicilian c3 queen center", "e4 c5 c3 d5 exd5 Qxd5 d4 Nf6 Nf3 e6 Be2 Be7 O-O O-O"),
    ("french-exchanged-center", "french", "French exchanged center", "e4 e6 d4 d5 exd5 exd5 Nf3 Nf6 Bd3 Bd6 O-O O-O Nc3 c6"),
    ("french-rubinstein-knight-exchange", "french", "French central knight exchange", "e4 e6 d4 d5 Nc3 dxe4 Nxe4 Nd7 Nf3 Ngf6 Nxf6+ Nxf6 Bd3 c5 O-O Be7"),
    ("french-tarrasch-bishop-first", "french", "French Tarrasch bishop-first development", "e4 e6 d4 d5 Nd2 Be7 Ngf3 Nf6 Bd3 c5 exd5 exd5 O-O O-O"),
    ("caro-panov-isolated-center", "caro-kann", "Caro exchanged center c-pawn advance", "e4 c6 d4 d5 exd5 cxd5 c4 Nf6 Nc3 e6 Nf3 Bb4 Bd3 O-O O-O Nc6"),
    ("caro-two-knights-bishop-exchange", "caro-kann", "Caro two knights bishop exchange", "e4 c6 Nc3 d5 Nf3 Bg4 h3 Bxf3 Qxf3 e6 d4 Nf6 Bd3 Be7 O-O O-O"),
    ("caro-f3-center", "caro-kann", "Caro f3 supported center", "e4 c6 d4 d5 f3 e6 Nc3 Bb4 a3 Bxc3+ bxc3 dxe4 fxe4 Ne7 Nf3 O-O"),
    ("scandinavian-knight-recapture", "other-e4-defense", "Scandinavian knight recapture", "e4 d5 exd5 Nf6 d4 Nxd5 Nf3 g6 Be2 Bg7 O-O O-O c4 Nb6"),
    ("alekhine-c-pawn-space", "other-e4-defense", "Alekhine c-pawn space", "e4 Nf6 e5 Nd5 d4 d6 c4 Nb6 exd6 exd6 Nc3 Be7 Nf3 O-O Be2 Nc6"),
    ("modern-queenside-castling", "other-e4-defense", "Modern queenside castling", "e4 g6 d4 Bg7 Nc3 d6 Be3 a6 Qd2 b5 f3 Nd7 O-O-O Bb7"),
    ("owen-bishop-pressure", "other-e4-defense", "Owen bishop pressure and closed center", "e4 b6 d4 Bb7 Bd3 e6 Nf3 Nf6 Qe2 d5 e5 Nfd7 O-O c5"),
    ("queen-gambit-accepted-e4", "queen-pawn-d5", "Accepted queen pawn offer broad center", "d4 d5 c4 dxc4 e4 e5 Nf3 exd4 Bxc4 Nc6 O-O Nf6 e5 Nd5"),
    ("queen-gambit-bishop-first", "queen-pawn-d5", "Queen pawn bishop-first development", "d4 d5 c4 e6 Nc3 Be7 Bf4 Nf6 e3 O-O Nf3 c5 dxc5 Bxc5"),
    ("queen-gambit-tarrasch", "queen-pawn-d5", "Queen pawn early c5 exchange", "d4 d5 c4 e6 Nc3 c5 cxd5 exd5 Nf3 Nc6 g3 Nf6 Bg2 Be7 O-O O-O"),
    ("chigorin-queen-center", "queen-pawn-d5", "Queen pawn knight counter center", "d4 d5 c4 Nc6 Nf3 Bg4 e3 e5 cxd5 Qxd5 Nc3 Bb4 Be2 O-O-O"),
    ("slav-exchanged-center", "queen-pawn-d5", "Slav exchanged center", "d4 d5 c4 c6 cxd5 cxd5 Nc3 Nf6 Nf3 Nc6 Bf4 Bf5 e3 e6"),
    ("semi-slav-e3-first", "queen-pawn-d5", "Semi-Slav e3-first development", "d4 d5 c4 c6 Nc3 Nf6 e3 e6 Nf3 Nbd7 Bd3 dxc4 Bxc4 b5"),
    ("nimzo-a3-doubled-pawns", "indian-e6", "Nimzo a3 doubled pawns", "d4 Nf6 c4 e6 Nc3 Bb4 a3 Bxc3+ bxc3 O-O f3 d5 cxd5 exd5 e3 Re8"),
)
PREFLIGHT = (
    ("preflight-king-fianchetto", "preflight-indian-g6", "Preflight queen pawn king fianchetto", "d4 Nf6 c4 g6 g3 Bg7 Bg2 O-O Nf3 d6 O-O Nbd7"),
    ("preflight-dutch-g6", "preflight-dutch", "Preflight Dutch paired fianchetto", "d4 f5 g3 Nf6 Bg2 g6 Nf3 Bg7 O-O O-O c4 d6"),
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def piece_identity(board):
    """Turn/rules/clocks ignored; include the evaluator's color/rank reflection."""
    return min(board.board_fen(), board.mirror().board_fen())


def position_identity(board):
    """Rules-aware transposition identity without move clocks, with reflection."""
    return min(" ".join(board.fen(en_passant="legal").split()[:4]),
               " ".join(board.mirror().fen(en_passant="legal").split()[:4]))


def prior_paths(root=ROOT):
    """Declared scope: prior banks, seed definitions, research and match receipts."""
    paths = set((root / "tests/data").glob("*.json"))
    paths.update((root / "scripts/neural_search_data").glob("seeds.json"))
    for name in ("docs/matches", "docs/analysis"):
        paths.update(p for p in (root / name).rglob("*") if p.suffix in (".json", ".jsonl"))
    for folder in (root / "docs/checks").iterdir():
        if ("neural" in folder.name or "learning" in folder.name
                or folder.name == "2026-10-21-m2-foundation"):
            paths.update(p for p in folder.rglob("*")
                         if p.suffix in (".json", ".jsonl") and "code-snapshot" not in p.parts)
    return sorted(p for p in paths if p.name not in (DEFAULT_FORMAL.name, DEFAULT_PREFLIGHT.name))


def collect_exclusions(paths, root=ROOT):
    """Read only named board/history fields; labels and results never select starts."""
    pieces, states, parents, histories, family_ids, feature_hashes = set(), set(), set(), set(), set(), set()
    inventory, issues, seen_lines = [], [], set()
    piece_cache, state_cache, history_cache = {}, {}, {}
    counts = Counter()

    def add(board):
        raw = board.board_fen()
        state = " ".join(board.fen(en_passant="legal").split()[:4])
        if state not in state_cache:
            if not board.is_valid():
                return
            reflected = board.mirror()
            piece_cache[raw] = min(raw, reflected.board_fen())
            state_cache[state] = min(state, " ".join(reflected.fen(en_passant="legal").split()[:4]))
        pieces.add(piece_cache[raw])
        states.add(state_cache[state])
        counts["valid_board_occurrences"] += 1

    def replay(initial, moves, declared, path, location):
        if not isinstance(initial, str) or not isinstance(moves, list) or not moves:
            return
        if not all(isinstance(m, str) and UCI.fullmatch(m) for m in moves):
            return
        line = (initial, tuple(moves), declared)
        if line in seen_lines:
            return
        seen_lines.add(line)
        try:
            use_cache = declared or initial == chess.STARTING_FEN
            move_tuple = tuple(moves)
            cached_length = 0
            if use_cache:
                for length in range(len(moves), -1, -1):
                    if (initial, move_tuple[:length]) in history_cache:
                        cached_length = length
                        break
            cached = history_cache.get((initial, move_tuple[:cached_length])) if use_cache else None
            board = cached[0].copy(stack=False) if cached else chess.Board(initial)
            standard = cached[2] if cached else board.fen() == chess.STARTING_FEN
            if not cached:
                add(board)
                if use_cache:
                    history_cache[(initial, ())] = (board.copy(stack=False), position_identity(board), standard)
            for ply in range(cached_length, len(moves)):
                board.push_uci(moves[ply])
                add(board)
                if use_cache:
                    history_cache[(initial, move_tuple[:ply + 1])] = (board.copy(stack=False), position_identity(board), standard)
            if standard and declared and len(moves) >= PARENT_PLIES:
                parents.update(history_cache[(initial, move_tuple[:ply])][1]
                               for ply in range(PARENT_PLIES, len(moves) + 1))
                histories.add(tuple(moves))
        except ValueError as exc:
            issues.append({"path": str(path.relative_to(root)), "location": location,
                           "error": str(exc), "accepted_legal_prefix_only": True})

    def visit(value, path, location="$", declared=False, context_fen=None):
        if isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, path, f"{location}[{i}]", declared, context_fen)
        elif isinstance(value, dict):
            named = declared or any(k in value for k in ("family_id", "seed_id", "name"))
            if isinstance(value.get("family_id"), str):
                family_ids.add(value["family_id"])
            for key in ("feature_hash", "parent_feature_hash"):
                if isinstance(value.get(key), str) and re.fullmatch("[0-9a-f]{64}", value[key]):
                    feature_hashes.add(value[key])
            for key in ("feature_hashes", "excluded_features"):
                if isinstance(value.get(key), list):
                    feature_hashes.update(v for v in value[key] if isinstance(v, str)
                                          and re.fullmatch("[0-9a-f]{64}", v))
            for key in value:
                if "fen" not in key.lower():
                    continue
                if isinstance(value.get(key), str):
                    try:
                        add(chess.Board(value[key]))
                    except ValueError:
                        pass  # Some receipts use prose, not a FEN, in generic fields.
            initial = value.get("initial_fen", value.get("start_fen", chess.STARTING_FEN))
            for key in ("moves", "moves_uci", "history", "uci_moves", "opening_uci_moves"):
                replay(initial, value.get(key), named or key == "opening_uci_moves", path, location + "." + key)
            replay(value.get("parent_initial_fen", initial), value.get("parent_history"),
                   True, path, location + ".parent_history")
            root_fen = next((value[k] for k in ("fen_before", "fen", "search_fen", "root_fen", "source_fen")
                             if isinstance(value.get(k), str)), context_fen)
            if root_fen is None and isinstance(value.get("initial_fen"), str) and not value.get("history"):
                root_fen = value["initial_fen"]
            for key in ("pv", "pv_uci"):
                if root_fen is not None:
                    replay(root_fen, value.get(key), False, path, location + "." + key)
                elif isinstance(value.get(key), list) and value[key]:
                    issues.append({"path": path.relative_to(root).as_posix(), "location": location + "." + key,
                                   "kind": "unrooted_pv", "error": "No explicit local or inherited root FEN; PV not replayed"})
            # The learning arena explicitly defines short prefixes and seed draws.
            for prefix in value.get("prefixes", []) if isinstance(value.get("prefixes"), list) else []:
                replay(chess.STARTING_FEN, prefix, True, path, location + ".prefixes")
            if value.get("schema") == "regicide.learning.arena.openings.v1":
                for seed in value["seeds"]:
                    rng = random.Random(seed)
                    line = list(rng.choice(value["prefixes"]))
                    board = chess.Board()
                    for text in line:
                        board.push_uci(text)
                    while len(line) < 12 and board.outcome(claim_draw=True) is None:
                        move = rng.choice(sorted(board.legal_moves, key=lambda m: m.uci()))
                        line.append(move.uci())
                        board.push(move)
                    replay(chess.STARTING_FEN, line, True, path, location + ".expanded_arena_seeds")
            for key, child in value.items():
                visit(child, path, location + "." + key,
                      declared or key in ("openings", "sources", "seeds", "match_openings_100", "match_openings_1000"), root_fen)

    for source_index, path in enumerate(paths, 1):
        before = counts.copy()
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        extraction = "named JSON board/history fields"
        try:
            if path.suffix == ".jsonl":
                for i, line in enumerate(text.splitlines(), 1):
                    if line.strip():
                        visit(json.loads(line), path, f"line[{i}]")
            else:
                visit(json.loads(text), path)
        except json.JSONDecodeError as exc:
            # This historical .json is actually a two-line plain text test receipt.
            # Do not silently ignore any other malformed source data.
            if path.name != "rules-differential.json" or not text.startswith("PASS differential:"):
                raise ValueError("Malformed exclusion source: " + str(path)) from exc
            extraction = "plain text rules-test receipt; no recorded board or move list"
            issues.append({"path": path.relative_to(root).as_posix(), "location": "$",
                           "error": "JSON extension contains plain text summary, not position data",
                           "kind": "non_data_receipt"})
        inventory.append({"path": path.relative_to(root).as_posix(),
                          "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                          "extraction": extraction,
                          "board_occurrences": counts["valid_board_occurrences"] - before["valid_board_occurrences"]})
        if source_index % 50 == 0:
            print(f"Exposure extraction: {source_index}/{len(paths)} sources", flush=True)
    return {"extraction_version": 3, "pieces": pieces, "states": states, "parents": parents, "histories": histories,
            "family_ids": family_ids, "sources": inventory, "issues": issues,
            "counts": dict(counts), "feature_hashes": feature_hashes}


def authored_rows(lines, kind, prior, seen_parents=None, seen_pieces=None, seen_paths=None):
    seen_parents = set() if seen_parents is None else seen_parents
    seen_pieces = set() if seen_pieces is None else seen_pieces
    seen_paths = set() if seen_paths is None else seen_paths
    rows = []
    for index, (slug, cluster, name, san_line) in enumerate(lines):
        sans = san_line.split()
        # Keep preflight at 12 plies. Formal starts have 16 of each turn.
        if kind == "formal" and index % 2 and len(sans) % 2 == 0:
            sans = sans[:-1]
        board, moves, normalized, prefix_states = chess.Board(), [], [], []
        for san in sans:
            if board.outcome(claim_draw=True) is not None:
                raise ValueError(f"post-terminal authored line: {slug}")
            move = board.parse_san(san)
            normalized.append(board.san(move))
            moves.append(move.uci())
            board.push(move)
            prefix_states.append(position_identity(board))
        if not board.is_valid() or board.is_check() or board.outcome(claim_draw=True) is not None:
            raise ValueError(f"invalid/check/terminal authored start: {slug}")
        parent = prefix_states[PARENT_PLIES - 1]
        piece = piece_identity(board)
        failures = []
        if piece in prior["pieces"]: failures.append("prior pieces-only final input")
        if hashlib.sha256(piece.encode("ascii")).hexdigest() in prior.get("feature_hashes", set()):
            failures.append("prior explicitly recorded feature hash")
        if parent in prior["parents"]: failures.append("prior 8-ply parent/transposition")
        if parent in seen_parents: failures.append("within-new 8-ply parent/transposition")
        if set(prefix_states[PARENT_PLIES - 1:]) & seen_paths:
            failures.append("within-new later parent transposition")
        if piece in seen_pieces: failures.append("within-new final input/reflection")
        if any(tuple(moves[:len(h)]) == h or h[:len(moves)] == tuple(moves)
               for h in prior["histories"]):
            failures.append("same prior >=8-ply declared branch or extension")
        # Catch a different move order reaching an old >=8-ply parent later.
        if any(state in prior["parents"] for state in prefix_states[PARENT_PLIES - 1:]):
            failures.append("later transposition into a prior parent")
        if failures:
            raise ValueError(slug + ": " + "; ".join(failures))
        seen_parents.add(parent)
        seen_pieces.add(piece)
        seen_paths.update(prefix_states[PARENT_PLIES - 1:])
        rows.append({"name": "M2 " + name, "family_id": f"m2-{kind}-{slug}-20261021",
                     "cluster_id": cluster, "parent_prefix_plies": PARENT_PLIES,
                     "parent_prefix_uci": moves[:PARENT_PLIES], "parent_position_identity": parent,
                     "moves": moves, "san_moves": normalized, "plies": len(moves),
                     "fen_after_opening": board.fen(en_passant="fen"),
                     "pieces_identity_sha256": hashlib.sha256(piece.encode("ascii")).hexdigest(),
                     "side_to_move": "white" if board.turn else "black"})
    return rows


def prepare(paths=None, prior=None):
    prior = collect_exclusions(prior_paths() if paths is None else paths) if prior is None else prior
    parents, pieces, seen_paths = set(), set(), set()
    formal = authored_rows(DECLARED, "formal", prior, parents, pieces, seen_paths)
    preflight = authored_rows(PREFLIGHT, "preflight", prior, parents, pieces, seen_paths)
    random.Random(SEED).shuffle(formal)
    common = {"schema_version": 1, "seed": SEED,
              "generator": "scripts/prepare_m2_formal_openings.py", "generator_sha256": digest(__file__),
              "provenance": "Self-authored SAN constructions; no external games, book, opening-guide data, engine evaluation, search, or match result used to select starts. Python-chess checks rules only.",
              "parent_policy": "One start per distinct 8-ply parent position, including legal transpositions and color/rank reflection. Earlier common moves are allowed. Final pieces-only inputs and >=8-ply old declared branch extensions are excluded against the recorded inventory.",
              "independence_limit": "Developer-selected opening branches are not independent random samples. cluster_id groups larger shared structures; use family- and cluster-level uncertainty. Novelty is relative to the explicit recorded inventory, not all training positions or all chess theory.",
              "engine_color_policy": "Use every selected start twice, once with each engine as White. Side to move is separate from engine color."}
    formal_doc = common | {"purpose": "Formal M2 fixed sample; use all 32, 100 ms and 1000 ms; never replace after results.",
                           "openings": formal}
    preflight_doc = common | {"purpose": "Engineering preflight only, separate parents from formal. Exactly first row is selected for the predeclared 24-game smoke; neither row contributes to formal results.",
                              "selected_indices_one_based": [1], "openings": preflight}
    audit = {"schema_version": 1, "seed": SEED, "parent_plies": PARENT_PLIES,
             "selection_fields": ["self-authored moves", "legality", "nonterminal/not-in-check start", "exposure identities"],
             "scores_search_models_results_used": False,
             "sources": prior["sources"], "prior_counts": prior["counts"],
             "prior_unique_pieces_including_reflection": len(prior["pieces"]),
             "prior_unique_states_including_reflection": len(prior["states"]),
             "prior_explicit_feature_hash_count": len(prior.get("feature_hashes", set())),
             "extraction_version": prior.get("extraction_version", 3),
             "prefreeze_extraction_repairs": prior.get("prefreeze_extraction_repairs", []),
             "prior_unique_parent_states_at_or_after_8ply": len(prior["parents"]),
             "prior_declared_histories_at_least_8ply": len(prior["histories"]),
             "prior_family_ids": sorted(prior["family_ids"]), "prior_replay_issues": prior["issues"],
             "formal_count": len(formal), "preflight_count": len(preflight),
             "formal_cluster_counts": dict(sorted(Counter(r["cluster_id"] for r in formal).items())),
             "formal_side_to_move_counts": dict(Counter(r["side_to_move"] for r in formal)),
             "formal_vs_prior_final_overlap": 0, "formal_vs_prior_parent_overlap": 0,
             "new_cross_set_parent_or_final_overlap": 0,
             "scope": "All tests/data JSON (including 9 historical comparison/pilot banks and 2 learning banks), neural_search_data seeds, prior docs/matches and docs/analysis JSON/JSONL, and prior neural/learning/M2-foundation docs/checks JSON/JSONL excluding byte-code snapshots. Only board/history fields are extracted. Invalid recorded lines retain their legal prefixes and are listed. Opening-guide data and UI fixtures are not learning exposure. Plain PGN-only/unrecorded UCI info PVs, missing original datasets, unsaved local user games and external theory are outside scope.",
             "short_prefix_policy": "Shared positions before ply 8 are not exclusion failures: treating e4/e5 or all 2-3 move families as old would exclude broad opening classes. They remain correlated within larger declared clusters.",
             "construction_changes_before_freeze": [
                 {"reason": "Keep authored Black-to-move branches at least 12 plies while balancing 16 starts per turn.",
                  "changes": "Bishop c6 center and four-knights lines received a3/a6 before taking the predetermined odd-ply stop; no parent changed."},
                 {"reason": "Original French advance branch transposed at ply 10 into the used SEE replication bank; exclusion rules were retained.",
                  "rejected_san": "e4 e6 d4 d5 e5 c5 c3 Qb6 Nf3 Nc6 a3 Nh6 b4 cxd4 cxd4 Nf5",
                  "replacement": "French Rubinstein central knight exchange, a different central pawn/minor-piece structure, not a renamed or reordered old branch.",
                  "selection_information": "Only legal replay and prior parent position identity; no engine evaluation/search or game result."}],
             "files": {"formal_sha256": hashlib.sha256(json_bytes(formal_doc)).hexdigest(),
                       "preflight_sha256": hashlib.sha256(json_bytes(preflight_doc)).hexdigest()}}
    return formal_doc, preflight_doc, audit


def load_exposure(path):
    """Cached extraction is acceptable only while every input byte hash matches."""
    document = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    for source in document["sources"]:
        if digest(ROOT / source["path"]) != source["sha256"]:
            raise ValueError("Prior exposure source changed: " + source["path"])
    for key in ("pieces", "states", "parents", "family_ids", "feature_hashes"):
        if key not in document:
            document[key] = []
        document[key] = set(document[key])
    document["histories"] = {tuple(h) for h in document["histories"]}
    return document


def supplement_legacy_fens(prior):
    """Upgrade a prefreeze extraction by reading additional *fen* field names."""
    counts = Counter()
    def visit(value):
        if isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                if "fen" in key.lower() and key not in LEGACY_FEN_FIELDS and isinstance(child, str):
                    try:
                        board = chess.Board(child)
                    except ValueError:
                        continue
                    if board.is_valid():
                        prior["pieces"].add(piece_identity(board))
                        prior["states"].add(position_identity(board))
                        counts[key] += 1
                elif isinstance(child, (dict, list)):
                    visit(child)
    for source in prior["sources"]:
        path = ROOT / source["path"]
        text = path.read_text(encoding="utf-8-sig")
        if source.get("extraction", "").startswith("plain text"):
            continue
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                if line.strip(): visit(json.loads(line))
        else:
            visit(json.loads(text))
    prior["extraction_version"] = 2
    prior["supplemental_fen_field_occurrences"] = dict(counts)
    return prior


def exposure_bytes(prior):
    serial = dict(prior)
    for key in ("pieces", "states", "parents", "family_ids", "feature_hashes"):
        serial[key] = sorted(prior[key])
    serial["histories"] = [list(h) for h in sorted(prior["histories"])]
    return gzip.compress(json_bytes(serial), mtime=0)


def upgrade_exposure(prior):
    """Repair only prefreeze cache extraction; never consult evaluation values."""
    if prior.get("extraction_version", 1) < 2:
        supplement_legacy_fens(prior)
    affected = sorted({ROOT / issue["path"] for issue in prior["issues"]
                       if issue.get("accepted_legal_prefix_only")})
    old_issue_count = len(prior["issues"])
    if affected:
        repaired = collect_exclusions(affected)
        for key in ("pieces", "states", "parents", "histories", "family_ids", "feature_hashes"):
            prior[key].update(repaired[key])
        affected_names = {p.resolve() for p in affected}
        prior["issues"] = [i for i in prior["issues"] if (ROOT / i["path"]).resolve() not in affected_names]
        prior["issues"].extend(repaired["issues"])
        prior["prefreeze_extraction_repairs"] = [{
            "reason": "Nested result PVs need their enclosing query root FEN instead of assuming initial chess position.",
            "replayed_source_count": len(affected), "previous_issue_count": old_issue_count,
            "remaining_issue_count": len(prior["issues"]),
            "conservative_superset": "Previously accumulated board identities retained; corrected actual PV identities added. No exclusion weakened."}]
    def visit(value):
        if isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)): visit(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in ("feature_hash", "parent_feature_hash") and isinstance(child, str) and re.fullmatch("[0-9a-f]{64}", child):
                    prior["feature_hashes"].add(child)
                elif key in ("feature_hashes", "excluded_features") and isinstance(child, list):
                    prior["feature_hashes"].update(v for v in child if isinstance(v, str) and re.fullmatch("[0-9a-f]{64}", v))
                if isinstance(child, (dict, list)): visit(child)
    for source in prior["sources"]:
        if source.get("extraction", "").startswith("plain text"): continue
        path = ROOT / source["path"]
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                if line.strip(): visit(json.loads(line))
        else: visit(json.loads(text))
    prior["extraction_version"] = 3
    return prior


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal", type=Path, default=DEFAULT_FORMAL)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--exposure-index", type=Path,
                        default=DEFAULT_AUDIT.with_name("exposure-index.json.gz"))
    args = parser.parse_args()
    outputs = (args.formal, args.preflight, args.audit)
    if len({p.resolve() for p in outputs}) != 3 or any(p.exists() for p in outputs):
        parser.error("All three outputs must be different new files; frozen files are never overwritten")
    if args.exposure_index.exists():
        prior = load_exposure(args.exposure_index)
        if prior.get("extraction_version") != 3:
            # Outputs do not yet exist (checked above), so this is a preparation
            # cache upgrade, never mutation of an already frozen experiment.
            prior = upgrade_exposure(prior)
            args.exposure_index.write_bytes(exposure_bytes(prior))
    else:
        prior = collect_exclusions(prior_paths())
        args.exposure_index.parent.mkdir(parents=True, exist_ok=True)
        with args.exposure_index.open("xb") as stream:
            stream.write(exposure_bytes(prior))
    documents = prepare(prior=prior)
    documents[2]["exposure_index"] = {"path": str(args.exposure_index.resolve()),
                                     "sha256": digest(args.exposure_index)}
    for path, doc in zip(outputs, documents):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(json_bytes(doc))
    print(json.dumps({"status": "PASS", "formal": len(documents[0]["openings"]),
                      "preflight": len(documents[1]["openings"]), "sources": len(documents[2]["sources"]),
                      "replay_issues": len(documents[2]["prior_replay_issues"]),
                      "sha256": {str(p): digest(p) for p in outputs}}, indent=2))


if __name__ == "__main__":
    main()
