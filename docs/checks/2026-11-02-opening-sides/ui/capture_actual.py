from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'tests'))
import human_play_tests as base
import chess

GODOT = Path('C:/Users/hanso/Tools/Godot/Godot_v4.7.2-stable_win64_console.exe')
cases = [('morphy', 'e2e4 e7e5 g1f3 b8c6 f1b5 a7a6', False),
         ('sicilian-position', 'e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6', True)]
results = []
with tempfile.TemporaryDirectory(prefix='regicide-opening-side-capture-') as folder:
    temporary = Path(folder)
    project = temporary/'godot'
    shutil.copytree(ROOT/'godot',project,ignore=shutil.ignore_patterns('.godot'))
    binary = temporary/'fake-engine.exe'
    binary.write_bytes(b'Fake peer only; never executed')
    init = subprocess.run([str(GODOT),'--path',str(project),'--language','en','--editor','--headless','--import','--quit'],
                          capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    (OUT/'capture-import.txt').write_text(init.stdout+init.stderr,encoding='utf-8')
    assert init.returncode == 0 and 'ERROR:' not in init.stdout+init.stderr
    for label, moves, custom in cases:
        destination = OUT/label
        destination.mkdir(exist_ok=True)
        game = base.play_human.HumanGame(binary,temporary/(label+'-games'),engine_factory=base.FakeFactory(),
                    hint_engine_factory=base.FakeFactory(),evaluation_engine_factory=base.FakeFactory())
        try:
            board = chess.Board()
            for uci in moves.split():
                board.push_uci(uci)
            if custom:
                board = chess.Board(board.fen(en_passant='fen'))
            with game._lock:
                game.board = board
                game.revision += 1
                game._sync_evaluation()
            state = game.state()
            assert 'sides' in state['opening_guide'], 'Backend sides not ready'
            source = destination/'actual-state.json'
            source.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        finally:
            game.close()
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        command = [str(GODOT),'--path',str(project),'--language','en','--script','res://tests/opening_guide_capture.gd',
                   '--','--state-file',str(source),'--output',str(destination)]
        run = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=50,
                             startupinfo=info,creationflags=subprocess.CREATE_NO_WINDOW)
        text = run.stdout+run.stderr
        (destination/'capture.txt').write_text(text,encoding='utf-8')
        assert run.returncode == 0 and 'ERROR:' not in text, text
        result = json.loads((destination/'capture-result.json').read_text(encoding='utf-8'))
        assert result['success'] and result['live_position_unchanged']
        result['fixture'] = label
        results.append(result)
        print(json.dumps({key:result[key] for key in ['fixture','success','white_opening','black_response']}),flush=True)
(OUT/'actual-capture-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
