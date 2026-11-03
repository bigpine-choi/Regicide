"""Probe proposed production repair slots without Stockfish-proposed roots."""
import json
import os
import subprocess
from datetime import datetime, timezone
import reproduce as p
import chess
import chess.engine


def run():
    meta=json.loads((p.SAVED/'1700-white/metadata.json').read_text(encoding='utf-8'))
    launch={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    native=chess.engine.SimpleEngine.popen_uci([str(p.resolve_runtime_engine(p.ROOT)),'uci'],timeout=15,**launch)
    sf=chess.engine.SimpleEngine.popen_uci(meta['config']['stockfish'],timeout=15,**launch)
    native.configure({'Hash':16,'UsePieceActivity':True})
    sf.configure({'Threads':1,'Hash':32,'UCI_LimitStrength':False})
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'native_configured_options':{**{k:v.default for k,v in native.options.items()},'Hash':16,'UsePieceActivity':True},'schedule_ms':[400,400,2500,500],'cases':[]}
    try:
        for case in p.cases():
            board=case['board']; own=board.turn; exclude={case['original']}; rows=[]
            for index,ms in enumerate(report['schedule_ms']):
                legal=[m for m in board.legal_moves if m.uci() not in exclude]
                result=p.search(native,board,chess.engine.Limit(time=ms/1000),own,root_moves=legal)
                uci=result['bestmove']; assert uci not in exclude; exclude.add(uci)
                child=board.copy(stack=True); child.push_uci(uci)
                audit=p.search(sf,child,chess.engine.Limit(nodes=200000),own)
                rows.append({'index':index+1,'budget_ms':ms,'native':result,'stockfish_child':audit})
            item={'id':case['id'],'fen':board.fen(en_passant='fen'),'history_uci':[m.uci() for m in board.move_stack],'original':case['original'],'candidates':rows}
            report['cases'].append(item)
            p.dump(p.OUT/'repair-schedule.json',report)
            print(json.dumps({'id':case['id'],'candidates':[{'budget_ms':r['budget_ms'],'move':r['native']['bestmove'],'depth':(r['native']['completed'] or {}).get('depth'),'native_cp':(r['native']['completed'] or {}).get('cp_own'),'sf_child_cp':(r['stockfish_child']['completed'] or {}).get('cp_own')} for r in rows]}),flush=True)
    finally:
        native.quit(); sf.quit()
    report['finished_utc']=datetime.now(timezone.utc).isoformat()
    p.dump(p.OUT/'repair-schedule.json',report)


if __name__=='__main__': run()
