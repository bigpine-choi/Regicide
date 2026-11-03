"""Native-only cheap root exclusion and legal from-square diversification."""
import json
from datetime import datetime, timezone
import chess
import chess.engine
import quality_pair_regressions as base


def run():
    saved=json.loads(base.SOURCE.read_text(encoding="utf-8"))
    native=chess.engine.SimpleEngine.popen_uci([str(base.probe.resolve_runtime_engine(base.ROOT)),"uci"],timeout=10,
        **({"creationflags":base.subprocess.CREATE_NO_WINDOW} if base.os.name=="nt" else {}))
    native.configure({"Hash":16,"UsePieceActivity":True})
    out=base.Path(__file__).resolve().parent/"quality-native-breadth-v1.json"
    doc={"started_utc":datetime.now(timezone.utc).isoformat(),
         "native_configured_options":{**{k:v.default for k,v in native.options.items()},"Hash":16,"UsePieceActivity":True},
         "protocol":"No external move input: root exclusions from original incumbent only; independently grouped legal roots by from-square. Diagnostic candidates only, no SF selection.","cases":[]}
    try:
        for ply in (33,39):
            row=saved["moves"][ply-1];board=chess.Board(saved["initial_fen"])
            for uci in saved["uci_moves"][:ply-1]:board.push_uci(uci)
            case={"ply":ply,"fen":board.fen(en_passant="fen"),"history_uci":[m.uci() for m in board.move_stack],"exclusion":{},"from_square_groups":[]}
            for ms in (100,200):
                excluded={row["uci"]};runs=[]
                for _ in range(16):
                    result=base.probe.search(native,board,chess.engine.Limit(time=ms/1000),board.turn,
                        [m for m in board.legal_moves if m.uci() not in excluded])
                    runs.append({"excluded_uci":sorted(excluded),**result});excluded.add(result["bestmove"])
                case["exclusion"][str(ms)]=runs
                print(json.dumps({"ply":ply,"ms":ms,"native_ranks":[(r["bestmove"],(r["completed"] or {}).get("depth")) for r in runs]}),flush=True)
            groups={}
            for move in board.legal_moves:
                if move.uci()!=row["uci"]:groups.setdefault(move.from_square,[]).append(move)
            for square,allowed in sorted(groups.items()):
                result=base.probe.search(native,board,chess.engine.Limit(time=.1),board.turn,allowed)
                case["from_square_groups"].append({"from_square":chess.square_name(square),"allowed_uci":[m.uci() for m in allowed],**result})
            print(json.dumps({"ply":ply,"groups":[(r["from_square"],r["bestmove"],(r["completed"] or {}).get("depth")) for r in case["from_square_groups"]]}),flush=True)
            doc["cases"].append(case);base.probe.dump(out,doc)
    finally:native.quit()
    doc["finished_utc"]=datetime.now(timezone.utc).isoformat();base.probe.dump(out,doc)


if __name__=="__main__":run()
