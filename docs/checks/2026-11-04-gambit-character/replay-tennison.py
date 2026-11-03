"""Native replay of the actual off-book Scandinavian branch found in v1."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
import gambit_character_validation as validation

validation.CASES = ({
    "id": "white-tennison", "color": "white", "san_prefix": "e4 d5",
    "line_id": "tennison", "signature_uci": "g1f3", "accept_uci": "d5e4",
},)
validation.SOURCES = (*validation.SOURCES, Path(__file__).relative_to(ROOT).as_posix())

if __name__ == "__main__":
    raise SystemExit(validation.main())
