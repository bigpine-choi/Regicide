from pathlib import Path
import json
import chess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
# Expected role facts are specified independently from public source lines.
# Multiple accepted White labels allow a more specific played setup.
ROWS = [
    ('initial', '', [], [], ['Sicilian Defense','French Defense','King\'s Indian Defense'], 'catalog'),
    ('reti_start', 'Nf3', ['Reti'], [], ['Sicilian Defense','King\'s Indian Defense','Pirc Defense'], 'reti'),
    ('reti_to_english', 'Nf3 Nf6 c4', ['English'], [], ['Sicilian Defense','King\'s Indian Defense','Pirc Defense'], 'reti'),
    ('reti_sicilian_prefix', 'Nf3 c5', ['Reti'], [], ['Sicilian Defense'], 'reti'),
    ('reti_to_sicilian', 'Nf3 c5 e4', ['King\'s Pawn','Open Sicilian','Sicilian'], ['Sicilian'], [], 'reti'),
    ('reti_shared_fianchetto', 'Nf3 Nf6 g3 g6', ['Reti','King\'s Indian Attack'], [], ['King\'s Indian Defense','Pirc Defense'], 'reti'),
    ('e4_waiting', 'e4', ['King\'s Pawn'], [], ['Sicilian Defense','French Defense','Caro-Kann Defense','Pirc Defense'], 'sicilian'),
    ('sicilian', 'e4 c5', ['King\'s Pawn','Sicilian'], ['Sicilian'], [], 'sicilian'),
    ('sicilian_french_not_french', 'e4 c5 Nf3 e6', ['King\'s Pawn','Sicilian'], ['Sicilian'], ['French Defense'], 'sicilian'),
    ('alapin', 'e4 c5 c3', ['Alapin','King\'s Pawn','Sicilian'], ['Sicilian'], [], 'sicilian'),
    ('french', 'e4 e6', ['King\'s Pawn'], ['French'], [], 'french'),
    ('caro', 'e4 c6', ['King\'s Pawn'], ['Caro-Kann'], [], 'caro'),
    ('pirc_start', 'e4 d6', ['King\'s Pawn'], ['Pirc'], ['King\'s Indian Defense'], 'pirc'),
    ('pirc_main', 'e4 d6 d4 Nf6 Nc3 g6', ['King\'s Pawn','Classical','Austrian'], ['Pirc'], ['King\'s Indian Defense'], 'pirc'),
    ('english', 'c4', ['English'], [], ['Sicilian Defense'], 'english'),
    ('english_e5_not_sicilian', 'c4 e5', ['English'], [], ['Sicilian Defense'], 'english'),
    ('english_c5_not_sicilian', 'c4 c5', ['English'], [], ['Sicilian Defense'], 'english'),
    ('queens_gambit_waiting', 'd4 d5 c4', ['Queen\'s Gambit'], [], ['Queen\'s Gambit Accepted','Queen\'s Gambit Declined','Slav Defense'], 'queens_gambit'),
    ('qgd', 'd4 d5 c4 e6', ['Queen\'s Gambit'], ['Queen\'s Gambit Declined'], [], 'queens_gambit'),
    ('slav', 'd4 d5 c4 c6', ['Queen\'s Gambit'], ['Slav'], [], 'queens_gambit'),
    ('qga', 'd4 d5 c4 dxc4', ['Queen\'s Gambit'], ['Queen\'s Gambit Accepted'], [], 'queens_gambit'),
    ('ruy_waiting', 'e4 e5 Nf3 Nc6 Bb5', ['Ruy Lopez','Spanish'], [], ['Morphy','Berlin'], 'ruy'),
    ('ruy_morphy', 'e4 e5 Nf3 Nc6 Bb5 a6', ['Ruy Lopez','Spanish'], ['Morphy'], [], 'morphy'),
    ('ruy_berlin', 'e4 e5 Nf3 Nc6 Bb5 Nf6', ['Ruy Lopez','Spanish'], ['Berlin'], [], 'berlin'),
    ('italian_waiting', 'e4 e5 Nf3 Nc6 Bc4', ['Italian'], [], ['Giuoco Piano','Two Knights'], 'italian'),
    ('italian_piano', 'e4 e5 Nf3 Nc6 Bc4 Bc5', ['Italian'], ['Giuoco Piano'], [], 'italian'),
    ('italian_two_knights', 'e4 e5 Nf3 Nc6 Bc4 Nf6', ['Italian'], ['Two Knights'], [], 'two_knights'),
    ('kid', 'd4 Nf6 c4 g6', ['Queen\'s Pawn','d4','Queen\'s Gambit'], ['King\'s Indian'], ['Pirc Defense'], 'kid'),
    ('grunfeld_transition', 'd4 Nf6 c4 g6 Nc3 d5', ['Queen\'s Pawn','d4','Queen\'s Gambit'], ['Grunfeld','Gruenfeld'], ['King\'s Indian Defense'], 'grunfeld'),
    ('nimzo_waiting', 'd4 Nf6 c4 e6 Nc3', ['Queen\'s Pawn','d4','Queen\'s Gambit'], [], ['Nimzo-Indian'], 'nimzo'),
    ('nimzo', 'd4 Nf6 c4 e6 Nc3 Bb4', ['Queen\'s Pawn','d4','Queen\'s Gambit'], ['Nimzo-Indian'], [], 'nimzo'),
    ('london', 'd4 d5 Bf4', ['London'], [], ['King\'s Indian Defense'], 'london'),
    ('london_indian', 'd4 Nf6 Bf4 g6', ['London'], [], ['King\'s Indian Defense','Pirc Defense'], 'london'),
    ('modern_queen_pawn', 'd4 d6 c4 g6', ['Queen\'s Pawn','d4','Queen\'s Gambit'], ['Modern'], ['Pirc Defense'], 'modern'),
    ('portuguese_white_choice', 'e4 e5 Bb5', ['Portuguese'], [], ['Portuguese Opening'], 'portuguese'),
]
cases=[]
for key, san, white, black, forbidden, source in ROWS:
    board=chess.Board()
    for token in san.split():
        board.push_san(token)
    cases.append({'id':key,'san':san,'uci':[m.uci() for m in board.move_stack],
                  'fen':board.fen(en_passant='fen'),'white_any':white,'black_any':black,
                  'black_forbidden':forbidden,'source_id':source})
value={'schema':'regicide.opening-sides.independent-cases.v1','origin':'Initial 34 public source cases chosen without classifier; one Portuguese role regression added after independent code review',
       'cases':cases}
(OUT/'expected-cases.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'cases':len(cases),'all_histories_legal':True}))
