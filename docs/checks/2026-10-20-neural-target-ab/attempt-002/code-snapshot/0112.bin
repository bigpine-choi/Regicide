"""Bounded real-engine replay and deterministic generation check.

The destination must be new. Both attempts and their raw evidence are retained.
This is data-generation validation, not a match or a strength claim.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.neural_search_data.generate import Config, generate
from scripts.neural_search_data.schema import load_dataset, sha256, write_json


def run(engine, output, *, nodes=10000, max_plies=48, max_seconds=120):
    engine, output = Path(engine).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = Config(nodes=nodes, max_plies=max_plies, sample_every=3,
                    max_searches=8 * max_plies, max_seconds=max_seconds)
    # This protocol is written before either engine is started.
    write_json(output / "protocol.json", {
        "schema_version": 1, "purpose": "Correctness and reproducibility of data generation only",
        "config": asdict(config), "engine_sha256": sha256(engine),
        "integration_script_sha256": sha256(Path(__file__)),
        "runs": 2, "selection": "All eight committed seed sources, no outcome-based selection",
        "requirements": ["byte-identical train/validation labels and semantic digest",
                         "both source kinds and both movers in each partition",
                         "no cross-partition family or feature overlap",
                         "no numeric WDL for unfinished games",
                         "fresh ucinewgame and fixed node command for every completed query",
                         "all source histories and PVs replay legally; root score sign correct"],
    })
    manifests = []
    for name in ("first", "repeat"):
        manifest = generate(engine, output / name, config=config)
        if manifest["status"] != "complete":
            raise AssertionError("Generation failed; evidence preserved: " + json.dumps(manifest))
        data = load_dataset(output / name)
        plan = json.loads((output / name / "plan.json").read_text(encoding="utf-8"))
        for source, expected in plan["provenance"]["source_sha256"].items():
            assert sha256(ROOT / source) == expected, "Source changed during generation: " + source
        for partition in ("train", "validation"):
            stats = manifest["stats"]["partitions"][partition]
            assert set(stats["kinds"]) == {"selfplay_start", "synthetic_endgame"}, stats
            assert set(stats["side_to_move"]) == {"white", "black"}, stats
            assert stats["at_most_12_pieces"] > 0, stats
            assert all("classical_static_stm_cp" in r for r in data[partition])
        assert not ({r["feature_hash"] for r in data["train"]} & {r["feature_hash"] for r in data["validation"]})
        assert not ({r["family_id"] for r in data["train"]} & {r["family_id"] for r in data["validation"]})
        records = [json.loads(line) for line in (output / name / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
        raw = [json.loads(line) for line in (output / name / "uci.jsonl").read_text(encoding="utf-8").splitlines()]
        for query in records:
            assert query["completed"] is True
            assert type(query["nodes"]) is int and 0 < query["nodes"] <= nodes
            block = [r for r in raw if query["uci_begin_seq"] <= r["seq"] <= query["uci_end_seq"]]
            sends = [r["line"] for r in block if r["direction"] == "send"]
            assert "ucinewgame" in sends, query["query_id"]
            assert "go nodes " + str(nodes) in sends, sends
            assert all("movetime" not in command for command in sends)
            position = "position " + ("startpos" if query["initial_fen"] ==
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" else "fen " + query["initial_fen"])
            if query["history"]:
                position += " moves " + " ".join(query["history"])
            assert position in sends, sends
            # Independently read the final native Regicide info line rather
            # than trusting agreement between two derived JSON records.
            receives = [r["line"] for r in block if r["direction"] == "recv"]
            infos = [line for line in receives if line.startswith("info depth ")]
            native = re.fullmatch(r"info depth (\d+) seldepth (\d+) score (cp|mate) (-?\d+) nodes (\d+) time (\d+)(?: pv (.+))?", infos[-1])
            assert native is not None, infos[-1]
            depth, seldepth, kind, value, count, _, pv = native.groups()
            assert query["score"] == {"kind": kind, "stm": int(value)}
            assert (query["depth"], query["seldepth"], query["nodes"]) == (int(depth), int(seldepth), int(count))
            assert query["pv_uci"] == (pv.split() if pv else [])
            assert receives[-1] == "bestmove " + query["bestmove_uci"]
        games = [json.loads(line) for line in (output / name / "games.jsonl").read_text(encoding="utf-8").splitlines()]
        assert all(g["result"] is None for g in games if g["termination"] != "outcome")
        assert manifest["cleanup"]["returncode_observed"]
        assert manifest["cleanup"]["returncode"] == 0, manifest["cleanup"]
        manifest["timing_summary"] = {"queries": len(records),
            "total_nodes": sum(q["nodes"] for q in records),
            "median_query_ms": statistics.median(q["wall_seconds"] for q in records) * 1000,
            "max_query_ms": max(q["wall_seconds"] for q in records) * 1000}
        manifests.append(manifest)
    for partition in ("train", "validation"):
        assert manifests[0]["partitions"][partition]["sha256"] == manifests[1]["partitions"][partition]["sha256"]
    assert manifests[0]["semantic_sha256"] == manifests[1]["semantic_sha256"]
    report = {"passed": True, "protocol_sha256": sha256(output / "protocol.json"),
              "semantic_sha256": manifests[0]["semantic_sha256"],
              "runs": [{"manifest_sha256": sha256(output / name / "manifest.json"),
                        "wall_seconds": manifest["wall_seconds"], "stats": manifest["stats"],
                        "timing_summary": manifest["timing_summary"]}
                       for name, manifest in zip(("first", "repeat"), manifests, strict=True)]}
    write_json(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--nodes", type=int, default=10000)
    parser.add_argument("--max-plies", type=int, default=48)
    parser.add_argument("--max-seconds", type=float, default=120)
    args = parser.parse_args()
    print(json.dumps(run(args.engine, args.output, nodes=args.nodes,
                         max_plies=args.max_plies, max_seconds=args.max_seconds), indent=2))
