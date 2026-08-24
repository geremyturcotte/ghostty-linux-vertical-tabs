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

```bash
.xvenv/bin/python3 scripts/acceptance-harness.py --hamburger      # positive control only
.xvenv/bin/python3 scripts/acceptance-harness.py --menu           # Task 9: row context menu
.xvenv/bin/python3 scripts/acceptance-harness.py --scroll-colour  # Task 11: colour + scroll
.xvenv/bin/python3 scripts/acceptance-harness.py --none-parity    # none-mode UI parity
```

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
prove the pixel-based signals actually discriminate a sidebar-present state)
and does not modify or replace `check-none-parity.sh`.

Each mode launches its own isolated ghostty process and prints its evidence
(a popover's X window id, or a structural pixel measurement) plus PNGs under
`.prokai/tmp/dispatch-artifacts/` (gitignored, regenerated per run).

## Regression — the escape hatch

`none` is the one promise this fork makes unconditionally. It has already been
broken once, by a `GtkSingleSelection` that autoselected on an empty model and
tripped an Adwaita assertion at window construction.

- [ ] `--gtk-sidebar-tabs=none` is indistinguishable from upstream: tab bar
      present, no sidebar, no sidebar shortcut. **Measured, partial FAIL**
      (`scripts/acceptance-harness.py --none-parity`) — `check-none-parity.sh`
      cannot verify this claim at all: it only diffs GTK/GLib *log* output
      (CRITICAL/WARNING/assertion lines), never a pixel or a widget, so it
      can pass with a sidebar visibly on screen in `none` mode as long as the
      logs happen to match. This is a genuinely different, UI-level check,
      done with this harness's existing XTEST + `win.get_image()` method (the
      same one already used two sections up for "`left`/`right` put the
      sidebar on the correct side").

      Two of the three sub-claims measure true: with 2 tabs open, a
      horizontal tab-bar-style chrome row sits where `left` mode has none
      (dominant pixel colour in that band is chrome-grey `(69,69,69)`, vs
      `left` mode's terminal-background blue-grey `(40,44,52)` in the same
      region — a same-run positive control on `left` mode confirms this
      signal actually discriminates the two states), and the terminal's
      shell prompt starts near the window's left edge (`x=143`) rather than
      after a sidebar column (`left` mode: `x=317`, the same positive
      control) — so no sidebar column is occupying space at launch.

      The third sub-claim is **false**: `Ctrl+Shift+B` is not a no-op in
      `none` mode. Screenshotting before and after the keypress shows a
      16% pixel diff (reproduced twice, identical `0.16183` ratio both
      times) — the horizontal tab bar disappears and a 2-row vertical
      sidebar column appears in its place, indistinguishable from `left`
      mode's layout. The sidebar toggle keybind still works even though
      `--gtk-sidebar-tabs=none` is supposed to make the sidebar
      unconditionally absent. This is a new instance of the exact failure
      mode this section's intro describes ("already been broken once") —
      found by this measurement, not fixed by it: fixing the keybind gate
      is out of this worker's declared scope (docs + harness only) and
      needs its own ticket. Left unchecked.
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
      the list. Verdict: **ABSENT** (see PR #8, merged into this doc's history
      on the `sidebar` branch as commit `5c5b75d27`). No code change made.

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
      exist for. **NOT MEASURABLE BY THIS HARNESS — confirmed a distinct gap
      from the popover false negative, not the same bug.** The Task 9/11
      false negatives above turned out to share one root cause: screenshotting
      the parent window instead of diffing the X tree for the popover's own
      override-redirect window, now fixed in `scripts/acceptance-harness.py`.
      This item was re-examined against that fix and is **not** the same
      failure — a click-and-drag doesn't spawn any new or remapped
      override-redirect X window to detect in the first place (confirmed:
      running the harness's `snap()`/diff during a synthetic drag attempt
      shows no popover-shaped window at all, new or remapped). GTK4's
      tab-tear-off is driven by `AdwTabView`'s internal
      `GtkDragSource`/`GtkDropTarget` drag-gesture recognizer, which needs a
      real grab-and-threshold sequence a plain XTEST `ButtonPress` +
      `MotionNotify` steps + `ButtonRelease` isn't confirmed to satisfy — an
      X-tree diff can't help either way since the feature under test isn't a
      popover. Still needs a human with a real mouse, or a harness that can
      verify GTK's drag-gesture state directly (e.g. via GTK inspector/AT-SPI)
      rather than X-tree diffing. Left unchecked.
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
- [x] With ~6 tabs, colour two, then scroll the list: the colours stay on the
      right tabs. Rows are recycled, so this is the case that would expose
      colour state living on the row instead of the page. **Measured, PASS**
      (`scripts/acceptance-harness.py --scroll-colour`): opened 6 tabs,
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
      requires the red center to fall inside row 1's expected y-band
      (measured row height ± 25px tolerance, to absorb the ~13px drift
      observed between the pre-scroll and post-"scroll to top" layouts) *and*
      the blue center inside row 2's band *and* neither colour present at all
      in the other row's band. An earlier version of this check only tested
      "is there a reddish pixel anywhere in the sidebar column, and a blueish
      one anywhere" — that version would have reported PASS unchanged even if
      the scroll had swapped the two colours onto each other's rows, which is
      exactly the failure this test case exists to catch (an independent
      review of the committed harness caught this). The band-restricted
      version was verified to still report PASS against the same recorded
      run (screenshots: `.prokai/tmp/dispatch-artifacts/task11-coloured-before-scroll.png`,
      `task11-after-scroll-recycle.png`; measured in that run: red center
      y=132.5 inside row 1's band, blue center y=186.5 inside row 2's band,
      no red in row 2's band, no blue in row 1's band). **`PROGRESS.md`'s
      Task 11 line ("Per-tab colour marks — ✅ done, verified on screen") is
      corroborated.**
