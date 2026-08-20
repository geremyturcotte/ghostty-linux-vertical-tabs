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
| Drag-to-reorder rows | Reordering is owned by `AdwTabView` (`adw_tab_view_reorder_page()`), so rows need manual `GtkDragSource`/`GtkDropTarget` plumbing and drop-index math. The existing `move_tab` keybind already covers the need. Revisit in v1.1. |
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

The implementation mirrors `command_palette`, which already solves this exact
problem in this exact codebase — see `ui/1.5/command-palette.blp:30-90` and
`class/command_palette.zig:304`. It mirrors it in *topology*, not only in
widget choice; §3.3 explains why that distinction is load-bearing.

- **Model:** `Gtk.SingleSelection` wrapping the `AdwTabPages` returned by
  `adw_tab_view_get_pages()`. The wrapper is assigned from Zig, since the pages
  model originates from the window's `AdwTabView` at runtime.
- **View:** `GtkListView` with `single-click-activate: true` and a
  **`GtkBuilderListItemFactory`** whose `template Gtk.ListItem` is declared in
  Blueprint. Row properties bind declaratively
  (`label: bind template.item as <Adw.TabPage>.title`), so **no per-row Zig
  callback exists** — the only Zig handler is `activate`, three lines, exactly
  as `command_palette.zig:304`. The blueprint must declare `using Adw 1;`, and
  `Adw-1.typelib` must be present at build time (it ships in
  `libadwaita-1-dev`).
- **Synchronization** is explicit, in two small handlers:
  - row → tab: `activate` delivers a position; look up
    `tab_view.getNthPage(pos)`, call `setSelectedPage()`, then `grabFocus()`.
  - tab → row: `AdwTabView notify::selected-page` sets the wrapper's selected
    index, so the highlight follows tab switches driven from anywhere else.
    This handler **must** guard two cases before any index lookup: a null
    `selected-page` (window teardown), and a page no longer present in the local
    model — a tab dragged out via `AdwTabView::create-window` fires both
    `items-changed` and `notify::selected-page`, and an unguarded lookup there is
    a crash or a silently wrong highlight.
- **Lifetime:** the pages model is owned by the `AdwTabView`. Drop the reference
  explicitly in `dispose` rather than relying on teardown order.
- **Row:** title (`page.title`), tooltip (`page.tooltip`), `needs-attention`
  indicator, close button — see §3.4, which is the least settled part of this
  design.

### 3.3 Why the model is wrapped, and not handed over directly

The obvious design hands `AdwTabPages` to the `GtkListView` as its selection
model. `AdwTabPages` implements `GtkSelectionModel`, `GtkListView` consumes one,
and selection would then synchronize in both directions with no handlers at all.

That design is wrong, and the reason is specific: **in `AdwTabPages`, selecting
is switching.** The model proxies `AdwTabView:selected-page`, so any selection
change immediately switches the live terminal tab. `GtkListView` with
`single-click-activate: true` selects rows *on hover*. Merely moving the pointer
across the sidebar would have switched the user's terminal, and arrow-keying
through the list would have thrashed it.

`command_palette` sets `single-click-activate: true` safely because its model is
a `Gtk.SingleSelection` over a `GtkFilterListModel`, where selection carries no
side effect. Copying its widget without copying its topology would have imported
the attribute and lost the property that makes the attribute safe.

Wrapping in `Gtk.SingleSelection` restores that separation: the list owns a
selection that means "highlighted", and tab switching happens only on explicit
activation. The cost is the two handlers above — real hand-written
synchronization, chosen deliberately rather than overlooked.

### Why not `GtkListBox` + `gtk_list_box_bind_model()`

An earlier revision chose `GtkListBox`, reasoning that `GtkListView` needs four
C-ABI callbacks (`setup`/`bind`/`unbind`/`teardown`) wired individually from Zig.
That reasoning assumed `GtkSignalListItemFactory`. Ghostty does not use it:
`command_palette` uses `GtkBuilderListItemFactory`, the row is a Blueprint
template, and the per-row callback count is zero.

`GtkListBox` would also have forced the same explicit selection synchronization
in the end — it predates `GtkSelectionModel` and ignores it — while giving up
the declarative row template and the recycling that comes with `GtkListView`.


### 3.4 The close button — settled

A `GtkBuilderListItemFactory` template cannot connect a signal to a Zig
handler, so the close button needs some other way to act on its tab.

**What was tried first, and why it failed.** Binding the button's action target
to the row index — `action-name: "win.close-tab-at"` with
`action-target: bind template.position` — is the cheap answer, and it
*compiles*. Blueprint emits a real GObject property binding:

```xml
<property name="action-target" bind-source="GtkListItem"
          bind-property="position" bind-flags="sync-create"/>
```

It then fails at runtime, on the first row built:

```
CRITICAL: GLib-GObject: gbinding.c:466:
Unable to convert a value of type guint to a value of type GVariant
```

GObject has no `guint`-to-`GVariant` transform. Two of three reviewers
predicted this; they placed it at compile time, and it is a runtime failure —
right about the mechanism, wrong about the moment. A build that goes green is
not evidence here.

**What ships.** A dedicated row widget, `GhosttySidebarRow`
(`class/sidebar_row.zig` + `ui/1.5/sidebar-row.blp`), carrying an
`AdwTabPage` property. The factory template instantiates it and binds
`page: bind template.item as <Adw.TabPage>` — an object-to-object binding,
which GObject handles natively with no transform. The row owns its page, so it
needs neither an index nor a variant, and its own template can carry a real
`clicked` handler. It reaches the tab view with `ext.getAncestor(Window, …)`,
the same idiom `split_tree.zig` and `title_dialog.zig` already use.

`win.close-tab-at(u)` remains registered — deliberately not named
`win.close-tab`, which already exists with a string variant parsed into a
`CloseTabMode`. It is reachable from a menu or from Zig, and reusing the taken
name would have been a signature collision.

The declarative row survives intact: the title, tooltip and attention
indicator are still Blueprint bindings, and there is still no per-row Zig
callback in the list factory itself.
### P0 — sidebar clicks steal keyboard focus

`tabViewSelectedPage()` in `window.zig` (v1.3.1: 1461-1483) does **not** call
`grabFocus`; the only two calls in the file are elsewhere (v1.3.1: 1357, 1679).
Selecting a page therefore does not return the keyboard to the terminal surface.

Clicking a sidebar row gives focus to the `GtkListItem` widget. Without an explicit
`grabFocus()` on the active surface, the terminal silently stops receiving
keystrokes after every sidebar-driven tab switch — intermittently, and
dependent on focus state, so it is miserable to reproduce after the fact.

This risk was surfaced by adversarial review before implementation, independently
by two reviewers, and confirmed against the source. The mitigation is in §4.

**The same failure has a second entrance.** When the breakpoint puts the sidebar
in overlay mode and it is dismissed while it holds focus, focus is left nowhere
and the terminal is input-dead until the user clicks it. A
`notify::show-sidebar` handler restores focus to the active surface whenever the
sidebar hides. Both entrances are one bug — keyboard focus must never be left on
a widget that just disappeared.

### Verified facts the design leans on

Citations name the symbol first and the line second, because line numbers move on
every rebase and symbol names do not. Lines are given for **v1.3.1**, the tag this
branch is based on — where `window.zig` is 2126 lines. An earlier revision cited
upstream `main` (2346 lines) by mistake, putting every pointer ~200 lines off.


| Claim | Evidence |
|---|---|
| `page.title` and `page.tooltip` update live | `newTabPage()` binds both from `GhosttyTab` with `.sync_create = true` (v1.3.1: 471-482) |
| `needs-attention` is usable | `tabViewSelectedPage()` clears it on tab selection (v1.3.1: 1482) |
| **No** per-page icon exists | the only `setIconName` targets the window itself (v1.3.1: 323) |
| `syncAppearance()` re-parents the tab bar | `toolbar.remove(tab_bar)` then `addTopBar` (v1.3.1: 697, 699) |
| `ctrl+shift+b` is unbound | absent from `ghostty +show-config --default` |

### Deliberately out of scope

Switching tabs through `AdwTabOverview` leaves focus on the tab-view chrome
rather than the terminal surface, because `tabViewSelectedPage`
(`window.zig`, v1.3.1: 1461-1483) never calls `grabFocus`. This is **pre-existing upstream
behavior**, not something the sidebar introduces, and fixing it would widen the
diff into code this fork has no reason to own. Recorded so a future reader does
not mistake it for a sidebar regression.

Reviewers disagreed on whether `AdwTabPages` implements `GtkSelectionModel` or
only `GListModel`. The wrapped design consumes it strictly as a `GListModel`, so
it is **insensitive to the answer** — a property worth keeping.

### Unverified, carried as risk

- `blueprint-compiler` on Ubuntu 24.04 may not parse Ghostty's `.blp` syntax.
  Resolved or refuted in Phase 0.
- `AdwTabOverview`'s open/close animation and `AdwOverlaySplitView`'s reveal
  animation are independent controllers over the same subtree. One reviewer
  predicts visual corruption when both run at once. Not reproduced yet; if real,
  the fix is to lock sidebar state for the duration of the overview transition.
- Behavior of the `AdwTabPages` model if the `AdwTabView` is disposed first is
  undocumented. Handled defensively rather than relied upon.
- A Blueprint factory template binds against the item type
  (`bind template.item as <Adw.TabPage>.title`). Every in-repo use of this idiom
  casts to a Ghostty-defined type; casting to a libadwaita type is expected to
  work but is unverified here. Settled in Phase 2.
- No `.blp` in the repository casts to a libadwaita type today
  (`grep -rn 'as <Adw\.' src/apprt/gtk/ui/` returns nothing), so
  `bind template.item as <Adw.TabPage>.title` would be the first. Expected to
  work — blueprint resolves any GIR-registered type — but unproven here.
  Settled in Phase 2.

## 8. Implementation phases

Each phase ends on a build that runs.

0. **Baseline** — unmodified upstream builds and launches.
1. **Config** — `gtk-sidebar-tabs` key plumbed, no UI.
2. **Widget** — `GhosttySidebar` standalone: Blueprint row template, list renders
   against a real `AdwTabView`, selection follows in both directions.
3. **Wiring + focus** — `window.blp` insertion, `syncAppearance()` rule, and the
   `grabFocus()` call in the same `activate` handler that switches the tab.
   These are one phase on purpose: a clickable sidebar without the focus call is
   an input-dead build, and the phase rule promises a build that runs.
4. **Hardening** — acceptance checklist, close-button action, focus and
   animation edge cases.
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
- The architecture was subjected to **four rounds** of adversarial multi-model
  review before implementation, looping until a round produced no new
  substantive finding. Round 1 rejected a full-height sidebar and the claimed
  maintenance surface; round 2 caught that `single-click-activate` selects on
  hover, which would have switched the live terminal tab on mouse-over; round 3
  rejected the close-button action mechanism; round 4 returned no findings from
  any reviewer. Corrections are recorded in place — §3.3, §3.4 and §7 state what
  was wrong and why — rather than quietly replaced.
- Reviewer output was not taken on trust. Every accepted finding was checked
  against the source, and the check found things the reviewers missed: that
  Ghostty builds lists with `GtkBuilderListItemFactory` (which reversed a
  reviewer recommendation), that no blueprint in the repository casts to a
  libadwaita type, and that `win.close-tab` was already taken by an incompatible
  signature.
- Scope, positioning, and product decisions were made by the repository owner.

**On the human-in-the-loop requirement:** Ghostty's policy asks that a
contributor be able to explain their code without AI assistance. That standard
applies to implementation, which has not started. It is a real commitment for a
Zig + GTK codebase, and it is taken seriously here: the phased plan in §8 exists
partly so each step stays small enough to be genuinely understood.
