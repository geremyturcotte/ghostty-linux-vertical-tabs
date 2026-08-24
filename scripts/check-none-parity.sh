#!/bin/sh
# Assert this fork's one unconditional promise: with `gtk-sidebar-tabs = none`,
# it behaves exactly as upstream Ghostty.
#
# That promise has already been broken twice, and both times the only thing
# that caught it was a human launching the binary and reading the log:
#
#   1. GtkSingleSelection autoselects on an empty model, reaching through
#      AdwTabPages into adw_tab_view_get_nth_page(0) on a tab view with no
#      pages — an Adwaita assertion at window construction.
#   2. AdwBreakpoint requires a window minimum size, and said so on every
#      launch, because the breakpoint was declared in the template rather than
#      installed only when the sidebar is on.
#
# Neither `zig build` nor `zig build test` nor four rounds of adversarial
# design review saw either one. Twice is a pattern, so the promise is a test
# now instead of a sentence in a README.
#
# Usage:  sh scripts/check-none-parity.sh [path-to-upstream-ghostty]
set -eu

FORK="./zig-out/bin/ghostty"
UPSTREAM="${1:-/usr/bin/ghostty}"

[ -x "$FORK" ] || { echo "no fork binary at $FORK — run zig build first" >&2; exit 2; }
[ -x "$UPSTREAM" ] || { echo "no upstream binary at $UPSTREAM (pass one as \$1)" >&2; exit 2; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Keep only GTK/GLib complaints. Everything else — versions, paths, pids,
# renderer chatter — differs between two builds for reasons that say nothing
# about behaviour.
noise() {
    # grep exits 1 on no match; an empty result is the good case here.
    #
    # 'Otherwise, please rebuild in a release mode.' is the CONTINUATION line
    # of the debug-build warning ('debug build' is the first line, already
    # filtered). A fork binary built with `zig build` (Debug) compared
    # against upstream's Release package emits this every run — it says
    # nothing about the sidebar, so both lines of the same warning must be
    # filtered together or the pair silently reappears as a false parity
    # break the next time the wording shifts by a line.
    grep -iE 'CRITICAL|WARNING|assertion' "$1" 2>/dev/null |
        grep -viE 'debug build|rebuild in a release mode|performance will be|GDK_DEBUG|GDK_DISABLE|hsl\(' |
        sed 's/0x[0-9a-f]*//g' |
        sort -u || true
}

# run <binary> <logfile> [extra-arg]
#
# Captures the exit status in RUN_RC. An earlier version swallowed it with
# `|| true` and reported PASS on a binary that was aborting on exit with
# `munmap_chunk(): invalid pointer` — the guard was blind to the single worst
# thing it could have caught. 124 is timeout's own code and means the window
# stayed up for the whole run, which is the healthy case here.
RUN_RC=0
run() {
    # `cmd && rc=0 || rc=$?` and not `cmd; rc=$?`: under `set -e` a failing
    # command aborts the script before the assignment ever runs.
    if [ -n "${3:-}" ]; then
        timeout 12 "$1" --quit-after-last-window-closed=true "$3" >"$2" 2>&1 &&
            RUN_RC=0 || RUN_RC=$?
    else
        timeout 12 "$1" --quit-after-last-window-closed=true >"$2" 2>&1 &&
            RUN_RC=0 || RUN_RC=$?
    fi
    return 0
}

# check_exit <label> <rc>
check_exit() {
    case "$2" in
        0|124) return 0 ;;
        13[0-9]|14[0-3])
            echo "FAIL — $1 died on a signal (exit $2). Last lines:"
            tail -5 "$3" | sed 's/^/  /'
            exit 1 ;;
        *)
            echo "FAIL — $1 exited $2, which is neither success nor timeout."
            tail -5 "$3" | sed 's/^/  /'
            exit 1 ;;
    esac
}

echo "fork:     $FORK"
echo "upstream: $UPSTREAM"
echo

run "$UPSTREAM" "$tmp/upstream.log"
check_exit "upstream" "$RUN_RC" "$tmp/upstream.log"
run "$FORK"     "$tmp/none.log" "--gtk-sidebar-tabs=none"
check_exit "none" "$RUN_RC" "$tmp/none.log"

noise "$tmp/upstream.log" > "$tmp/upstream.txt"
noise "$tmp/none.log"     > "$tmp/none.txt"

if diff -u "$tmp/upstream.txt" "$tmp/none.txt" > "$tmp/diff.txt"; then
    echo "PASS — gtk-sidebar-tabs=none is indistinguishable from upstream."
else
    echo "FAIL — none mode diverges from upstream:"
    echo
    sed 's/^/  /' "$tmp/diff.txt"
    echo
    echo "The sidebar must be inert when disabled. Something is running on the"
    echo "none path that should be behind the config guard."
    exit 1
fi

# The sidebar modes are allowed to differ from upstream, but never by emitting
# a CRITICAL. Those are always bugs, not features.
for mode in left right; do
    run "$FORK" "$tmp/$mode.log" "--gtk-sidebar-tabs=$mode"
    check_exit "$mode" "$RUN_RC" "$tmp/$mode.log"
    n="$(grep -c 'CRITICAL' "$tmp/$mode.log" || true)"
    if [ "$n" -ne 0 ]; then
        echo "FAIL — $mode emitted $n CRITICAL line(s):"
        grep 'CRITICAL' "$tmp/$mode.log" | sed 's/^/  /'
        exit 1
    fi
    echo "PASS — $mode is CRITICAL-free and exited cleanly."
done
