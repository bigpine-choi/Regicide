from pathlib import Path
from collections import Counter
import argparse
import hashlib
import json
import sys
import time
import unicodedata

import chess

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from opening_guide import get_opening_guide, _catalog

STATUSES={'confirmed','position','last_known','pending','unknown','unavailable'}

def normalize(value):
    return unicodedata.normalize('NFKD',value or '').encode('ascii','ignore').decode().lower()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def board_from(case):
    board=chess.Board()
    for move in case['uci']:
        parsed=chess.Move.from_uci(move)
        assert parsed in board.legal_moves, case['id']
        board.push(parsed)
    assert board.fen(en_passant='fen')==case['fen']
    return board

def inspect(board):
    before=(board.fen(en_passant='fen'),tuple(board.move_stack))
    guide=get_opening_guide(board)
    assert before==(board.fen(en_passant='fen'),tuple(board.move_stack)), 'Mutated inspected board'
    sides=guide['sides']
    assert isinstance(sides['note'],str)
    for color in ('white','black'):
        value=sides[color]
        assert set(value)=={'name','status','detail','matched_ply'}, value
        assert value['status'] in STATUSES, value
        assert value['name'] is None or isinstance(value['name'],str) and value['name'], value
        assert isinstance(value['detail'],str) and value['detail'], value
        if value['matched_ply'] is not None:
            assert type(value['matched_ply']) is int and 0<=value['matched_ply']<=len(board.move_stack)
        if value['status']=='last_known':
            assert value['name'] and value['matched_ply']<len(board.move_stack)
            assert '마지막' in value['detail']
        if value['status']=='position':
            assert guide['history_scope']=='custom'
            assert '실제' in value['detail'] or '이전' in value['detail']
        if guide['history_scope']=='custom':
            assert value['status']!='confirmed', value
    return guide

def run(args):
    source_names=['scripts/opening_guide.py','scripts/opening_sides.py','scripts/opening_names.py']
    before_sources={name:sha(ROOT/name) for name in source_names}
    source=Path(__file__).with_name('expected-cases.json')
    cases=json.loads(source.read_text(encoding='utf-8'))['cases']
    failures=[]
    rows=[]
    checked=Counter()
    for case in cases:
        board=board_from(case)
        guide=inspect(board)
        sides=guide['sides']
        errors=[]
        for color in ('white','black'):
            expected=case[color+'_any']
            name=normalize(sides[color]['name'])
            if expected and not any(normalize(token) in name for token in expected):
                errors.append(color+' role misses any of '+repr(expected))
        name=normalize(sides['black']['name'])
        for token in case['black_forbidden']:
            if normalize(token) in name:
                errors.append('Premature/wrong black role '+token)
        if case['id']=='initial':
            assert all(sides[color]['status']=='pending' and sides[color]['name'] is None for color in ('white','black'))
        if case['id']=='e4_waiting':
            assert sides['black']['status']=='pending' and sides['black']['name'] is None
        rows.append({'id':case['id'],'sides':sides,'current_matches':guide['current_matches'],
                     'matched_opening':guide['matched_opening'],'errors':errors})
        failures.extend({'id':case['id'],'error':error} for error in errors)
        checked['source_cases']+=1
        if case['uci']:
            fen_only=chess.Board(board.fen(en_passant='fen'))
            fen_guide=inspect(fen_only)
            assert fen_guide['history_scope']=='custom'
            assert all(fen_guide['sides'][color]['status']!='confirmed' for color in ('white','black'))
            checked['fen_without_invented_history']+=1
            move=board.pop()
            prior=inspect(board)
            board.push(move)
            assert inspect(board)['sides']==sides, case['id']
            checked['undo_replay_identity']+=1

    # Deliberately leave the named Ruy endpoint; the old name must be historical.
    departure=chess.Board()
    for san in 'e4 e5 Nf3 Nc6 Bb5 h6'.split(): departure.push_san(san)
    departed=inspect(departure)
    if not departed['current_matches']:
        assert departed['sides']['white']['status']=='last_known', departed['sides']
        checked['last_known_departure']+=1
    else:
        failures.append({'id':'departure','error':'Chosen public departure now has named endpoint; choose explicit unnamed continuation'})
    # Same named position through two orders must receive identical family roles.
    for a,b in [('Nf3 c5 e4','e4 c5 Nf3'),('Nf3 Nf6 c4','c4 Nf6 Nf3')]:
        left,right=chess.Board(),chess.Board()
        for token in a.split(): left.push_san(token)
        for token in b.split(): right.push_san(token)
        gl,gr=inspect(left),inspect(right)
        assert gl['position_key']==gr['position_key']
        assert [gl['sides'][s]['name'] for s in ('white','black')]==[gr['sides'][s]['name'] for s in ('white','black')]
        checked['transposition_role_equivalence']+=1

    catalog_rows=[]
    if args.full_catalog:
        catalog=_catalog()
        for index, opening in enumerate(catalog.openings):
            board=chess.Board()
            for move in opening.moves: board.push_uci(move)
            guide=inspect(board)
            sides=guide['sides']
            catalog_rows.append({'id':index,'catalog_name':opening.name,'white':sides['white'], 'black':sides['black']})
            for color in ('white','black'):
                checked['catalog_'+color+'_'+sides[color]['status']]+=1
            checked['catalog_rows']+=1
        # These first-move families are authored as White choices; Black must not inherit them.
        forbidden_black={'Portuguese Opening','Italian Game','Ruy Lopez','English Opening','London System','King\'s Indian Attack'}
        for row in catalog_rows:
            if row['black']['name'] in forbidden_black:
                failures.append({'id':'catalog:'+str(row['id']),'error':'White family assigned to Black: '+row['black']['name']})
        (args.output.parent/(args.output.stem+'-catalog.json')).write_text(json.dumps(catalog_rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    assert before_sources=={name:sha(ROOT/name) for name in source_names}, 'Product changed during audit'
    result={'status':'passed' if not failures else 'failed','counts':dict(checked),'failures':failures,
            'fixture_sha256':sha(source),'audit_script_sha256':sha(Path(__file__)),
            'product_sources':before_sources,
            'scope':'Source-backed role cases, honest history and schema; full catalogue structural scan does not verify every named role against an external source',
            'observations':rows,'departure':departed['sides']}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('status','counts','failures')},ensure_ascii=True))
    return 0 if not failures else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--full-catalog',action='store_true')
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.output.exists(): parser.error('Use fresh output filename')
    raise SystemExit(run(args))
