from pathlib import Path
import json
import sys
import chess
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from opening_guide import get_opening_guide
data=json.loads((Path(__file__).with_name('expected-cases.json')).read_text(encoding='utf-8'))
rows=[]
for case in data['cases']:
    b=chess.Board()
    for move in case['uci']: b.push_uci(move)
    guide=get_opening_guide(b)
    rows.append({'id':case['id'],'status':guide['status'],'current':[m['name'] for m in guide['current_matches']],
                 'last':guide['matched_opening'],'sides':guide.get('sides')})
Path(__file__).with_name('observed-names.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
for row in rows:
    print(json.dumps({k:row[k] for k in ['id','current','last']},ensure_ascii=True))
