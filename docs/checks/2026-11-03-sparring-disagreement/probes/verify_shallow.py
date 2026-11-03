"""Complete exact-depth evidence for already-native-generated candidates."""
import json
import os
import subprocess
from datetime import datetime, timezone
import reproduce as p
import chess
import chess.engine


def run():
    launch={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    native=chess.engine.SimpleEngine.popen_uci([str(p.resolve_runtime_engine(p.ROOT)),'uci'],timeout=15,**launch)
    native.configure({'Hash':16,'UsePieceActivity':True})
    schedule=json.loads((p.OUT/'repair-schedule.json').read_text(encoding='utf-8'))
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'native_configured_options':{**{k:v.default for k,v in native.options.items()},'Hash':16,'UsePieceActivity':True},'rows':[]}
    try:
        for identifier,uci in [('1320-black-30','e7e6'),('1320-black-42','d6d5'),('original-knight-17','d4c4')]:
            evidence=next(c for c in schedule['cases'] if c['id']==identifier)
            prior=next(c for c in evidence['candidates'] if c['native']['bestmove']==uci)
            board=next(c['board'] for c in p.cases() if c['id']==identifier)
            row={'id':identifier,'uci':uci,'native_provenance':{'file':'repair-schedule.json','candidate_index':prior['index']},'queries':{}}
            for ms in (400,900):
                row['queries'][str(ms)]=p.search(native,board,chess.engine.Limit(time=ms/1000),board.turn,root_moves=[chess.Move.from_uci(uci)])
            report['rows'].append(row)
            print(json.dumps({'id':identifier,'uci':uci,'queries':{k:{'depth':(v['completed'] or {}).get('depth'),'cp':(v['completed'] or {}).get('cp_own'),'ms':v['elapsed_ms']} for k,v in row['queries'].items()}}),flush=True)
    finally: native.quit()
    p.dump(p.OUT/'verify-shallow.json',report)


if __name__=='__main__':run()
