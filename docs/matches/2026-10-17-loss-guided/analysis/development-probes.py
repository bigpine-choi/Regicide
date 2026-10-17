import hashlib,json,datetime,subprocess
from pathlib import Path
import chess,chess.engine
R=next(p for p in Path(__file__).resolve().parents if (p/'CMakeLists.txt').exists())
old=R/'build-strength-baseline/source/build/regicide.exe'; new=R/'build/regicide.exe'
games=json.loads((R/'docs/matches/2026-10-17-hash-memory/time100-retry.json').read_text())['games']
out={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'limits':{'depth':3},'purpose':'development fixtures, not heldout strength evidence','binaries':{},'records':[]}
for name,binary,opts in [('baseline',old,{}),('both',new,{}),('checks_only',new,{'UseQuietMoveOrdering':False}),('ordering_only',new,{'UseQuiescenceChecks':False}),('both_off',new,{'UseQuietMoveOrdering':False,'UseQuiescenceChecks':False})]:
    out['binaries'][name]={'path':str(binary),'sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'options':opts}
    with chess.engine.SimpleEngine.popen_uci([str(binary),'uci'],timeout=30) as e:
        e.configure(opts)
        for gid,ply in [(7,41),(10,44),(11,59),(13,17)]:
            g=next(g for g in games if g['game']==gid); b=chess.Board()
            for text in g['uci_moves'][:ply-1]: b.push_uci(text)
            info=e.analyse(b,chess.engine.Limit(depth=3),game=object())
            pv=info['pv']; check=b.copy()
            for m in pv:
                assert m in check.legal_moves
                check.push(m)
            record={'configuration':name,'game':gid,'ply':ply,'fen':b.fen(en_passant='fen'),'history':g['uci_moves'][:ply-1],'recorded_move':g['uci_moves'][ply-1],'best_move':pv[0].uci(),'score':str(info['score'].pov(b.turn)),'depth':info['depth'],'nodes':info['nodes'],'seldepth':info.get('seldepth'),'pv':[m.uci() for m in pv]}
            out['records'].append(record)
            print(name,gid,record['best_move'],record['score'],record['nodes'],flush=True)
out['finished_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
(R/'build/task06-evidence/development-probes.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
