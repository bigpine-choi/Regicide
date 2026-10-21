"""Run one product replay and reuse it for descriptive summary generation."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'scripts'))
import m2_formal_v6 as formal
import m2_formal_summary_v6 as statistics

directory = Path(sys.argv[1]).resolve()
phase = sys.argv[2]
audit = formal.audit_phase(directory, phase)
formal.write_json(directory/phase/'product-audit.json', audit)
with patch.object(formal, 'audit_phase', return_value=audit):
    summary = statistics.summarize(directory, phase)
formal.write_json(directory/phase/'summary.json', summary)
print(json.dumps({'status': audit['status'], 'valid_for_comparison':audit['valid_for_comparison'],
    'completed_games': summary['completed_games'], 'product_replays':1,
    'summary_reuses_same_input_audit':True}))
