#!/usr/bin/env python3
"""Audit docs/acceptance.md's verdicts against the code they assert.

A verdict that says "Measured" without saying WHEN, on WHICH BINARY, at WHICH
CODE, by WHICH MODE and under WHICH VARIABLES is a perishable claim: it can
be true when written and false an hour later without a single line of the
document changing. That happened -- the drag-and-drop item published
"Measured: the feature does not exist" while the same tree wired
GtkDragSource/GtkDropTarget and the released binary passed the fork's own
--drag-reorder. It was found by chance, by running the mode.

This script is the mechanical version of that lucky find. It answers, for
every checklist item: was this verdict written BEFORE the code it asserts
last changed? It does NOT answer "is it true" -- only a run does that.

    python3 scripts/acceptance-verdict-audit.py            # human table
    python3 scripts/acceptance-verdict-audit.py --json     # machine output
    python3 scripts/acceptance-verdict-audit.py --rev <ref>

Two limits, named rather than hidden:

  1. "Behaviour commit" is APPROXIMATED by "touches src/**". It discriminates
     on the history this was written against (the commit that wrote 18 of the
     23 verdicts has src=0; the five behaviour commits after it all have
     src>=1) -- it will not discriminate always. A src-only refactor counts
     as behaviour; a behaviour change made entirely in a .blp or a build file
     does not.
  2. Staleness is decided TOPOLOGICALLY (git merge-base --is-ancestor), never
     by date. Commit dates are rewritten by every rebase -- this repository's
     branches are rebased routinely, and author-date and committer-date
     already disagree by hours on three of these commits. Ancestry does not
     move. The blame date is printed for the reader, and is NOT used for any
     verdict: the date a LINE was written is not the date the MEASUREMENT was
     taken, which is the whole defect this script exists to expose.
"""
import argparse, json, re, subprocess, sys

DOC = "docs/acceptance.md"
ITEM_RE = re.compile(r"^- \[([ x])\] ")
MEASURED_RE = re.compile(r"\*\*Measured", re.I)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MODE_RE = re.compile(r"--([a-z0-9]+(?:-[a-z0-9]+)*)\b")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def blame_commits(rev):
    """line number -> (short sha, committer epoch), for DOC at rev."""
    out = git("blame", "--line-porcelain", rev, "--", DOC).split("\n")
    by_line, cur = {}, None
    for line in out:
        m = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", line)
        if m:
            cur = {"sha": m.group(1), "ln": int(m.group(2))}
        elif line.startswith("committer-time ") and cur:
            cur["t"] = int(line.split()[1])
        elif line.startswith("\t") and cur:
            by_line[cur["ln"]] = (cur["sha"], cur.get("t"))
            cur = None
    return by_line


def last_behaviour_commit(rev):
    out = git("log", rev, "--format=%H", "--", "src/").split()
    return out[0] if out else None


def is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b]).returncode == 0


def modes_available():
    src = open("scripts/acceptance-harness.py", encoding="utf-8").read()
    return sorted(set(re.findall(r'"--([a-z0-9-]+)"', src)))


def audit(rev):
    doc = git("show", f"{rev}:{DOC}").split("\n")
    blame = blame_commits(rev)
    behaviour = last_behaviour_commit(rev)
    known_modes = set(modes_available())

    starts = [n for n, line in enumerate(doc, 1) if ITEM_RE.match(line)]
    items = []
    for k, ln in enumerate(starts):
        end = starts[k + 1] - 1 if k + 1 < len(starts) else len(doc)
        block = doc[ln - 1:end]
        text = "\n".join(block)
        verdict_lines = [ln + i for i, b in enumerate(block) if MEASURED_RE.search(b)]
        shas = [blame[x][0] for x in verdict_lines if x in blame]
        # the newest verdict line decides: if EVERY one predates the behaviour
        # commit, the whole verdict does.
        predates = None
        if shas and behaviour:
            predates = all(s != behaviour and is_ancestor(s, behaviour) for s in shas)
        ts = [blame[x][1] for x in verdict_lines if x in blame and blame[x][1]]
        items.append({
            "line": ln,
            "checked": ITEM_RE.match(doc[ln - 1]).group(1) == "x",
            "text": doc[ln - 1][6:].strip()[:70],
            "has_verdict": bool(verdict_lines),
            "verdict_shas": sorted({s[:9] for s in shas}),
            "blame_epoch_for_the_reader_only": max(ts) if ts else None,
            "carries_a_date_in_prose": bool(DATE_RE.search(text)),
            "names_a_mode": sorted({m for m in MODE_RE.findall(text) if m in known_modes}),
            "predates_last_behaviour_commit": predates,
        })
    return {"rev": rev, "last_behaviour_commit": behaviour[:9] if behaviour else None,
            "items": items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = audit(args.rev)
    if args.json:
        print(json.dumps(r, indent=1))
        return 0
    it = r["items"]
    stale = [i for i in it if i["predates_last_behaviour_commit"]]
    undated = [i for i in it if not i["carries_a_date_in_prose"]]
    modeless = [i for i in it if not i["names_a_mode"]]
    print(f"{DOC} at {r['rev']} -- last behaviour commit (touches src/**): {r['last_behaviour_commit']}")
    print(f"{'line':>5} {'chk':>3} {'stale':>5} {'dated':>5} {'mode':<28} text")
    for i in it:
        p = i["predates_last_behaviour_commit"]
        print(f"{i['line']:>5} {'x' if i['checked'] else ' ':>3} "
              f"{('YES' if p else 'no' if p is not None else '?'):>5} "
              f"{('yes' if i['carries_a_date_in_prose'] else 'NO'):>5} "
              f"{(','.join(i['names_a_mode']) or '-'):<28} {i['text']}")
    print()
    print(f"  {len(stale)}/{len(it)} verdicts were written BEFORE the code they assert last changed")
    print(f"  {len(undated)}/{len(it)} carry no date in their prose")
    print(f"  {len(modeless)}/{len(it)} name no harness mode -- this script cannot say whether a mode")
    print( "      covers them; it can only say the document does not carry the link")
    print()
    print( "  None of these three counts means a verdict is FALSE. Only a run says that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
