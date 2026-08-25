#!/bin/sh
# acceptance-abstain-gate.sh — the CLIQUET D'ABSTENTION's wrapper half.
#
# Wraps ONE acceptance-mode invocation and turns its raw exit code into a
# CI verdict that tells "I don't know" (CANNOT_MEASURE, exit 2) apart from
# "it's broken" (exit 1) — but only for modes scripts/acceptance-abstentions.txt
# names, with a reason. The ratchet guards the LIST, not the verdict:
#
#   rc=0, mode NOT listed  -> GREEN  normal pass
#   rc=0, mode LISTED      -> RED    it stopped abstaining — the list is
#                                    stale, remove the entry in this PR
#   rc=1                   -> RED    real failure, always, listed or not
#   rc=2, mode LISTED      -> GREEN  expected abstention, on record
#   rc=2, mode NOT LISTED  -> RED    unrecorded abstention — either add a
#                                    line with a reason, or fix the instrument
#   anything else          -> RED    unrecognised exit code
#
# Usage: sh scripts/acceptance-abstain-gate.sh <mode> -- <command...>
#
#   sh scripts/acceptance-abstain-gate.sh none-shortcut -- \
#       python3 scripts/acceptance-harness.py --none-shortcut
set -eu

SELF_DIR="$(dirname "$0")"
ABSTENTIONS="${ACCEPTANCE_ABSTENTIONS_FILE:-$SELF_DIR/acceptance-abstentions.txt}"

if [ "$#" -lt 3 ]; then
    echo "usage: $0 <mode> -- <command...>" >&2
    exit 64
fi

MODE="$1"
shift
if [ "$1" != "--" ]; then
    echo "usage: $0 <mode> -- <command...>" >&2
    exit 64
fi
shift

LISTED_LINE=""
if [ -f "$ABSTENTIONS" ]; then
    LISTED_LINE="$(grep -E "^[[:space:]]*${MODE}[[:space:]]*\|" "$ABSTENTIONS" 2>/dev/null | head -1 || true)"
fi
if [ -n "$LISTED_LINE" ]; then
    LISTED=1
    REASON="$(printf '%s' "$LISTED_LINE" | cut -d'|' -f2)"
else
    LISTED=0
    REASON=""
fi

echo "acceptance-abstain-gate: mode=$MODE listed=$LISTED cmd: $*"

set +e
"$@"
RC=$?
set -e

echo "acceptance-abstain-gate: mode=$MODE rc=$RC listed=$LISTED"

case "$RC" in
    0)
        if [ "$LISTED" -eq 1 ]; then
            echo "RED — '$MODE' is listed in $ABSTENTIONS as an expected"
            echo "abstention (reason: $REASON) but it just PASSED (rc=0)."
            echo "It stopped abstaining. The list is stale: remove this"
            echo "mode's line from $ABSTENTIONS in the same PR that fixed it."
            exit 1
        fi
        echo "GREEN — '$MODE' passed (rc=0), not listed as an abstainer."
        exit 0
        ;;
    1)
        echo "RED — '$MODE' failed (rc=1). A real failure is never masked"
        echo "by this gate, listed or not."
        exit 1
        ;;
    2)
        if [ "$LISTED" -eq 1 ]; then
            echo "GREEN — '$MODE' abstained (rc=2, CANNOT_MEASURE), and it is"
            echo "listed in $ABSTENTIONS (reason: $REASON). Expected."
            exit 0
        fi
        echo "RED — '$MODE' abstained (rc=2, CANNOT_MEASURE) but is NOT"
        echo "listed in $ABSTENTIONS. An unrecorded abstention is a hole in"
        echo "coverage, not a known limitation: add a line with a reason and"
        echo "a chantier tag, or fix the instrument so it stops abstaining."
        exit 1
        ;;
    *)
        echo "RED — '$MODE' exited $RC, which this gate does not recognise"
        echo "(only 0=pass, 1=fail, 2=CANNOT_MEASURE are defined)."
        exit 1
        ;;
esac
