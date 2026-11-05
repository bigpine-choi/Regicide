"""Offline native-search screening of material offers outside a source index.

Nothing in this module proves a sacrifice sound or globally novel.  A passing
row means that the recorded, finite native searches met the configured gates
and every legal *immediate* opponent reply was examined.  The playing engine
does not import these candidates automatically.
"""
from __future__ import annotations

import copy
import csv
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re

import chess
import chess.pgn

import gambit_knowledge

SCHEMA = "regicide.gambit-discovery.v1"
OPENING_DIRECTORY = Path(__file__).resolve().parents[1] / "data/openings"
VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300,
          chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
_INDEX_CACHE = {}


class SourceUnavailableError(ValueError):
    """Missing baseline sources cannot establish even source-scoped novelty."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def position_key(board):
    """Source membership ignores counters, retaining legal en-passant rights."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def material_balance(board, color):
    return sum(value * (len(board.pieces(kind, color)) - len(board.pieces(kind, not color)))
               for kind, value in VALUES.items())


def replay_case(case):
    """Reproduce the actual history, rejecting a mismatched position or owner."""
    if not isinstance(case, dict):
        raise ValueError("case must be an object")
    if "history_uci" not in case and "moves" not in case:
        raise ValueError("full history_uci or moves is required")
    history = case.get("history_uci", case.get("moves"))
    if not isinstance(history, (list, tuple)) or not all(isinstance(move, str) for move in history):
        raise ValueError("history must be a list of UCI moves")
    if "moves" in case and list(case["moves"]) != list(history):
        raise ValueError("moves and history_uci disagree")
    board = chess.Board(case.get("root_fen", chess.STARTING_FEN))
    if not board.is_valid() or board.chess960:
        raise ValueError("invalid standard-chess root position")
    for uci in history:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError("illegal history move: " + uci)
        board.push(move)
    expected = case.get("fen")
    if expected is not None:
        expected_board = chess.Board(expected)
        if (not expected_board.is_valid() or expected_board.fen(en_passant="fen") not in
                (board.fen(en_passant="fen"), board.fen(en_passant="legal"))):
            raise ValueError("history does not reproduce fen")
    if case.get("position_key") is not None and case["position_key"] != position_key(board):
        raise ValueError("history does not reproduce position_key")
    if case.get("owner") not in (None, "white", "black"):
        raise ValueError("owner must be white or black")
    if case.get("owner") is not None and (case["owner"] == "white") != board.turn:
        raise ValueError("case owner is not the side to move")
    return board


@lru_cache(maxsize=2)
def _parse_opening_rows(source_identity, files):
    """Parse only already verified bytes; cache identity includes all hashes."""
    rows = []
    for filename, source_text in files:
        reader = csv.DictReader(io.StringIO(source_text), delimiter="\t")
        if not reader.fieldnames or not {"eco", "name", "pgn"}.issubset(reader.fieldnames):
            raise SourceUnavailableError("invalid full-opening TSV columns: " + filename)
        for entry in reader:
            game = chess.pgn.read_game(io.StringIO(entry["pgn"] + " *"))
            if game is None or game.errors:
                raise SourceUnavailableError("invalid full-opening PGN in " + filename)
            board, moves = game.board(), []
            if board.fen(en_passant="fen") != chess.STARTING_FEN or board.chess960:
                raise SourceUnavailableError("unexpected full-opening root in " + filename)
            for move in game.mainline_moves():
                if move not in board.legal_moves:
                    raise SourceUnavailableError("illegal full-opening move in " + filename)
                moves.append(move.uci())
                board.push(move)
            if not moves:
                raise SourceUnavailableError("empty full-opening line in " + filename)
            rows.append({"file": filename, "eco": entry["eco"], "name": entry["name"],
                         "moves": moves})
    return rows


def _pinned_opening_source(directory=None):
    """Load all pinned A--E rows, verifying the original manifest and license."""
    directory = Path(directory or OPENING_DIRECTORY)
    try:
        raw_manifest = (directory / "manifest.json").read_bytes()
        source = json.loads(raw_manifest)
        if (source.get("schema") != "regicide.opening-guide.catalog.v1"
                or source.get("license") != "CC0-1.0"
                or not re.fullmatch(r"[0-9a-f]{40}", source.get("commit", ""))):
            raise SourceUnavailableError("invalid full-opening source identity")
        texts, hashes = [], {}
        for filename in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv", "LICENSE"):
            raw = (directory / filename).read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != source["files"][filename]["sha256"]:
                raise SourceUnavailableError("full-opening source checksum mismatch: " + filename)
            hashes[filename] = actual
            if filename != "LICENSE":
                texts.append((filename, raw.decode("utf-8-sig")))
    except SourceUnavailableError:
        raise
    except (OSError, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SourceUnavailableError("full-opening sources unavailable: " + type(error).__name__) from error
    manifest = {"schema": source["schema"], "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "repository_url": source.get("repository_url"), "commit": source["commit"],
                "license": source["license"], "license_url": source.get("license_url"),
                "files_sha256": hashes, "all_file_hashes_verified": True}
    identity = _digest(manifest)
    rows = _parse_opening_rows(identity, tuple(texts))
    manifest["catalog_rows"] = len(rows)
    manifest["source_identity_sha256"] = identity
    return rows, manifest


def knowledge_index(reference_pgn_paths=()):
    """Index gambits, every pinned ordinary opening, and optional PGNs.

    PGN variations are indexed as well as main lines.  File hashes describe
    precisely the checked reference scope; they do not imply worldwide novelty.
    """
    rows = gambit_knowledge.repertoire()
    if not rows:
        raise SourceUnavailableError("gambit repertoire is unavailable or empty")
    row_hash = _digest(rows)
    full_rows, full_manifest = _pinned_opening_source()
    references = []
    for item in reference_pgn_paths:
        path = Path(item).expanduser().resolve()
        raw = path.read_bytes()
        references.append((str(path), hashlib.sha256(raw).hexdigest(), raw.decode("utf-8-sig")))
    identity = _digest([row_hash, full_manifest, [(path, sha) for path, sha, _text in references]])
    if identity in _INDEX_CACHE:
        return _INDEX_CACHE[identity]
    positions, transitions = set(), set()

    def add_line(board, moves):
        if not board.is_valid() or board.chess960:
            raise ValueError("invalid reference root")
        positions.add(position_key(board))
        for value in moves:
            move = chess.Move.from_uci(value) if isinstance(value, str) else value
            if move not in board.legal_moves:
                raise ValueError("illegal reference move: " + move.uci())
            transitions.add((position_key(board), move.uci()))
            board.push(move)
            positions.add(position_key(board))

    for row in rows:
        add_line(chess.Board(row.get("root_fen", chess.STARTING_FEN)), row["moves"])
    for row in full_rows:
        add_line(chess.Board(), row["moves"])
    pgn_manifest = []
    for path, sha, source_text in references:
        stream, games, edges = io.StringIO(source_text), 0, 0
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            if game.errors:
                raise ValueError("invalid reference PGN: " + path)
            games += 1
            board = game.board()
            if not board.is_valid() or board.chess960:
                raise ValueError("invalid reference PGN root: " + path)
            stack = [(game, board)]
            while stack:
                node, before = stack.pop()
                positions.add(position_key(before))
                for child in node.variations:
                    after = before.copy(stack=False)
                    add_line(after, [child.move])
                    edges += 1
                    stack.append((child, after))
        pgn_manifest.append({"path": path, "sha256": sha, "games": games,
                             "indexed_variation_edges": edges})
    manifest = {"schema": "regicide.gambit-discovery-reference.v1", "scope_sha256": identity,
                "repertoire_rows": len(rows), "repertoire_content_sha256": row_hash,
                "full_opening_source": full_manifest,
                "reference_pgns": pgn_manifest, "positions": len(positions),
                "transitions": len(transitions), "position_identity": "FEN first four fields; legal EP",
                "claim": "Novelty only within these exact supplied sources; no world-first claim"}
    result = {"positions": positions, "transitions": transitions, "manifest": manifest}
    if len(_INDEX_CACHE) >= 4:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[identity] = result
    return result


def _novelty(before, move, after, index):
    known_move = (position_key(before), move.uci()) in index["transitions"]
    known_position = position_key(after) in index["positions"]
    classification = ("known_transition" if known_move else
                      "known_transposition" if known_position else "unseen_position")
    return {"classification": classification, "unseen_move": not known_move,
            "unseen_position": not known_position,
            "known_transposition": not known_move and known_position,
            "source_scope_sha256": index["manifest"]["scope_sha256"]}


def _capture_square(board, move):
    if board.is_en_passant(move):
        return move.to_square - 8 if board.turn else move.to_square + 8
    return move.to_square


def _acceptances(after_offer, owner, before_balance):
    """A permitted capture can concern another piece, including en passant."""
    result = {}
    for reply in after_offer.legal_moves:
        if not after_offer.is_capture(reply):
            continue
        square = _capture_square(after_offer, reply)
        victim = after_offer.piece_at(square)
        if victim is None or victim.color != owner:
            continue
        accepted = after_offer.copy(stack=True)
        accepted.push(reply)
        balance = material_balance(accepted, owner)
        if balance < before_balance:
            result[reply.uci()] = {"reply_uci": reply.uci(), "captured_piece": victim.symbol(),
                                  "captured_square": chess.square_name(square),
                                  "captured_value_cp": VALUES[victim.piece_type],
                                  "en_passant": after_offer.is_en_passant(reply),
                                  "balance_after_acceptance_cp": balance}
    return result


def _probe(analyzer, board, owner, minimum_depth, nodes):
    """Normalize the supplied analyzer's side-to-move score without guessing."""
    try:
        raw = analyzer.analyze(board.copy(stack=True), nodes=nodes)
    except Exception as error:  # A spent/failed research budget remains unknown.
        return {"status": "unknown", "reason": "analyzer_exception", "error": type(error).__name__,
                "cp": None, "mate": None, "owner_cp": None, "owner_mate": None,
                "depth": 0, "nodes": 0, "pv_uci": [], "query_id": None}
    if not isinstance(raw, dict):
        raw = {"status": "unknown", "reason": "invalid_analyzer_result"}
    result = {key: copy.deepcopy(raw.get(key)) for key in
              ("status", "cp", "mate", "depth", "nodes", "pv_uci", "bestmove_uci", "query_id")}
    result["reason"] = raw.get("reason")
    result["score_pov"] = "white" if board.turn else "black"
    sign = 1 if board.turn == owner else -1
    cp = result["cp"] if type(result["cp"]) is int else None
    mate = result["mate"] if type(result["mate"]) is int and result["mate"] != 0 else None
    result["cp"], result["mate"] = cp, mate
    result["owner_cp"] = sign * cp if cp is not None else None
    result["owner_mate"] = sign * mate if mate is not None else None
    pv = result["pv_uci"]
    pv_valid = isinstance(pv, list)
    replay = board.copy(stack=True)
    if pv_valid:
        try:
            for uci in pv:
                move = chess.Move.from_uci(uci)
                if move not in replay.legal_moves:
                    raise ValueError("illegal native PV")
                replay.push(move)
        except (TypeError, ValueError):
            pv_valid = False
    result["pv_uci"] = list(pv) if pv_valid else []
    if result["status"] == "terminal":
        if board.outcome(claim_draw=True) is None or cp is None and mate is None:
            result.update(status="unknown", reason="invalid_terminal_result")
    elif result["status"] != "complete":
        result.update(status="unknown", reason=result["reason"] or "native_search_incomplete")
    elif (type(result["depth"]) is not int or result["depth"] < minimum_depth
          or cp is None and mate is None or not pv_valid or not pv):
        result.update(status="unknown", reason="insufficient_depth_score_or_legal_pv")
    if not pv_valid:
        result.update(status="unknown", reason="illegal_native_pv")
    return result


def _complete(probe):
    return probe["status"] in ("complete", "terminal")


def _score(probe):
    """Private ordering only; mate sentinels are never reported as centipawns."""
    if not _complete(probe):
        return None
    if probe["owner_mate"] is not None:
        mate = probe["owner_mate"]
        return 1_000_000 - mate if mate > 0 else -1_000_000 - mate
    return probe["owner_cp"]


def _score_gate(probe, baseline, max_loss_cp, absolute_floor_cp):
    if not _complete(probe) or not _complete(baseline):
        return "unknown"
    value, reference = _score(probe), _score(baseline)
    if value is None or reference is None:
        return "unknown"
    if probe["owner_mate"] is not None:
        if probe["owner_mate"] < 0:
            return "fail"
        return "pass"
    if baseline["owner_mate"] is not None and baseline["owner_mate"] > 0:
        return "fail"  # Do not exchange a found mate for a finite CP estimate.
    return "pass" if value >= absolute_floor_cp and value >= reference - max_loss_cp else "fail"


def _material_evidence(board, probe, owner, before_balance, acceptance):
    after = board.copy(stack=True)
    trace = [{"plies_after_acceptance": 0, "material_balance_cp": material_balance(after, owner)}]
    for ply, uci in enumerate(probe["pv_uci"], 1):
        after.push_uci(uci)  # _probe has already validated this exact PV.
        trace.append({"plies_after_acceptance": ply, "uci": uci,
                      "material_balance_cp": material_balance(after, owner)})
    immediate_recovery = []
    for move in board.legal_moves:
        if not board.is_capture(move):
            continue
        reply = board.copy(stack=True)
        reply.push(move)
        if material_balance(reply, owner) >= before_balance:
            immediate_recovery.append(move.uci())
    # Conservative evidence: a capture that can immediately be recovered is
    # reported as unresolved exchange, rather than certified as a sacrifice.
    persistent = (_complete(probe) and bool(probe["pv_uci"])
                  and all(row["material_balance_cp"] < before_balance for row in trace)
                  and not immediate_recovery)
    return {**acceptance, "balance_before_offer_cp": before_balance,
            "balance_after_native_continuation_cp": material_balance(after, owner),
            "material_investment_vs_before_offer_cp": max(0, before_balance - material_balance(after, owner)),
            "absolute_material_debt_after_continuation_cp": max(0, -material_balance(after, owner)),
            "material_trace": trace, "immediate_material_recovery_uci": sorted(immediate_recovery),
            "persistent_material_investment": persistent,
            "material_units": "pawn=100; knight=bishop=300; material only, not evaluation",
            "interpretation": "Permitted capture and finite continuation; acceptance is not forced"}


def discover_case(case, analyzer, *, candidate_limit=6, reply_limit=0,
                  max_loss_cp=60, minimum_depth=4):
    """Generate research candidates; never publish them as a playing policy.

    Optional case settings are ``reference_pgn_paths``, ``discovery_nodes`` and
    ``absolute_floor_cp`` (default -100).  Limits truncate evidence, never turn
    an unsearched reply into a pass.  All CP fields preserve their stated POV.
    """
    if (type(candidate_limit) is not int or candidate_limit < 1
            or type(reply_limit) is not int or reply_limit < 0
            or type(max_loss_cp) is not int or max_loss_cp < 0
            or type(minimum_depth) is not int or minimum_depth < 1):
        raise ValueError("invalid discovery limits")
    result = {"schema": SCHEMA, "case_id": case.get("case_id") if isinstance(case, dict) else None,
              "status": "unknown", "candidates": [], "query_ids": [],
              "claim": "Finite native-search screening only; no soundness or world-first proof"}
    try:
        board = replay_case(case)
        references = case.get("reference_pgn_paths", ())
        if not isinstance(references, (list, tuple)):
            raise ValueError("reference_pgn_paths must be a list")
        index = knowledge_index(references)
        floor = case.get("absolute_floor_cp", -100)
        nodes = case.get("discovery_nodes")
        if type(floor) is not int or nodes is not None and (type(nodes) is not int or nodes < 1):
            raise ValueError("invalid absolute floor or discovery node budget")
    except SourceUnavailableError as error:
        return {**result, "status": "unknown", "reason": "reference_scope_unavailable", "detail": str(error)}
    except (KeyError, OSError, UnicodeError, TypeError, ValueError) as error:
        return {**result, "status": "invalid", "reason": str(error)}
    owner, before_balance = board.turn, material_balance(board, board.turn)
    result.update(owner="white" if owner else "black", root_fen=board.root().fen(en_passant="fen"),
                  history_uci=[move.uci() for move in board.move_stack], fen=board.fen(en_passant="fen"),
                  position_key=position_key(board), knowledge_manifest=copy.deepcopy(index["manifest"]),
                  gates={"absolute_floor_cp": floor, "baseline_max_loss_cp": max_loss_cp,
                         "minimum_depth": minimum_depth, "candidate_limit": candidate_limit,
                         "reply_limit": reply_limit, "requested_nodes_per_probe": nodes})
    if board.outcome(claim_draw=True) is not None:
        return {**result, "status": "terminal", "reason": "root_terminal_or_claimable_draw"}
    baseline = _probe(analyzer, board, owner, minimum_depth, nodes)
    result["baseline"] = baseline
    bestmove = baseline.get("bestmove_uci") or next(iter(baseline["pv_uci"]), None)
    offers = []
    for move in sorted(board.legal_moves, key=lambda item: item.uci()):
        after = board.copy(stack=True)
        after.push(move)
        acceptance = _acceptances(after, owner, before_balance)
        if acceptance or move.uci() == bestmove:
            novelty = _novelty(board, move, after, index)
            offers.append((move, after, acceptance, novelty))
    # The native baseline is researched too when it offers material; remaining
    # candidates prioritize source-unseen moves deterministically, not geometry
    # falsely represented as a native centipawn advantage.
    offers.sort(key=lambda row: (row[0].uci() != bestmove,
                                not row[3]["unseen_move"], row[0].uci()))
    result["generated_candidates"] = len(offers)
    result["candidate_generation_truncated"] = len(offers) > candidate_limit
    for move, after_offer, acceptances, novelty in offers[:candidate_limit]:
        row = {"uci": move.uci(), "is_native_baseline": move.uci() == bestmove,
               "novelty": novelty, "fen_after_offer": after_offer.fen(en_passant="fen"),
               "material_balance_before_cp": before_balance,
               "material_balance_after_offer_cp": material_balance(after_offer, owner),
               "permitted_acceptances": list(acceptances), "replies": [],
               "bounded_screen_passed": False, "status": "unknown"}
        if not acceptances:
            row.update(status="rejected", reason="no_immediate_material_offer",
                       all_replies_complete=False, legal_reply_count=after_offer.legal_moves.count(),
                       examined_reply_count=0)
            result["candidates"].append(row)
            continue
        candidate_probe = _probe(analyzer, after_offer, owner, minimum_depth, nodes)
        row["native_after_offer"] = candidate_probe
        row["candidate_score_gate"] = _score_gate(candidate_probe, baseline, max_loss_cp, floor)
        replies = sorted(after_offer.legal_moves,
                         key=lambda reply: (reply.uci() not in acceptances, reply.uci()))
        selected = replies[:reply_limit] if reply_limit else replies
        for reply in selected:
            after_reply = after_offer.copy(stack=True)
            after_reply.push(reply)
            probe = _probe(analyzer, after_reply, owner, minimum_depth, nodes)
            evidence = None
            if reply.uci() in acceptances:
                evidence = _material_evidence(after_reply, probe, owner, before_balance,
                                              acceptances[reply.uci()])
            row["replies"].append({"uci": reply.uci(),
                                   "kind": "acceptance" if evidence else
                                           "capture_defense" if after_offer.is_capture(reply) else
                                           "checking_defense" if after_offer.gives_check(reply) else "quiet_defense",
                                   "native": probe,
                                   "score_gate": _score_gate(probe, baseline, max_loss_cp, floor),
                                   "material_evidence": evidence})
        all_complete = len(selected) == len(replies) and all(_complete(item["native"]) for item in row["replies"])
        all_score_pass = all(item["score_gate"] == "pass" for item in row["replies"])
        investments = [item["material_evidence"] for item in row["replies"]
                       if item["material_evidence"] is not None]
        persistent = any(item["persistent_material_investment"] for item in investments)
        row.update(all_replies_complete=all_complete, legal_reply_count=len(replies),
                   examined_reply_count=len(selected), replies_truncated=len(selected) != len(replies),
                   persistent_material_investment_found=persistent)
        known_failure = (row["candidate_score_gate"] == "fail"
                         or any(item["score_gate"] == "fail" for item in row["replies"]))
        if known_failure:
            row.update(status="rejected", reason="native_score_gate_failed")
        elif not all_complete or row["candidate_score_gate"] == "unknown" or not _complete(baseline):
            row.update(status="unknown", reason="incomplete_native_or_reply_coverage")
        elif not persistent:
            row.update(status="rejected", reason="exchange_or_material_recovery_not_confirmed_sacrifice")
        elif all_score_pass and row["candidate_score_gate"] == "pass":
            row.update(status="screened", reason="all_immediate_replies_passed_finite_native_gates",
                       bounded_screen_passed=True)
        else:
            row.update(status="unknown", reason="insufficient_scoring_evidence")
        result["candidates"].append(row)
    probes = [baseline]
    for row in result["candidates"]:
        if "native_after_offer" in row:
            probes.append(row["native_after_offer"])
        probes.extend(item["native"] for item in row["replies"])
    result["query_ids"] = list(dict.fromkeys(item["query_id"] for item in probes
                                            if item.get("query_id") is not None))
    result["status"] = ("complete" if all(_complete(item) for item in probes)
                        and all(row["status"] != "unknown" for row in result["candidates"])
                        else "unknown")
    result["screened_candidate_count"] = sum(row["bounded_screen_passed"] for row in result["candidates"])
    result["scope_complete"] = (result["status"] == "complete"
                                and not result["candidate_generation_truncated"]
                                and not any(row.get("replies_truncated") for row in result["candidates"]))
    return result
