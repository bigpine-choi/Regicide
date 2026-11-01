from pathlib import Path
import hashlib
import json
import sys
import chess

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from opening_guide import _catalog, _display_catalog, get_opening_guide
from opening_variety import plan

def stamp(catalog):
    value={'moves':[row.moves for row in catalog.openings],
           'positions':dict(catalog.positions),'named':dict(catalog.named)}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

base=_catalog()
before=stamp(base)
boards=[]
for san in ['', 'd4 d6 c4', 'd4 d6 c4 g6', 'Nf3 Nf6 c4 g6 d4 Bg7 Nc3 d5']:
    board=chess.Board()
    for token in san.split(): board.push_san(token)
    boards.append(board)
plans=[plan(board,1000,'0123456789abcdef0123456789abcdef') for board in boards]
for board in boards: get_opening_guide(board)
view=_display_catalog(base)
assert _catalog() is base and stamp(base)==before
assert len(base.openings)==3865 and len(base.positions)==7983
assert len(view.openings)==3866
assert plans==[plan(board,1000,'0123456789abcdef0123456789abcdef') for board in boards]
modern=get_opening_guide(boards[2])['current_matches'][0]
assert modern['name']=='Modern Defense' and modern['eco']=='' and modern['name_origin']=='supplemental'
assert modern['name_source_url']=='https://www.chess.com/openings/Modern-Defense'
result={'status':'passed','base_rows':len(base.openings),'base_positions':len(base.positions),
        'display_rows':len(view.openings),'display_positions':len(view.positions),
        'base_structure_sha256_before_and_after':before,'candidate_plans_preserved':len(plans),
        'modern_named_with_source_and_without_invented_eco':True,
        'scope':'Read-only index and proposal-plan comparison; no native engine searches'}
Path(__file__).with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result))
