#!/usr/bin/env python3
"""Census of a hand-written `*LegalizerInfo.cpp` constructor.

    tools/import-inventory.py <path-to-XxxLegalizerInfo.cpp>

Answers one question before anybody transcribes a target by hand: which
builder calls does this constructor use, in which argument form, and which
guards are not a conjunction of feature reads and `LegalityPredicates`?

The output is markdown so it can be pasted into `docs/REPORT-<target>.md`.
Nothing here parses C++ in general: it understands exactly the shape a
legalizer constructor has (`getActionDefinitionsBuilder(...)` followed by
method calls, plus `if`/`else`/`for` around them), and anything it does not
recognise is reported as `?` instead of being dropped.
"""

import collections
import re
import sys

# builder method -> argument form; the DSL spelling is next to it.
FORMS = {
    "legalFor": "types", "legalForCartesianProduct": "cartesian",
    "legalForTypesWithMemDesc": "memdesc", "legalIf": "pred",
    "customFor": "types", "customIf": "pred", "customForCartesianProduct": "cartesian",
    "libcallFor": "types", "libcallIf": "pred",
    "lowerFor": "types", "lowerForCartesianProduct": "cartesian", "lowerIf": "pred",
    "unsupportedFor": "types", "unsupportedIf": "pred",
    "maxScalar": "scalar", "minScalar": "scalar", "clampScalar": "scalarbound",
    "maxScalarIf": "predscalar", "minScalarIf": "predscalar",
    "widenScalarIf": "predmutate", "narrowScalarFor": "types",
    "scalarSameSizeAs": "same", "widenScalarToNextPow2": "pow2",
    "widenScalarOrEltToNextPow2OrMinSize": "pow2",
    "clampNumElements": "lanes", "clampMaxNumElements": "lanes",
    "moreElementsToNextPow2": "pow2", "fewerElementsFor": "types",
    "bitcastIf": "predmutate", "bitcast": "types",
    "lower": "", "unsupported": "", "alwaysLegal": "always",
    "scalarize": "", "libcall": "types", "custom": "types",
}

# A guard is "closed" when the DSL can spell it: feature reads plus the
# predicate vocabulary.  These markers mean it cannot, as written.
# `typeIsLegal{IntOrFP,Bool,Ptr}Vec` are spelled out in the same file as
# `typeInSet` plus feature reads, so a guard that only calls them is closed
# (see docs/REPORT-riscv.md).
CLOSED_HELPERS = r"typeIsLegal(?:IntOrFP|Bool|Ptr)Vec\s*\("

OPAQUE = [
    ("lambda", r"\[\s*[=&]?\s*\]\s*\("),
    ("subtarget-call", r"\bST\.\w+\("),
    ("subtarget-call", r"\bSTI\.\w+\("),
    ("query-field", r"\bQuery\.\w+"),
]


def strip_comments(lines):
    return [re.sub(r"//.*$", "", l) for l in lines]


def top_statements(text):
    """Split at top-level `;`, keeping `{...}` blocks together."""
    out, depth, cur, instr = [], 0, [], False
    for c in text:
        if instr:
            cur.append(c)
            if c == '"':
                instr = False
        elif c == '"':
            instr = True
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == ";" and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def constructor_body(lines):
    start = next((i for i, l in enumerate(lines)
                  if re.search(r"::\w+LegalizerInfo\(const \w+Subtarget", l)), None)
    if start is None:
        raise SystemExit("no constructor found")
    end = next((i for i, l in enumerate(lines[start:], start)
                if "legalizeCustom" in l), len(lines))
    chunk = lines[start:end]
    brace = next(i for i, l in enumerate(chunk) if l.rstrip().endswith("{"))
    # Stop at the constructor's matching brace, not at the next function: the
    # custom-legalize body below it is a program, not a rule table.
    depth = 0
    for i in range(brace, len(chunk)):
        depth += chunk[i].count("{") - chunk[i].count("}")
        if depth == 0 and i > brace:
            return "\n".join(chunk[brace + 1:i]), start + brace + 1
    raise SystemExit("unbalanced constructor")


def chain_calls(chunk):
    """`getActionDefinitionsBuilder(...)` or `<name>.<method>(...)` pieces."""
    return re.findall(r"(?:getActionDefinitionsBuilder\s*\(|\.)([a-zA-Z_]\w*)\s*\(", chunk)


def classify_guard(text):
    """Reason a guard is not closed, or None."""
    text = re.sub(CLOSED_HELPERS, "(closed-helper ", text)
    for name, pat in OPAQUE:
        if re.search(pat, text):
            return name
    return None


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__)
    path = argv[1]
    with open(path) as f:
        lines = strip_comments(f.read().splitlines())
    body, first = constructor_body(lines)
    stmts = [s for s in top_statements(body) if s.strip()]

    chains = [s for s in stmts
              if "getActionDefinitionsBuilder" in s or re.match(r"[\w&*]+\s*\.\w+\(", s.strip())]
    calls = collections.Counter()
    forms = collections.Counter()
    guards = collections.Counter()
    opaque = collections.Counter()
    for st in chains:
        for m in re.findall(r"\.([a-zA-Z_]\w*)\s*\(", st):
            if m not in FORMS:
                continue
            calls[m] += 1
            forms[FORMS[m] or "none"] += 1
        if re.search(r"\.(legalIf|customIf|lowerIf|unsupportedIf|bitcastIf|widenScalarIf|minScalarIf|maxScalarIf)\s*\(", st):
            g = st[st.find(".", st.find("Builder")):] if False else st
            why = classify_guard(g)
            guards["closed" if why is None else why] += 1
            if why:
                opaque[why] += 1
    blocks = sum(1 for s in stmts if re.match(r"if\s*\(|for\s*\(", s))
    named = collections.Counter(
        m.group(1) for s in stmts if "getActionDefinitionsBuilder" in s
        for m in [re.search(r"auto\s*&\s*(\w+)\s*=", s)] if m)
    extended = collections.Counter(
        re.match(r"(\w+)\s*\.", s).group(1) for s in stmts
        if re.match(r"(\w+)\s*\.\w+\(", s.strip()))

    print("source: `%s`" % path)
    print()
    print("| metric | value |")
    print("|---|---|")
    print("| constructor starts at line | %d |" % first)
    print("| top-level statements | %d |" % len(stmts))
    print("| builder statements | %d |" % len(chains))
    print("| `getActionDefinitionsBuilder(...)` | %d |" %
          sum(1 for s in stmts if "getActionDefinitionsBuilder" in s))
    print("| named chains (`auto &X = ...`) | %d |" % len(named))
    print("| chains extended later by name | %d |" % sum(extended.values()))
    print("| `if`/`for` blocks in the constructor | %d |" % blocks)
    print("| guards: closed / not | %d / %d |" %
          (guards["closed"], sum(v for k, v in guards.items() if k != "closed")))
    per = {"types": 1, "cartesian": 1, "memdesc": 1, "pow2": 1, "scalar": 1,
           "scalarbound": 2, "same": 2, "pred": 1, "predmutate": 1,
           "lanes": 2, "": 1, "always": 1}
    print("| estimated core rules | %d |" % sum(per[FORMS[m] or ""] * c
                                               for m, c in calls.items()))
    print()
    print("## Builder calls")
    print()
    print("| method | count | argument form |")
    print("|---|---:|---|")
    for m, c in calls.most_common():
        print("| `%s` | %d | %s |" % (m, c, FORMS[m] or "no argument"))
    print()
    print("## Chains extended after creation")
    print()
    for n, c in extended.most_common():
        print("- `%s` (%d statements)" % (n, c))
    print()
    print("## Guards that are not closed")
    print()
    if not opaque:
        print("None: every guard is feature reads plus `LegalityPredicates`.")
    else:
        for n, c in opaque.most_common():
            print("- %s: %d guard(s)" % (n, c))


if __name__ == "__main__":
    main(sys.argv)
