"""Compare the first three moves with v2 using eight fixed seeds per position.

Run --phase old then --phase new; repeat both with --focus for the two catalogue
blind spots. The existing audit driver saves source, engine and search receipts.
Only disposable games and owned engines are used; no live game is accessed.
"""

import argparse
from pathlib import Path
from types import SimpleNamespace

import check_opening_variety as audit


BASELINE = "9137e5f815ad9af12649968d9f8e7f0bb694fd9c"
CASES = (
    ("white-first", ""),
    ("black-after-e4", "e2e4"),
    ("white-second-open", "e2e4 e7e5"),
    ("black-second-open", "e2e4 e7e5 g1f3"),
    ("white-third-open", "e2e4 e7e5 g1f3 b8c6"),
    ("black-third-italian", "e2e4 e7e5 g1f3 b8c6 f1c4"),
)
FOCUS = (
    ("white-second-Nc3-Nf6", "b1c3 g8f6"),
    ("white-third-Nc3-Nf3", "b1c3 g8f6 g1f3 d7d6"),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("old", "new"), required=True)
    parser.add_argument("--focus", action="store_true")
    parser.add_argument("--output", type=Path, help="Fresh directory for a changed source or binary")
    parser.add_argument("--binary", type=Path, default=audit.ROOT / "build/regicide.exe")
    args = parser.parse_args()
    audit.CASES = FOCUS if args.focus else CASES
    audit.SEEDS = tuple(f"{number:032x}" for number in range(8))
    folder = "focus" if args.focus else "regular"
    output = args.output or audit.ROOT / "build/early-opening-v3-validation" / folder
    output.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(output=output, baseline_ref=BASELINE,
                               new_label="new", binary=args.binary.resolve(strict=True))
    audit.run_policy(settings, args.phase, BASELINE if args.phase == "old" else None)


if __name__ == "__main__":
    main()
