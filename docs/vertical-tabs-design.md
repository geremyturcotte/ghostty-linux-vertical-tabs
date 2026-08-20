# Design — Vertical tab sidebar for Ghostty on Linux

**Status:** approved design, not yet implemented
**Date:** 2026-08-20
**Target:** Ghostty 1.3.1, GTK4 + libadwaita backend (`src/apprt/gtk`)

---

## 1. Why this fork exists

Ghostty has no vertical tabs on Linux, and this is a decision rather than a gap.

`gtk-tabs-location` accepted `left` and `right` under the old GTK Notebook tab
widget. libadwaita became mandatory in Ghostty 1.2.0, and libadwaita's
`AdwTabBar` is horizontal by GNOME HIG design, so those values were dropped. On
1.3.1 the validator is explicit:

```
$ ghostty +validate-config --config-file=<(echo 'gtk-tabs-location = left')
gtk-tabs-location: invalid value "left", valid values are: top, bottom
```

Upstream closed the request. Discussion #2549 ("Vertical Tabs", 70 👍, 24
replies) was answered and closed by Mitchell Hashimoto on 2026-03-13:

> Vertical tabs requires — at least on macOS — custom tab bars. We do plan on
> implementing custom tab bars at some point [...] However, it's so far off and
> so uncertain that I don't see any need to keep this feature request open when
> the short term answer is "no". Ghostty forks enable vertical tabs and that's
> an excellent option if you want that.

The thread is locked. Issues #2548 and #3180 were closed as wrong-venue, and
#11743 as `not_planned`. A fork is the sanctioned path.

**Two sidebar forks already exist, and both are macOS-only:**
`tomreinert/ghostty` ("Sidegeist") touches only
`macos/Sources/Features/Terminal/Sidebar/*.swift`, and `manaflow-ai/cmux`
describes itself as a macOS terminal. Nothing exists for the GTK backend. That
is the gap this repository fills.

## 2. Scope

**v1 ships:** a persistent, collapsible vertical sidebar listing the window's
tabs — select, close, live titles, bell indicator, keyboard toggle.

**v1 does not ship**, deliberately:

| Deferred | Why |
|---|---|
| Drag-to-reorder rows | `GtkListBox` has no native reorderable mode; this is ~200 lines of manual `GtkDragSource`/`GtkDropTarget` plumbing. The existing `move_tab` keybind already covers the need. Revisit in v1.1. |
| Per-tab icons | Ghostty never sets an icon on a page (verified — see §7). Rendering one means reaching into `GhosttyTab`/`GhosttySurface` internals, which widens the rebase surface. |
| Project/workspace grouping | A second data model on top of `AdwTabView`. Separate feature, separate design. |
| Git panel | Out of scope. This is a tab sidebar. |
| Packaging (AUR/deb/AppImage) | Build-from-source first. A second `ghostty` binary also collides in `/usr/bin`. |

## 3. Architecture

Upstream's tree in `src/apprt/gtk/ui/1.5/window.blp`:

```
Adw.ApplicationWindow
└ Adw.TabOverview tab_overview        (view: tab_view)
  └ Adw.ToolbarView toolbar
    ├ [top] Adw.HeaderBar
    ├ [top] Adw.TabBar tab_bar        (view: tab_view, visible: bind template.tabs-visible)
    └ Box → Adw.ToastOverlay → Adw.TabView tab_view
```

With the sidebar (⬤ new, ▸ modified):

```
Adw.ApplicationWindow
└ Adw.TabOverview tab_overview
  └ Adw.ToolbarView toolbar
    ├ [top] Adw.HeaderBar             unchanged, spans full width
    ├ [top] Adw.TabBar tab_bar      ▸ hidden while the sidebar is active
    └ content: Adw.OverlaySplitView ⬤ libadwaita ≥ 1.4
        ├ sidebar: $GhosttySidebar  ⬤ class/sidebar.zig + ui/1.5/sidebar.blp
        └ content: Box → Adw.ToastOverlay → Adw.TabView tab_view   unchanged
```

### The sidebar is NOT full-height — this is deliberate

A VS Code-style sidebar spanning the title bar would mean inserting
`AdwOverlaySplitView` *above* `AdwToolbarView`. An adversarial review of that
choice produced three independent objections, and any one of them is
disqualifying:

1. `AdwHeaderBar` and its CSD window controls live inside `AdwToolbarView`. Above
   it, the header bar would span only the content column, not the window —
   requiring us to reimplement client-side decorations.
2. `AdwTabOverview` animates its child subtree on open/close. The sidebar would
   be captured in that animation and reveal/collapse independently of it.
3. `AdwToolbarView`'s `raised`/`raised-border` top-bar shadow composites against
   the content edge, and renders incorrectly against an overlapping panel.

The sidebar therefore sits **inside** `AdwToolbarView`, below the header bar
(Nautilus-style, not VS Code-style). This is a real UX concession, made
knowingly, in exchange for not forking CSD handling.

### `GhosttySidebar` — a closed unit

The widget takes an `AdwTabView` and knows nothing else about Ghostty. That
boundary is what keeps the edits to `window.zig` small, and small edits are what
make rebases survivable.

- **Model:** `adw_tab_view_get_pages()` returns an `AdwTabPages` implementing
  `GtkSelectionModel`. It emits `items-changed` on open/close/reorder, and its
  selection is a live proxy of `AdwTabView:selected-page` — so row→tab and
  tab→row stay in sync without hand-written synchronization.
- **View:** `GtkListBox` + `gtk_list_box_bind_model()`, *not* `GtkListView` +
  `GtkSignalListItemFactory`. The factory pattern needs four C-ABI callbacks
  wired individually from Zig; `bind_model` needs one. At 5-30 tabs the only
  thing lost is row recycling, which is worthless at this scale.
- **Lifetime:** the pages model is owned by the `AdwTabView`. Drop the reference
  explicitly in `dispose` rather than relying on teardown order.
- **Row:** title (bound to `page.title`), tooltip (`page.tooltip`),
  `needs-attention` indicator, close button on hover.

## 4. Behavior

**Config.** A new key, `gtk-sidebar-tabs = none | left | right`, defaulting to
`left`. Reviving `gtk-tabs-location = left` was rejected: a narrow rotated tab
strip has no room for readable titles, and the title is the entire point when
several long-running sessions are open. Reusing the old name for different
behavior would also mislead anyone searching for the removed option.

**`none` must be byte-identical to upstream.** It is the escape hatch and the
first regression test. All sidebar logic sits behind that guard.

**Toggle.** A `win.toggle-sidebar` action, a header-bar button, and
`ctrl+shift+b` — verified unbound in Ghostty's defaults, and consistent with
both the `ctrl+shift+*` convention and the VS Code muscle memory.

**Tab bar.** Sidebar active ⇒ `tab_bar` hidden regardless of
`window-show-tab-bar`. Sidebar `none` ⇒ upstream behavior untouched. This hooks
into `syncAppearance()` (`window.zig:681`) as a **single call** into
`sidebar.zig`, never an inline block — a one-line rebase conflict is trivial to
resolve, a block is not.

**Adaptive collapse.** An `AdwBreakpoint` on window width flips
`AdwOverlaySplitView` to overlay mode below a threshold, so a narrow window does
not lose terminal columns to the sidebar.

**Focus.** `row-activated` calls `setSelectedPage()` **and then** `grabFocus()`
on the active surface, and the sidebar stays out of the keyboard focus chain
during normal terminal use. See §7 — this is the highest-severity risk in the
design.

## 5. Fork and rebase strategy

This repository is a **detached** copy, not a GitHub fork: GitHub excludes forks
from search by default, which defeats the point of a repository named for what
people search. Upstream is a git remote, so rebasing is unaffected.

```
main      mirrors the current upstream tag (v1.3.1); never modified
sidebar   2-3 clean commits, REBASED onto each new upstream tag
tags      v1.3.1-sidebar.1, v1.4.0-sidebar.1, …
```

Rebase, never merge: the patch stays a readable series a stranger can audit.

**Diff shape is the load-bearing constraint.** Two new files carry the logic.
Four upstream files receive insertion points only:

| File | Change |
|---|---|
| `src/apprt/gtk/ui/1.5/window.blp` | wrap content in `Adw.OverlaySplitView`, add sidebar child |
| `src/apprt/gtk/class/window.zig` | property, action, one call in `syncAppearance()` |
| `src/config/Config.zig` | the `gtk-sidebar-tabs` key |
| build glue | register `sidebar.blp` |

`window.zig` is 2346 lines and handles titlebar style, tab autohide, winproto,
config sync and tab-view signals. It will conflict on most non-trivial upstream
GTK commits. That is accepted, not denied.

**libadwaita gating.** `AdwOverlaySplitView` requires 1.4. Ghostty's
version-gated `ui/1.0|1.2|1.3|1.4|1.5` scheme means the sidebar exists only from
1.4 up; older libadwaita degrades to stock upstream behavior. No blueprint is
duplicated into `ui/1.0`–`ui/1.3`.

**License.** Ghostty is MIT. Upstream copyright is preserved and attributed. The
README states plainly that this is a fork of Ghostty by Mitchell Hashimoto and
does **not** imply the feature is coming upstream. No PR will be opened against
`ghostty-org/ghostty` — the discussion is locked and it would be closed.

## 6. Build and test

Ghostty requires **Zig 0.16.0** (`build.zig.zon` `minimum_zig_version`,
corroborated by `flake.nix`) — newer than Ubuntu's repositories, so it is a
manual install.

Phase 0 is blocking: **unmodified upstream must build and launch on the target
machine** before a line of sidebar code is written. Baseline on Ubuntu 24.04:

| Component | State |
|---|---|
| GTK 4.14.5, libadwaita 1.5.0 (runtime) | present — `OverlaySplitView` available |
| zig 0.16.0 | absent |
| blueprint-compiler | absent — Ubuntu 24.04's build may be too old for Ghostty's `.blp` syntax |
| pkg-config, `libgtk-4-dev`, `libadwaita-1-dev` | absent |

**Testing, honestly.** A GTK widget does not submit to classic TDD. What is
defensible:

1. Upstream `zig build test` stays green.
2. A versioned manual acceptance checklist, including a dedicated case for the
   focus bug in §7 — it surfaces no other way.
3. `gtk-sidebar-tabs = none` produces behavior identical to upstream.

## 7. Risks

### P0 — sidebar clicks steal keyboard focus

`tabViewSelectedPage` (`window.zig:1671-1693`) does **not** call `grabFocus`; the
only two calls in the file are at lines 1567 and 1889, on other paths. Selecting
a page therefore does not return the keyboard to the terminal surface.

Clicking a sidebar row gives focus to the `GtkListBoxRow`. Without an explicit
`grabFocus()` on the active surface, the terminal silently stops receiving
keystrokes after every sidebar-driven tab switch — intermittently, and
dependent on focus state, so it is miserable to reproduce after the fact.

This risk was surfaced by adversarial review before implementation, independently
by two reviewers, and confirmed against the source. The mitigation is in §4.

### Verified facts the design leans on

| Claim | Evidence |
|---|---|
| `page.title` and `page.tooltip` update live | `window.zig:508-519` binds both from `GhosttyTab` with `.sync_create = true` |
| `needs-attention` is usable | `window.zig:1692` clears it on tab selection |
| **No** per-page icon exists | the only `setIconName` is `window.zig:343`, on the window itself |
| `syncAppearance` re-parents the tab bar | `window.zig:757` `toolbar.remove(tab_bar)`, `:759` `addTopBar` |
| `ctrl+shift+b` is unbound | absent from `ghostty +show-config --default` |

### Unverified, carried as risk

- `blueprint-compiler` on Ubuntu 24.04 may not parse Ghostty's `.blp` syntax.
  Resolved or refuted in Phase 0.
- `AdwTabOverview`'s open/close animation and `AdwOverlaySplitView`'s reveal
  animation are independent controllers over the same subtree. One reviewer
  predicts visual corruption when both run at once. Not reproduced yet; if real,
  the fix is to lock sidebar state for the duration of the overview transition.
- Behavior of the `AdwTabPages` model if the `AdwTabView` is disposed first is
  undocumented. Handled defensively rather than relied upon.

## 8. Implementation phases

Each phase ends on a build that runs.

0. **Baseline** — unmodified upstream builds and launches.
1. **Config** — `gtk-sidebar-tabs` key plumbed, no UI.
2. **Widget** — `GhosttySidebar` standalone, list renders.
3. **Wiring** — `window.blp` insertion, `syncAppearance()` rule.
4. **Focus** — the P0 mitigation and its acceptance test.
5. **Polish** — adaptive breakpoint, keybind, header-bar button.
6. **Release** — README, GIF, tagged build.

---

## 9. AI disclosure

Ghostty's `AI_POLICY.md` requires that all AI usage be disclosed. This fork does
not contribute upstream, so the policy does not bind it — it is adopted here
voluntarily, because the audience for this repository is the Ghostty community
and the norm is a good one.

**This design document was produced with Claude Code (Opus 5), in a session with
the repository owner.** Specifically:

- Research, source reading, and drafting: AI-assisted.
- Every load-bearing factual claim was verified against primary sources rather
  than asserted from model memory — upstream file contents via the GitHub API,
  the config surface via `ghostty +validate-config` and `+show-config` on the
  target machine, and the upstream decision via GitHub Discussion #2549. Line
  numbers in §7 point at real code and can be checked.
- The architecture was subjected to adversarial multi-model review before being
  written down. Three of the original design's assumptions were rejected — a
  full-height sidebar, `GtkListView` with a factory, and the claimed maintenance
  surface — and §3, §5 and §7 record the corrections rather than hiding them.
- Scope, positioning, and product decisions were made by the repository owner.

**On the human-in-the-loop requirement:** Ghostty's policy asks that a
contributor be able to explain their code without AI assistance. That standard
applies to implementation, which has not started. It is a real commitment for a
Zig + GTK codebase, and it is taken seriously here: the phased plan in §8 exists
partly so each step stays small enough to be genuinely understood.
