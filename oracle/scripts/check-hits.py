#!/usr/bin/env python3
"""Cross-check --dump-hits against the runner's own answer path.

    check-hits.py RUNNER QUERIES

Every query is answered twice: once by `--queries` (the `getAction` path) and
once by `--dump-hits` (rule.match in first-match order).  They must agree:

  * first_match >= 0  -> the matched rule's action equals the answer action;
  * first_match == -1 -> the answer is Unsupported (or an explicit
                         `unanswerable` record from the runner's guard).

This is the U1 semantic gate: if it fails, coverage rows mean nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("command failed: %r" % (cmd,))
    return proc.stdout


def load_lines(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("runner")
    ap.add_argument("queries")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        rules_path = Path(tmp) / "rules.jsonl"
        run([args.runner, "--dump-rules", str(rules_path)])
        rules = {}
        for row in load_lines(rules_path.read_text()):
            rules[(row["config"], row["opcode"], row["rule_index"])] = row["action"]

    hits = load_lines(run([args.runner, "--dump-hits", args.queries]))
    answers = load_lines(run([args.runner, "--queries", args.queries]))
    if len(hits) != len(answers):
        raise SystemExit("row count differs: hits=%d answers=%d"
                         % (len(hits), len(answers)))

    mismatches, unanswerable, shadowed_queries = [], 0, 0
    for hit, answer in zip(hits, answers):
        if answer.get("status") == "unanswerable":
            unanswerable += 1
            continue
        if hit["later_matches"]:
            shadowed_queries += 1
        if hit["first_match"] < 0:
            if answer.get("action") != "Unsupported":
                mismatches.append((hit, answer, None))
            continue
        key = (hit["config"], hit["ruleset"], hit["first_match"])
        rule_action = rules.get(key)
        if rule_action is None:
            mismatches.append((hit, answer, "missing rule row %r" % (key,)))
        elif rule_action != answer.get("action"):
            mismatches.append((hit, answer, rule_action))

    print("queries=%d unanswerable=%d with_shadowed_matches=%d mismatches=%d"
          % (len(hits), unanswerable, shadowed_queries, len(mismatches)))
    for hit, answer, expected in mismatches[:5]:
        print("MISMATCH", json.dumps(hit, sort_keys=True))
        print("  answer  ", json.dumps(answer, sort_keys=True))
        if expected is not None:
            print("  expected", expected)
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
