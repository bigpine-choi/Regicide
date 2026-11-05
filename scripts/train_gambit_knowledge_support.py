"""Extend declared named defenses using only bounded Regicide searches."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import chess
import chess.engine

import gambit_knowledge_support as support
from neural_search_data.engine import _RawLog, _logging_protocol

ROOT = Path(__file__).resolve().parents[1]


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def research(binary, output, *, depth=7, nodes=1000000):
    output.mkdir(parents=True, exist_ok=False)
    raw_catalog = (support.DIRECTORY / "catalog.json").read_bytes()
    catalog = json.loads(raw_catalog)
    seeds = json.loads((support.DIRECTORY / "native_continuation_seeds.json").read_bytes())
    sources = {row["line_id"]: row for row in catalog["lines"]}
    identity = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "evaluation_id": "classical_activity_v1"}
    if identity["binary_sha256"] != "5011da4c5e6ef01c452bd7125e4e22b372d2c9cfb858ed8759057325acb0e6ba":
        raise ValueError("This declared study requires the pinned deployed native baseline")
    log = _RawLog(output / "native-uci.jsonl")
    queries, entries, cache, branches = {}, {}, {}, []
    started = datetime.now(timezone.utc).isoformat()
    engine = chess.engine.SimpleEngine.popen(_logging_protocol(log), [str(binary), "uci"], timeout=20)
    options = {"Hash":16, "Move Overhead":0, "UsePieceActivity":True,
               "UseNeuralEvaluation":False, "UseNNUEEvaluation":False}
    engine.configure(options)

    def query(board, roots=None):
        history = [move.uci() for move in board.move_stack]
        allowed = sorted(move.uci() for move in roots) if roots is not None else None
        key = support.digest([history, allowed, nodes, depth])
        if key in cache:
            return cache[key]
        begin = log.seq + 1
        with engine.analysis(board.copy(stack=True), chess.engine.Limit(depth=depth, nodes=nodes),
                             game=object(), root_moves=roots, info=chess.engine.INFO_ALL) as analysis:
            packets = []
            for info in analysis:
                if "depth" in info and "score" in info and info.get("pv") and not info.get("lowerbound") and not info.get("upperbound"):
                    packets.append(info)
            reply = analysis.wait()
        if not packets or reply.move is None:
            raise ValueError("No completed native query")
        info = packets[-1]
        pv = [move.uci() for move in info["pv"]]
        if info["depth"] < 4 or reply.move.uci() != pv[0]:
            raise ValueError("Native query did not complete minimum comparable depth")
        replay = board.copy(stack=True)
        for uci in pv:
            replay.push_uci(uci)
        score = info["score"].pov(board.turn)
        record = {"query_id":key, "history_uci":history, "fen":board.fen(en_passant="fen"),
                  "root_moves":allowed, "bestmove_uci":reply.move.uci(), "pv_uci":pv,
                  "depth":info["depth"], "nodes":info.get("nodes"), "requested_depth":depth,
                  "requested_nodes":nodes, "score":{"kind":"mate" if score.is_mate() else "cp",
                  "stm":score.mate() if score.is_mate() else score.score()},
                  "lowerbound":False,"upperbound":False,"completed":True,
                  "uci_begin_seq":begin,"uci_end_seq":log.seq}
        queries[key] = record
        cache[key] = record
        return record

    def owner_turn(board, seed):
        if board.is_game_over(claim_draw=True):
            return None
        row = query(board)
        if not board.is_check() and row["score"]["kind"] == "cp":
            history = [move.uci() for move in board.move_stack]
            record_id = support.digest([seed["line_id"], history])
            entries[record_id] = {"record_id":record_id,"source_line_id":seed["line_id"],
                "family_id":seed["family_id"],"name":seed["name"] + " · native continuation",
                "owner":seed["owner"],"history_uci":history,"fen":board.fen(en_passant="fen"),
                "position_key":support.position_key(board),"uci":row["bestmove_uci"],
                "query_id":row["query_id"],"engine_identity":identity}
        board.push_uci(row["bestmove_uci"])
        return row

    try:
        for line_id in seeds["source_line_ids"]:
            seed = sources[line_id]
            board = support.board_for(seed["moves"])
            owner = seed["owner"] == "white"
            if board.turn != owner:
                board.push_uci(query(board)["bestmove_uci"])
            if owner_turn(board, seed) is None or board.is_game_over(claim_draw=True):
                continue
            defense = query(board)
            alternatives = [move for move in board.legal_moves if move.uci() != defense["bestmove_uci"]]
            replies = [defense]
            if alternatives:
                replies.append(query(board, alternatives))
            for defense_index, response in enumerate(replies):
                branch = board.copy(stack=True)
                branch.push_uci(response["bestmove_uci"])
                branches.append({"source_line_id":line_id,"defense_index":defense_index,
                    "defense_query_id":response["query_id"],"history_uci":[m.uci() for m in branch.move_stack]})
                for turn in range(2):
                    if owner_turn(branch, seed) is None or turn == 1 or branch.is_game_over(claim_draw=True):
                        break
                    branch.push_uci(query(branch)["bestmove_uci"])
            print(json.dumps({"case":line_id,"queries":len(queries),"entries":len(entries)}),flush=True)
    finally:
        engine.quit()
        returncode = engine.returncode.result(timeout=3)
        log.close()
        if returncode != 0:
            raise ValueError("Owned native engine did not exit normally")
    data = {"schema":support.SCHEMA,"policy":support.POLICY,"origin":"Regicide own search only",
        "base_catalog_sha256":hashlib.sha256(raw_catalog).hexdigest(),"engine_identity":identity,
        "options":options,"started_utc":started,"finished_utc":datetime.now(timezone.utc).isoformat(),
        "seeds":seeds,"source_prefixes":{line_id:{key:sources[line_id][key] for key in
            ("source_url", "source_license", "catalog_commit", "name", "moves", "family_id", "owner")}
            for line_id in seeds["source_line_ids"]},
        "branches":branches,"queries":queries,"entries":list(entries.values()),
        "counts":{"seeds":len(seeds["source_line_ids"]),"branches":len(branches),"queries":len(queries),
                  "entries":len(entries),"families":dict(Counter(row["family_id"] for row in entries.values())),
                  "max_history_plies":max(len(row["history_uci"]) for row in entries.values()),
                  "completed_depths":dict(Counter(row["depth"] for row in queries.values()))},
        "native_uci_sha256":hashlib.sha256((output / "native-uci.jsonl").read_bytes()).hexdigest(),
        "scope":"Development coverage only; restricted defense scores are not unrestricted labels; no external engine"}
    data["support_id"] = support.digest(data)
    write(output / "native_continuations.json", data)
    manifest = {"schema":support.SCHEMA,"data_sha256":hashlib.sha256((output / "native_continuations.json").read_bytes()).hexdigest(),
                "base_catalog_sha256":data["base_catalog_sha256"],"support_id":data["support_id"],
                "engine_returncode":returncode,"counts":data["counts"]}
    write(output / "native_continuations_manifest.json", manifest)
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--nodes",type=int,default=1000000)
    parser.add_argument("--depth",type=int,default=7)
    args = parser.parse_args()
    if not 4 <= args.depth <= 12 or not 10000 <= args.nodes <= 10000000:
        parser.error("Bounded depth 4..12 and nodes 10000..10000000 required")
    print(json.dumps(research(args.binary.resolve(),args.output.resolve(),depth=args.depth,nodes=args.nodes)["counts"]))
