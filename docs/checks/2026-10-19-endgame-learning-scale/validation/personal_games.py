"""Compare personal game bytes without publishing game content or filenames."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from game_archive import default_game_directory

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('before', 'after'))
    args = parser.parse_args()
    directory = default_game_directory()
    hashes = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(directory.rglob('*')) if path.is_file()} if directory.exists() else {}
    combined = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode('utf-8')).hexdigest()
    result = {'checked_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'All personal game files, names and contents remain private; digest of sorted filename-to-content-sha mapping.',
              'file_count': len(hashes), 'combined_sha256': combined}
    out = ROOT/'build/endgame-learning-scale'
    if args.mode == 'before':
        if (out/'personal-games-before.json').exists():
            raise FileExistsError('Do not replace the prior personal-game snapshot')
    else:
        before = json.loads((out/'personal-games-before.json').read_text(encoding='utf-8'))
        result['unchanged'] = before['file_count'] == len(hashes) and before['combined_sha256'] == combined
    (out/f'personal-games-{args.mode}.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    if args.mode == 'after' and not result['unchanged']:
        raise SystemExit('Personal game files changed; inspect before claiming preservation')

if __name__ == '__main__':
    main()
