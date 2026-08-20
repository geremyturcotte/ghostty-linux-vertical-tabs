# Ghostty with vertical tabs, on Linux

A fork of [Ghostty](https://github.com/ghostty-org/ghostty) that puts the tab
list in a collapsible **vertical sidebar** on the GTK/libadwaita backend.

> **This will not arrive in upstream Ghostty.** The request was answered and
> closed by Mitchell Hashimoto in
> [Discussion #2549](https://github.com/ghostty-org/ghostty/discussions/2549)
> on 2026-03-13 — *"the short term answer is no"* — and the thread is locked.
> Forks are the path upstream explicitly points to. This is one.

<!-- SCREENSHOT: drop a PNG or GIF of the sidebar here. -->

## Why this exists

`gtk-tabs-location` used to accept `left` and `right`. libadwaita became
mandatory in Ghostty 1.2.0, and its `AdwTabBar` is horizontal by GNOME HIG
design, so those values were dropped. On 1.3.1 the validator is blunt about it:

```
$ ghostty +validate-config --config-file=/tmp/t.conf
gtk-tabs-location: invalid value "left", valid values are: top, bottom
```

Two sidebar forks already existed when this one started —
[`tomreinert/ghostty`](https://github.com/tomreinert/ghostty) ("Sidegeist") and
[`manaflow-ai/cmux`](https://github.com/manaflow-ai/cmux) — and **both are macOS
only**. Neither touches anything outside `macos/Sources/`. The GTK backend had
nothing. That is the gap this fills.

## What it does

- A vertical sidebar listing the window's tabs, on the left or the right.
- Live tab titles, with the working directory underneath — the last segment
  only, because `~/Github/prokai-plugins` ellipsizes into noise in a narrow
  column while `prokai-plugins` is what tells two tabs apart.
- The bell indicator for background tabs, and a close button on each row.
- Right-click a row to rename it, colour it, or close it. Renaming does **not**
  switch tabs first, so you can rename tab 3 while looking at tab 1.
- Five colour marks to group tabs by hand. They are Adwaita's semantic classes,
  so they follow your light/dark theme instead of being painted on.
- Toggle with the titlebar button or `Ctrl+Shift+B`.
- Collapses to an overlay on narrow windows instead of eating terminal columns.
- `gtk-sidebar-tabs = none` turns the whole thing off and leaves Ghostty exactly
  as upstream ships it — asserted by `scripts/check-none-parity.sh`, because
  that promise has been broken twice and only a human reading a log caught it.

### Configuration

```ini
# ~/.config/ghostty/config
gtk-sidebar-tabs = left     # none | left | right   (default: left)
```

While the sidebar is shown the horizontal tab bar is hidden, regardless of
`window-show-tab-bar` — they are alternatives, never both at once.

`Ctrl+Shift+B` is a fixed accelerator, **not** configurable through
`keybind =`. Making it configurable would mean adding an action to Ghostty's
input layer and threading it through four more core files; the reasoning is in
[the design doc](docs/vertical-tabs-design.md).

### Not here yet

Panes as a second level under each tab, renameable in place. Deferred
deliberately: panes live in `GhosttySplitTree`, not in `AdwTabPages`, so it
needs a data model of its own rather than a tweak.

Splitting already works without it: `Ctrl+Shift+O` and `Ctrl+Shift+E`. Renaming
a *tab* is on the sidebar's right-click menu. To rename an individual **pane**,
or to rename from the keyboard, bind the two actions Ghostty ships unbound:

```ini
keybind = ctrl+shift+r=prompt_surface_title
keybind = ctrl+shift+comma=prompt_tab_title
```

## Building

There are no packages yet; build from source.

Ghostty pins an **exact** Zig minor version, and `v1.3.1` wants **0.15.2** —
`requireZig()` rejects a newer Zig just as hard as an older one, so 0.16.0 does
not work here. Ubuntu's `blueprint-compiler` is 0.12.0 where Ghostty needs
≥ 0.16, so that is built from source too. Everything except the apt line
installs under `~/.local`.

```bash
# system packages — the only step needing root
sudo apt install -y gettext pkg-config libgtk-4-dev libadwaita-1-dev \
  gir1.2-adw-1 python3-venv

# Zig 0.15.2 (note the arch/OS order in the tarball name)
mkdir -p ~/.local/bin ~/.local/opt && cd /tmp
curl -fsSL -o zig.tar.xz https://ziglang.org/download/0.15.2/zig-x86_64-linux-0.15.2.tar.xz
tar xf zig.tar.xz && mv zig-x86_64-linux-0.15.2 ~/.local/opt/zig-0.15.2
ln -sf ~/.local/opt/zig-0.15.2/zig ~/.local/bin/zig

# meson + ninja. PEP 668 refuses pip --user on 24.04; a venv costs one line
# and does not put the system Python at risk.
python3 -m venv ~/.local/opt/build-venv
~/.local/opt/build-venv/bin/pip install --upgrade pip meson ninja
ln -sf ~/.local/opt/build-venv/bin/meson ~/.local/bin/meson
ln -sf ~/.local/opt/build-venv/bin/ninja ~/.local/bin/ninja

# blueprint-compiler 0.22.2
cd /tmp && git clone --depth 1 --branch v0.22.2 \
  https://gitlab.gnome.org/GNOME/blueprint-compiler.git
cd blueprint-compiler && meson setup _build --prefix="$HOME/.local"
ninja -C _build install
# meson installs the module where Python 3.12 does not look for it
echo "$HOME/.local/lib/python3/dist-packages" \
  > "$(python3 -m site --user-site)/blueprintcompiler.pth"

# put ~/.local/bin on PATH for good: `zig build` invokes blueprint-compiler by
# name, so a fresh terminal without this fails with "not found"
grep -q '.local/bin' ~/.profile || \
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
export PATH="$HOME/.local/bin:$PATH"
git clone https://github.com/geremyturcotte/ghostty-linux-vertical-tabs.git
cd ghostty-linux-vertical-tabs && git checkout sidebar
zig build -Doptimize=ReleaseFast --search-prefix "$HOME/.local"
./zig-out/bin/ghostty --gtk-sidebar-tabs=left
```

`-Doptimize=ReleaseFast` matters. Zig defaults to a debug build, which for
Ghostty is roughly five times larger and announces *"Performance will be very
poor"* on every launch — a bad way to judge a terminal. Drop the flag only if
you are working on the code and want the assertions.

**If the link fails on `gtk4-layer-shell-0`:** Ghostty requires it
unconditionally, no distro package provides it on Ubuntu 24.04, and the `.so`
that does exist is named without the `-0`. Point the expected name at it:

```bash
mkdir -p ~/.local/lib
ln -sf /usr/lib/libgtk4-layer-shell.so ~/.local/lib/libgtk4-layer-shell-0.so
```

## How it is built

`main` mirrors the upstream tag verbatim and is never modified. The feature
lives on `sidebar` as a small series rebased onto each upstream release — never
merged — so the patch stays a diff a stranger can read end to end.

The logic lives in files that are entirely new (`class/sidebar.zig`,
`class/sidebar_row.zig` and their blueprints). Upstream files receive insertion
points only, never rewrites. That shape is the whole maintenance strategy:
upstream cannot conflict with a file that exists only here, so conflicts are
limited to the handful of lines actually inserted.

- [Design](docs/vertical-tabs-design.md) — architecture, the rejected
  alternatives, and the risks; including what four rounds of adversarial review
  changed, and where the review itself turned out to be wrong.
- [Plan](docs/vertical-tabs-plan.md) — the implementation plan, and the five
  places it did not survive contact with the machine.
- [Progress](docs/PROGRESS.md) — current state.
- [Acceptance](docs/acceptance.md) — the manual checklist that stands in for
  unit tests, because a GTK widget does not submit to them.

## License and attribution

MIT, the same as upstream. Ghostty is by Mitchell Hashimoto and its
contributors; its copyright and license are preserved unchanged, and the
upstream README, [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`HACKING.md`](HACKING.md) remain in git history and at
[ghostty.org](https://ghostty.org). This fork claims only the sidebar.

No pull request will be opened against `ghostty-org/ghostty` — the discussion is
closed and locked, and sending one anyway would only cost a maintainer time.

## AI disclosure

Upstream's [`AI_POLICY.md`](AI_POLICY.md) requires that AI use be disclosed.
This fork does not contribute upstream, so it is not bound by that policy. The
disclosure is made anyway: the audience here is the Ghostty community, and the
norm is a good one.

This work was done with Claude Code (Opus 5), with the repository owner in the
loop. Load-bearing claims were verified against primary sources rather than
model memory; the architecture went through four rounds of adversarial
multi-model review before implementation; and corrections are recorded in place
rather than quietly replaced — including the ones that proved the review wrong.
See [§9 of the design doc](docs/vertical-tabs-design.md).
