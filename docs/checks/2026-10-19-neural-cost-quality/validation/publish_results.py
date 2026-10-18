"""Publish selected auditable evidence; redact machine-specific user paths."""
import hashlib, json, re, shutil
from pathlib import Path
root=Path.cwd()
scratch=root/'build/neural-cost-quality'
destination=root/'docs/checks/2026-10-19-neural-cost-quality'
state=root/'build/learning-cycle'
latest=json.loads((state/'latest.json').read_text(encoding='utf-8'))
run=state/latest['run']
def sha(data): return hashlib.sha256(data).hexdigest()
def scrub(text):
    for base,replacement in [(str(root),'<REPO>'),('<INSTALLED_REPO>','<INSTALLED_REPO>'),
                             ('<USER>','<USER>')]:
        for sep in ('/', '\\', '\\\\', '\\\\\\\\'):
            old=base.replace('\\','/').replace('/',sep)
            text=re.sub(re.escape(old),lambda _:replacement,text,flags=re.IGNORECASE)
    return text
def published(data,suffix):
    if suffix in ('.nn',): return data
    return scrub(data.decode('utf-8-sig',errors='strict')).replace('\r\n','\n').encode('utf-8')
items=[]
def include(source, relative):
    data=source.read_bytes()
    target=destination/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    output=published(data,source.suffix)
    target.write_bytes(output)
    items.append({'source':str(source.relative_to(root)).replace('\\','/'),
                  'published':str(relative).replace('\\','/'),
                  'original_sha256':sha(data),'published_sha256':sha(output),
                  'original_bytes':len(data),'published_bytes':len(output)})
for path in sorted(run.rglob('*')):
    if path.is_file() and path.suffix not in ('.exe','.obj','.pdb'):
        include(path,Path('run')/path.relative_to(run))
for path in sorted((scratch/'diagnostic').rglob('*')):
    if path.is_file(): include(path,Path('diagnostic')/path.relative_to(scratch/'diagnostic'))
for path in sorted(scratch.iterdir()):
    if path.is_file() and path.suffix in ('.json','.log','.py','.md') and path.name not in ('ci-final.json',):
        include(path,Path('validation')/path.name)
manifest={'schema':'regicide.published.evidence.v1','source_run':latest['run'],
          'source_commit':json.loads((scratch/'power-state.json').read_text())['source_commit'],
          'policy':'NN bytes preserved; other UTF-8 evidence may redact user paths and normalize CRLF. Internal original hashes remain original-byte identities; this mapping connects published files. Engine binary omitted and identified by SHA256.',
          'files':items,
          'authored': [{'published':'README.md','sha256':sha((destination/'README.md').read_bytes())}],
          'related_documents': [{'path':name,'sha256':sha((root/name).read_bytes())}
              for name in ('README.md','docs/CONTINUAL_LEARNING.md','docs/NEURAL_COST_QUALITY.md',
                           'docs/experiments/2026-10-19-neural-cost-quality-plan.md')]}
destination.mkdir(parents=True,exist_ok=True)
(destination/'publication-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print('Published',len(items),'files;',sum(i['published_bytes'] for i in items),'bytes')
