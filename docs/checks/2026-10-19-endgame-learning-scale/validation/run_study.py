import json
from pathlib import Path
import sys
import datetime

root=Path.cwd()
sys.path.insert(0,str(root/'scripts'))
from learning_cycle import KeepAwake
import endgame_study

out=root/'build/endgame-learning-scale'
mode=sys.argv[1]
state={'active':True,'phase':mode,'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
(out/'power-state.json').write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
try:
    with KeepAwake():
        code=endgame_study.main(sys.argv[1:])
finally:
    state.update(active=False,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (out/'power-state.json').write_text(json.dumps(state,indent=2)+'\n',encoding='utf-8')
raise SystemExit(code)
