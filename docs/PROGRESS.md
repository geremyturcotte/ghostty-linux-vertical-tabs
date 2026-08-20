# Progress

Live status of the vertical tab sidebar. Updated as each task lands.
Plan: [`vertical-tabs-plan.md`](vertical-tabs-plan.md) · Design: [`vertical-tabs-design.md`](vertical-tabs-design.md)

| # | Task | Status |
|---|---|---|
| 0 | Toolchain + baseline build | ✅ done |
| 1 | Symbol-first code citations | ✅ done |
| 2 | `gtk-sidebar-tabs` config key | ✅ done |
| 3 | `GhosttySidebar` widget, standalone | ✅ done |
| 4 | Wire into the window, with focus | ✅ done, verified on screen |
| 5 | Toggle: action, keybind, header button | 🟡 built, shortcut untested |
| 6 | Per-row close button | ⬜ |
| 7 | Adaptive collapse + acceptance checklist | ⬜ |
| 8 | README, GIF, tagged release | ⬜ |

## What works right now

- `gtk-sidebar-tabs = none | left | right` parses, validates, and shows up in
  `ghostty +show-config --docs`. Default `left`.
- The `GhosttySidebar` widget compiles: a `GtkListView` with a Blueprint row
  template, backed by a `GtkSingleSelection` over the tab view's pages.
- The sidebar shows on screen, lists the open tabs, and switching works.
  Confirmed by hand, not inferred from a clean log: clicking a row switches the
  tab and the keyboard returns to the terminal, and hovering rows does **not**
  switch anything. Those are the two defects adversarial review predicted before
  a line was written — both absent in practice.

## Deferred to v1.1

Showing **panes** in the sidebar as a second level under each tab, renameable
in place. Deliberately out of v1: panes live in `GhosttySplitTree`, not in
`AdwTabPages`, so it needs a data model of our own rather than a tweak.

Splitting and renaming already work today without any of this —
`ctrl+shift+o` / `ctrl+shift+e` split, and the `prompt_surface_title` /
`prompt_tab_title` actions rename a pane or a tab. Those two actions ship with
**no default keybind**, which is the only thing missing; two lines of config
fix it.

## Risks the work has settled

| Risk | Verdict |
|---|---|
| Can Blueprint cast to a libadwaita type? `bind template.item as <Adw.TabPage>.title` was the first such cast in this repo. | **Resolved.** The generated `sidebar.ui` emits the canonical nested lookup against `AdwTabPage`. |
| Does Ghostty build on Ubuntu 24.04 at all? | **Resolved.** See the plan's Task 0 for the recipe and its five deviations. |

Still open, each with a named task: the close-button mechanism (Task 6), and
arrow-key behaviour through the sidebar (Task 7's checklist).

## Things that were wrong and got corrected

Recorded because a plan that only shows its successes is not useful to anyone
reading it later.

- **Zig 0.15.2, not 0.16.0.** The version was read from upstream `main` through
  the GitHub API while this branch sits on tag v1.3.1. `requireZig()` wants an
  exact major.minor match, so the newer toolchain was rejected outright.
- **Spec line numbers were ~200 off**, same root cause: `window.zig` is 2346
  lines on `main` and 2126 at v1.3.1. Citations now lead with the symbol name.
- **The first design used `GtkListBox`.** Adversarial review showed
  `gtk_list_box_bind_model()` ignores `GtkSelectionModel`, so the highlight
  would have drifted from the active tab on every keyboard switch.
- **The model was going to be handed over unwrapped.** In `AdwTabPages`,
  selecting *is* switching — with `single-click-activate`, hovering the sidebar
  would have switched the live terminal.
- **`win.close-tab` was already taken**, by a string-variant action parsed into
  a `CloseTabMode` enum. The new action is `win.close-tab-at`.
- **Task 3's build gate proved nothing at first.** Zig only compiles reachable
  code, and nothing imported `sidebar.zig` — a deliberately injected syntax
  error left the build green. Fixed by making it reachable, then re-verified the
  same way.
- **`GtkSingleSelection` autoselects by default, and that broke `none`.** On an
  empty model it forces a selection of item 0, which reaches through
  `AdwTabPages` into `adw_tab_view_get_nth_page(0)` on a tab view with no pages
  and trips an Adwaita assertion at window construction. It fired even with
  `gtk-sidebar-tabs = none`, breaking this fork's central promise. Neither
  `zig build` nor `zig build test` nor four rounds of design review caught it —
  only launching the binary did. Measured three ways (upstream 0, `none` 1,
  `left` 1), fixed with `autoselect: false`, re-measured at 0 across all three.
