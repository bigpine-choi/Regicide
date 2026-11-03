"""Bounded 43...Bxc3 discovery breadth probe, independent native candidates."""
import json
import os
from pathlib import Path
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
    case=next(c for c in p.cases() if c['id']=='1700-black-86')
    board=case['board']; own=board.turn
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'options':{'Hash':16,'UsePieceActivity':True},'case':case['id'],'fen':board.fen(en_passant='fen'),'history_uci':[m.uci() for m in board.move_stack],'original':case['original'],'budgets':{}}
    try:
        for ms in (400,900):
            exclude={case['original']}; rows=[]
            for index in range(8):
                legal=[m for m in board.legal_moves if m.uci() not in exclude]
                result=p.search(native,board,chess.engine.Limit(time=ms/1000),own,root_moves=legal)
                uci=result['bestmove']; assert uci not in exclude; exclude.add(uci)
                child=board.copy(stack=True); child.push_uci(uci)
                audit=p.search(sf,child,chess.engine.Limit(nodes=200000),own)
                row={'index':index+1,'native':result,'stockfish_child':audit}; rows.append(row)
                print(json.dumps({'budget_ms':ms,'index':index+1,'move':uci,'depth':(result['completed'] or {}).get('depth'),'native_cp':(result['completed'] or {}).get('cp_own'),'sf_cp':(audit['completed'] or {}).get('cp_own')}),flush=True)
            report['budgets'][str(ms)]=rows
            p.dump(p.OUT/'candidate-tail-43.json',report)
    finally:
        native.quit(); sf.quit()


if __name__=='__main__': run()
