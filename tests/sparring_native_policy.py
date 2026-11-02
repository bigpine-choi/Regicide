"""Native functional smoke with explicitly controlled past observations."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import chess
import chess.engine
import sparring_play
from sparring_play_tests import quality_game

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--reduced", action="store_true")
parser.add_argument("--fools-mate", action="store_true", help="Inspect shallow winning-mate avoidance without a full match")
parser.add_argument("--finish-from-archive", type=Path, help="Inspect move-70 rating-only analysis from archived game 8")
args = parser.parse_args()
BINARY = args.binary.resolve(strict=True)
cases = []
fixture_source = None
positions = [("accurate_control", *quality_game([0] * 6), [0] * 6),
             ("frequent_error_control", *quality_game([250] * 6), [250] * 6)]
if args.reduced:
    board, rows = chess.Board(), []
    line = "e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Nxd4 Qxd4 d6 Nc3 Nf6 Bg5 Be7 O-O-O O-O e5 dxe5 Qxd8 Rxd8 h3".split()
    for index, san in enumerate(line):
        prefix, fen = [move.uci() for move in board.move_stack], board.fen(en_passant="fen")
        move = board.parse_san(san)
        board.push(move)
        if index % 2 == 0 and index + 1 < len(line):
            rows.append({"before_fen": board.fen(en_passant="fen"), "history_uci": [m.uci() for m in board.move_stack],
                         "selected_uci": board.parse_san(line[index + 1]).uci(),
                         "opponent_observation": {"valid": True, "forced": False, "ply": index + 1,
                                                  "history_before": prefix, "before_fen": fen, "played_uci": move.uci(),
                                                  "loss_cp": 250, "depth": 6}})
    positions = [("reduced_material_frequent_error_control", board, rows, [250] * len(rows))]
if args.fools_mate:
    board = chess.Board()
    for san in "f3 e5 g4".split():
        board.push_san(san)
    positions = [("shallow_fools_mate_finite_alternative", board, [], [])]
if args.finish_from_archive:
    archive = args.finish_from_archive.resolve(strict=True)
    record_name = "ratings-2100-2500/game-008.json"
    with zipfile.ZipFile(archive) as saved:
        record = json.loads(saved.read(record_name))
    board = chess.Board()
    for uci in record["uci_moves"][:139]:
        board.push_uci(uci)
    assert len(board.move_stack) == 139 and not board.is_game_over(claim_draw=True)
    fixture_source = {"archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "record": record_name, "prefix_plies": 139,
                      "scope": "existing game prefix; only the next decision is newly searched"}
    positions = [("move70_baseline_with_rating_only_observation", board, [], [])]
for name, board, rows, losses in positions:
    with chess.engine.SimpleEngine.popen_uci([str(BINARY), "uci"], creationflags=subprocess.CREATE_NO_WINDOW) as engine:
        configured = {name: value for name, value in {"UsePieceActivity": True, "Threads": 1, "Hash": 16}.items()
                      if name in engine.options}
        engine.configure(configured)
        def search(budget, *, root_move=None, root_moves=None, publish=False, fresh=True, depth=None, position=None):
            current = board if position is None else position
            options = {"game": object(), "info": chess.engine.INFO_ALL}
            if root_move is not None:
                options["root_moves"] = [root_move]
            elif root_moves is not None:
                options["root_moves"] = root_moves
            packets, latest = {}, {}
            with engine.analysis(current.copy(stack=True), chess.engine.Limit(time=budget / 1000, depth=depth), **options) as analysis:
                for info in analysis:
                    if type(info.get("depth")) is int and "score" in info:
                        latest = dict(info)
                        packets[info["depth"]] = latest
                reply = analysis.wait()
            return reply, latest, packets
        started = time.monotonic()
        base, info, packets = search(1000)
        baseline_ms = (time.monotonic() - started) * 1000
        decision, discard = sparring_play.choose(board, base.move, info, packets, search, engine.close,
                                                decisions=rows, seed="20261102-sparring-native-policy",
                                                movetime_ms=1000)
        if args.finish_from_archive:
            assert decision["phase"] == "finish" and decision["selected_uci"] == base.move.uci()
            assert decision["analysis_only"] and decision["candidate_budget_ms"] == 0
            assert decision["extra_budget_ms"] == decision["quality_budget_ms"] == 500
            assert decision["probes"] == decision["discovery"] == []
        cases.append({"name": name, "fen": board.fen(), "controlled_past_loss_cp": losses,
                      "configured_options": configured,
                      "past_observation_scope": "test-controlled receipts, not measured skill or a played game",
                      "current_observation_scope": "the latest human move is measured by the real engine",
                      "baseline_elapsed_ms": round(baseline_ms, 3), "discard_engine": discard,
                      "decision": decision})
result = {"schema": "regicide.sparring-native-policy-smoke.v1", "binary": str(BINARY),
          "fixture_source": fixture_source,
          "binary_sha256": hashlib.sha256(BINARY.read_bytes()).hexdigest(),
          "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                            for name in ("scripts/sparring_play.py", "scripts/sparring_pacing.py", "scripts/evaluation_stability.py",
                                         "scripts/boss_play.py", "scripts/opening_variety.py")},
          "baseline_budget_ms": 1000, "maximum_extra_budget_ms": 2000,
          "scope": "native functional cases with controlled previous observations; not strength or human-feel evidence",
          "cases": cases}
path = args.output.resolve()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"output": str(path), "cases": [{"name": c["name"], "reason": c["decision"]["reason"],
     "baseline": c["decision"]["baseline_uci"], "selected": c["decision"]["selected_uci"],
     "loss_cp": c["decision"]["loss_cp"], "target": c["decision"]["target_loss_cp"],
     "band_control": c["decision"]["band_control"], "comparison_mode": c["decision"]["comparison_mode"],
     "stability": c["decision"]["reliability"], "extra_ms": c["decision"]["extra_elapsed_ms"]} for c in cases]}, indent=2))
