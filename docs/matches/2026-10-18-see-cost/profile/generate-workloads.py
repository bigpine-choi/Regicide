import json, pathlib, chess, hashlib
root=pathlib.Path('.'); out=root/'build-see-cost'; groups={}
loss=json.loads((root/'build/see-cost-improvement/analysis/development-positions.json').read_text(encoding='utf-8'))
groups['loss-scenes']=[chess.Board(p['fen']) for p in loss['positions']]
profile=json.loads((root/'tests/data/profile_positions.json').read_text(encoding='utf-8'))
boards=[]
for p in profile['positions']:
 b=chess.Board(p.get('fen',chess.STARTING_FEN))
 for m in p['moves']: b.push_uci(m)
 boards.append(b)
for fen in ['r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1','7k/8/8/3pP3/8/8/8/6K1 w - d6 0 1','1rr5/P6k/8/8/8/8/8/7K w - - 0 1','7K/8/6k1/8/8/1R6/p7/1n5R w - - 0 1','5rnk/6pp/7N/3Q4/8/8/8/K7 w - - 0 1','5k2/2n5/8/3p4/2B2n2/8/8/5R1K w - - 0 1','5k2/8/2b5/3p4/2B2n2/8/8/5R1K w - - 0 1']:
 boards.append(chess.Board(fen))
for initial in [1,73,20261019,0xabcdef]:
 b=chess.Board();seed=initial
 for ply in range(100):
  if ply%5==0:boards.append(b.copy(stack=False))
  moves=sorted(b.legal_moves,key=lambda m:(m.from_square,m.to_square,m.promotion or 0))
  if not moves:break
  seed=(seed*1664525+1013904223)&0xffffffff
  b.push(moves[seed%len(moves)])
groups['mixed-regression']=boards
meta={}
for name,boards in groups.items():
 rows=[b.fen(en_passant='fen')+'\t'+m.uci() for b in boards for m in sorted(b.legal_moves,key=lambda m:m.uci()) if b.is_capture(m) or m.promotion]
 raw=('\n'.join(rows)+'\n').encode();(out/(name+'.tsv')).write_bytes(raw)
 (out/(name+'.fens')).write_text('\n'.join(b.fen(en_passant='fen') for b in boards)+'\n',encoding='utf-8')
 meta[name]={'positions':len(boards),'queries':len(rows),'sha256':hashlib.sha256(raw).hexdigest()}
(out/'workload-metadata.json').write_text(json.dumps(meta,indent=2)+'\n',encoding='utf-8')
print(json.dumps(meta,indent=2))
