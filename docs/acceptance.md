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

## Regression — the escape hatch

`none` is the one promise this fork makes unconditionally. It has already been
broken once, by a `GtkSingleSelection` that autoselected on an empty model and
tripped an Adwaita assertion at window construction.

- [ ] `--gtk-sidebar-tabs=none` is indistinguishable from upstream: tab bar
      present, no sidebar, no sidebar shortcut. **Not measured by hand** —
      this is exactly what `scripts/check-none-parity.sh` automates
      (diffs `none` against upstream's GTK/GLib warnings). Left unchecked,
      gated on that script's verdict on PR #7, not a manual pass here.
- [ ] `none` produces **zero** `CRITICAL` lines. Compare against upstream:
      ```bash
      ./zig-out/bin/ghostty --gtk-sidebar-tabs=none --quit-after-last-window-closed=true 2>&1 \
        | grep -c CRITICAL     # must be 0
      ```
      **Not measured by hand** — same automated check as above, same gate on
      PR #7. Not re-verified manually here.

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
      exist for. **NOT MEASURABLE BY THIS HARNESS.** Attempted twice: once
      confounded by the queued-click backlog above (inconclusive), and once
      cleanly — a proper `ButtonPress` on a row, seven `MotionNotify` steps out
      past the window's right/bottom edge with waits between each, then
      `ButtonRelease` 1.1s later. No second window appeared either time, and
      the sidebar was left untouched (no crash, no stray reorder). GTK4's
      tab-tear-off is driven by the toolkit's own drag-gesture recognizer
      (`AdwTabView`'s internal `GtkDragSource`/`GtkDropTarget` machinery), not
      a plain button-down + move + button-up sequence — XTEST can synthesize
      the latter but I have no way to confirm it satisfies the former's grab
      and threshold semantics. Given the false negatives already found and
      corrected earlier in this same run (the tab-overview button, the OSC
      title, the bell), I do **not** trust a clean negative here as proof of
      absence — I can't rule out either "the harness can't trigger this" or
      "it's genuinely broken," and calling it either would be a guess. Left
      unchecked. Needs a human with a real mouse.
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
- [ ] Right-click → Colour → Red marks the row. **NOT MEASURABLE BY THIS
      HARNESS**, and I want to be precise about why given how much latency
      already fooled me once this run (see the tab-overview note above).
      This is not that: I retried right-click twice, cleanly, each with an
      explicit `d.sync()` and up to 1.5s settle before screenshotting — the
      same generous-settle recipe that *did* eventually reveal the
      tab-overview button working. Both retries showed nothing. I then ran a
      control in a completely different location — right-clicking bare
      terminal space, which upstream Ghostty normally answers with its own
      copy/paste context menu — and got the same silence. Left-click, by
      contrast, worked everywhere I tried it, every time, throughout this
      entire sweep. So this isn't a stale-frame artifact; XTEST's synthesized
      button-3 events simply never produced a popup for me, in either the
      sidebar's custom menu or Ghostty's own built-in one. That symmetry makes
      me suspect a GTK4 popup-grab/seat-state requirement that synthetic X11
      button events don't satisfy, rather than something specific to this
      fork's row menu — but I have no way to confirm that theory without a
      real pointer, so I won't report it as either "works" or "broken."
      **This directly bears on `docs/PROGRESS.md`'s Task 9 line** ("Right-click
      menu on rows — ✅ done, verified on screen"): I can neither corroborate
      nor contradict that claim. My inability to reach it says more about this
      harness's reach than about the feature. Needs a human with a real mouse.
- [ ] With ~6 tabs, colour two, then scroll the list: the colours stay on the
      right tabs. Rows are recycled, so this is the case that would expose
      colour state living on the row instead of the page. **NOT MEASURABLE BY
      THIS HARNESS** — blocked on the item above: there is no way to set a
      row's colour without the right-click menu this harness can't open, so
      there is nothing to test scroll-persistence *of*. Also bears on
      `PROGRESS.md`'s Task 11 line ("Per-tab colour marks — ✅ done, verified
      on screen") the same way: not corroborated, not contradicted.
