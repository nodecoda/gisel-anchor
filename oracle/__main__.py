"""Command line entry point for the U-side oracle.

    python3 -m oracle universe --arity x86.arity.jsonl --out universe.jsonl
    python3 -m oracle coverage --cov out/*.cov.jsonl --target x86 --out coverage.json
    python3 -m oracle emit --cov out/*.cov.jsonl --universe universe.jsonl \
        --out queries.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys

from .src import coverage, emit, expand, universe
from .src.arity import load_arity


def cmd_universe(args) -> int:
    arity = load_arity(args.arity)
    if args.profile == "safe":
        if not args.opcodes:
            raise SystemExit("--profile safe needs at least one --opcodes")
        rows = universe.safe_build(arity, args.opcodes, target=args.target)
    else:
        rows = universe.build(arity, seed=args.seed, limit=args.limit,
                              opcodes=args.opcodes, target=args.target)
    count = universe.write(rows, args.out)
    print(json.dumps({"arity": args.arity, "out": args.out,
                      "seed": args.seed, "limit": args.limit,
                      "opcodes": len(arity), "queries": count,
                      "target": args.target,
                      "profile": args.profile}, sort_keys=True))
    return 0


def cmd_coverage(args) -> int:
    rows = coverage.merge(args.cov)
    report = coverage.build(rows, args.target)
    coverage.write(report, args.out)
    counts = report["counts"]
    print(json.dumps({"out": args.out, "target": args.target,
                      "configs": [section["config"]
                                  for section in report["configs"]],
                      "counts": counts}, sort_keys=True))
    if not args.gate:
        return 0
    gaps = coverage.load_json(args.known_gaps) if args.known_gaps else {"gaps": []}
    problems = coverage.gate(report, gaps)
    for problem in problems:
        print(json.dumps({"gate": problem}, sort_keys=True), file=sys.stderr)
    return 0 if not problems else 1


def cmd_emit(args) -> int:
    rows = coverage.merge(args.cov)
    universe_rows = emit.load_universe(args.universe)
    queries = emit.build(rows, universe_rows, sample=args.sample)
    count = emit.write(queries, args.out)
    print(json.dumps({"out": args.out, "queries": count,
                      "sample": args.sample}, sort_keys=True))
    return 0


def cmd_expand(args) -> int:
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    if not configs:
        raise SystemExit("--configs must name at least one configuration")
    rows = expand.load(args.universe)
    count = expand.write(args.out, expand.build(rows, configs))
    print(json.dumps({"out": args.out, "universe": len(rows),
                      "configs": len(configs), "queries": count},
                     sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="oracle")
    sub = ap.add_subparsers(dest="command", required=True)

    un = sub.add_parser("universe", help="emit a rule-independent candidate set")
    un.add_argument("--arity", required=True,
                    help="JSONL from the runner's --dump-arity")
    un.add_argument("--out", required=True)
    un.add_argument("--seed", type=int, default=0)
    un.add_argument("--limit", type=int, default=1200,
                    help="candidates per opcode (0 = no cap)")
    un.add_argument("--opcodes", action="append", default=None,
                    help="restrict to these opcodes (repeatable)")
    un.add_argument("--profile", default="full", choices=("full", "safe"),
                    help="safe = narrow salvage set for asserted opcodes")
    un.add_argument("--target", default="generic",
                    choices=("generic", "x86", "aarch64", "riscv", "arm",
                             "mips", "ppc", "m68k", "wasm", "spirv"),
                    help="shape capability filter (x86 has no scalable vectors)")
    un.set_defaults(func=cmd_universe)

    cov = sub.add_parser("coverage", help="merge --witness files into coverage.json")
    cov.add_argument("--cov", action="append", required=True,
                     help="witness JSONL; later files override earlier ones")
    cov.add_argument("--target", required=True)
    cov.add_argument("--out", required=True)
    cov.add_argument("--gate", action="store_true",
                     help="fail unless every gap has a known_gaps.json entry")
    cov.add_argument("--known-gaps", default=None)
    cov.set_defaults(func=cmd_coverage)

    em = sub.add_parser("emit", help="write the U query file")
    em.add_argument("--cov", action="append", required=True)
    em.add_argument("--universe", required=True)
    em.add_argument("--out", required=True)
    em.add_argument("--sample", type=int, default=8,
                    help="extra evenly spaced queries per (config, opcode)")
    em.set_defaults(func=cmd_emit)

    ex = sub.add_parser("expand",
                        help="expand a universe across configurations")
    ex.add_argument("--universe", required=True)
    ex.add_argument("--configs", required=True,
                    help="comma-separated configuration names")
    ex.add_argument("--out", required=True)
    ex.set_defaults(func=cmd_expand)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
