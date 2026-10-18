"""Finite temporary sleep-prevention lease; no persistent power settings."""
import datetime
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from learning_cycle import KeepAwake
WORK = Path(__file__).resolve().parent
stop = WORK/'stop-awake'
state = WORK/'power-lease.json'
record = {'active': True, 'pid': os.getpid(), 'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'maximum_seconds': 7200, 'persistent_power_settings_changed': False}
try:
    with KeepAwake():
        state.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
        started = time.monotonic()
        while not stop.exists() and time.monotonic()-started < 7200:
            time.sleep(.5)
finally:
    record.update(active=False, finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
    state.write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
