"""Offline opening names and finite legal continuations, separate from search.

The pinned CC0 catalogue supplies names and example move sequences. It supplies
neither win rates nor a ranking of moves. No engine or network is used here.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
import threading

import chess
import chess.pgn

from opening_names import (FAMILY_DISPLAY_NAMES, SUPPLEMENTAL_DISPLAY_LINES, VERIFIED_ON,
                           display_name, family_display_name)
from opening_sides import empty_sides, get_opening_sides

DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "data/openings"
MAX_CONTINUATIONS = 20
MAX_CURRENT_MATCHES = 6
MAX_IMMEDIATE_MATCHES = 6
MAX_REACHABLE_OPENINGS = 5
DISCLAIMER = "공개된 오프닝 수순의 분류입니다. 승률이나 엔진의 추천 수가 아닙니다."


class CatalogError(ValueError):
    """Missing, modified or malformed local catalogue."""


@dataclass(frozen=True, slots=True)
class Opening:
    eco: str
    name: str
    moves: tuple[str, ...]
    keys: tuple[str, ...]
    zeroing: tuple[bool, ...]
    catalog_name: str
    name_source_url: str | None
    family_name_source_url: str | None = None


@dataclass(frozen=True, slots=True)
class Catalog:
    openings: tuple[Opening, ...]
    positions: Mapping[str, tuple[tuple[int, int], ...]]
    named: Mapping[str, tuple[int, ...]]
    source: Mapping[str, object]


def position_key(board: chess.Board) -> str:
    """Pieces, turn, castling rights and only a legally usable en-passant square."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def _load_catalog(directory: Path) -> Catalog:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict)
                or manifest.get("schema") != "regicide.opening-guide.catalog.v1"
                or not isinstance(manifest.get("commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
                or manifest.get("license") != "CC0-1.0"):
            raise CatalogError("Unsupported opening catalogue manifest")
        if (not isinstance(manifest.get("files"), dict)
                or any(not isinstance(manifest.get(name), str) or not manifest[name]
                       for name in ("repository_url", "license_url"))):
            raise CatalogError("Invalid opening catalogue manifest fields")
        for name in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv", "LICENSE"):
            entry = manifest["files"].get(name)
            if (not isinstance(entry, dict) or type(entry.get("bytes")) is not int
                    or entry["bytes"] <= 0 or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or any(not isinstance(entry.get(field), str) or not entry[field]
                           for field in ("upstream_path", "source_url"))):
                raise CatalogError("Invalid opening catalogue file metadata: " + name)
            raw = (directory / name).read_bytes()
            if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise CatalogError("Opening catalogue checksum mismatch: " + name)
        openings, positions, named = [], {}, {}
        for filename in ("a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"):
            with (directory / filename).open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream, delimiter="\t")
                if reader.fieldnames != ["eco", "name", "pgn"]:
                    raise CatalogError("Unexpected opening TSV columns")
                for row in reader:
                    if (set(row) != {"eco", "name", "pgn"}
                            or any(not isinstance(row[field], str) for field in ("eco", "name", "pgn"))
                            or not re.fullmatch(r"[A-E][0-9]{2}", row["eco"])
                            or not row["name"] or len(row["pgn"]) > 4096):
                        raise CatalogError("Invalid opening row")
                    game = chess.pgn.read_game(io.StringIO(row["pgn"]))
                    if game is None or game.errors:
                        raise CatalogError("Illegal opening sequence: " + row["name"])
                    moves = tuple(move.uci() for move in game.mainline_moves())
                    if not moves or len(moves) > 160:
                        raise CatalogError("Empty or excessive opening sequence")
                    line_id = len(openings)
                    board = chess.Board()
                    keys, zeroing, prefix_counts = [], [], Counter()
                    for offset in range(len(moves) + 1):
                        key = position_key(board)
                        keys.append(key)
                        prefix_counts[key] += 1
                        positions.setdefault(key, []).append((line_id, offset))
                        if offset == len(moves):
                            named.setdefault(key, []).append(line_id)
                        else:
                            if (board.halfmove_clock >= 150 or prefix_counts[key] >= 5
                                    or board.is_insufficient_material()):
                                raise CatalogError("Opening sequence continues after automatic game end")
                            move = chess.Move.from_uci(moves[offset])
                            zeroing.append(board.is_zeroing(move))
                            board.push(move)
                    name, name_source_url = display_name(row["eco"], row["name"], moves)
                    openings.append(Opening(row["eco"], name, moves, tuple(keys), tuple(zeroing),
                                            row["name"], name_source_url,
                                            None if name_source_url else family_display_name(row["name"])[1]))
        source = {
            "repository_url": manifest["repository_url"], "commit": manifest["commit"],
            "license": manifest["license"], "license_url": manifest["license_url"],
            "attribution": "Lichess chess-openings · CC0 1.0 · 일부 명칭 Chess.com 기준",
            "catalogue_rows": len(openings), "catalogue_positions": len(positions),
            "name_policy": "selected_exact_lines_verified_on_chess_com",
            "name_verified_on": VERIFIED_ON,
            "name_override_rows": sum(item.name_source_url is not None for item in openings),
            "name_references": tuple(sorted({item.name_source_url for item in openings if item.name_source_url})),
            "family_title_aliases": len(FAMILY_DISPLAY_NAMES),
            "family_title_rows": sum(item.family_name_source_url is not None for item in openings),
            "family_title_references": tuple(sorted({item.family_name_source_url for item in openings
                                                       if item.family_name_source_url})),
            "family_title_note": "확인한 계열 이름만 Chess.com 표기로 맞췄으며, 세부 변형명 전체를 확인했다는 뜻은 아닙니다.",
        }
        return Catalog(tuple(openings),
                       MappingProxyType({key: tuple(value) for key, value in positions.items()}),
                       MappingProxyType({key: tuple(value) for key, value in named.items()}),
                       MappingProxyType(source))
    except (OSError, KeyError, TypeError, UnicodeError, csv.Error, json.JSONDecodeError) as error:
        raise CatalogError("Opening catalogue could not be read") from error


@lru_cache(maxsize=1)
def _cached_catalog() -> Catalog:
    return _load_catalog(DATA_DIRECTORY)


_CATALOG_LOCK = threading.Lock()


def _catalog() -> Catalog:
    # Serialize first load as well: lru_cache alone permits concurrent misses.
    # The index and its values are immutable. Each response is freshly built.
    with _CATALOG_LOCK:
        return _cached_catalog()


_DISPLAY_LOCK = threading.Lock()
_DISPLAY_BASE = _DISPLAY_VIEW = None


def _display_catalog(catalog: Catalog) -> Catalog:
    """Presentation-only additions; the engine's immutable index is unchanged."""
    global _DISPLAY_BASE, _DISPLAY_VIEW
    with _DISPLAY_LOCK:
        if _DISPLAY_BASE is catalog:
            return _DISPLAY_VIEW
        openings = list(catalog.openings)
        positions, named = dict(catalog.positions), dict(catalog.named)
        for eco, name, moves, source_url in SUPPLEMENTAL_DISPLAY_LINES:
            board, keys, zeroing = chess.Board(), [], []
            line_id = len(openings)
            for offset in range(len(moves) + 1):
                key = position_key(board)
                keys.append(key)
                positions[key] = (*positions.get(key, ()), (line_id, offset))
                if offset == len(moves):
                    named[key] = (*named.get(key, ()), line_id)
                else:
                    move = chess.Move.from_uci(moves[offset])
                    if move not in board.legal_moves or board.is_game_over(claim_draw=False):
                        raise CatalogError("Invalid supplemental opening line")
                    zeroing.append(board.is_zeroing(move))
                    board.push(move)
            openings.append(Opening(eco, name, moves, tuple(keys), tuple(zeroing), "", source_url))
        source = {**catalog.source, "supplemental_name_rows": len(SUPPLEMENTAL_DISPLAY_LINES),
                  "supplemental_name_references": tuple(row[3] for row in SUPPLEMENTAL_DISPLAY_LINES),
                  "display_rows": len(openings), "display_positions": len(positions)}
        view = Catalog(tuple(openings), MappingProxyType(positions), MappingProxyType(named), MappingProxyType(source))
        _DISPLAY_BASE, _DISPLAY_VIEW = catalog, view
        return view


def _name_evidence(opening: Opening) -> dict:
    return {"name_source_url": opening.name_source_url,
            "family_name_source_url": opening.family_name_source_url,
            "name_verification": ("exact_line" if opening.name_source_url else
                                  "family_only" if opening.family_name_source_url else "catalog"),
            "name_origin": "supplemental" if not opening.catalog_name else "catalog"}


def _matches(catalog: Catalog, key: str, history: tuple[str, ...], scope: str) -> list[dict]:
    result = {}
    for line_id in catalog.named.get(key, ()):
        opening = catalog.openings[line_id]
        match_type = ("position" if scope == "custom" else
                      "exact" if opening.moves == history else "transposition")
        item = {"name": opening.name, "eco": opening.eco, "ply": len(history),
                "match_type": match_type, "catalog_ply": len(opening.moves),
                **_name_evidence(opening)}
        identity = opening.eco, opening.name
        order = (match_type != "exact", len(opening.moves), opening.eco, opening.name)
        if identity not in result or order < result[identity][0]:
            result[identity] = order, item
    return [item for _, item in sorted(result.values(), key=lambda value: value[0])]


def _history_counts(board: chess.Board) -> Counter:
    probe, counts = board.copy(stack=True), Counter()
    while True:
        counts[position_key(probe)] += 1
        if not probe.move_stack:
            return counts
        probe.pop()


def _can_finish_line(halfmove_clock: int, seen: Counter, keys: tuple[str, ...],
                     zeroing: tuple[bool, ...]) -> bool:
    """Do not cross an automatic draw; a terminal named endpoint is allowed.

    The catalogue already proves position-dependent legality and termination.
    Its exact keys and zeroing flags let us add the actual current counters and
    history without copying and replaying every alternative board.
    """
    added = Counter()
    for offset, (key, resets_clock) in enumerate(zip(keys, zeroing, strict=True)):
        halfmove_clock = 0 if resets_clock else halfmove_clock + 1
        added[key] += 1
        if offset + 1 < len(keys) and (halfmove_clock >= 150 or seen[key] + added[key] >= 5):
            return False
    return True


def _continuations(catalog: Catalog, board: chess.Board, key: str) -> tuple[list[dict], int]:
    if board.is_game_over(claim_draw=False):
        return [], 0
    following_moves = set()
    seen = _history_counts(board)
    for line_id, offset in catalog.positions.get(key, ()):
        opening = catalog.openings[line_id]
        if offset < len(opening.moves):
            following_moves.add(opening.moves[offset])
    rows = []
    for uci in following_moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            continue
        child = board.copy(stack=False)
        child.push(move)
        child_key = position_key(child)
        resets_clock = board.is_zeroing(move)
        immediate = {}
        for index in catalog.named.get(child_key, ()):
            opening = catalog.openings[index]
            identity = opening.eco, opening.name
            if identity not in immediate or (not immediate[identity].name_source_url and opening.name_source_url):
                immediate[identity] = opening
        candidates, all_lines = {}, set()
        for line_id, offset in catalog.positions.get(child_key, ()):
            opening = catalog.openings[line_id]
            suffix = (uci, *opening.moves[offset:])
            keys = (child_key, *opening.keys[offset + 1:])
            zeroing = (resets_clock, *opening.zeroing[offset:])
            if not _can_finish_line(board.halfmove_clock, seen, keys, zeroing):
                continue
            identity = opening.eco, opening.name
            all_lines.add((identity, suffix))
            order = len(suffix), suffix
            if identity not in candidates or order < candidates[identity][0]:
                candidates[identity] = order, suffix, opening
        if not candidates:
            continue
        reachable = []
        selected = sorted(candidates.items(), key=lambda item: (item[1][0][0], *item[0], item[1][1]))
        for (eco, name), (_, suffix, opening) in selected[:MAX_REACHABLE_OPENINGS]:
            moves = [chess.Move.from_uci(token) for token in suffix]
            # variation_san copies the board and formats move numbers from this
            # actual position, rather than from the catalogue's original line.
            reachable.append({"name": name, "eco": eco, "distance_plies": len(suffix),
                              "remaining_uci": list(suffix), "remaining_san": board.variation_san(moves),
                              **_name_evidence(opening)})
        immediate_rows = [{"name": name, "eco": eco, **_name_evidence(immediate[eco, name])}
                          for eco, name in sorted(immediate)]
        rows.append({"uci": uci, "san": board.san(move),
                     "immediate_openings": immediate_rows[:MAX_IMMEDIATE_MATCHES],
                     "immediate_total": len(immediate_rows),
                     "immediate_truncated": len(immediate_rows) > MAX_IMMEDIATE_MATCHES,
                     "reachable_openings": reachable, "reachable_total": len(candidates),
                     "reachable_lines_total": len(all_lines),
                     "reachable_truncated": len(candidates) > MAX_REACHABLE_OPENINGS})
    # Alphabetical SAN is display order, not popularity or engine preference.
    rows.sort(key=lambda row: (row["san"], row["uci"]))
    return rows[:MAX_CONTINUATIONS], len(rows)


def get_opening_guide(board: chess.Board) -> dict:
    """Return a fresh JSON-safe guide without changing the board or its history."""
    if not isinstance(board, chess.Board):
        raise TypeError("opening guide requires chess.Board")
    scope = "standard" if board.root().fen() == chess.STARTING_FEN else "custom"
    response = {"status": "unknown", "history_scope": scope,
                "history_plies": len(board.move_stack), "position_key": position_key(board),
                "current_matches": [], "current_matches_total": 0, "current_matches_truncated": False,
                "matched_opening": None, "continuations": [], "continuations_total": 0,
                "sides": empty_sides(),
                "continuations_truncated": False, "source": None, "disclaimer": DISCLAIMER,
                "ordering": "SAN 표기순이며 추천 순위가 아닙니다.",
                "history_note": ("초기 배치부터의 착수 이력을 사용합니다." if scope == "standard" else
                                 "사용자 지정 배치에서 시작해 그 이전 착수 이력은 알 수 없습니다.")}
    if board.chess960 or not board.is_valid():
        response.update(status="unavailable", message="표준 체스의 유효한 위치에서 오프닝 안내를 사용할 수 있습니다.")
        return response
    try:
        catalog = _display_catalog(_catalog())
        response["source"] = dict(catalog.source)
        history = tuple(move.uci() for move in board.move_stack)
        matches = _matches(catalog, response["position_key"], history, scope)
        response.update(current_matches=matches[:MAX_CURRENT_MATCHES], current_matches_total=len(matches),
                        current_matches_truncated=len(matches) > MAX_CURRENT_MATCHES)
        probe = board.copy(stack=True)
        while True:
            previous = _matches(catalog, position_key(probe), history[:len(probe.move_stack)], scope)
            if previous:
                response["matched_opening"] = previous[0]
                break
            if not probe.move_stack:
                break
            probe.pop()
        if response["position_key"] in catalog.positions:
            response["status"] = "in_catalog"
        elif response["matched_opening"]:
            response["status"] = "beyond_catalog"
        continuations, total = _continuations(catalog, board, response["position_key"])
        response.update(continuations=continuations, continuations_total=total,
                        continuations_truncated=total > MAX_CONTINUATIONS)
        response["sides"] = get_opening_sides(board, catalog, response)
        return response
    except (CatalogError, ValueError) as error:
        response.update(status="unavailable", message="오프닝 자료를 불러올 수 없습니다.")
        return response
