from collections import Counter
from pathlib import Path
import argparse
import csv
import hashlib
import io
import json
import sys

import chess
import chess.pgn

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from opening_guide import _catalog, get_opening_guide
from opening_names import family_display_name

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def position(san):
    board=chess.Board()
    for token in san.split(): board.push_san(token)
    return board

def run(args):
    source_names=['scripts/opening_names.py','scripts/opening_guide.py','scripts/opening_sides.py']
    hashes={name:sha(ROOT/name) for name in source_names}
    proof_path=ROOT/'docs/checks/2026-11-02-opening-sides/terminology-verification.json'
    proof=json.loads(proof_path.read_text(encoding='utf-8'))
    mapping={row['catalog_family']:row for row in proof['changed_families']}
    manifest=json.loads((ROOT/'data/openings/manifest.json').read_text(encoding='utf-8'))
    raw=[]
    for name in ('a.tsv','b.tsv','c.tsv','d.tsv','e.tsv','LICENSE'):
        path=ROOT/'data/openings'/name
        assert sha(path)==manifest['files'][name]['sha256']
        assert len(path.read_bytes())==manifest['files'][name]['bytes']
        if name.endswith('.tsv'):
            raw.extend(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8')),delimiter='\t'))
    base=_catalog()
    assert len(raw)==len(base.openings)==3865
    counts=Counter()
    rows=[]
    for source,item in zip(raw,base.openings,strict=True):
        game=chess.pgn.read_game(io.StringIO(source['pgn']))
        assert not game.errors
        moves=tuple(move.uci() for move in game.mainline_moves())
        assert item.catalog_name==source['name'] and item.eco==source['eco'] and item.moves==moves
        family,sep,suffix=source['name'].partition(':')
        if family not in mapping:
            continue
        expected=mapping[family]['display_family']+sep+suffix
        full_exact=(source['eco']=='E60' and source['name']=='Indian Defense: West Indian Defense'
                    and moves==('d2d4','g8f6','c2c4','g7g6'))
        if full_exact: expected="King's Indian Defense"
        assert item.name==expected,(source['name'],item.name,expected)
        counts[family]+=1
        is_known_exact=full_exact or (source['name'] in ('Bird Opening','Nimzo-Larsen Attack','Hungarian Opening') and len(moves)==1)
        if is_known_exact:
            assert item.name_source_url and item.family_name_source_url is None
            counts['exact_priority']+=1
        else:
            assert item.name_source_url is None
            assert item.family_name_source_url==mapping[family]['source_url']
            counts['family_only']+=1
        board=chess.Board()
        for move in moves: board.push_uci(move)
        guide=get_opening_guide(board)
        found=[row for row in guide['current_matches'] if row['name']==expected and row['eco']==source['eco']]
        assert found,(source['name'],guide['current_matches'])
        shown=found[0]
        assert shown['name_verification']==('exact_line' if is_known_exact else 'family_only')
        assert shown['name_source_url']==item.name_source_url
        assert shown['family_name_source_url']==item.family_name_source_url
        rows.append({'catalog_name':source['name'],'display_name':item.name,'eco':item.eco,
                     'name_verification':shown['name_verification'],
                     'exact_source':shown['name_source_url'],'family_source':shown['family_name_source_url']})
    for family,row in mapping.items(): assert counts[family]==row['catalog_rows']
    assert counts['family_only']==534 and counts['exact_priority']==4
    assert base.source['name_override_rows']==38
    assert base.source['family_title_aliases']==9 and base.source['family_title_rows']==534
    endpoints=[]
    for row in proof['additional_exact_endpoint_candidates']:
        a,b=position(row['catalog_san']),position(row['official_san'])
        assert a.fen(en_passant='legal').split()[:4]==b.fen(en_passant='legal').split()[:4]
        for board in (a,b):
            matched=get_opening_guide(board)['current_matches'][0]
            assert matched['name']==row['display_name']
            assert matched['name_source_url']==row['source_url']
            assert matched['name_verification']=='exact_line'
        endpoints.append({'name':row['display_name'],'both_orders_same_position':True})
    for row in proof['excluded']:
        if 'catalog_name' in row:
            items=[item for item in base.openings if item.catalog_name==row['catalog_name']]
            assert items
            for item in items:
                assert item.name==item.catalog_name and item.name_source_url is None and item.family_name_source_url is None
    boundary_checks=0
    for family,row in mapping.items():
        for text in ['Prefix '+family,'Unrelated Opening: '+family+' Variation',family+'Extra']:
            assert family_display_name(text)==(text,None)
            boundary_checks+=1
    structure={'moves':[row.moves for row in base.openings],'positions':dict(base.positions),'named':dict(base.named)}
    structural_sha=hashlib.sha256(json.dumps(structure,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    prior=json.loads(Path(__file__).with_name('audit_display_isolation.json').read_text(encoding='utf-8'))
    assert structural_sha==prior['base_structure_sha256_before_and_after']
    assert hashes=={name:sha(ROOT/name) for name in source_names},'Product changed during audit'
    result={'status':'passed','family_counts':dict(counts),'eligible_rows':len(rows),'exact_overrides_total':38,
            'source_family_aliases':9,'boundary_non_replacement_checks':boundary_checks,
            'raw_data_checksums_unchanged':6,'raw_catalog_move_rows_unchanged':len(raw),
            'base_index_structure_sha256':structural_sha,'new_exact_endpoints':endpoints,
            'product_sources':hashes,'source_proof_sha256':sha(proof_path),'audit_script_sha256':sha(Path(__file__)),
            'scope':'Nine source-verified family aliases, exact precedence, provenance scope, original catalog identity and exclusions. Variant suffixes are not claimed externally verified.',
            'rows':rows}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key not in ('rows','product_sources')},ensure_ascii=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Use fresh output filename')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    run(args)
