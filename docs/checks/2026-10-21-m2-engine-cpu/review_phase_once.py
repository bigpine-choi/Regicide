"""Run one product replay and reuse it for descriptive summary generation."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

root = next(parent for parent in Path(__file__).resolve().parents
            if (parent/'scripts/m2_formal_v6.py').is_file())
sys.path.insert(0, str(root/'scripts'))
import m2_formal_v6 as formal
import m2_formal_summary_v6 as statistics

directory = Path(sys.argv[1]).resolve()
phase = sys.argv[2]
if (directory/phase/'product-audit.json').exists() or (directory/phase/'summary.json').exists():
    raise ValueError('Preserve existing phase review')
if formal.read_json(directory/'frozen-plan.json')['plan'].get('baseline96_selection'):
    import m2_baseline96
    audit = m2_baseline96.audit_phase(directory, phase)
else:
    audit = formal.audit_phase(directory, phase)
formal.write_json(directory/phase/'product-audit.json', audit)
with patch.object(formal, 'audit_phase', return_value=audit):
    summary = statistics.summarize(directory, phase)
formal.write_json(directory/phase/'summary.json', summary)
print(json.dumps({'status': audit['status'], 'valid_for_comparison':audit['valid_for_comparison'],
    'completed_games': summary['completed_games'], 'product_replays':1,
    'summary_reuses_same_input_audit':True}))
