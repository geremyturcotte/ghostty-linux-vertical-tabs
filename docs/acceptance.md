# Acceptance checklist

A GTK widget does not submit to unit testing, so this file *is* the test suite
for the sidebar. Run it before every tagged release.

```bash
export PATH="$HOME/.local/bin:$PATH"
zig build --search-prefix "$HOME/.local"
./zig-out/bin/ghostty --gtk-sidebar-tabs=left
```

Two of these cases exist because adversarial review predicted the failure before
a line was written, and both failures were real. They are marked ⚠ — if you only
have time for two checks, do those.

## Automated first

```bash
sh scripts/check-none-parity.sh
```

Diffs the fork's `none` mode against upstream's GTK/GLib warnings and asserts
`left`/`right` emit no CRITICAL. It exists because the `none` promise has been
broken twice, and nothing but a human reading a log ever caught it.

## XTEST harness

### Prerequisites — how to actually run any of this

The commands below were unrunnable from a fresh clone: they name a `.xvenv`
that nothing in this repository creates, documents, or lists the contents of.
The working recipe existed only inside `.github/workflows/fork-ci.yml`, which
is not where someone trying to *reproduce* a measurement looks. Both routes
are written out here, as measured on 2026-08-25.

**What the harness actually needs:** two Python packages — `Xlib`
(`python-xlib`) and `PIL` (`pillow`) — an X display, and a ghostty binary at
`zig-out/bin/ghostty`.

**Route 1 — what CI really does** (`fork-ci.yml`, no venv at all):

```bash
sudo apt-get install -y python3-xlib python3-pil xvfb
xvfb-run --auto-servernum -- python3 scripts/acceptance-harness.py --none-shortcut
```

**Route 2 — locally, when the system Python lacks those modules** (it does on
a stock Ubuntu desktop: `import Xlib` and `import PIL` both raise
`ModuleNotFoundError`). This is where `.xvenv` comes from:

```bash
python3 -m venv .xvenv
.xvenv/bin/pip install python-xlib pillow
echo '.xvenv/' >> .git/info/exclude          # NOT .gitignore -- see below
```

`.xvenv/` belongs in `.git/info/exclude`, deliberately: `.gitignore` is an
upstream file and every line the fork adds to it widens the rebase surface
(the fork modifies six upstream files; `.gitignore` is not one of them, and
keeping it that way is the point). The cost is that `info/exclude` is local
to one clone and is never cloned — so each checkout has to add the line
again, which is why it is written here rather than assumed.

**The display.** CI uses `xvfb-run`. On a Wayland desktop session, all three
of these are needed together, and a nested X server to point at:

```bash
Xephyr :2 -screen 1400x900 -ac -noreset &
DISPLAY=:2 GDK_BACKEND=x11 WAYLAND_DISPLAY= .xvenv/bin/python3 scripts/acceptance-harness.py --menu
```

Emptying `WAYLAND_DISPLAY` is not optional: with it set, GTK goes back to
Wayland regardless of `GDK_BACKEND`, the window is not an X window, and
synthetic clicks never take focus — the harness then reports a positive
control failure rather than a result, which is correct but wastes a run.

**The binary.** Every mode launches `zig-out/bin/ghostty`. To measure a
binary built elsewhere — a release archive, another worktree's build —
symlink it in rather than copying: `mkdir -p zig-out/bin && ln -sf
<path-to-binary> zig-out/bin/ghostty`. `launch_ghostty` raises with this
same advice if the path is missing.

### Modes

```bash
.xvenv/bin/python3 scripts/acceptance-harness.py --hamburger      # positive control only
.xvenv/bin/python3 scripts/acceptance-harness.py --menu           # Task 9: row context menu
.xvenv/bin/python3 scripts/acceptance-harness.py --scroll-colour  # Task 11: colour + scroll
.xvenv/bin/python3 scripts/acceptance-harness.py --none-parity    # none-mode UI parity
.xvenv/bin/python3 scripts/acceptance-harness.py --none-shortcut  # none-mode Ctrl+Shift+B no-op, stricter
.xvenv/bin/python3 scripts/acceptance-harness.py --drag-reorder   # tab drag-to-reorder, with a move_tab:1 control
.xvenv/bin/python3 scripts/acceptance-harness.py --panes           # v1.1: pane sub-rows under a split tab's row
.xvenv/bin/python3 scripts/acceptance-harness.py --a11y-focus      # keyboard path into the list, and back out
.xvenv/bin/python3 scripts/acceptance-harness.py --section-header  # the header names the repo at t0, not at t1
```

Three of those modes were missing from this list while present in the
harness (`--panes`, `--a11y-focus`, `--section-header`) — a reader following
this document alone would not have known they existed.

A GTK4 popover on X11 is a separate override-redirect X window, not a region
of the parent — screenshotting the parent misses it every time, which is what
produced the false "NOT MEASURABLE" verdicts below on PR #9. This harness
detects popovers by diffing the X window tree before/after a click (new
**and** remapped windows — GTK reuses popover surfaces on the 2nd+ open), and
always runs the hamburger menu as a positive control in the same run before
trusting any negative — that control requires a popup at least 200×200 (the
real menu is 349×662), not just any override-redirect window, since a bare
size-floor accepts the "Menu principal" tooltip GTK can show without the menu
itself ever opening.

`--none-parity` is a different kind of check: it verifies the UI-level half
of the `none`-mode promise (tab bar present, no sidebar, no sidebar
shortcut) that `scripts/check-none-parity.sh` structurally cannot, since
that script only diffs GTK/GLib log output, never a pixel. It reuses the
positive-control pattern (measuring `left` mode first, in the same run, to
prove the structural signals actually discriminate a sidebar-present state)
and does not modify or replace `check-none-parity.sh`. It refuses to launch
on top of a still-running instance of the same binary (a stray one skews
window placement for the next launch and produced one flaky reading during
review — the guard compares each candidate process's *actual executable*
via `/proc/<pid>/exe`, not `pgrep -f`'s command-line text match, which a
second review caught matching an unrelated process that merely mentioned the
binary's path as a string), and re-measures its structural signals until two
consecutive readings agree rather than trusting a single screenshot after a
fixed sleep. Its "is a sidebar/tab-bar present" signals are deliberately
insensitive to how much chrome stacks above the terminal (headerbar, tab
bar, a Debug build's own banner) — an earlier fixed-offset version was found,
via independent review cross-testing against a Debug binary, to misread the
debug banner as the tab bar and the banner's own text as the shell prompt.

`--none-shortcut` is a stricter, dedicated companion using the SAME
sidebar-column-restricted detector `--none-parity` uses for its own
Ctrl+Shift+B check (x<260, not the whole window — a whole-window diff ratio
doesn't discriminate this case, since a sidebar appearing on one side and
the terminal reflowing on the other nets out to a similar percentage either
way), so the two commands can't disagree on the same binary the way an
earlier, independently-implemented whole-window check once did. Runs a
same-run positive control confirming the identical gesture produces a large
column diff in `left` mode. Also measures, informationally, whether upstream
Ghostty leaks the same unbound key onto its own terminal prompt for the
identical gesture (content-based: does new text appear at the end of the
prompt's own line, found via the LAST contiguous text-row-group in the
window, not a fixed offset, since that too was found to land on a Debug
banner's row instead of the prompt) — skipped, rather than reported
misleadingly, whenever the sidebar column itself just changed, since a
reflowed terminal's wrapped prompt produces the same "text end moved" signal
for an unrelated reason.

`--drag-reorder` measures whether GTK4 drag-and-drop actually reorders
sidebar rows. Same positive-control discipline as everything else here: it
colours the newest (focused) row a unique red, sends `move_tab:1` (wired via
a custom keybind this mode adds to its own launch config — there's no
default binding for it) to prove reordering is *observable* to the detector
at all (with exactly 3 tabs this wraps rank 3 to rank 1), and only then
attempts a synthetic drag and checks whether the marked row moved. Row
positions come from `Session.measure_row_geometry()` (below), not a
hardcoded offset — an earlier constant here was wrong, and a wrong row
target would have made a "nothing moved" reading meaningless regardless of
the positive control.

`Session.measure_row_geometry()` locates every visible row's close ("x")
button in a fixed x-band and takes the median gap between consecutive
centers, instead of trusting a hardcoded row-spacing constant.
`--scroll-colour` and `--drag-reorder` both use it. An earlier version
hardcoded 66px (guessed from one early measurement); independent review,
using this harness against a live window, measured the true spacing at
54.0px, uniform — confirmed here independently across a full 6-row window:
centers `[132.5, 186.5, 240.5, 294.5, 348.5, 402.5]`, step 54.0px exactly.
The wrong constant missed the row card entirely from row 3 onward and made
`--scroll-colour`'s band check pass only by chance (see that section below).

Each mode launches its own isolated ghostty process and prints its evidence
(a popover's X window id, or a structural pixel measurement) plus PNGs under
`.prokai/tmp/dispatch-artifacts/` (gitignored, regenerated per run).

## The rule every verdict in this document must satisfy

A **verdict is as perishable as a number.** `Measured` without a date is a
claim that can be true when written and false an hour later, with no line of
this document changing to say so. That is not hypothetical: the
drag-and-drop item published *"Measured: the feature does not exist"* while
the same tree wired `GtkDragSource`/`GtkDropTarget` and the released binary
passed this fork's own `--drag-reorder`. It was caught by chance, by running
the mode.

**Every `Measured` verdict carries five things**, on the line or right after
it:

1. **the date** of the measurement;
2. **the binary** — sha256 of the published artefact, or commit sha plus
   build time for a local build. *A verdict measured on a local build is
   about that build, not about the product;* the case above was true of a
   binary built at 11:30 and false of the commit that landed at 17:43 the
   same day;
3. **the code sha** the verdict is about — so *"is this verdict older than
   the last behaviour change?"* is a mechanical question and not a manual
   audit. **That sha names the PRODUCT**, and it is not automatically the sha
   the *instrument* ran at: a published binary is built from one commit while
   the harness measuring it lives at another. When the two differ, say so and
   say what moved between them. This document already carries the case — the
   section-header verdict above cites binary `v1.3.1-sidebar.3` (built from
   `b4489d474`) beside code `9a543aa22`; `git diff b4489d474 9a543aa22 --
   src/` is **empty**, so the product did not move and the verdict holds,
   while `scripts/acceptance-harness.py` moved by `+223/-18` — additively,
   with `thresh = 150` unchanged — which is why the probe reading is still
   comparable. Two shas with nothing said about the gap invites the reader to
   assume the binary came from the code;
4. **the mode** of `scripts/acceptance-harness.py` that produced it, or `by
   hand` written out. This is what makes a verdict *replayable* rather than
   merely auditable;
5. **the variables known to bite**, currently: the **launch directory**, the
   **window geometry**, and the **display**. A variable that has bitten once
   joins this list and is declared from then on. The launch directory earns
   its place: it moves the harness's scanned close-button band to `(172,204)`,
   `(166,198)` or `(124,156)` on one and the same window.

**A refutation is a measurement and carries the same five.** *"Not measured"*
and *"refuted"* are not the same statement: the first is an abstention and
costs no evidence, the second is a positive claim wearing the clothes of a
limit. This document has already carried a refutation that was false, written
from a screenshot belonging to a different run than the measurement it
claimed to overturn.

**Old verdicts are not back-dated from `git blame`.** The date a *line* was
written is not the date the *measurement* was taken; filling one in from the
other would manufacture exactly the false precision this rule exists to
prevent, and would do it under the appearance of a correction. Verdicts
predating this rule stay undated and are marked as such until a run re-dates
them.

`scripts/acceptance-verdict-audit.py` reports, mechanically, which verdicts
were written before the code they assert last changed — by ancestry, never by
date, since a rebase rewrites dates and this repository rebases routinely. It
does not say whether a verdict is *true*: only a run says that.

## Regression — the escape hatch

`none` is the one promise this fork makes unconditionally. It has already been
broken once, by a `GtkSingleSelection` that autoselected on an empty model and
tripped an Adwaita assertion at window construction.

- [x] `--gtk-sidebar-tabs=none` is indistinguishable from upstream: tab bar
      present, no sidebar, no sidebar shortcut. **Measured true on
      2026-08-25, on the published `v1.3.1-sidebar.3` archive — not on a
      local build.** The partial FAIL recorded below was real when it was
      written and is kept verbatim as the record; the fix it points at
      (PR #12, branch `none-inert`) merged on 2026-08-24T15:14:38Z and is
      in `main`.
      `check-none-parity.sh` cannot verify this claim at all: it only diffs
      GTK/GLib *log* output, never a pixel or a widget, so it can pass with a
      sidebar visibly on screen in `none` mode as long as the logs happen to
      match. `scripts/acceptance-harness.py --none-parity` and
      `--none-shortcut` measure the UI directly, with the same XTEST +
      `win.get_image()` method already used two sections up for
      "`left`/`right` put the sidebar on the correct side".

      Two of the three sub-claims measure true, using two signals designed
      to be insensitive to how much chrome (headerbar, tab bar, a Debug
      build's own banner) stacks above the terminal, since a fixed-offset
      version of this check was found (independent review, cross-testing
      against a Debug binary) to misread a debug banner as the tab bar and
      the banner's own text as the shell prompt — see `_sidebar_column_signal`
      and `_terminal_start_y`'s docstrings for the redesign. A horizontal
      tab-bar-style chrome row sits above the terminal in `none` mode that
      isn't there in `left` mode (`terminal_start_y` differs by 40px between
      the two, measured in the same run against the same binary so any
      *other* chrome, like a debug banner, contributes equally to both and
      cancels out of the comparison); and the sidebar's own column (sampled
      well below any chrome, immune to how tall it is) reads as plain
      terminal background in `none` mode vs. sidebar chrome in `left` mode
      (a same-run positive control on `left` mode confirms this signal
      actually discriminates the two states).

      The third sub-claim is **false on this branch**: `Ctrl+Shift+B` is not
      a no-op in `none` mode. Measured on the sidebar's own column
      specifically (x<260, not the whole window — a whole-window diff picks
      up unrelated terminal-side effects and mislabels them "the sidebar
      toggled"; see `_shortcut_column_diff`'s docstring) — 55% of that column
      changes, matching `left` mode's 59% for the identical gesture almost
      exactly. The sidebar toggle keybind still works even though
      `--gtk-sidebar-tabs=none` is supposed to make the sidebar
      unconditionally absent. This is a new instance of the exact failure
      mode this section's intro describes ("already been broken once").
      **A fix exists**: cross-testing this same harness against PR #12
      (branch `none-inert`, not yet merged) confirms it resolves this
      sub-claim — 0% sidebar-column diff for the identical gesture, both
      `--none-parity` and `--none-shortcut` PASS. Confirming a fix exists
      elsewhere doesn't change this measurement of the `sidebar` branch as
      it stands today: left unchecked here; the fix lands on its own PR.

      **SUPERSEDED 2026-08-25 — the two paragraphs above are the record of a
      state that no longer holds, kept rather than deleted.** PR #12 merged
      (2026-08-24T15:14:38Z) and is in `main`. Re-measured against the
      *published* archive `v1.3.1-sidebar.3` (`b4489d4`), not a local build:
      `--none-shortcut` reports `diff_ratio 0.00000` on the sidebar column
      in `none` mode, against `0.96637` for the identical gesture in `left`
      mode in the same run — so the positive control confirms the gesture
      *can* move that column, and the zero is a real absence of effect.
      `check-none-parity.sh` on the same archive: `PASS —
      gtk-sidebar-tabs=none is indistinguishable from upstream`, plus
      `left` and `right` CRITICAL-free.

      One correction to this item's own text while it is being updated: it
      says `check-none-parity.sh` "cannot verify this claim at all"  because
      it only diffs logs. That remains true *of that script*, and it is why
      the CI no longer relies on it alone — `fork-ci.yml` also runs
      `acceptance-harness.py --none-shortcut` under `xvfb`, which measures
      the interface. The claim is now gated by a pixel test in CI, not by a
      log diff.

      One measurement mistake worth recording since it briefly produced a
      wrong root-cause theory: an unbound `Ctrl+Shift+B` still reaches the
      terminal and echoes a raw `8;6u` (CSI-u) sequence plus a bell — on
      PR #12's fixed binary this looked at first like a still-broken
      shortcut, but content-based comparison (not a whole-window pixel diff,
      which would conflate two different windows' title/banner/font
      differences with the one thing actually comparable) shows *upstream
      does the exact same thing* for the identical gesture: prompt-line text
      end moved from x=690 to x=740 on both binaries, an exact match.
      Independently corroborated via `ghostty +list-keybinds --default`:
      both binaries print 72 default binds and neither includes
      Ctrl+Shift+B — the sidebar toggle is a GTK accelerator, invisible to
      the keybind table, on both sides. So once the sidebar column itself
      stops responding (PR #12's fix), the leaked escape sequence is not a
      remaining defect — it's what any unbound key does on both upstream and
      the fork, and `none` mode would then be genuinely indistinguishable
      from upstream on all three sub-claims. This comparison isn't
      meaningful on the *current*, unfixed `sidebar` branch: the sidebar
      column itself changes width when it toggles, which reflows the
      prompt's wrapped text and produces the same "text end moved" signal
      for an unrelated reason — `scripts/acceptance-harness.py
      --none-shortcut` detects this confound and skips the comparison
      rather than reporting a number that isn't causally meaningful.
- [x] `none` produces **zero** `CRITICAL` lines. Compare against upstream:
      ```bash
      ./zig-out/bin/ghostty --gtk-sidebar-tabs=none --quit-after-last-window-closed=true 2>&1 \
        | grep -c CRITICAL     # must be 0
      ```
      **Measured**, and precisely scoped to what's actually guarded, because
      the guard is narrower than this bullet's own wording suggests:
      `check-none-parity.sh`'s hard "must be 0 `CRITICAL`" assertion loop
      (`for mode in left right`) does not include `none` — `none` is only
      *diffed against upstream's* log, which proves **parity with upstream**,
      not an unconditional zero. If upstream ever emitted the same CRITICAL
      line, that diff would still pass and this line would no longer be
      true, silently. What's true **today**, reproduced directly (not via
      the diff, a direct count on each binary):
      ```
      ./zig-out/bin/ghostty --gtk-sidebar-tabs=none --quit-after-last-window-closed=true 2>&1 | grep -c CRITICAL  # => 0
      /usr/bin/ghostty --quit-after-last-window-closed=true 2>&1 | grep -c CRITICAL                              # => 0
      ```
      Checked on that basis: 0 and 0, today, reproducibly — not on the
      stronger claim the bullet's own text implies. Making `none` a hard
      zero-CRITICAL gate (adding it to the script's `for mode in ...` loop)
      is a real fix, tracked as a follow-up *after* PR #7 merges, on a single
      branch — `scripts/check-none-parity.sh` is untouched here since it's
      byte-identical between PR #7 and this PR and changing it here would
      reopen a conflict already resolved on PR #7.

## The sidebar itself

- [x] `left` and `right` put the sidebar on the correct side, and the horizontal
      tab bar is hidden in both. **Measured** (XTEST + `win.get_image()`,
      separate launches): `--gtk-sidebar-tabs=left` puts the row list on the
      left with the terminal to its right, no `AdwTabBar`; `--gtk-sidebar-tabs=right`
      mirrors it exactly, list on the right. Neither run shows a horizontal
      tab bar.
- [x] Opening a tab adds a row; closing one removes it. **Measured**: opened 2
      tabs via `Ctrl+Shift+T` — row count went 1→2→3, each new row title
      matching the tab's cwd. Closed a specific row's `×` button — row count
      went 3→2, only that row disappeared, the other two and the active tab
      were untouched (see also the close-button-targeting note below).
- [x] Titles update live as the shell changes them (`printf '\033]0;hello\007'`).
      **Measured, with a real gotcha**: a naive `printf '\033]0;LIVE-TITLE-TEST\007'`
      followed by Enter showed *no* visible change — because this system's
      shell prompt (`PROMPT_COMMAND`) fires its own OSC-0 title-set on the very
      next prompt, overwriting the manual one before the screenshot landed.
      Confirmed live update is real by appending `; sleep 5` so the shell stays
      busy: screenshotted mid-sleep and both the window title bar and the
      sidebar row's title read "LIVE-TITLE-TEST"; after the sleep, the next
      prompt's own OSC reverted it. The mechanism works — my first attempt
      would have been a false negative if I hadn't caught the confound.
- [x] A bell in a background tab lights that row's indicator, and it clears when
      the tab is selected. **Measured**: from an inactive row, ran
      `sleep 2; printf '\a'` then immediately switched to a different tab so the
      first one went to background before the bell fired. ~2s later the
      backgrounded row grew a 🔔 badge (also visible in the window title while
      that row was still inactive). Clicking that row to select it made the
      badge disappear immediately. (First attempt used an *unquoted* `printf \a`,
      which bash strips to a literal `a` before it reaches `printf` — no bell,
      no signal. Re-ran with `printf '\a'` quoted; worth flagging since it's an
      easy false negative to reproduce by accident.)
- [x] The per-row close button closes **that** row's tab, not another one.
      **Measured**: with 3 rows open, clicked the `×` on the *middle* row
      (`/tmp`). Only that row vanished; row 1 and row 3 (and row 1's active
      terminal content/scrollback) were unchanged.

## ⚠ Focus — type, do not just click

`tabViewSelectedPage` never calls `grabFocus`, so nothing returns the keyboard
to the terminal unless the sidebar does it explicitly. A miss here is
intermittent and focus-state dependent, which makes it miserable to find later.

- [x] Click a sidebar row, then **type immediately**: the terminal receives the
      keystrokes, with no extra click. **Measured** repeatedly across this
      whole sweep: every `click(row); type_string(...)` pair (no intervening
      click) landed cleanly in the terminal — confirmed by sentinel commands
      (`echo L56-OK`, etc.) executing and printing their own output.
- [x] Toggle the sidebar closed while it holds focus, then **type**: the
      terminal receives the keystrokes. **Measured**: clicked a row (which, per
      the arrow-key finding below, immediately hands focus to the terminal —
      the checklist's "while it holds focus" precondition doesn't actually
      arise, see the note on that item), then `Ctrl+Shift+B` to collapse the
      sidebar, then typed `echo L58-OK` with no extra click — executed
      correctly.
- [x] Narrow the window until the sidebar collapses, open it, dismiss it, then
      **type**: the terminal receives the keystrokes. **Measured**: resized
      the X11 window to 650px (below the ~700px threshold), sidebar
      auto-hid entirely; clicked the titlebar toggle to reveal it as an
      overlay (drawn on top of the terminal, not shrinking it — see the
      Adaptive section); clicked the terminal area outside the overlay to
      dismiss it; typed `echo L60-OK` immediately — executed correctly.

## ⚠ Hover — do not click

In `AdwTabPages`, selecting *is* switching. With `single-click-activate`, a
model handed over unwrapped would switch the live terminal on mouse-over. The
`GtkSingleSelection` wrapper is what prevents it.

- [x] Move the pointer slowly across every row **without clicking**. The active
      terminal must not change. **Measured**: sent a `MotionNotify`-only sweep
      (no button events) across all three rows and back, window title read
      before and after — unchanged, and the screenshot after the sweep shows
      the same row highlighted as before it started. `GtkSingleSelection`
      does its job.
- [x] Arrow-key through the sidebar. Note the behaviour and confirm it is the
      same every time. **Measured** (XTEST-driven live run, `zig-out/bin/ghostty`
      rebuilt post-sidebar, DISPLAY=:0, 3 tabs): clicking a row activates it and
      immediately returns focus to the terminal (`sidebar.zig:121`, by design).
      From there, the sidebar `GtkListView` never holds keyboard focus again —
      there is no click, keybind, or Tab path that gives it focus without also
      activating a row. So every arrow press (Down/Down/Up/Up tried, and
      reconfirmed fresh on this branch) lands in the terminal as an ordinary
      readline keystroke (history recall / bell), never touching the sidebar
      list, and the sidebar selection never moves. Behaviour is identical on
      every press. The feared regression — a 2nd arrow leaking to the terminal
      after a 1st arrow successfully navigated the list via `rowActivated`'s
      `grabFocus()` — does not occur, because its precondition (the list
      holding focus after arrow #1) never arises: **not even arrow #1** reaches
      the list. Verdict: **ABSENT**. No code change made. The measurement was
      made under PR #8 and is reported here in full; PR #8 itself is closed
      without merging, because its copy of this document is an earlier state
      of it — merging that copy would un-check twenty measurements recorded
      since. Nothing above depends on that branch still existing.

## Synchronization

- [x] Switch tabs with `ctrl+shift+arrow`: the sidebar highlight follows.
      **Measured**: `Ctrl+Shift+Right` moved both the window title and the
      sidebar highlight from row 1 to row 2; `Ctrl+Shift+Left` moved both back.
      Highlight and title always agreed.
- [x] Switch tabs from the tab overview: the highlight follows. Focus landing on
      chrome here is **pre-existing upstream behaviour**, not a regression — see
      the design's "Deliberately out of scope". **Measured, with a methodology
      note worth keeping.** My first attempts to open the overview (clicking
      the header bar's grid button, and separately its neighbouring hamburger
      menu) appeared to do *nothing* — I fired off a grid-button click and, in
      the same breath, a 15-point click sweep over the hamburger's area with
      short waits between each, and every screenshot looked identical to
      before. I nearly wrote both off as broken. Then, moments later, an
      unrelated drag gesture's screenshot showed the tab overview **open** —
      meaning one of those earlier clicks had actually landed and was simply
      queued behind the X server / GTK main loop, not lost. I redid a single,
      isolated grid-button click with a generous `d.sync()` + 1.5s settle and
      it opened cleanly and reproducibly. **Lesson**: rapid-fire synthetic
      input without enough settle time between events produces false
      negatives here — a screenshot taken too soon reads as "nothing
      happened" when the effect just hadn't been dispatched yet. Once open,
      clicking a different tab's card in the overview closed it and switched
      the active tab; the sidebar highlight moved to match. Confirmed working.
- [ ] Drag a tab out into its own window: neither window crashes and both
      sidebars are correct. This is the path the `notify::selected-page` guards
      exist for. **Still unmeasured — and the reason it used to be called
      "measured" no longer holds.**

      The block below is kept verbatim as `SUPERSEDED 2026-08-25`. It
      concluded that tab drag-and-drop "does not exist" and that one
      `--drag-reorder` run settled both reordering *and* tear-out. Both
      halves are now false:

      1. **The DND wiring exists in this very tree.**
         `src/apprt/gtk/class/sidebar_row.zig` declares `drag_source:
         *gtk.DragSource` and `drop_target: *gtk.DropTarget`, and reorders
         through `tab_view.reorderPage(source_page, target_pos)`. It arrived
         in `0fd146333` (2026-08-24 17:43), an ancestor of `main`. This file
         was edited twice after that commit without the claim being revisited.

      2. **The published artefact passes `--drag-reorder`.** Measured against
         the release binary `v1.3.1-sidebar.3` (sha256 `63caaf67…5a2ab`,
         checked against the release's own `SHA256SUMS`), not a local dev
         build: `exit 0`, *"the synthetic drag moved the coloured row from
         rank 1 to rank 3, with the same-run `move_tab:1` positive control
         also green"*. Reproduced twice. The earlier "zero effect" reading
         came from a dev binary built **before** `0fd146333`, so it was true
         of that binary and false of the product — the reason this checklist
         is replayed against the *published* artefact after every release.

      Two conditions must travel with that PASS, or it is not reproducible:

      - **Launch directory matters.** `--drag-reorder` passes from a
        non-repository working directory and *abstains* (`exit 2`, positive
        control failed) when launched from the root of a git repository,
        where `measure_row_geometry` finds one band more than there are tabs.
        Measured on the same window (922x722), same binary, same display,
        with the launch directory as the only variable.

        **That extra band is the repository section header.** *Measured
        2026-08-25 · binary `v1.3.1-sidebar.3` sha256 `63caaf67…5a2ab`
        (checked against the release's `SHA256SUMS`) · code `9a543aa22` ·
        probe reproducing `measure_row_geometry`'s exact predicate (`r>150
        and g>150 and b>150`, never an average) against the live window and
        saving the image it measured · cwd = the repository root, display
        `:7` (Xephyr 1400x900), window 922x722.* Within the scanned band
        `(172, 204)` the ink runs `y=88..98` (11px) and starts at the band's
        left edge (`x=173`); the three real rows are 8px tall with ink only
        at `x=192..199` and never touch that edge. Across the whole column
        the header inks **122 distinct columns** (`x=20..209`) against **17**
        for a row (`x=87..199`). The header is the repository name in bold,
        above every row, with no close button — introduced by `507f1fb38`
        (repository grouping, #10) and re-bound by `98a71255d`. No
        repository means no section, no header, and exactly N bands; from a
        repository, N+1. `measure_row_geometry` does not distinguish a close
        button from any bright text inside its x-band: it counts ink.

        **Those two numbers separate here and nowhere else — do not reuse
        them as a test.** Re-measured across three launch directories and
        two binaries (six cells, same window and display): from the
        repository root the header inks 122 columns against 17 for a row,
        but from a directory with a very long path the bands are
        `[178, 207, 127, 207, 127, 212, 161]` — the header's 178 sits
        *between* row values, seven bands appear for three tabs, and every
        band touches the scan band's left edge. The pre-`507f1fb38` binary
        shows no header at all from the repository root (three bands, three
        tabs) and still produces a fourth band from the long path. So the
        section header explains the extra band **at the repository root**;
        it does not explain every extra band, and the 122-vs-17 gap is a
        property of one launch directory, not a discriminator.

        **SUPERSEDED 2026-08-25 — this item previously stated the
        opposite**, and it was wrong. Kept verbatim rather than deleted:

        > What that extra band is has **not** been measured; the reading
        > that it is a repository section header is refuted by the
        > screenshots (no header is drawn).

        That sentence claimed a *refutation* — a positive claim carrying a
        full burden of proof — while reading as a statement of a limit. It
        was written from a screenshot belonging to a **different run** than
        the measurement it was refuting, and the row **subtitle** in that
        image carries the same string as the header in a lighter weight,
        which is what made the header look absent. A negative published
        against an image not paired to its run is a negative without a
        control.
      - **SUPERSEDED 2026-08-25 — "abstains from a repository root" no
        longer holds**, on this branch. The two paragraphs above are the
        record of the state that produced that reading and are kept
        verbatim rather than deleted. `measure_row_geometry` did not
        reunite a row's title/subtitle sub-bands into one row before
        counting, so a leading, non-repeating band (the repository section
        header, or on `--menu`'s own scene a title+subtitle pair alone)
        shifted every centre after it — the same root cause behind
        `--menu`'s false "row-context-menu: confirmed ABSENT" documented
        below. `_merge_sidebar_row_bands` now derives the repeat unit from
        the bands' own height pattern and drops a non-repeating leading
        band instead of folding it into row 1.

        **Measured 2026-08-25** · binary `v1.3.1-sidebar.3` sha256
        `63caaf674cd7337cbd8a26137cc9b61287e15dd072fc08646fb6d05811a5a2ab`
        · code sha printed by the mode itself (see `_measured_declaration`)
        · mode `--drag-reorder` · cwd = repository root, display Xephyr
        `:2` (800x600 outer, 922x722 reference window this mode resizes
        itself to). `measure_row_geometry` now reports `raw_bands=[[88,
        98], [137, 144], [193, 200], [249, 256]] header=[88, 98] ->
        centers=[140.5, 196.5, 252.5]` — the header dropped, exactly 3 rows
        for 3 tabs — and the mode itself reports `PASS`, positive control
        included, from the repository root. The launch-directory condition
        this bullet used to require no longer applies to the row-detector
        version in this commit; it may still apply to
        `scripts/acceptance-harness.py` at older commits, which is why the
        record above is kept rather than deleted.
      - **Tear-out is a different feature.** `--drag-reorder` measures
        reordering *within* the sidebar. Dragging a tab **out into its own
        window** is not exercised by any harness mode, so this item stays
        unchecked as genuinely unmeasured — not as a measured absence.

      <details><summary>SUPERSEDED 2026-08-25 — the original text, kept verbatim</summary>

      ```text
      - [ ] Drag a tab out into its own window: neither window crashes and both
      sidebars are correct. This is the path the `notify::selected-page` guards
      exist for. **Measured: the feature does not exist, this is not a
      harness limitation.** Earlier rounds treated this as unmeasurable
      because a click-and-drag spawns no popover-shaped X window for the
      harness's window-tree-diff detector to see — true, but the wrong
      question. `scripts/acceptance-harness.py --drag-reorder` measures the
      underlying mechanism directly: it colours the focused tab a unique
      red, sends `move_tab:1` (a positive control — with exactly 3 tabs this
      wraps rank 3 to rank 1, proving reordering *is* observable to the
      detector), then attempts a synthetic drag from rank 1 toward rank 3
      (30 `MotionNotify` steps, 1s hold before release). The positive
      control succeeds — the marked row moves to rank 1 exactly as
      predicted — and the drag then has **zero effect**: the row stays at
      rank 1, reproduced twice with an identical reading both times
      (screenshots: `.prokai/tmp/dispatch-artifacts/drag-reorder-1-coloured-rank3.png`
      through `drag-reorder-3-after-drag.png`).

      Confirmed by reading the source directly, not just inferred from the
      negative: `src/apprt/gtk/class/sidebar.zig` and `sidebar_row.zig` wire
      no `GtkDragSource` or `GtkDropTarget` at all. The only `DropTarget`
      anywhere in the GTK apprt is `surface.zig`'s, for file drops onto the
      terminal — unrelated to tabs. There is nothing to drag. Reordering
      *within* the sidebar and tearing a tab out into its own window are
      both driven by the same missing DND wiring on the tab widget, so this
      one measurement settles both: **not "unmeasurable" — measured, and
      the feature isn't implemented.** Left unchecked (a measured absence is
      still a checklist item that isn't satisfied); implementing the DND
      wiring is out of this worker's declared scope (docs + harness only)
      and needs its own ticket.
      ```

      </details>
- [x] Close the last tab: the window closes cleanly. **Measured**: closed the
      2nd-to-last row's `×`, confirmed 1 row remained; closed that last row's
      `×`; the process exited fully within ~1s (`ps aux` showed nothing), no
      crash, no defunct process, no dialog.

## Toggle

- [x] The titlebar button collapses and reveals the sidebar. **Measured**:
      clicking the header-bar toggle at both widths (default ~922px and
      narrowed to 650px) reliably hid/showed the sidebar each time.
- [x] `Ctrl+Shift+B` does the same while the terminal has focus. **Measured**:
      with the sidebar collapsed via the button and the terminal focused
      (never clicked a row first), `Ctrl+Shift+B` reopened it.

## Adaptive

- [x] Narrow the window below ~700px: the split collapses and the sidebar
      overlays rather than permanently shrinking the terminal. **Measured**:
      resized the X11 window to 650×600 — the sidebar disappeared from the
      layout (terminal took the full width). Reopening it via the toggle drew
      it as a floating panel *on top of* the terminal (terminal content still
      visible, dimmed, underneath/beside it) rather than squeezing the
      terminal into a narrower column.
- [x] Widen it again: the sidebar returns to its own column. **Measured**:
      resized back to 922×722 — sidebar reappeared as a permanent column
      beside (not over) the terminal.

## Subtitle and colours

- [x] Each row shows the last segment of its working directory under the title,
      and it changes on `cd`. **Measured** two ways: (1) at creation, 3 tabs
      opened with `cd /tmp` / `cd /home/prokai` showed subtitles "tmp" /
      "prokai" immediately; (2) on an *already-displayed*, already-selected
      row (not freshly created), running `cd /tmp` live-updated both the
      window title and that same row's title/subtitle from "arrow-keys" to
      "/tmp" / "tmp" without needing a new tab or a reselect.
- [x] Right-click → Colour → Red marks the row. **Measured** (root cause of
      the earlier "NOT MEASURABLE" verdict found and fixed — see
      `scripts/acceptance-harness.py --menu`): the previous attempt's
      detector was wrong, not the input. A GTK4 popover on X11 opens as a
      *separate* override-redirect X window; screenshotting the parent
      window (what the earlier attempt did) can never show it, no matter how
      long you wait. XTEST's synthetic button-3 *was* opening the menu the
      whole time. Detecting by diffing `root.query_tree()` before/after the
      click (counting windows that are new **or** remapped to `IsViewable` —
      GTK reuses the popover surface after the first open, so only "new"
      undercounts from the 2nd run on) finds it every time: right-clicking
      row 1 opens a 276×156 override-redirect window at a fixed offset from
      the click, containing "Changer le titre de l'onglet…" / "Colour" /
      "Fermer l'onglet". A same-run hamburger-menu positive control (opens a
      349×662 menu) confirms popovers are reachable at all before trusting
      this result. Clicking "Colour" resizes that same popover window in
      place (276×156 → 276×252) to show None/Blue/Green/Orange/Red; clicking
      Red visibly marks the row with a red dot. **`docs/PROGRESS.md`'s Task 9
      line ("Right-click menu on rows — ✅ done, verified on screen") is
      corroborated.**

      **A second, separate false negative in `--menu` itself, found and
      fixed 2026-08-25.** `cmd_menu` opens no tab of its own — its scene is
      whatever the launched process starts with, one tab — and right-clicks
      `measure_row_geometry`'s `row1_y` at `x=40` to check the row's own
      context menu exists. Before this fix, on a single-tab window that
      x-band's title+subtitle sub-bands (and, from the repository root, the
      section header above them) came back as separate, un-reunited bands
      with no row/header distinction, so `row1_y` could BE the header's own
      centre — the click landed above the row entirely, and `--menu`
      published `row-context-menu: confirmed ABSENT`, a false negative with
      a real popover behind it.

      **Measured 2026-08-25** · binary `v1.3.1-sidebar.3` sha256
      `63caaf674cd7337cbd8a26137cc9b61287e15dd072fc08646fb6d05811a5a2ab`
      (matches the archive named throughout this document) · code sha
      `0d1a97af8eee47a15ad7c8f735ddd921e11a8db7` (pre-fix) vs. this commit
      (post-fix) · mode `scripts/acceptance-harness.py --menu` · cwd =
      repository root, display Xephyr `:2` (800x600), one tab. Same run
      shape, code toggled via `git stash`, nothing else changed:

      ```
      pre-fix:  measure_row_geometry: centers=[93.0, 133.0, 150.5] heights=[11, 15, 10]
                menu: right-clicking (40, 93)
                row-context-menu: confirmed ABSENT                          # exit 1

      post-fix: measure_row_geometry: raw_bands=[[88, 98], [126, 140], [146, 155]]
                header=[88, 98] -> centers=[140.5] heights=[30]
                menu: row1 spans y=[126,155] -- right-clicking (40, 140)
                row-context-menu: PRESENT — 258x156 popover                 # exit 0
      ```

      `_merge_sidebar_row_bands` (in `scripts/acceptance-harness.py`) fixes
      this by reuniting a row's sub-bands and dropping a non-repeating
      leading band — deriving the sub-band count per row from the height
      sequence's own repetition (never a fixed size: the same function
      also serves the close-button x-band, where one sub-band already is
      one row). `cmd_menu` additionally now proves, in its own log line,
      that the right-click y falls inside row 1's measured span before
      publishing an absence — see its docstring-adjacent comment.
- [ ] With ~6 tabs, colour two, then scroll the list: the colours stay on the
      right tabs. Rows are recycled, so this is the case that would expose
      colour state living on the row instead of the page.

      **`--scroll-colour` does not pass. Measured 2026-08-26** · binary
      `v1.3.1-sidebar.3` sha256 `63caaf67…5a2ab`, checked against the
      release's `SHA256SUMS` · code `c0ffd5dab` · mode
      `scripts/acceptance-harness.py --scroll-colour` · Xephyr `:7`
      (1400x900 screen, fresh server for this run, checked alive before and
      after), window 922x722, launch directory = the repository root, which
      is the invocation this document documents. **Exit 1.**

      The mode does not reach the colour check. It reports
      `measure_row_geometry: centers=[93.0, 140.5, 196.5, 252.5, 308.5,
      364.5, 420.5] step_y=56.0 heights=[11, 8, 8, 8, 8, 8, 8]` and then
      `measured row1_y=93.0` — and `93.0`, the band 11px tall against 8px
      for every real row, is **the repository section header**, not a tab
      row. It colours "row 1" on the header. The same reading breaks
      `--drag-reorder`, whose positive control is asked to move a rank 1
      that is the header; only `--panes` is unaffected, because it alone
      passes `y_scan=(105, None)` and so skips that band.

      This is a defect of the HARNESS, not of the product: nothing here
      says the colours migrate. The item is unchecked because **no run
      currently demonstrates that they do not** — an unproven claim, not a
      measured failure of the feature.

      **SUPERSEDED 2026-08-26 — the verdict this item carried, kept
      verbatim:**

      > **Measured, PASS** (`scripts/acceptance-harness.py
      > --scroll-colour`)

      It was written against a local development build from 2026-08-24
      11:30, which predates `507f1fb38` (repository grouping) — that build
      draws **no section header at all**, so the defect above could not
      occur on it. The verdict was true of that binary and is false of the
      published product. That is the exact case element 2 of the rule above
      exists for, and the run behind the verdict never named its binary.

      What the original entry described of the check's own method is kept
      below, since the method is unchanged and still worth reading:

      Opened 6 tabs,
      coloured row 1 red and row 2 blue via the now-working row menu, then
      shrank the window (a raw `ConfigureWindow` resize, not XTEST — see the
      script's docstring on `cmd_scroll_colour` for why) so the 6-row list
      no longer fits and must scroll. Scrolled to the bottom and back to the
      top with synthetic mouse-wheel events — this recycles rows 1-3's list
      widgets to render rows 4-6 and back, which is exactly the case that
      would expose colour state living on the recycled widget instead of the
      tab model.

      What the check actually verifies, precisely: it locates the y-center of
      the reddish and blueish pixels in the post-scroll screenshot and
      requires the red center to fall inside row 1's expected y-band *and*
      the blue center inside row 2's band *and* neither colour present at all
      in the other row's band. An earlier version of this check only tested
      "is there a reddish pixel anywhere in the sidebar column, and a blueish
      one anywhere" — that version would have reported PASS unchanged even if
      the scroll had swapped the two colours onto each other's rows, which is
      exactly the failure this test case exists to catch (an independent
      review of the committed harness caught this). The band-restricted
      version was verified to still report PASS against the same recorded
      run.

      The row spacing the bands are built from is *measured at runtime*
      (`Session.measure_row_geometry()`), not a hardcoded constant. An
      earlier version hardcoded 66px, guessed from a single early
      measurement; independent review, using this harness against a real
      target, found the true spacing is 54.0px, uniform, verified by
      locating all 6 rows' close buttons and taking the median gap between
      their centers. Left uncorrected, that wrong constant would have missed
      the row-card entirely from row 3 onward, and the band check above only
      passed the earlier +/-25px-tolerance version *by chance* (row 2's
      wrong-constant band left 12.5px of a 25px margin; row 3's would have
      sat exactly on the boundary). Measuring the spacing removes the
      luck — verified in the current run: red center y=132.5 exactly on row
      1's measured center, blue center y=186.5 exactly on row 2's, no red in
      row 2's band, no blue in row 1's band (screenshots:
      `.prokai/tmp/dispatch-artifacts/task11-coloured-before-scroll.png`,
      `task11-after-scroll-recycle.png`). **`PROGRESS.md`'s Task 11 line
      ("Per-tab colour marks — ✅ done, verified on screen") is
      corroborated.**
