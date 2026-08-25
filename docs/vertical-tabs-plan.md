# Vertical Tab Sidebar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`. Steps use checkbox (`- [ ]`) syntax.

## Context

Ghostty has no vertical tabs on Linux, and upstream declined to add them: Discussion #2549 was answered and locked by Mitchell Hashimoto on 2026-03-13 ("the short term answer is no"), pointing users to forks. The two sidebar forks that exist — `tomreinert/ghostty` (Sidegeist) and `manaflow-ai/cmux` — are **macOS-only**; both touch `macos/Sources/...Swift` exclusively. Nothing exists for the GTK backend.

This plan builds that: a persistent, collapsible sidebar listing the window's tabs, in `geremyturcotte/ghostty-linux-vertical-tabs` — a detached copy of Ghostty pinned to tag `v1.3.1`.

**Goal:** ship a vertical tab sidebar on the GTK/libadwaita backend, as a small rebasable patch series rather than a divergent fork.

**Architecture:** a `GhosttySidebar` widget (`GtkListView` + `GtkBuilderListItemFactory`, model = `Gtk.SingleSelection` wrapping `adw_tab_view_get_pages()`) hosted in an `Adw.OverlaySplitView` **inside** `Adw.ToolbarView`, below the header bar. The widget takes only an `AdwTabView` and knows nothing else about Ghostty.

**Tech stack:** Zig 0.15.2, GTK 4.14.5, libadwaita 1.5.0, GTK Blueprint, `zig build`.

**Spec:** `docs/vertical-tabs-design.md` on branch `sidebar` (420 lines; converged through 4 rounds of adversarial review, unanimous APPROVE). The plan argues from the spec — read both.

---

## Global Constraints

- **Zig 0.15.2** — measured, not the 0.16.0 this plan first claimed. `build.zig.zon`
  at **v1.3.1** pins `0.15.2`; the 0.16.0 figure came from upstream `main`.
  `requireZig()` in `src/build/zig.zig` demands an **exact major.minor match**
  with `patch >=`, so a *newer* Zig is rejected just as hard as an older one.
  `.minimum_zig_version` is a misleading name — it is a pinned minor, not a floor.
- **`blueprint-compiler` ≥ 0.16.0** (HACKING.md § Extra Dependencies). **Ubuntu 24.04
  ships 0.12.0 — too old, measured.** Built from source at **v0.22.2**.
- **The build command on this machine is** `zig build --search-prefix "$HOME/.local"`,
  with `~/.local/bin` on `PATH`. The prefix is not optional — see Task 0 § 5.
- **libadwaita ≥ 1.4** for `AdwOverlaySplitView`. Below that, degrade to stock upstream behavior. No blueprint duplicated into `ui/1.0`–`ui/1.3`.
- **`gtk-sidebar-tabs = none` must be byte-identical to upstream.** This is the escape hatch and the first regression test.
- **Diff shape is load-bearing.** Logic lives in new files. Only four upstream files receive insertion points: `ui/1.5/window.blp`, `class/window.zig`, `src/config/Config.zig`, `build/gresource.zig`. Never inline a block into `syncAppearance` — insert one call.
- **Never open a PR against `ghostty-org/ghostty`.** The discussion is locked. The `upstream` remote already has its push URL disabled.
- **MIT.** Preserve upstream copyright; never imply the feature is coming upstream.
- **Cite code by symbol first, line second.** See Task 1.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/apprt/gtk/class/sidebar.zig` | **create** | The `GhosttySidebar` GObject class: model wiring, the two sync handlers, `activate`. |
| `src/apprt/gtk/ui/1.5/sidebar.blp` | **create** | The widget tree: `GtkListView`, `Gtk.SingleSelection`, the `Gtk.ListItem` row template. |
| `src/apprt/gtk/build/gresource.zig` | modify | One line in the `blueprints` array. |
| `src/config/Config.zig` | modify | The `gtk-sidebar-tabs` key + its enum. |
| `src/apprt/gtk/ui/1.5/window.blp` | modify | Wrap content in `Adw.OverlaySplitView`; add the sidebar child. |
| `src/apprt/gtk/class/window.zig` | modify | Template child, action, one call in `syncAppearance`. **Recurring rebase conflict site.** |
| `docs/acceptance.md` | **create** | Manual acceptance checklist (see Task 7). |

---

## Task 0: Toolchain and baseline build — ✅ **DONE** (2026-08-20)

Kept as the record of what actually worked, because five of its steps were wrong
as planned. This section is the source for the README build instructions
(Task 8 Step 2).

**Result:** `zig-out/bin/ghostty` builds and runs, reports `Ghostty 1.3.1`,
rejects `gtk-tabs-location = left` and accepts `bottom` — matching the installed
upstream binary.

### The recipe that works on Ubuntu 24.04

```bash
# 1. the ONLY step needing root (must be run from a real terminal —
#    sudo cannot prompt for a password from a non-TTY tool call)
sudo apt install -y gettext pkg-config libgtk-4-dev libadwaita-1-dev gir1.2-adw-1

# 2. Zig 0.15.2 (NOT 0.16.0) — resolve the URL from the release index,
#    the arch/OS order in tarball names flipped in recent releases
mkdir -p ~/.local/bin ~/.local/opt && cd /tmp
curl -fsSL -o zig.tar.xz https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz
tar xf zig.tar.xz && mv zig-x86_64-linux-0.15.2 ~/.local/opt/zig-0.15.2
ln -sf ~/.local/opt/zig-0.15.2/zig ~/.local/bin/zig

# 3. meson+ninja in an isolated venv (PEP 668 refuses pip --user on 24.04;
#    do NOT use --break-system-packages)
python3 -m venv ~/.local/opt/build-venv
~/.local/opt/build-venv/bin/pip install --upgrade pip meson ninja
ln -sf ~/.local/opt/build-venv/bin/meson ~/.local/bin/meson
ln -sf ~/.local/opt/build-venv/bin/ninja ~/.local/bin/ninja

# 4. blueprint-compiler 0.22.2 from source (Ubuntu ships 0.12.0; Ghostty needs >= 0.16.0)
cd /tmp && git clone --depth 1 --branch v0.22.2 \
  https://gitlab.gnome.org/GNOME/blueprint-compiler.git
cd blueprint-compiler && meson setup _build --prefix="$HOME/.local" && ninja -C _build install
# meson installs the module to lib/python3/dist-packages, which Python 3.12 does
# not read; a .pth fixes it permanently, without a PYTHONPATH export
echo "$HOME/.local/lib/python3/dist-packages" > "$(python3 -m site --user-site)/blueprintcompiler.pth"

# 5. gtk4-layer-shell: the .so exists but under the wrong name (see below)
mkdir -p ~/.local/lib
ln -sf /usr/lib/libgtk4-layer-shell.so ~/.local/lib/libgtk4-layer-shell-0.so

# 6. build
cd ~/Github/ghostty-linux-vertical-tabs
export PATH="$HOME/.local/bin:$PATH"
zig build --search-prefix "$HOME/.local"
```

### The five deviations, and why each mattered

**§1 — Zig 0.15.2, not 0.16.0.** The plan read `minimum_zig_version` from
upstream `main` via the GitHub API; this branch is at v1.3.1, which pins
`0.15.2`. `requireZig()` demands an exact major.minor match, so 0.16.0 was
rejected *for being too new*. **Root cause is the same one that put the spec's
line numbers ~200 lines off: probing `main` while believing the answer described
the checked-out tag. Read the working tree for anything version-dependent.**
Both toolchains are kept side by side — 0.16.0 is what a future rebase onto
`main` will need.

**§2 — Zig tarball naming.** `zig-x86_64-linux-0.15.2.tar.xz`, not
`zig-linux-x86_64-…`. Resolve from `https://ziglang.org/download/index.json`
rather than hand-building the URL.

**§3 — blueprint-compiler installs itself out of Python's reach.** After a clean
`ninja install --prefix=$HOME/.local`, the binary is on PATH and immediately
raises `ModuleNotFoundError: No module named 'blueprintcompiler'`: meson uses the
Debian `lib/python3/dist-packages` layout while Python 3.12 reads
`lib/python3.12/site-packages`. The `.pth` above fixes it in a way that survives
a fresh terminal.

**§4 — `gettext` was required.** An earlier draft listed it; it was cut for being
"unproven". The first build reached the very end and died only on `msgfmt
failure` across ~30 `.mo` files. Cutting it was the error, not listing it.

**§5 — `gtk4-layer-shell` is an orphan.** `/usr/lib/libgtk4-layer-shell.so`
exists but **no dpkg package owns it**, there is no `.pc` and no headers, and
`libgtk4-layer-shell-dev` is not in Ubuntu 24.04 at all. The dependency is
unconditional in `src/build/Config.zig:464` — no flag opts out, even though
layer-shell only serves the Wayland quick-terminal. Zig's
`linkSystemLibrary("gtk4-layer-shell-0")` tries pkg-config, finds nothing, and
falls back to hunting `libgtk4-layer-shell-0.so`, a name that does not exist.
The symlink above supplies that name in a prefix we own; `--search-prefix` is
what makes the build look there. Linking a headerless library is safe here:
Ghostty ships its own Zig bindings, and `/usr/bin/ghostty` already links this
exact file.

### One correction this task forced on the rest of the plan

`ghostty +validate-config --config-file=<(echo …)` **panics** —
`Config.zig` asserts `std.fs.path.isAbsolute(path)`. Every verification step now
writes a real temp file instead. Already applied throughout this document.

## Task 1: Correct the spec's code citations

The spec cites `window.zig` line numbers taken from upstream `main`, but the repository is pinned at **v1.3.1**, where `window.zig` is 2126 lines rather than 2346. Every cited line is off by roughly 200. The **facts** are all still true — every symbol exists, `grabFocus` still appears exactly twice and still not inside `tabViewSelectedPage` — but the pointers send a reader to the wrong place, and a future rebase will shift them again.

**Files:**
- Modify: `docs/vertical-tabs-design.md`

**Interfaces:**
- Produces: a spec whose citations survive rebases, because they lead with symbol names.

- [ ] **Step 1: Measure both refs**

```bash
cd ~/Github/ghostty-linux-vertical-tabs
for pat in 'fn syncAppearance' 'fn tabViewSelectedPage' 'toolbar.remove' \
           '"close-tab", actionCloseTab' 'setIconName' 'setNeedsAttention'; do
  printf '%-32s v1.3.1:%s\n' "$pat" \
    "$(grep -n "$pat" src/apprt/gtk/class/window.zig | head -1 | cut -d: -f1)"
done
grep -n grabFocus src/apprt/gtk/class/window.zig
```

Expected at v1.3.1: `syncAppearance` 621, `tabViewSelectedPage` 1461, `toolbar.remove` 697, `close-tab` action 359, `setIconName` 323, `setNeedsAttention` 1482, `grabFocus` at 1357 and 1679.

- [ ] **Step 2: Rewrite every citation symbol-first**

Change the form of each citation from `window.zig:1671-1693` to
`window.zig` → `tabViewSelectedPage()` (v1.3.1: 1461). Apply to §3.4, §7 "P0", §7 "Verified facts", §7 "Deliberately out of scope".

Add one line at the top of §7 "Verified facts":

```markdown
Citations name the symbol first and the line second, because line numbers move
on every rebase and symbol names do not. Line numbers are given for **v1.3.1**,
the tag this branch is based on.
```

- [ ] **Step 3: Verify no stale numbers remain**

```bash
grep -n 'window\.zig:[0-9]' docs/vertical-tabs-design.md
```

Expected: no output, or only lines that also name a symbol and say v1.3.1.

- [ ] **Step 4: Commit**

```bash
git add docs/vertical-tabs-design.md
git commit -m "docs: cite code by symbol, with v1.3.1 line numbers

The spec's line numbers came from upstream main, where window.zig is 2346
lines; this branch is based on v1.3.1, where it is 2126. Every fact held,
but every pointer was ~200 lines off. Citations now lead with the symbol
name, which survives a rebase."
```

---

## Task 2: The `gtk-sidebar-tabs` config key

The only genuinely unit-testable piece in this plan. Ghostty parses config values in Zig with real test coverage; use it.

**Files:**
- Modify: `src/config/Config.zig` (field near `@"gtk-tabs-location"`; enum near `GtkTabsLocation`)

**Interfaces:**
- Produces: `Config.@"gtk-sidebar-tabs"` of type `Config.GtkSidebarTabs = enum { none, left, right }`, default `.left`. Tasks 4 and 5 read it.

- [ ] **Step 1: Write the failing test**

Add to the test block at the end of `src/config/Config.zig`, following the style of the neighbouring parse tests:

```zig
test "parse gtk-sidebar-tabs" {
    const testing = std.testing;
    var cfg = try Config.default(testing.allocator);
    defer cfg.deinit();

    // Default is left: the sidebar is why this fork exists.
    try testing.expectEqual(GtkSidebarTabs.left, cfg.@"gtk-sidebar-tabs");

    var it = try std.process.ArgIteratorGeneral(.{}).init(
        testing.allocator,
        "--gtk-sidebar-tabs=right",
    );
    defer it.deinit();
    try cfg.loadIter(testing.allocator, &it);
    try testing.expectEqual(GtkSidebarTabs.right, cfg.@"gtk-sidebar-tabs");
}
```

If the surrounding tests use a different loader idiom, copy theirs verbatim — consistency beats this snippet.

- [ ] **Step 2: Run it and watch it fail**

```bash
zig build test -Dtest-filter="parse gtk-sidebar-tabs" 2>&1 | tail -20
```

Expected: compile error — `GtkSidebarTabs` is undefined.

- [ ] **Step 3: Add the enum**

Next to `GtkTabsLocation` (v1.3.1: ~8985 in main's numbering; find it with `grep -n 'pub const GtkTabsLocation'`):

```zig
/// See gtk-sidebar-tabs
pub const GtkSidebarTabs = enum {
    none,
    left,
    right,
};
```

- [ ] **Step 4: Add the field**

Immediately after the `@"gtk-tabs-location"` field. The doc comment becomes the `ghostty +show-config --docs` text, so write it for a user:

```zig
/// Show tabs in a vertical sidebar instead of a horizontal tab bar.
///
/// Valid values are:
///
///  * `none` - No sidebar. Ghostty behaves exactly as upstream.
///  * `left` - Sidebar on the left of the terminal.
///  * `right` - Sidebar on the right of the terminal.
///
/// When the sidebar is shown, the horizontal tab bar is hidden regardless
/// of `window-show-tab-bar`.
///
/// Requires libadwaita 1.4 or newer. On older versions this option has no
/// effect.
@"gtk-sidebar-tabs": GtkSidebarTabs = .left,
```

- [ ] **Step 5: Run the test and the whole suite**

```bash
zig build test -Dtest-filter="parse gtk-sidebar-tabs" 2>&1 | tail -20
zig build test 2>&1 | tail -20
```

Expected: both pass. The full suite must stay green — that is the standing contract.

- [ ] **Step 6: Verify the surfaced config**

```bash
zig build && ./zig-out/bin/ghostty +show-config --default --docs | grep -A14 'gtk-sidebar-tabs'
printf 'gtk-sidebar-tabs = sideways\n' > /tmp/bad.conf
./zig-out/bin/ghostty +validate-config --config-file=/tmp/bad.conf
```

Expected: the docs block prints; the invalid value is rejected naming `none, left, right`.

- [ ] **Step 7: Commit**

```bash
git add src/config/Config.zig
git commit -m "feat: add gtk-sidebar-tabs config key

none|left|right, default left. none is the escape hatch and must stay
byte-identical to upstream. No UI yet."
```

---

## Task 3: The `GhosttySidebar` widget, standalone

Builds the widget and proves the one unverified Blueprint assumption before anything depends on it. **Not yet wired into the window.**

**Files:**
- Create: `src/apprt/gtk/ui/1.5/sidebar.blp`
- Create: `src/apprt/gtk/class/sidebar.zig`
- Modify: `src/apprt/gtk/build/gresource.zig`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Sidebar` in `class/sidebar.zig`, with `pub fn setTabView(self: *Self, tab_view: *adw.TabView) void` and GObject type name `GhosttySidebar`.

- [ ] **Step 1: Register the blueprint**

In `src/apprt/gtk/build/gresource.zig`, add one line to the `blueprints` array, keeping it beside the other 1.5 entries:

```zig
    .{ .major = 1, .minor = 5, .name = "sidebar" },
```

- [ ] **Step 2: Write the blueprint**

Create `src/apprt/gtk/ui/1.5/sidebar.blp`. `using Adw 1;` is mandatory — the row template casts to `Adw.TabPage`, and without the import blueprint-compiler cannot resolve the type:

```blueprint
using Gtk 4.0;
using Adw 1;

template $GhosttySidebar: Adw.Bin {
  Gtk.ScrolledWindow {
    hscrollbar-policy: never;
    vexpand: true;

    Gtk.ListView view {
      show-separators: false;
      single-click-activate: true;
      activate => $row_activated();

      model: Gtk.SingleSelection model {};

      styles [
        "navigation-sidebar",
      ]

      factory: Gtk.BuilderListItemFactory {
        template Gtk.ListItem {
          child: Gtk.Box {
            orientation: horizontal;
            spacing: 6;
            tooltip-text: bind template.item as <Adw.TabPage>.tooltip;

            Gtk.Label {
              ellipsize: end;
              halign: start;
              hexpand: true;
              single-line-mode: true;
              label: bind template.item as <Adw.TabPage>.title;
            }

            Gtk.Image {
              icon-name: "radio-symbolic";
              visible: bind template.item as <Adw.TabPage>.needs-attention;
            }
          };
        }
      };
    }
  }
}
```

The close button is deliberately absent — it is Task 6, whose mechanism is unsettled.

- [ ] **Step 3: Build, to settle the `Adw.TabPage` cast risk**

```bash
zig build 2>&1 | tail -30
```

Expected: compiles. **This is the moment the spec's §7 risk resolves.** No `.blp` in the repository casts to a libadwaita type (`grep -rn 'as <Adw\.' src/apprt/gtk/ui/` returns nothing), so this is the first. If blueprint-compiler rejects the cast, fall back to binding through a Zig-side expression and record the outcome in §7 — do not fight it for more than an hour.

- [ ] **Step 4: Write the class**

Create `src/apprt/gtk/class/sidebar.zig`, following the shape of `class/command_palette.zig`:

```zig
const std = @import("std");

const adw = @import("adw");
const gio = @import("gio");
const gobject = @import("gobject");
const gtk = @import("gtk");

const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
const Tab = @import("tab.zig").Tab;

const log = std.log.scoped(.gtk_ghostty_sidebar);

pub const Sidebar = extern struct {
    const Self = @This();
    parent_instance: Parent,
    pub const Parent = adw.Bin;
    pub const getGObjectType = gobject.ext.defineClass(Self, .{
        .name = "GhosttySidebar",
        .classInit = &Class.init,
        .parent_class = &Class.parent,
        .private = .{ .Type = Private, .offset = &Private.offset },
    });

    const Private = struct {
        /// The list view showing one row per tab.
        view: *gtk.ListView,

        /// Selection wrapper over the tab view's pages. Wrapping matters:
        /// in AdwTabPages, selecting IS switching, so handing it to the
        /// list view directly would switch tabs on hover.
        model: *gtk.SingleSelection,

        /// The tab view we mirror. Not owned.
        tab_view: ?*adw.TabView = null,

        pub var offset: c_int = 0;
    };

    /// Point the sidebar at a tab view. Safe to call once, at construction.
    pub fn setTabView(self: *Self, tab_view: *adw.TabView) void {
        const priv = self.private();
        priv.tab_view = tab_view;
        priv.model.setModel(tab_view.getPages().as(gio.ListModel));

        _ = gobject.Object.signals.notify.connect(
            tab_view,
            *Self,
            &notifySelectedPage,
            self,
            .{ .detail = "selected-page" },
        );
    }

    /// tab -> row. Guarded: a null page means teardown, and a page dragged
    /// out via AdwTabView::create-window is gone from this model already.
    fn notifySelectedPage(
        tab_view: *adw.TabView,
        _: *gobject.ParamSpec,
        self: *Self,
    ) callconv(.c) void {
        const priv = self.private();
        const page = tab_view.getSelectedPage() orelse return;
        const pos = tab_view.getPagePosition(page);
        if (pos < 0) return;
        priv.model.setSelected(@intCast(pos));
    }

    /// row -> tab. Switching focus back to the surface is Task 4; without
    /// it the terminal stops receiving keystrokes.
    fn rowActivated(_: *gtk.ListView, pos: c_uint, self: *Self) callconv(.c) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;
        const page = tab_view.getNthPage(@intCast(pos));
        tab_view.setSelectedPage(page);
    }

    pub const Class = extern struct {
        parent_class: Parent.Class,
        var parent: *Parent.Class = undefined;
        pub const Instance = Self;

        fn init(class: *Class) callconv(.c) void {
            gtk.Widget.Class.setTemplateFromResource(
                class.as(gtk.Widget.Class),
                comptime gresource.blueprint(.{
                    .major = 1,
                    .minor = 5,
                    .name = "sidebar",
                }),
            );

            class.bindTemplateChildPrivate("view", .{});
            class.bindTemplateChildPrivate("model", .{});
            class.bindTemplateCallback("row_activated", &rowActivated);
        }

        pub const as = C.Class.as;
        pub const bindTemplateChildPrivate = C.Class.bindTemplateChildPrivate;
        pub const bindTemplateCallback = C.Class.bindTemplateCallback;
    };

    const C = Common(Self, Private);
    pub const as = C.as;
    pub const private = C.private;
};
```

Names in `Common`, `defineClass`, and the signal-connect helpers must match `command_palette.zig` exactly — read it and copy its idiom rather than trusting this snippet's details. Add the `gio` import if `setModel` needs it.

- [ ] **Step 5: Build**

```bash
zig build 2>&1 | tail -30
```

Expected: compiles. Nothing instantiates the class yet, so there is nothing to see at runtime.

- [ ] **Step 6: Commit**

```bash
git add src/apprt/gtk/class/sidebar.zig src/apprt/gtk/ui/1.5/sidebar.blp \
        src/apprt/gtk/build/gresource.zig
git commit -m "feat: add GhosttySidebar widget, not yet wired

GtkListView + GtkBuilderListItemFactory, model is a Gtk.SingleSelection
wrapping the tab view's pages. Wrapping is deliberate: in AdwTabPages
selecting is switching, so a direct model would switch tabs on hover."
```

---

## Task 4: Wire the sidebar into the window, **with focus**

Wiring and focus ship together on purpose. A clickable sidebar without `grabFocus` is an input-dead build, and the phase rule promises a build that runs.

**Files:**
- Modify: `src/apprt/gtk/ui/1.5/window.blp`
- Modify: `src/apprt/gtk/class/window.zig`

**Interfaces:**
- Consumes: `Sidebar.setTabView()` from Task 3; `Config.@"gtk-sidebar-tabs"` from Task 2.
- Produces: a working sidebar honouring the config key.

- [ ] **Step 1: Wrap the content in `window.blp`**

The current content chain inside `Adw.ToolbarView` is `Box → Adw.ToastOverlay → Adw.TabView`. Wrap it, leaving that chain untouched inside `content:`:

```blueprint
      Adw.OverlaySplitView split_view {
        show-sidebar: false;
        sidebar-position: start;

        sidebar: $GhosttySidebar sidebar {};

        content: Box {
          orientation: vertical;

          $GhosttyDebugWarning {
            visible: bind template.debug;
          }

          Adw.ToastOverlay toast_overlay {
            Adw.TabView tab_view {
              // ... every existing attribute, unchanged
            }
          }
        };
      }
```

- [ ] **Step 2: Bind the new template children**

In `window.zig`'s `Class.init`, beside the existing `bindTemplateChildPrivate` calls:

```zig
            class.bindTemplateChildPrivate("split_view", .{});
            class.bindTemplateChildPrivate("sidebar", .{});
```

And in `window.zig`'s `Private`:

```zig
        /// The collapsible sidebar split, and the sidebar inside it.
        split_view: *adw.OverlaySplitView,
        sidebar: *Sidebar,
```

Import it at the top: `const Sidebar = @import("sidebar.zig").Sidebar;`

- [ ] **Step 3: Point the sidebar at the tab view**

In the window's init, after `tab_view` exists:

```zig
        priv.sidebar.setTabView(priv.tab_view);
```

- [ ] **Step 4: Add the focus call — the P0 mitigation**

In `class/sidebar.zig`, extend `rowActivated` so it hands the keyboard back. `tabViewSelectedPage()` in `window.zig` (v1.3.1: 1461) never calls `grabFocus`, so nothing else will:

```zig
    fn rowActivated(_: *gtk.ListView, pos: c_uint, self: *Self) callconv(.c) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;
        const page = tab_view.getNthPage(@intCast(pos));
        tab_view.setSelectedPage(page);

        // Hand the keyboard back to the terminal. Without this the
        // GtkListViewRow keeps focus and the terminal silently stops
        // receiving keystrokes after every sidebar click.
        const child = page.getChild();
        if (gobject.ext.cast(Tab, child)) |tab| {
            if (tab.getActiveSurface()) |surface| surface.grabFocus();
        }
    }
```

Read how `window.zig` reaches the active surface (`getActiveSurface`) and copy that path exactly.

- [ ] **Step 5: Honour the config key in `syncAppearance`**

Insert **one call** into `syncAppearance()` (v1.3.1: 621), never a block — this function is the recurring rebase conflict site, and a one-line conflict is resolvable while a block is not:

```zig
        self.syncSidebar(config);
```

Define `syncSidebar` elsewhere in `window.zig`, next to the other sync helpers:

```zig
    /// Show or hide the sidebar, and hide the horizontal tab bar when it
    /// is showing. `none` must leave upstream behavior untouched.
    fn syncSidebar(self: *Self, config: *const Config) void {
        const priv = self.private();
        const mode = config.@"gtk-sidebar-tabs";
        const show = mode != .none;

        priv.split_view.setShowSidebar(@intFromBool(show));
        priv.split_view.setSidebarPosition(switch (mode) {
            .right => .end,
            else => .start,
        });

        // The sidebar replaces the tab bar; never show both.
        if (show) priv.tab_bar.as(gtk.Widget).setVisible(0);
    }
```

Check how the existing code reads config (it may already hold a `*Config` in `Private`) and match it.

- [ ] **Step 6: Test all three values by hand**

```bash
zig build
./zig-out/bin/ghostty --gtk-sidebar-tabs=left    # sidebar left, no tab bar
./zig-out/bin/ghostty --gtk-sidebar-tabs=right   # sidebar right
./zig-out/bin/ghostty --gtk-sidebar-tabs=none    # identical to upstream
```

For each: open 3 tabs, click a sidebar row, **then type** — the terminal must receive the keystrokes. Switch tabs with `ctrl+shift+arrow` and confirm the sidebar highlight follows. Hover slowly across the rows without clicking and confirm the terminal does **not** switch.

- [ ] **Step 7: Run the full suite and commit**

```bash
zig build test 2>&1 | tail -20
git add src/apprt/gtk/ui/1.5/window.blp src/apprt/gtk/class/window.zig \
        src/apprt/gtk/class/sidebar.zig
git commit -m "feat: wire the sidebar into the window, with focus handoff

syncAppearance gets one call, not a block, so the recurring rebase
conflict stays resolvable. rowActivated hands the keyboard back to the
surface: tabViewSelectedPage never calls grabFocus, so nothing else does."
```

---

## Task 5: Toggle — action, keybind, header-bar button

**Files:**
- Modify: `src/apprt/gtk/class/window.zig`, `src/apprt/gtk/ui/1.5/window.blp`

**Interfaces:**
- Consumes: `split_view` from Task 4.
- Produces: the `win.toggle-sidebar` action.

- [ ] **Step 1: Register the action**

In `window.zig`'s action array (v1.3.1: ~359, where `close-tab` is registered), keeping alphabetical order:

```zig
            .init("toggle-sidebar", actionToggleSidebar, null),
```

```zig
    fn actionToggleSidebar(
        _: *gio.SimpleAction,
        _: ?*glib.Variant,
        self: *Window,
    ) callconv(.c) void {
        const priv = self.private();
        const showing = priv.split_view.getShowSidebar() != 0;
        priv.split_view.setShowSidebar(@intFromBool(!showing));
    }
```

- [ ] **Step 2: Restore focus when the sidebar hides**

The same bug has a second entrance: dismissing the sidebar while it holds focus strands the keyboard on a widget that just disappeared. Connect `notify::show-sidebar` on `split_view` and, when it becomes false, `grabFocus()` on the active surface — reuse the path from Task 4 Step 4.

- [ ] **Step 3: Add the header-bar button**

In `window.blp`'s `Adw.HeaderBar`, in the `[start]` box beside the new-tab button:

```blueprint
        Gtk.ToggleButton {
          icon-name: "sidebar-show-symbolic";
          tooltip-text: _("Toggle Tab Sidebar");
          active: bind split_view.show-sidebar bidirectional;
          can-focus: false;
          focus-on-click: false;
        }
```

`can-focus: false` matters — it keeps the button out of the terminal's focus chain, matching the neighbouring buttons.

- [ ] **Step 4: Bind `ctrl+shift+b`**

Measured free: it appears in no default keybind. Add it where the other GTK defaults live in `Config.zig`, following the exact form of its neighbours.

- [ ] **Step 5: Test and commit**

```bash
zig build && ./zig-out/bin/ghostty
```

Toggle with the button and with `ctrl+shift+b`. After each hide, **type immediately** — the terminal must respond without a click.

```bash
git commit -am "feat: toggle the sidebar via action, button and ctrl+shift+b

Hiding the sidebar restores focus to the surface: focus must never be
left on a widget that just disappeared."
```

---

## Task 6: The close button — unsettled mechanism, named fallback

Spec §3.4. Timebox the declarative attempt; the fallback is not a defeat.

**Files:**
- Modify: `src/apprt/gtk/ui/1.5/sidebar.blp`, `src/apprt/gtk/class/window.zig`

**Interfaces:**
- Produces: `win.close-tab-at` taking a `u` (uint32) row index.

- [ ] **Step 1: Register `win.close-tab-at`**

**Not `win.close-tab`** — that name is taken. It is registered as `.init("close-tab", actionCloseTab, s_variant_type)` (v1.3.1: 359) with a **string** variant parsed into a `CloseTabMode` enum; a uint32 action under that name would collide with an incompatible signature.

```zig
            .init("close-tab-at", actionCloseTabAt, u32_variant_type),
```

```zig
    fn actionCloseTabAt(
        _: *gio.SimpleAction,
        param_: ?*glib.Variant,
        self: *Window,
    ) callconv(.c) void {
        const param = param_ orelse {
            log.warn("win.close-tab-at called without a parameter", .{});
            return;
        };
        const pos = param.getUint32();
        const priv = self.private();
        const page = priv.tab_view.getNthPage(@intCast(pos));
        priv.tab_view.closePage(page);
    }
```

Find how `s_variant_type` is declared and declare the `u` equivalent the same way.

- [ ] **Step 2: Try the declarative binding first (timebox: 1 hour)**

In the row template in `sidebar.blp`:

```blueprint
            Gtk.Button {
              icon-name: "window-close-symbolic";
              valign: center;
              action-name: "win.close-tab-at";
              action-target: bind template.position;
              styles [ "flat", "circular", ]
            }
```

```bash
zig build 2>&1 | tail -20
```

Two reviewers of three predicted this fails, because `action-target` is typed `GVariant` rather than a plain bindable property, and no blueprint in this repository uses actionable attributes at all (`grep -rn 'action-name\|action-target' src/apprt/gtk/ui/` returns nothing). If it compiles, click a close button and confirm it closes **that** row's tab and not row 0.

- [ ] **Step 3: If it fails or misbehaves, take the fallback**

Switch the close button alone to a Zig-side handler: give the button a `clicked` template callback in `sidebar.zig`, read `GtkListItem:position` at click time, and activate `win.close-tab-at` imperatively with `glib.Variant.newUint32(pos)`. The design already accepts one Zig handler; a second changes nothing architecturally.

- [ ] **Step 4: Record which path won**

Update spec §3.4 to say what actually happened, and move the item out of §7 "Unverified". A design doc that records the answer is worth more than one that keeps the question.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat: per-row close button via win.close-tab-at

win.close-tab was already taken by a string-variant action parsed into
CloseTabMode, so the new action takes a uint32 row index under a new name."
```

---

## Task 7: Adaptive collapse and the acceptance checklist

**Files:**
- Modify: `src/apprt/gtk/ui/1.5/window.blp`
- Create: `docs/acceptance.md`

- [ ] **Step 1: Add the breakpoint**

On the `Adw.ApplicationWindow` template, so a narrow window stops losing terminal columns to the sidebar:

```blueprint
  Adw.Breakpoint {
    condition ("max-width: 700px")
    setters {
      split_view.collapsed: true;
    }
  }
```

- [ ] **Step 2: Write the acceptance checklist**

Create `docs/acceptance.md`. A GTK widget does not submit to unit testing, so this file is the test artifact — it must be run before every release:

```markdown
# Acceptance checklist

Run before every tagged release. Build with `zig build`, run `./zig-out/bin/ghostty`.

## Regression — the escape hatch
- [ ] `--gtk-sidebar-tabs=none` is indistinguishable from upstream: tab bar present, no sidebar, no new keybind effect.

## Sidebar basics
- [ ] `left` and `right` place the sidebar on the correct side; the horizontal tab bar is hidden in both.
- [ ] Opening a tab adds a row; closing one removes it; titles update live as the shell changes them.
- [ ] A bell in a background tab lights that row's attention indicator, and it clears when the tab is selected.

## Focus — the P0. Type, do not just click.
- [ ] Click a sidebar row, then type immediately: the terminal receives the keystrokes.
- [ ] Toggle the sidebar closed while it holds focus, then type: the terminal receives the keystrokes.
- [ ] Narrow the window until the sidebar collapses, open it, dismiss it, then type: the terminal receives the keystrokes.

## Hover — the round-2 defect
- [ ] Move the pointer slowly across every row without clicking. The active terminal must NOT change.
- [ ] Arrow-key through the sidebar. Confirm the intended behavior, and that it is the same every time.

## Sync
- [ ] Switch tabs with `ctrl+shift+arrow`: the sidebar highlight follows.
- [ ] Switch tabs from the tab overview: the highlight follows. (Focus landing on chrome here is a known, pre-existing upstream behavior — not a regression.)
- [ ] Drag a tab out into its own window: neither window crashes, and both sidebars are correct.
- [ ] Close the last tab: the window closes cleanly, no crash.
```

- [ ] **Step 3: Run the whole checklist**

Fix what fails. If something cannot be fixed inside this task's scope, record it in spec §7 rather than leaving it unwritten.

- [ ] **Step 4: Commit**

```bash
git add src/apprt/gtk/ui/1.5/window.blp docs/acceptance.md
git commit -m "feat: adaptive collapse, plus the manual acceptance checklist

A GTK widget does not submit to unit testing; this checklist is the test
artifact, and the focus and hover cases are the two that matter."
```

---

## Task 8: Release

**Files:**
- Modify: `README.md`
- Create: `.github/workflows/build.yml` (optional)

- [ ] **Step 1: Replace the README**

The repository currently shows Ghostty's own README, which describes Ghostty and never mentions this fork — misleading to a visitor. Replace it, and be precise about three things: this is a fork of Ghostty by Mitchell Hashimoto under MIT; the feature is **not** coming upstream (#2549 is closed and locked); and it is Linux/GTK only. Link `docs/vertical-tabs-design.md` — the design doc is the most credible thing in the repository.

Carry the AI disclosure forward from spec §9 into the README, in one line with a link. Upstream's `AI_POLICY.md` requires disclosure; this fork does not contribute upstream so it is not bound, but the audience is the Ghostty community and burying the disclosure in a `docs/` file that nobody opens would be disclosure in name only.

- [ ] **Step 2: Record the build instructions that actually worked**

From Task 0, verbatim, including the blueprint-compiler-from-source step and why (`apt` ships 0.12.0, Ghostty needs ≥ 0.16.0). This is the single most valuable paragraph for anyone trying to reproduce the build.

- [ ] **Step 3: Record a GIF**

A vertical tab sidebar is a visual feature; a screenshot in the README is what makes the repository legible in five seconds.

- [ ] **Step 4: Squash the series and tag**

```bash
git rebase -i v1.3.1     # target: 2-3 clean commits, per the spec's diff-shape rule
git tag v1.3.1-sidebar.1
git push origin sidebar --tags
```

- [ ] **Step 5: Verify the diff shape**

```bash
git diff --stat v1.3.1..sidebar
```

Expected: two new source files plus docs, and insertion points in exactly `window.blp`, `window.zig`, `Config.zig`, `gresource.zig`. If a fifth upstream file appears, justify it in the spec or back it out — this constraint is what keeps rebases survivable.

---

## Verification

**Per task:** `zig build` compiles and `zig build test` stays green. No task may leave either red.

**End to end:**

```bash
cd ~/Github/ghostty-linux-vertical-tabs
zig build && zig build test
printf 'gtk-sidebar-tabs = right\n' > /tmp/ok.conf
./zig-out/bin/ghostty +validate-config --config-file=/tmp/ok.conf
./zig-out/bin/ghostty --gtk-sidebar-tabs=none    # must equal upstream
./zig-out/bin/ghostty --gtk-sidebar-tabs=left    # the feature
```

Then run `docs/acceptance.md` in full. The two cases that decide whether this shipped correctly are **type after clicking a row** and **hover without clicking** — the first is the P0 the review caught before implementation, the second is the defect that would have made the sidebar actively hostile.

**Rebase check, once:** `git fetch upstream && git rebase --onto <next-upstream-tag> v1.3.1 sidebar`, to find out early whether the diff shape holds. Expect conflicts in `window.zig` — the spec says so.

---

## Open questions carried into implementation

Each is settled by a named task, not left to drift:

| Question | Settled by |
|---|---|
| Does blueprint-compiler accept `as <Adw.TabPage>`? First such cast in the repo. | Task 3 Step 3 |
| Does `action-target: bind template.position` work? Reviewers split 2-1; no in-repo precedent. | Task 6 Steps 2-3 |
| Does Ubuntu's toolchain build Ghostty at all? | Task 0 |
| Does arrow-keying the sidebar behave acceptably under `Gtk.SingleSelection`? | Task 7 checklist |
