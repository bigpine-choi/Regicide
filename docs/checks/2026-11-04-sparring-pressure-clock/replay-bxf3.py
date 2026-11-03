"""Reproduce a recorded incumbent using real native discovery and a real referee.

Example from the repository root:
  .venv-tests/Scripts/python.exe docs/checks/2026-11-04-sparring-pressure-clock/replay-bxf3.py --output docs/checks/2026-11-04-sparring-pressure-clock/bxf3-replay-rerun.json

The recorded move is fixed input, not a claim that timed native search always
chooses it. Reference-engine recommendations never enter candidate discovery.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
import chess
import sparring_quality_native_integration as integration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, default=ROOT / "docs/matches/2026-11-04-pressure-clock-v2/black/game.json")
    parser.add_argument("--ply", type=int, default=44)
    parser.add_argument("--budget-ms", type=int, default=6922)
    parser.add_argument("--audit-ms", type=int, default=3000)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; choose a new output path to preserve prior evidence.")
    saved = json.loads(args.game.read_text(encoding="utf-8"))
    row = saved["moves"][args.ply - 1]
    board = chess.Board(saved["initial_fen"])
    for uci in saved["uci_moves"][:args.ply - 1]:
        board.push_uci(uci)
    assert board.fen(en_passant="fen") == row["before_fen"]
    assert chess.Move.from_uci(row["uci"]) in board.legal_moves
    before = integration.source_hashes()
    production_verify = integration.sparring_quality.verify
    # Reuse the production-module replay callbacks while applying the exact
    # recorded clock-aware budget instead of that helper's 8000ms default.
    with patch.object(integration.sparring_quality, "verify", side_effect=lambda *items, **options:
                      production_verify(*items, **{**options, "budget_ms": args.budget_ms})):
        result = integration.run_case({
            "id": f"recorded-ply-{args.ply}-quality-v5",
            "board": board, "original_uci": row["uci"],
            "scope": "fixed recorded incumbent; actual native search and real referee; independent audits occur after selection",
            "input_game": str(args.game.resolve()),
            "input_game_sha256": hashlib.sha256(args.game.read_bytes()).hexdigest(),
            "production_guard_budget_ms": args.budget_ms,
            "independent_audit_ms_each": args.audit_ms,
        }, Path(saved["config"]["binary"]), Path(saved["config"]["stockfish"]), args.audit_ms)
    result.update(source_before=before, source_after=integration.source_hashes())
    result["source_unchanged"] = result["source_before"] == result["source_after"]
    verification = result["verification"]
    # Preserve strict_pass and the actual replaced/mitigated label. A separate
    # functional result can pass when damage limitation independently meets
    # the deeper floor, without pretending the short-reference floor passed.
    functional = bool(verification["original_replaced"] and verification["native_only"]
        and verification["independent_floor_met"]
        and verification["independent_improvement_cp"] > 100
        and result["receipt"]["status"] in ["replaced", "mitigated"])
    result["functional_repair_pass"] = functional
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"selected": result["selected_uci"], "status": result["receipt"]["status"],
        "functional_repair_pass": functional, "strict_guard_pass": verification["strict_pass"],
        "independent": verification["scores_cp_own"], "source_unchanged": result["source_unchanged"]}))
    return 0 if functional and result["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
