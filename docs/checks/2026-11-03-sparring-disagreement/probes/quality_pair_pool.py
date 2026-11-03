"""Bounded native-only additional candidates for two live-game regressions."""
import json
import hashlib
from datetime import datetime, timezone
import chess
import chess.engine
import quality_pair_regressions as base


def run():
    saved=json.loads(base.SOURCE.read_text(encoding="utf-8"))
    native=chess.engine.SimpleEngine.popen_uci([str(base.probe.resolve_runtime_engine(base.ROOT)),"uci"],timeout=10,
        **({"creationflags":base.subprocess.CREATE_NO_WINDOW} if base.os.name=="nt" else {}))
    native.configure({"Hash":16,"UsePieceActivity":True})
    sf=base.Path(saved["config"]["stockfish"])
    out=base.Path(__file__).resolve().parent/"quality-pair-pool-v1.json"
    doc={"started_utc":datetime.now(timezone.utc).isoformat(),"source_sha256":hashlib.sha256(base.SOURCE.read_bytes()).hexdigest(),
         "native_configured_options":{**{k:v.default for k,v in native.options.items()},"Hash":16,"UsePieceActivity":True},
         "schedule_ms":[400,400,2500,500,400,400],"cases":[]}
    try:
        for ply in (33,39):
            row=saved["moves"][ply-1]
            board=chess.Board(saved["initial_fen"])
            for uci in saved["uci_moves"][:ply-1]:board.push_uci(uci)
            assert board.fen(en_passant="fen")==row["before_fen"]
            case={"ply":ply,"fen":board.fen(en_passant="fen"),"history_uci":[m.uci() for m in board.move_stack],"candidates":[]}
            excluded={row["uci"]}
            for budget in doc["schedule_ms"]:
                result=base.probe.search(native,board,chess.engine.Limit(time=budget/1000),board.turn,
                    [m for m in board.legal_moves if m.uci() not in excluded])
                uci=result["bestmove"]
                candidate={"excluded_uci":sorted(excluded),"discovery":result,"promotion":None}
                excluded.add(uci)
                if not result["completed"] or result["completed"]["depth"]<4:
                    candidate["promotion"]=base.probe.search(native,board,chess.engine.Limit(time=.4),board.turn,[chess.Move.from_uci(uci)])
                case["candidates"].append(candidate)
            for candidate in case["candidates"]:
                uci=candidate["discovery"]["bestmove"]
                child=board.copy(stack=True);child.push_uci(uci)
                candidate["independent_child"]=base.independent_audit(sf,child,board.turn,3000)
                print(json.dumps({"ply":ply,"uci":uci,"native_depth":(candidate["discovery"]["completed"] or {}).get("depth"),
                    "promoted_depth":((candidate["promotion"] or {}).get("completed") or {}).get("depth"),
                    "sf_cp_own":(candidate["independent_child"]["completed"] or {}).get("cp_own")}),flush=True)
            doc["cases"].append(case);base.probe.dump(out,doc)
    finally:native.quit()
    doc["finished_utc"]=datetime.now(timezone.utc).isoformat();base.probe.dump(out,doc)


if __name__=="__main__":run()
