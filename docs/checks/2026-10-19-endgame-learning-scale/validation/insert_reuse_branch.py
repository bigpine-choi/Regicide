from pathlib import Path

path = Path('scripts/endgame_study.py')
text = path.read_text(encoding='utf-8')
start = text.index('        stage("training-loss-endgame-diagnosis")', text.index('def prepare('))
end = text.index('        stage("immutable-augmentation")', start)
original = text[start:end]
replacement = '''        reuse_source = getattr(args, "reuse_data_run", None)
        if reuse_source:
            stage("reuse-completed-endgame-data")
            report["data_reuse"] = reuse_completed_data(state, catalog, reuse_source, run,
                                                         digest, report["configuration"])
            report["diagnosis"] = copy.deepcopy(report["data_reuse"]["diagnosis_summary"])
            save()
        else:
'''
replacement += ''.join('    ' + line if line.strip() else line for line in original.splitlines(keepends=True))
text = text[:start] + replacement + text[end:]
path.write_text(text, encoding='utf-8', newline='\n')
