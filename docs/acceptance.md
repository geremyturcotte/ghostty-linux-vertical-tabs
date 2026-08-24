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
      present, no sidebar, no sidebar shortcut.
- [ ] `none` produces **zero** `CRITICAL` lines. Compare against upstream:
      ```bash
      ./zig-out/bin/ghostty --gtk-sidebar-tabs=none --quit-after-last-window-closed=true 2>&1 \
        | grep -c CRITICAL     # must be 0
      ```

## The sidebar itself

- [ ] `left` and `right` put the sidebar on the correct side, and the horizontal
      tab bar is hidden in both.
- [ ] Opening a tab adds a row; closing one removes it.
- [ ] Titles update live as the shell changes them (`printf '\033]0;hello\007'`).
- [ ] A bell in a background tab lights that row's indicator, and it clears when
      the tab is selected.
- [ ] The per-row close button closes **that** row's tab, not another one.

## ⚠ Focus — type, do not just click

`tabViewSelectedPage` never calls `grabFocus`, so nothing returns the keyboard
to the terminal unless the sidebar does it explicitly. A miss here is
intermittent and focus-state dependent, which makes it miserable to find later.

- [ ] Click a sidebar row, then **type immediately**: the terminal receives the
      keystrokes, with no extra click.
- [ ] Toggle the sidebar closed while it holds focus, then **type**: the
      terminal receives the keystrokes.
- [ ] Narrow the window until the sidebar collapses, open it, dismiss it, then
      **type**: the terminal receives the keystrokes.

## ⚠ Hover — do not click

In `AdwTabPages`, selecting *is* switching. With `single-click-activate`, a
model handed over unwrapped would switch the live terminal on mouse-over. The
`GtkSingleSelection` wrapper is what prevents it.

- [ ] Move the pointer slowly across every row **without clicking**. The active
      terminal must not change.
- [x] Arrow-key through the sidebar. Note the behaviour and confirm it is the
      same every time. **Measured** (XTEST-driven live run, `zig-out/bin/ghostty`
      rebuilt post-sidebar, DISPLAY=:0, 3 tabs): clicking a row activates it and
      immediately returns focus to the terminal (`sidebar.zig:121`, by design).
      From there, the sidebar `GtkListView` never holds keyboard focus again —
      there is no click, keybind, or Tab path that gives it focus without also
      activating a row. So every arrow press (Down/Down/Up/Up tried) lands in
      the terminal as an ordinary readline keystroke (history recall / bell),
      never touching the sidebar list, and the sidebar selection never moves.
      Behaviour is identical on every press. The feared regression — a 2nd
      arrow leaking to the terminal after a 1st arrow successfully navigated
      the list via `rowActivated`'s `grabFocus()` — does not occur, because
      its precondition (the list holding focus after arrow #1) never arises:
      **not even arrow #1** reaches the list. Verdict: **ABSENT**. No code
      change made.

## Synchronization

- [ ] Switch tabs with `ctrl+shift+arrow`: the sidebar highlight follows.
- [ ] Switch tabs from the tab overview: the highlight follows. Focus landing on
      chrome here is **pre-existing upstream behaviour**, not a regression — see
      the design's "Deliberately out of scope".
- [ ] Drag a tab out into its own window: neither window crashes and both
      sidebars are correct. This is the path the `notify::selected-page` guards
      exist for.
- [ ] Close the last tab: the window closes cleanly.

## Toggle

- [ ] The titlebar button collapses and reveals the sidebar.
- [ ] `Ctrl+Shift+B` does the same while the terminal has focus. The accelerator
      is fixed, not configurable through `keybind =` — see the design's §5.

## Adaptive

- [ ] Narrow the window below ~700px: the split collapses and the sidebar
      overlays rather than permanently shrinking the terminal.
- [ ] Widen it again: the sidebar returns to its own column.

## Subtitle and colours

- [ ] Each row shows the last segment of its working directory under the title,
      and it changes on `cd`.
- [ ] Right-click → Colour → Red marks the row.
- [ ] With ~6 tabs, colour two, then scroll the list: the colours stay on the
      right tabs. Rows are recycled, so this is the case that would expose
      colour state living on the row instead of the page.
