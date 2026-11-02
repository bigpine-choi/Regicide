"""Short tactical opening proposals for the optional boss mode.

Each project-selected seed ends at the opening's defining move. The caller must
evaluate proposals with Regicide and return to ordinary full-strength search
when a seed ends or the opponent deviates. These are prospective opening names,
not a claim that the current position already belongs to the named gambit.

Names and factual move sequences were checked on 2026-11-02 against the bundled
Lichess chess-openings catalogue (CC0-1.0; data/openings/LICENSE and manifest.json)
and, where listed, Chess.com's own opening definitions. No explanatory prose,
win rates, engine evaluations, or large external opening book is imported.
The candidate list does not establish objective soundness or playing strength.
"""

from dataclasses import dataclass
import hashlib
import json

import chess


POLICY = "regicide-boss-traps-v3"
VERIFIED_ON = "2026-11-02"
CATALOG_COMMIT = "a6189a30dc273ccb21fc2536a9a2fefd5592a67a"
CATALOG_LICENSE = "CC0-1.0"
CATALOG_URL = f"https://github.com/lichess-org/chess-openings/tree/{CATALOG_COMMIT}"


@dataclass(frozen=True)
class _Seed:
    line_id: str
    name: str
    color: chess.Color
    moves: tuple[str, ...]
    catalog_file: str
    definition_url: str | None = None


# These deliberately stop before trap continuations. For example, the Englund
# seed ends at ...e5, not at the later ...Qe7/...Qb4+ queen manoeuvre.
_SEEDS = (
    _Seed("englund", "Englund Gambit", chess.BLACK,
          ("d2d4", "e7e5"), "a.tsv",
          "https://www.chess.com/openings/englund-gambit"),
    _Seed("budapest", "Indian Defense: Budapest Gambit", chess.BLACK,
          ("d2d4", "g8f6", "c2c4", "e7e5"), "a.tsv",
          "https://www.chess.com/openings/Budapest-Gambit"),
    _Seed("albin", "Queen's Gambit Declined: Albin Countergambit", chess.BLACK,
          ("d2d4", "d7d5", "c2c4", "e7e5"), "d.tsv",
          "https://www.chess.com/openings/Queens-Gambit-Declined-Albin-Countergambit"),
    _Seed("stafford", "Petrov's Defense: Stafford Gambit", chess.BLACK,
          ("e2e4", "e7e5", "g1f3", "g8f6", "f3e5", "b8c6"), "c.tsv",
          "https://www.chess.com/openings/Petrovs-Defense-Classical-Stafford-Gambit"),
    _Seed("latvian", "Latvian Gambit", chess.BLACK,
          ("e2e4", "e7e5", "g1f3", "f7f5"), "c.tsv",
          "https://www.chess.com/openings/Kings-Pawn-Opening-Latvian-Gambit"),
    _Seed("elephant", "Elephant Gambit", chess.BLACK,
          ("e2e4", "e7e5", "g1f3", "d7d5"), "c.tsv"),
    _Seed("kings", "King's Gambit", chess.WHITE,
          ("e2e4", "e7e5", "f2f4"), "c.tsv",
          "https://www.chess.com/openings/Kings-Gambit"),
    _Seed("danish", "Danish Gambit", chess.WHITE,
          ("e2e4", "e7e5", "d2d4", "e5d4", "c2c3"), "c.tsv",
          "https://www.chess.com/openings/Danish-Gambit"),
    _Seed("smith-morra", "Sicilian Defense: Smith-Morra Gambit", chess.WHITE,
          ("e2e4", "c7c5", "d2d4", "c5d4", "c2c3"), "b.tsv",
          "https://www.chess.com/openings/Sicilian-Defense-Smith-Morra-Gambit"),
    _Seed("evans", "Italian Game: Evans Gambit", chess.WHITE,
          ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "b2b4"), "c.tsv"),
    _Seed("blackmar-diemer", "Blackmar-Diemer Gambit", chess.WHITE,
          ("d2d4", "d7d5", "e2e4"), "d.tsv"),
    _Seed("vienna", "Vienna Game: Vienna Gambit", chess.WHITE,
          ("e2e4", "e7e5", "b1c3", "g8f6", "f2f4"), "c.tsv"),
    _Seed("scotch", "Scotch Game: Scotch Gambit", chess.WHITE,
          ("e2e4", "e7e5", "g1f3", "b8c6", "d2d4", "e5d4", "f1c4"), "c.tsv"),
    _Seed("jerome", "Italian Game: Jerome Gambit", chess.WHITE,
          ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "f8c5", "c4f7"), "c.tsv"),
    _Seed("halloween", "Four Knights Game: Halloween Gambit", chess.WHITE,
          ("e2e4", "e7e5", "g1f3", "b8c6", "b1c3", "g8f6", "f3e5"), "c.tsv"),
    _Seed("falkbeer", "King's Gambit Declined: Falkbeer Countergambit", chess.BLACK,
          ("e2e4", "e7e5", "f2f4", "d7d5"), "c.tsv"),
    _Seed("traxler", "Italian Game: Two Knights Defense, Traxler Counterattack", chess.BLACK,
          ("e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "f3g5", "f8c5"), "c.tsv"),
    _Seed("fishing-pole", "Ruy Lopez: Berlin Defense, Fishing Pole Variation", chess.BLACK,
          ("e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "g8f6", "e1g1", "f6g4"), "c.tsv"),
    _Seed("from", "Bird Opening: From's Gambit", chess.BLACK,
          ("f2f4", "e7e5"), "a.tsv"),
)
MAX_PLIES = max(len(line.moves) for line in _SEEDS)


def repertoire():
    """Return independent, JSON-compatible metadata for all finite seeds."""
    return [{"line_id": line.line_id, "name": line.name,
             "color": "white" if line.color else "black", "moves": list(line.moves),
             "prefix_to_signature": list(line.moves),
             "source_url": f"{CATALOG_URL.replace('/tree/', '/blob/')}/{line.catalog_file}",
             "definition_url": line.definition_url,
             "source_license": CATALOG_LICENSE, "verified_on": VERIFIED_ON}
            for line in _SEEDS]


def _valid_history(board):
    if (board.chess960 or len(board.move_stack) >= MAX_PLIES
            or not board.is_valid() or board.is_check()
            or board.root().fen(en_passant="fen") != chess.STARTING_FEN):
        return None
    replay = chess.Board()
    history = []
    for move in board.move_stack:
        if move not in replay.legal_moves:
            return None
        replay.push(move)
        history.append(move.uci())
    # A FEN without full history, or a board altered after push(), must not
    # accidentally re-enter a line. Include clocks and en passant explicitly.
    if replay.fen(en_passant="fen") != board.fen(en_passant="fen"):
        return None
    if board.is_game_over(claim_draw=True):
        return None
    return tuple(history)


def proposals(board, seed):
    """Propose unique legal next moves for the side that owns each gambit.

    Exact history matching intentionally excludes transpositions and re-entry.
    A different seed only changes proposal order; it never changes legality,
    supplies an opponent move, or authorizes bypassing the caller's risk gate.
    """
    history = _valid_history(board)
    if history is None:
        return []
    offset = len(history)
    legal = {move.uci() for move in board.legal_moves}
    matches = {}
    for line in _SEEDS:
        if (line.color != board.turn or offset >= len(line.moves)
                or line.moves[:offset] != history):
            continue
        uci = line.moves[offset]
        if uci in legal:
            matches.setdefault(uci, []).append(line)

    def rank(value):
        payload = [POLICY, str(seed), history, board.fen(en_passant="fen"), value]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":"))
                              .encode("utf-8")).digest()

    result = []
    # Order moves, not seed rows: shared initial e4 paths get no extra weight.
    for uci in sorted(matches, key=lambda value: (rank(value), value)):
        line = min(matches[uci], key=lambda value: (rank(value.line_id), value.line_id))
        result.append({"uci": uci, "name": line.name, "line_id": line.line_id,
                       "signature": offset == len(line.moves) - 1})
    return result
