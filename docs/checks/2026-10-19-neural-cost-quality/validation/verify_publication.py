import hashlib,json
from pathlib import Path
root=Path.cwd(); folder=root/'docs/checks/2026-10-19-neural-cost-quality'
d=json.loads((folder/'publication-manifest.json').read_text())
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
for item in d['files']:
 assert sha(folder/item['published'])==item['published_sha256'],item['published']
 assert sha(root/item['source'])==item['original_sha256'],item['source']
for item in d['authored']: assert sha(folder/item['published'])==item['sha256']
for item in d['related_documents']: assert sha(root/item['path'])==item['sha256']
private=[]; bad=[]
for p in folder.rglob('*'):
 if not p.is_file(): continue
 text=p.read_text(encoding='utf-8').replace('\\','/').casefold()
 while '//' in text: text=text.replace('//','/')
 if '<USER>' in text: private.append(str(p.relative_to(folder)))
 if '\ufffd' in text: bad.append(str(p.relative_to(folder)))
assert not private,private
assert not bad,bad
print('Public evidence verified:',len(d['files']),'derived,',len(d['authored']),'authored;',len(d['related_documents']),'related docs; no private prefixes or replacement characters')
