"""Native-only root exclusions; Stockfish scores each already-generated child."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
import reproduce as p
import chess
import chess.engine


def run():
    metadata=json.loads((p.SAVED/'1700-white/metadata.json').read_text(encoding='utf-8'))
    binary=p.resolve_runtime_engine(p.ROOT)
    sf_path=Path(metadata['config']['stockfish'])
    launch={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    native=chess.engine.SimpleEngine.popen_uci([str(binary),'uci'],timeout=15,**launch)
    sf=chess.engine.SimpleEngine.popen_uci(str(sf_path),timeout=15,**launch)
    native.configure({k:v for k,v in {'Threads':1,'Hash':16,'UsePieceActivity':True}.items() if k in native.options})
    sf.configure({'Threads':1,'Hash':32,'UCI_LimitStrength':False})
    report={'started_utc':datetime.now(timezone.utc).isoformat(),'native_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'stockfish_sha256':hashlib.sha256(sf_path.read_bytes()).hexdigest(),
            'native_configured_options':{**{k:v.default for k,v in native.options.items()},'Hash':16,'UsePieceActivity':True},
            'protocol':'Production UsePieceActivity=True. Three sequential native root-exclusion searches, 900ms each, fresh game state each. Only legal roots minus previously returned moves. No Stockfish move is provided to native. SF200000-node independent child checks. May overlap other bounded native probes on separate CPU threads.', 'cases':[]}
    try:
        for case in p.cases():
            board=case['board']; own=board.turn
            exclude={case['original']}
            item={'id':case['id'],'fen':board.fen(en_passant='fen'),'history_uci':[m.uci() for m in board.move_stack], 'original':case['original'],'candidates':[]}
            for index in range(3):
                legal=[m for m in board.legal_moves if m.uci() not in exclude]
                result=p.search(native,board,chess.engine.Limit(time=.9),own,root_moves=legal)
                uci=result['bestmove']
                assert uci not in exclude
                exclude.add(uci)
                child=board.copy(stack=True); child.push_uci(uci)
                audit=p.search(sf,child,chess.engine.Limit(nodes=200000),own)
                item['candidates'].append({'index':index,'native':result,'stockfish_child':audit})
            report['cases'].append(item)
            p.dump(p.OUT/'native-alternatives.json',report)
            print(json.dumps({'id':item['id'],'candidates':[{'move':r['native']['bestmove'],'depth':(r['native']['completed'] or {}).get('depth'),'native_cp':(r['native']['completed'] or {}).get('cp_own'),'ms':r['native']['elapsed_ms'],'sf_child_cp':(r['stockfish_child']['completed'] or {}).get('cp_own')} for r in item['candidates']]}),flush=True)
    finally:
        native.quit(); sf.quit()
    report['finished_utc']=datetime.now(timezone.utc).isoformat()
    p.dump(p.OUT/'native-alternatives.json',report)


if __name__=='__main__': run()
