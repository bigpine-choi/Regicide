"""Run from the repository root: python -m scripts.neural_search_data ..."""

import argparse
from dataclasses import fields
import json

from .generate import Config, DEFAULT_SEEDS, generate
from .schema import load_dataset


def main():
    parser = argparse.ArgumentParser(description="Bounded own fixed-node search dataset")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("generate")
    run.add_argument("--engine", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--seeds", default=str(DEFAULT_SEEDS))
    for field in fields(Config):
        if field.name in ("quiet", "static_control"):
            continue
        run.add_argument("--" + field.name.replace("_", "-"), type=type(field.default), default=field.default)
    run.add_argument("--no-static-control", action="store_true", help="Omit the separate static-evaluation control")
    check = commands.add_parser("check")
    check.add_argument("dataset")
    check.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    if args.command == "generate":
        settings = {f.name: getattr(args, f.name) for f in fields(Config) if f.name not in ("quiet", "static_control")}
        result = generate(args.engine, args.output, config=Config(**settings, static_control=not args.no_static_control),
                          seeds_path=args.seeds)
        print(json.dumps({k: result[k] for k in ("status", "wall_seconds", "stats", "error")}, indent=2))
        return 0 if result["status"] == "complete" else 2
    result = load_dataset(args.dataset, allow_partial=args.allow_partial)
    print(json.dumps({"status": result["manifest"]["status"],
                      "train": len(result["train"]), "validation": len(result["validation"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
