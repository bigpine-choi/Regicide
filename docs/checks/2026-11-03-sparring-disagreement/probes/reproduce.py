"""Bounded fresh-search disagreement probes; production files are read-only."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
import chess
import chess.engine
from runtime_engine import resolve_runtime_engine

SAVED = ROOT / "docs/matches/2026-11-03-sparring-validation"
OUT = Path(__file__).resolve().parent / "activity-enabled"
OUT.mkdir(exist_ok=True)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def packet(info, own):
    result = {k: info[k] for k in ("depth", "seldepth", "nodes", "time", "nps", "hashfull", "upperbound", "lowerbound") if k in info}
    if "score" in info:
        score = info["score"].pov(own)
        result.update(cp_own=score.score(), mate_own=score.mate(), raw_score=str(info["score"]))
    if "pv" in info:
        result["pv_uci"] = [m.uci() for m in info["pv"]]
    return result


def search(engine, board, limit, own, root_moves=None):
    start = time.perf_counter()
    packets = []
    with engine.analysis(board, limit, game=object(), info=chess.engine.INFO_ALL, root_moves=root_moves) as result:
        for info in result:
            if "depth" in info and "score" in info and "pv" in info:
                packets.append(packet(info, own))
        best = result.wait()
    elapsed = round((time.perf_counter() - start) * 1000, 3)
    exact = [p for p in packets if not p.get("upperbound") and not p.get("lowerbound") and p.get("pv_uci")]
    chosen = best.move.uci() if best.move else None
    matching = [p for p in exact if p["pv_uci"][0] == chosen]
    completed = max(matching, key=lambda p: p["depth"]) if matching else None
    return {"bestmove": chosen, "elapsed_ms": elapsed, "completed": completed,
            "limit": {k: getattr(limit, k) for k in ("time", "depth", "nodes") if getattr(limit, k) is not None},
            "packets": packets}


def cases():
    reviewed = json.loads((SAVED / "reviewed-cases.json").read_text(encoding="utf-8"))["cases"]
    result = []
    for game, ply in (("1700-white", 99), ("1700-black", 86), ("1320-black", 30), ("1320-black", 42), ("1320-white", 53)):
        case = next(c for c in reviewed if c["game"] == game and c["ply"] == ply)
        source = SAVED / game / "game.json"
        saved = json.loads(source.read_text(encoding="utf-8"))
        row = saved["moves"][ply - 1]
        board = chess.Board(saved["initial_fen"])
        for uci in saved["uci_moves"][:ply - 1]:
            board.push_uci(uci)
        assert board.fen(en_passant="fen") == case["fen"]
        decision = row["sparring_decision"]
        result.append({"id": game + "-" + str(ply), "board": board, "original": case["move_uci"],
                       "recorded": {"depth": case["own_baseline_depth"], "cp_own": case["own_baseline_cp_own"],
                                    "baseline_ms": decision["baseline_budget_ms"], "ref_best": case["independent_bestmove"]},
                       "source": str(source.relative_to(ROOT)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    board = chess.Board()
    for uci in "b1c3 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 c6d4 d1d4 g8f6 c3b5 f8e7 c1f4 d7d6 e1c1 e8g8".split():
        board.push_uci(uci)
    result.append({"id": "original-knight-17", "board": board, "original": "b5a7",
                   "recorded": {"depth": 5, "cp_own": 17, "baseline_ms": 1000, "ref_best": None},
                   "source": "User game prefix reproduced in prior tactical safety validation"})
    return result


def run(selected_ids):
    metadata = json.loads((SAVED / "1700-white/metadata.json").read_text(encoding="utf-8"))
    binary = resolve_runtime_engine(ROOT)
    sf_path = Path(metadata["config"]["stockfish"])
    launch = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    native = chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=15, **launch)
    sf = chess.engine.SimpleEngine.popen_uci(str(sf_path), timeout=15, **launch)
    native.configure({k: v for k, v in {"Threads": 1, "Hash": 16, "UsePieceActivity": True}.items() if k in native.options})
    sf.configure({"Threads": 1, "Hash": 32, "UCI_LimitStrength": False})
    receipt = {"started_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
               "platform": platform.platform(), "native": {"path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "id": native.id,
                   "configured_options": {**{k:v.default for k,v in native.options.items()}, "Hash":16, "UsePieceActivity":True}},
               "stockfish": {"path": str(sf_path), "sha256": hashlib.sha256(sf_path.read_bytes()).hexdigest(), "id": sf.id},
               "protocol": "Production HumanGame classical options: UsePieceActivity=True, native Hash16; SF Hash32 Threads1. Fresh ucinewgame per search. No production edits; timed probes under ordinary machine load. Fixed-depth probes also have 8-second caps; only completed depths count. SF comparisons 200000 nodes each.",
               "cases": []}
    try:
        for case in cases():
            if selected_ids and case["id"] not in selected_ids:
                continue
            board = case.pop("board")
            own = board.turn
            case.update(fen=board.fen(en_passant="fen"), root_fen=board.root().fen(en_passant="fen"),
                        history_uci=[m.uci() for m in board.move_stack], own="white" if own else "black")
            case["native_root"] = {}
            for name, limit in (("recorded_budget", chess.engine.Limit(time=case["recorded"]["baseline_ms"] / 1000)),
                                ("3000ms", chess.engine.Limit(time=3)), ("6000ms", chess.engine.Limit(time=6)),
                                ("depth7_max8s", chess.engine.Limit(depth=7, time=8)),
                                ("depth8_max8s", chess.engine.Limit(depth=8, time=8))):
                case["native_root"][name] = search(native, board, limit, own)
            child = board.copy(stack=True); child.push_uci(case["original"])
            case["native_original_child_3000ms"] = search(native, child, chess.engine.Limit(time=3), own)
            case["sf_root"] = search(sf, board, chess.engine.Limit(nodes=200000), own)
            candidates = set([case["original"], case["sf_root"]["bestmove"]])
            candidates.update(r["bestmove"] for r in case["native_root"].values())
            case["sf_children"] = {}
            for uci in sorted(candidates):
                child = board.copy(stack=True); child.push_uci(uci)
                case["sf_children"][uci] = search(sf, child, chess.engine.Limit(nodes=200000), own)
            dump(OUT / (case["id"] + ".json"), case)
            receipt["cases"].append(case["id"])
            dump(OUT / "manifest.json", receipt)
            summary = {"id": case["id"], "native": {k: {"move": v["bestmove"], "depth": (v["completed"] or {}).get("depth"), "cp": (v["completed"] or {}).get("cp_own"), "ms": v["elapsed_ms"]} for k, v in case["native_root"].items()},
                       "sf_root": case["sf_root"]["completed"], "sf_child_cp": {k: v["completed"]["cp_own"] if v["completed"] else None for k, v in case["sf_children"].items()}}
            print(json.dumps(summary), flush=True)
    finally:
        native.quit(); sf.quit()
    receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
    dump(OUT / "manifest.json", receipt)


if __name__ == "__main__":
    import subprocess
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", default=[])
    run(parser.parse_args().case)
