#!/usr/bin/env python3
"""XTEST-driven acceptance harness for the sidebar's GTK4 popovers on X11.

A GTK4 popover (row context menu, hamburger menu) is a SEPARATE
override-redirect X window, not a region of the parent window. Screenshotting
the parent window misses it every time -- that produced the "NOT MEASURABLE"
false negative in docs/acceptance.md. This harness instead diffs the X window
tree (root.query_tree, recursive) before and after a synthetic click and
reports a popover as any override-redirect window that is either NEW or was
REMAPPED (map_state transitioned to IsViewable). GTK reuses popover surfaces
across opens, so on the second and later opens "new" is 0 and only "remapped"
sees the menu -- counting new windows alone false-negatives from the second
run onward.

Usage (run with the repo's isolated venv, from the repo root):
    .xvenv/bin/python3 scripts/acceptance-harness.py --hamburger
    .xvenv/bin/python3 scripts/acceptance-harness.py --menu
    .xvenv/bin/python3 scripts/acceptance-harness.py --scroll-colour
    .xvenv/bin/python3 scripts/acceptance-harness.py --none-parity
    .xvenv/bin/python3 scripts/acceptance-harness.py --none-shortcut
    .xvenv/bin/python3 scripts/acceptance-harness.py --drag-reorder
    .xvenv/bin/python3 scripts/acceptance-harness.py --panes
    .xvenv/bin/python3 scripts/acceptance-harness.py --a11y-focus
    .xvenv/bin/python3 scripts/acceptance-harness.py --section-header

Every mode launches its own ghostty process against an isolated
XDG_CONFIG_HOME and terminates it on exit. --menu and --scroll-colour always
fire the hamburger positive control first in the same run and refuse to
report a negative if it fails. --none-parity and --none-shortcut are a
separate concern: they measure the UI-level half of `--gtk-sidebar-tabs=none`'s
promise (tab bar present, no sidebar, no sidebar shortcut) via pixel
structure rather than popover detection -- see cmd_none_parity()'s and
cmd_none_shortcut()'s docstrings. Neither modifies or replaces
scripts/check-none-parity.sh, which only diffs GTK/GLib log output and
cannot see a pixel or a widget. --drag-reorder measures whether GTK4 DND
actually reorders sidebar rows, using a move_tab:1 positive control (wired
via a custom keybind this mode adds to its own launch config) to prove
reordering is observable before trusting a synthetic-drag negative -- see
cmd_drag_reorder()'s docstring. --panes measures the v1.1 tranche 1 pane
sub-rows the same way: real geometry read at runtime, a positive control
before any delta is trusted, and a live before/after/back round trip through
the app's own state (a pane's pwd) rather than assuming a click did anything
-- see cmd_panes()'s docstring.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

from Xlib import X, XK, display
from Xlib.ext import xtest
from PIL import Image, ImageChops

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHOSTTY_BIN = os.path.join(REPO_ROOT, "zig-out", "bin", "ghostty")
ARTIFACT_DIR = os.path.join(REPO_ROOT, ".prokai", "tmp", "dispatch-artifacts")

# Coordinates measured on the 922x722 reference window (docs/acceptance.md).
ROW1 = (160, 133)
HAMBURGER = (724, 78)  # stale off the 922x722 reference window; cmd_hamburger/cmd_menu now
                        # scan the real position at runtime (_scan_hamburger_button) instead
                        # of clicking this. Kept only as a reference value for that scan's
                        # docstring and colour_row()'s (currently unused) fallback.
HAMBURGER_MIN_SIZE = (200, 200)  # the real menu is 349x662; a "Menu principal" tooltip is 121x32
TAB_OVERVIEW = (690, 78)  # negative control: in-window, opens 0 new X windows
TERMINAL_FOCUS = (600, 300)


def log(msg):
    print(f"[harness] {msg}", flush=True)


def _find_stray_ghostty_pids():
    """PIDs whose actual executable (/proc/<pid>/exe, resolved) is this exact
    binary. Deliberately NOT `pgrep -f GHOSTTY_BIN`: -f matches the full
    command line text of ANY process, so a shell merely containing the
    binary's path as a string (e.g. in a command it's about to run, or in
    its scrollback) is indistinguishable from a live ghostty process to
    pgrep -- caught in review: an operator's own shell was matched and the
    guard below refused to launch against a process that wasn't ghostty at
    all. Comparing the resolved executable path is the actual measurement;
    command-line text is a coincidence, not evidence."""
    target = os.path.realpath(GHOSTTY_BIN)
    pids = []
    try:
        entries = os.listdir("/proc")
    except FileNotFoundError:
        return pids  # no /proc -- can't check, proceed as before
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            continue  # process exited mid-scan, or exe unreadable -- not ours
        if exe == target:
            pids.append(entry)
    return pids


def _wait_for_no_stray_ghostty(timeout=8.0):
    """Refuse to launch on top of a still-running instance of this exact
    binary. A stray process from a prior run (this harness's own cleanup
    failing to land in time, or a previous invocation killed mid-run) makes
    the window manager place the new window at an offset from the expected
    origin -- observed directly: abs=(139,177) instead of (89,127), with the
    sidebar not yet fully painted, which produced one flaky positive-control
    reading (prompt_text_x=117 instead of ~317). The positive control caught
    that flake and correctly refused to trust the result, but the fix
    belongs here, at the root cause, not just in the guard that noticed it."""
    deadline = time.time() + timeout
    while True:
        pids = _find_stray_ghostty_pids()
        if not pids:
            return
        if time.time() >= deadline:
            raise RuntimeError(
                f"refusing to launch: {GHOSTTY_BIN} already running (pid {', '.join(pids)}) "
                f"after waiting {timeout}s -- a stray instance from a prior run skews window "
                "placement for the new one. Kill it and retry."
            )
        time.sleep(0.5)


def launch_ghostty(extra_config="", sidebar_mode="left", cwd=None):
    if not os.path.exists(GHOSTTY_BIN):
        raise RuntimeError(
            f"no ghostty binary at {GHOSTTY_BIN} (GHOSTTY_BIN is REPO_ROOT/zig-out/bin/ghostty, "
            f"REPO_ROOT={REPO_ROOT}). Either build one there (`zig build --search-prefix "
            "\"$HOME/.local\"`), or if you're testing against a binary built elsewhere, "
            "symlink it in: `mkdir -p zig-out/bin && ln -sf <path-to-binary> zig-out/bin/ghostty` "
            "-- this symlink is gitignored (zig-out/) and must be re-created any time it's "
            "missing; a bare Popen() against a missing path fails with an unhelpful raw "
            "FileNotFoundError instead of this message, which is why this check exists."
        )
    _wait_for_no_stray_ghostty()
    env = dict(os.environ)
    env["GDK_BACKEND"] = "x11"
    env["DISPLAY"] = env.get("DISPLAY", ":0")
    env.pop("WAYLAND_DISPLAY", None)
    cfgdir = tempfile.mkdtemp(prefix="acceptance-harness-xdg-")
    os.makedirs(os.path.join(cfgdir, "ghostty"), exist_ok=True)
    with open(os.path.join(cfgdir, "ghostty", "config"), "w") as f:
        f.write(f"gtk-sidebar-tabs = {sidebar_mode}\n")
        f.write(extra_config)
    env["XDG_CONFIG_HOME"] = cfgdir
    proc = subprocess.Popen(
        [GHOSTTY_BIN], env=env, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return proc


def tree(win, acc=None):
    if acc is None:
        acc = []
    try:
        children = win.query_tree().children
    except Exception:
        return acc
    for c in children:
        try:
            g = c.get_geometry()
            a = c.get_attributes()
            nm = c.get_wm_name()
        except Exception:
            continue
        acc.append((c.id, nm, g.width, g.height, g.x, g.y, bool(a.override_redirect), a.map_state))
        tree(c, acc)
    return acc


def find_ghostty_window(d, root, timeout=8.0):
    """Match by WM_CLASS ("ghostty" or "ghostty-debug" -- a Debug/ReleaseSafe
    build appends "-debug" to its app-id, see src/apprt/gtk/App.zig:26 and
    src/apprt/gtk/winproto/x11.zig:47 -- not WM_NAME, since the window title
    is the shell's cwd and varies with the launch directory."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for (wid, nm, w, h, x, y, orr, ms) in tree(root):
            if w > 300 and h > 200 and ms == X.IsViewable and not orr:
                try:
                    win = d.create_resource_object("window", wid)
                    cls = win.get_wm_class()
                except Exception:
                    continue
                if cls and any(c in ("ghostty", "ghostty-debug") for c in cls):
                    return wid, w, h
        time.sleep(0.3)
    return None


class Session:
    """One ghostty process + its X connection, window handle, and absolute origin."""

    def __init__(self, extra_config="", sidebar_mode="left", cwd=None):
        self.cwd = cwd or os.getcwd()  # declared, not left implicit -- the sidebar's tab
                                        # title is the cwd, so its width (and therefore
                                        # where the row's own close button lands) depends
                                        # on this same as it depends on window geometry
        self.proc = launch_ghostty(extra_config, sidebar_mode, cwd=cwd)
        time.sleep(6.0)  # window map + first paint settle (proven recipe)
        self.d = display.Display(os.environ.get("DISPLAY", ":0"))
        self.root = self.d.screen().root
        found = find_ghostty_window(self.d, self.root)
        if not found:
            self.close()
            raise RuntimeError("ghostty window not found within timeout")
        self.wid, self.ww, self.wh = found
        self.win = self.d.create_resource_object("window", self.wid)
        tr = self.root.translate_coords(self.win, 0, 0)
        self.ax, self.ay = tr.x, tr.y
        log(f"window 0x{self.wid:x} {self.ww}x{self.wh} abs=({self.ax},{self.ay})")
        self.focus_terminal()

    def focus_terminal(self):
        # A right-click on an unfocused window only focuses it, it does not
        # also open the context menu -- click the terminal first.
        self._click(*TERMINAL_FOCUS, button=1, settle=0.8)

    def snap(self):
        return {t[0]: t for t in tree(self.root)}

    def screenshot(self):
        raw = self.win.get_image(0, 0, self.ww, self.wh, X.ZPixmap, 0xFFFFFFFF)
        return Image.frombytes("RGB", (self.ww, self.wh), raw.data, "raw", "BGRX")

    def type_text(self, text):
        """Send literal text as XTEST key events, uppercased/symbol chars
        pressed with Shift. Used by --panes to give one pane a pwd the
        other doesn't have, so a click swapping the active surface has
        something externally observable to swap."""
        names = {" ": "space", "/": "slash", "\n": "Return", "-": "minus"}
        for ch in text:
            name = names.get(ch, ch)
            sym = XK.string_to_keysym(name)
            if sym == 0:
                raise RuntimeError(f"type_text: no keysym for {ch!r}")
            needs_shift = ch.isupper()
            kc = self.d.keysym_to_keycode(sym)
            shift_kc = self.d.keysym_to_keycode(0xFFE1) if needs_shift else None
            if shift_kc:
                xtest.fake_input(self.d, X.KeyPress, shift_kc)
            xtest.fake_input(self.d, X.KeyPress, kc)
            self.d.sync()
            time.sleep(0.03)
            xtest.fake_input(self.d, X.KeyRelease, kc)
            if shift_kc:
                xtest.fake_input(self.d, X.KeyRelease, shift_kc)
            self.d.sync()
            time.sleep(0.03)

    def _click(self, rx, ry, button, settle):
        d = self.d
        xtest.fake_input(d, X.MotionNotify, x=self.ax + rx, y=self.ay + ry)
        d.sync()
        time.sleep(0.4 if button != 1 or settle >= 0.8 else 0.3)
        xtest.fake_input(d, X.ButtonPress, button)
        d.sync()
        time.sleep(0.2)
        xtest.fake_input(d, X.ButtonRelease, button)
        d.sync()
        time.sleep(settle)

    def escape(self):
        xtest.fake_input(self.d, X.KeyPress, 9)
        xtest.fake_input(self.d, X.KeyRelease, 9)
        self.d.sync()
        time.sleep(0.8)

    def probe_click(self, rx, ry, button, label, settle=2.0, dismiss=True, min_size=None):
        """Click at (rx, ry) relative to the window origin; diff the X tree;
        capture + save the PNG of any override-redirect popup found. By
        default sends Escape afterward to close it -- pass dismiss=False when
        the caller needs the popup to stay open for further clicks (e.g. to
        navigate into a submenu), since Escape would close it first.

        Among multiple new/remapped override-redirect windows (a click can
        also produce a tooltip alongside the real popup, or vice-versa), picks
        the LARGEST by area, not an arbitrary one -- iterating a set() gives
        no ordering guarantee. Pass min_size=(w, h) to additionally require
        the chosen popover be at least that big; this matters for the
        hamburger-menu positive control specifically, since a bare
        w>20,h>20 filter is satisfied by a "Menu principal" tooltip
        (121x32) that can appear without the 349x662 menu ever having
        opened -- a control that tolerates a tooltip isn't a control."""
        before = self.snap()
        self._click(rx, ry, button, settle)
        after = self.snap()

        new_ids = [k for k in after if k not in before]
        remapped_ids = [
            k for k in after
            if k in before and before[k][7] != X.IsViewable and after[k][7] == X.IsViewable
        ]
        popovers = []
        for k in set(new_ids) | set(remapped_ids):
            t = after[k]
            (wid, nm, w, h, x, y, orr, ms) = t
            if orr and w > 20 and h > 20 and ms == X.IsViewable:
                popovers.append(t)

        log(f"{label}: btn{button} @({rx},{ry}) -> nouvelles={len(new_ids)} remappees={len(remapped_ids)} "
            f"popovers={len(popovers)}")

        candidates = popovers
        if min_size is not None:
            min_w, min_h = min_size
            candidates = [t for t in popovers if t[2] >= min_w and t[3] >= min_h]
            if not candidates and popovers:
                log(f"{label}: {len(popovers)} popover(s) seen but none met min_size={min_size} "
                    f"(largest was {max(popovers, key=lambda t: t[2]*t[3])[2]}x{max(popovers, key=lambda t: t[2]*t[3])[3]}) "
                    "-- treating as no real popup")

        result = None
        if candidates:
            (wid, nm, w, h, x, y, orr, ms) = max(candidates, key=lambda t: t[2] * t[3])
            os.makedirs(ARTIFACT_DIR, exist_ok=True)
            out_path = os.path.join(ARTIFACT_DIR, f"{label}-0x{wid:x}.png")
            pw = self.d.create_resource_object("window", wid)
            raw = pw.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)
            Image.frombytes("RGB", (w, h), raw.data, "raw", "BGRX").save(out_path)
            log(f"{label}: POPUP window 0x{wid:x} {w}x{h} @({x},{y}) -> {out_path}")
            result = {"id": wid, "w": w, "h": h, "x": x, "y": y, "png": out_path}

        if dismiss:
            self.escape()
        return result

    def open_tabs(self, count):
        ctrl_kc = self.d.keysym_to_keycode(0xFFE3)  # Control_L
        shift_kc = self.d.keysym_to_keycode(0xFFE1)  # Shift_L
        t_kc = self.d.keysym_to_keycode(0x0074)  # t
        for _ in range(count):
            xtest.fake_input(self.d, X.KeyPress, ctrl_kc)
            xtest.fake_input(self.d, X.KeyPress, shift_kc)
            xtest.fake_input(self.d, X.KeyPress, t_kc)
            self.d.sync()
            time.sleep(0.15)
            xtest.fake_input(self.d, X.KeyRelease, t_kc)
            xtest.fake_input(self.d, X.KeyRelease, shift_kc)
            xtest.fake_input(self.d, X.KeyRelease, ctrl_kc)
            self.d.sync()
            time.sleep(0.5)

    def click_root(self, x, y, button=1, settle=0.4):
        xtest.fake_input(self.d, X.MotionNotify, x=x, y=y)
        self.d.sync()
        time.sleep(0.4)
        xtest.fake_input(self.d, X.ButtonPress, button)
        self.d.sync()
        time.sleep(0.15)
        xtest.fake_input(self.d, X.ButtonRelease, button)
        self.d.sync()
        time.sleep(settle)

    def colour_row(self, row_xy, colour_index, label):
        """Right-click a row, open its Colour submenu, pick colour_index
        (0=None,1=Blue,2=Green,3=Orange,4=Red). Coordinates for the submenu
        items are measured from the reference popover: header ~44px tall,
        5 items evenly spaced with the first item's center 76px from the
        popover's top and 32px between item centers -- see
        docs/acceptance.md's Task 11 note for how these were derived.
        Uses dismiss=False on the menu-open click: probe_click's default
        Escape-after-capture would otherwise close the menu before this
        method can click into it."""
        ctrl = self.probe_click(*HAMBURGER, button=1, label=f"{label}-ctrl", min_size=HAMBURGER_MIN_SIZE)
        if ctrl is None:
            raise RuntimeError(f"positive control failed before colouring {label}")

        menu = self.probe_click(*row_xy, button=3, label=f"{label}-menu", dismiss=False)
        if menu is None:
            raise RuntimeError(f"row context menu did not open for {label}")

        colour_x = menu["x"] + menu["w"] // 2
        colour_y = menu["y"] + 75  # "Colour" item center in the 276x156 reference menu
        self.click_root(colour_x, colour_y)

        win = self.d.create_resource_object("window", menu["id"])
        g = win.get_geometry()
        item_x = g.x + g.width // 2
        item_y = g.y + 76 + 32 * colour_index
        self.click_root(item_x, item_y)

    def measure_row_geometry(self, close_btn_x=(216, 234), y_scan=(60, None)):
        """Measure the actual y-center of each visible sidebar row and the
        spacing between them, instead of trusting a hardcoded constant.
        ROW_STEP_Y=66 (guessed from a single early measurement) was found,
        independent review, to be wrong -- the true spacing is 54.0px,
        uniform across rows, measured by locating each row's close ("x")
        button (a small bright glyph in a fixed x-band) and taking the
        median gap between consecutive centers. A click computed from the
        wrong constant misses the row card starting at row 3
        (ROW1[1] + 2*66 = 265 vs the real row 3 center at 240.5) and a
        band-tolerance check derived from it holds only by chance at row 2
        and would fail at row 3. Measuring this at runtime instead of
        hardcoding it also makes row-targeting immune to row-height
        variation (a missing subtitle, a different font, HiDPI scale) the
        same way the chrome-agnostic redesign made --none-parity immune to
        a debug build's banner.

        Returns {"row1_y": float, "step_y": float or None, "centers": [...]}.
        step_y is None if fewer than 2 rows were detected (nothing to take
        a spacing from); raises if zero rows were detected at all."""
        y0, y1 = y_scan
        y1 = y1 or self.wh
        raw = self.win.get_image(0, 0, self.ww, self.wh, X.ZPixmap, 0xFFFFFFFF)
        img = Image.frombytes("RGB", (self.ww, self.wh), raw.data, "raw", "BGRX")
        px = img.load()
        x0, x1 = close_btn_x
        x1 = min(x1, self.ww)
        thresh = 150

        rows_with_btn = []
        for y in range(y0, y1):
            has = False
            for x in range(x0, x1):
                r, g, b = px[x, y][:3]
                if r > thresh and g > thresh and b > thresh:
                    has = True
                    break
            rows_with_btn.append(has)

        groups = []
        gap = 999
        for i, has in enumerate(rows_with_btn):
            if has:
                if gap > 3:
                    groups.append([y0 + i, y0 + i])
                else:
                    groups[-1][1] = y0 + i
                gap = 0
            else:
                gap += 1

        centers = [(g0 + g1) / 2 for g0, g1 in groups]
        heights = [g1 - g0 + 1 for g0, g1 in groups]
        if not centers:
            raise RuntimeError("measure_row_geometry: no sidebar rows detected -- "
                                "is the sidebar visible and at least one tab open?")

        step_y = None
        if len(centers) >= 2:
            diffs = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
            step_y = diffs[len(diffs) // 2]  # median

        log(f"measure_row_geometry: centers={centers} step_y={step_y} heights={heights}")
        return {"row1_y": centers[0], "step_y": step_y, "centers": centers, "heights": heights}

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _scan_hamburger_button(session, y_max=60, thresh=150, min_icon_width=10, max_ctrl_width_delta=3):
    """The hamburger button's (x, y) center, SCANNED at runtime instead of
    trusting the frozen module-level HAMBURGER=(724, 78).

    HAMBURGER was measured on a ~922-wide window a real window manager
    handed out. Under a bare X server (Xephyr locally, xvfb in CI) the
    window is 800x600 and undecorated: measured 2026-08-25, same binary,
    same display, same window, same cwd -- only the window-management state
    differs -- the header row's icon glyphs sit at x~[28, 68, 96, 624, 658],
    y~28, not x=724, y=78. A click at the frozen constant lands right of and
    below the real button and opens nothing: probe_click reports
    "nouvelles=0 remappees=0 popovers=0" every time, exactly cmd_hamburger's
    POSITIVE CONTROL FAILED symptom.

    Method: scan the header band (y in [0, y_max)) for columns with at
    least one bright (>thresh in every channel) pixel -- the same "any
    bright pixel in this scanline" grouping measure_row_geometry/
    _detect_row_bands already use for sidebar rows, transposed to columns
    instead of rows since the header lays its icons out horizontally, not
    vertically. Each resulting band's y-center is the bright-pixel extent
    within that column range. The header's rightmost three bands are always
    the window controls (minimize/maximize/close): narrower (measured 8px)
    and mutually within max_ctrl_width_delta of each other, unlike the
    wider (measured 12-16px) content icons left of them. The hamburger
    button is the band immediately to their left.

    Confirmed identical (same relative offset from the window-control
    cluster, same band width) across four resizes of the SAME running
    window -- 800x600, 922x722, 1000x650, 760x580, via a raw ConfigureWindow
    request, not a second launch -- and confirmed to actually open the menu:
    on 800x600, clicking the derived center opened a 328x601 popover in the
    same run where clicking the frozen HAMBURGER constant opened nothing.
    Not a second frozen number: this scans fresh every call, so it tracks
    whatever width the window actually has.

    Returns (x, y), or None if fewer than 4 header bands were found, or the
    trailing 3 aren't a clean same-width cluster -- nothing to anchor the
    hamburger band on, which is a CANNOT_MEASURE, not a click attempt."""
    img = session.screenshot()
    px = img.load()
    w, h = img.width, img.height
    y_max = min(y_max, h)

    cols = []
    for x in range(w):
        has = False
        for y in range(y_max):
            r, g, b = px[x, y][:3]
            if r > thresh and g > thresh and b > thresh:
                has = True
                break
        cols.append(has)

    groups = []
    gap = 999
    for i, has in enumerate(cols):
        if has:
            if gap > 2:
                groups.append([i, i])
            else:
                groups[-1][1] = i
            gap = 0
        else:
            gap += 1

    bands = []
    for x0, x1 in groups:
        ys = []
        for y in range(y_max):
            for x in range(x0, x1 + 1):
                r, g, b = px[x, y][:3]
                if r > thresh and g > thresh and b > thresh:
                    ys.append(y)
                    break
        bands.append({
            "xc": (x0 + x1) / 2,
            "w": x1 - x0 + 1,
            "yc": (min(ys) + max(ys)) / 2 if ys else y_max / 2,
        })

    if len(bands) < 4:
        return None
    ctrl_widths = [b["w"] for b in bands[-3:]]
    if max(ctrl_widths) - min(ctrl_widths) > max_ctrl_width_delta:
        return None
    hamburger = bands[-4]
    if hamburger["w"] < min_icon_width:
        return None
    return (round(hamburger["xc"]), round(hamburger["yc"]))


def cmd_hamburger():
    s = Session()
    try:
        ham = _scan_hamburger_button(s)
        if ham is None:
            return _abstain(
                "hamburger button not found",
                f"header-band scan on window {s.ww}x{s.wh} did not yield a clean "
                "icon-band-before-window-controls layout to anchor on -- module-level "
                f"HAMBURGER={HAMBURGER} was NOT used as a fallback (it is stale off-window "
                "on this display, see _scan_hamburger_button's docstring)",
            )
        log(f"cmd_hamburger: hamburger button SCANNED at {ham} on window {s.ww}x{s.wh} "
            f"(module-level HAMBURGER={HAMBURGER} is the pre-fix reference value, unused here)")
        result = s.probe_click(*ham, button=1, label="hamburger", min_size=HAMBURGER_MIN_SIZE)
        if result is None:
            log("POSITIVE CONTROL FAILED")
            return 2
        log(f"POSITIVE CONTROL OK: 0x{result['id']:x} {result['w']}x{result['h']} -> {result['png']}")
        return 0
    finally:
        s.close()


def cmd_menu():
    s = Session()
    try:
        ham = _scan_hamburger_button(s)
        if ham is None:
            return _abstain(
                "hamburger button not found",
                f"header-band scan on window {s.ww}x{s.wh} did not yield a clean "
                "icon-band-before-window-controls layout to anchor on",
            )
        log(f"cmd_menu: hamburger button SCANNED at {ham} on window {s.ww}x{s.wh} "
            f"(module-level HAMBURGER={HAMBURGER} is the pre-fix reference value, unused here)")
        ctrl = s.probe_click(*ham, button=1, label="hamburger-control", min_size=HAMBURGER_MIN_SIZE)
        if ctrl is None:
            log("POSITIVE CONTROL FAILED — refusing to report the row-menu result")
            return 2
        neg = s.probe_click(*TAB_OVERVIEW, button=1, label="tab-overview-in-window-control")
        if neg is not None:
            log("NOTE: in-window negative control unexpectedly produced an override-redirect window")
        # Measure the first row instead of trusting ROW1's hardcoded y.
        # ROW1 = (160, 133) was measured on a build whose sidebar starts
        # with a tab row. On the by-repository-sections build (PR #10) the
        # first thing at that y is a SECTION HEADER, which has no context
        # menu -- so this probe reported "row-context-menu confirmed
        # ABSENT" on a build where the menu works fine. A negative that
        # depends on which build you happen to be pointing at is not a
        # measurement. measure_row_geometry() finds the rows by their close
        # button, so it lands on a real row on either build.
        #
        # close_btn_x is SCANNED (_scan_close_btn_band), not left at
        # measure_row_geometry's frozen default (216,234): on the CI xvfb
        # window (800x600, cwd=REPO_ROOT here) that default finds only ONE
        # row band ([112.0], step_y=None, height=9px) instead of the real
        # row -- cmd_drag_reorder and cmd_scroll_colour already worked
        # around the same frozen-band gap for their own row detection.
        #
        # The right-click itself lands at x=40 (row title text), not
        # ROW1[0]=160: cmd_scroll_colour (l.665-667) and cmd_drag_reorder
        # (l.866-868) both document x=160 landing on the row's OWN close
        # button instead of the row body on a narrow window, and cmd_menu
        # was the one mode still clicking there -- a "confirmed ABSENT"
        # from a right-click that hit the close button proves nothing about
        # whether the row's context menu exists; it is a false negative,
        # same shape as trusting TAB_OVERVIEW without probing it.
        band = _scan_close_btn_band(s)
        log(f"menu: close-button band SCANNED at x={band} (window {s.ww}x{s.wh}, cwd={s.cwd})")
        geom = s.measure_row_geometry(close_btn_x=band)
        row1_y = round(geom["row1_y"])
        log(f"menu: row centers measured at {geom['centers']} -- right-clicking (40, {row1_y})")
        result = s.probe_click(40, row1_y, button=3, label="row-context-menu")
        if result is None:
            log("row-context-menu: confirmed ABSENT (positive control succeeded in the same run, "
                f"and the right-click landed on a MEASURED row center y={row1_y} at x=40 -- row "
                "title text, not the row's own close button -- not a guessed coordinate)")
            return 1
        log(f"row-context-menu: PRESENT — 0x{result['id']:x} {result['w']}x{result['h']} -> {result['png']}")
        return 0
    finally:
        s.close()


def cmd_scroll_colour():
    """Task 11: open 6 tabs, colour rows 1 (Red) and 2 (Blue) via the row
    menu, shrink the window so the 6-row list must scroll, scroll to the
    bottom and back to the top (forcing GTK to recycle rows 1-3's widgets
    for rows 4-6 and back), then re-screenshot and report PASS/FAIL by
    checking the coloured dots are still on rows 1 and 2 -- never "not
    testable". The window resize uses a raw Xlib ConfigureWindow request
    (not XTEST): shrinking via window-height/window-width config at launch
    was tried first and left the window without X input focus for keyboard
    events; resizing an already-focused window after tab creation avoids
    that and needs no keyboard input itself (only XTEST pointer events,
    which don't require input focus).

    Reference-window resize + colour_row bypass: ROW1/HAMBURGER (module-
    level) and colour_row()'s own positive control are calibrated for the
    ~922x722 window a real window manager gives; a bare X server (Xephyr
    here, xvfb in CI) hands out an undecorated 800x600 window instead,
    where both the frozen close-button band AND HAMBURGER=(724,78) land
    wrong. This is the same root cause cmd_drag_reorder already
    measured and worked around -- resize to the reference size, scan the
    close-button band instead of assuming it, and skip colour_row's own
    stale hamburger-menu positive control (still wrong even after the
    resize -- see cmd_drag_reorder's comment) in favour of the row's own
    context-menu popover as positive-control-enough evidence."""
    s = Session()
    try:
        s.win.configure(width=922, height=722)
        s.d.sync()
        time.sleep(1.0)
        g = s.win.get_geometry()
        s.ww, s.wh = g.width, g.height
        log(f"task11: resized to reference window {s.ww}x{s.wh}")

        s.open_tabs(5)  # 6 total with the initial tab

        close_btn_band = _scan_close_btn_band(s)
        log(f"task11: close-button band SCANNED at x={close_btn_band} (window {s.ww}x{s.wh})")
        geom = s.measure_row_geometry(close_btn_x=close_btn_band)
        if geom["step_y"] is None:
            raise RuntimeError("cmd_scroll_colour: could not measure row spacing "
                                "(fewer than 2 rows detected with 6 tabs open)")
        row1_y, step_y = geom["row1_y"], geom["step_y"]
        log(f"task11: measured row1_y={row1_y} step_y={step_y} (not the hardcoded "
            "ROW_STEP_Y=66 an earlier version used, which was wrong -- true spacing is 54px)")

        # colour_row()'s own hamburger-menu positive control is stale here
        # (module-level HAMBURGER=(724,78), see docstring above) -- open
        # each row's context menu directly instead, same workaround
        # cmd_drag_reorder uses. Row-click x=40 (title text), not
        # ROW1[0]=160, since the scanned close-button band ((130,162)-ish
        # on this reference window) sits close enough to 160 to eat a
        # right-click meant for the row title.
        def colour_row_no_hamburger_ctrl(row_y, colour_index, label):
            menu = s.probe_click(40, round(row_y), button=3, label=f"{label}-menu", dismiss=False)
            if menu is None:
                raise RuntimeError(f"row context menu did not open for {label}")
            colour_x = menu["x"] + menu["w"] // 2
            colour_y = menu["y"] + 75  # "Colour" item center in the 276x156 reference menu
            s.click_root(colour_x, colour_y)
            win = s.d.create_resource_object("window", menu["id"])
            mg = win.get_geometry()
            item_x = mg.x + mg.width // 2
            item_y = mg.y + 76 + 32 * colour_index
            s.click_root(item_x, item_y)

        colour_row_no_hamburger_ctrl(row1_y, 4, "row1")  # Red
        colour_row_no_hamburger_ctrl(row1_y + step_y, 1, "row2")  # Blue

        before_shot = os.path.join(ARTIFACT_DIR, "task11-coloured-before-scroll.png")
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX").save(before_shot)
        log(f"task11: coloured rows 1 (red) + 2 (blue), 6 tabs total -> {before_shot}")

        s.win.configure(height=300)
        s.d.sync()
        time.sleep(1.0)

        for _ in range(8):  # scroll to the bottom
            xtest.fake_input(s.d, X.MotionNotify, x=s.ax + ROW1[0], y=s.ay + 200)
            s.d.sync()
            xtest.fake_input(s.d, X.ButtonPress, 5)
            s.d.sync()
            xtest.fake_input(s.d, X.ButtonRelease, 5)
            s.d.sync()
            time.sleep(0.15)
        time.sleep(0.5)

        for _ in range(10):  # scroll back to the top
            xtest.fake_input(s.d, X.MotionNotify, x=s.ax + ROW1[0], y=s.ay + 200)
            s.d.sync()
            xtest.fake_input(s.d, X.ButtonPress, 4)
            s.d.sync()
            xtest.fake_input(s.d, X.ButtonRelease, 4)
            s.d.sync()
            time.sleep(0.15)
        time.sleep(0.5)

        g = s.win.get_geometry()
        after_shot = os.path.join(ARTIFACT_DIR, "task11-after-scroll-recycle.png")
        raw = s.win.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xFFFFFFFF)
        img = Image.frombytes("RGB", (g.width, g.height), raw.data, "raw", "BGRX")
        img.save(after_shot)
        log(f"task11: scrolled to bottom and back to top (forces row-widget recycling) -> {after_shot}")

        # Finding "a red pixel somewhere" and "a blue pixel somewhere" is not
        # the criterion under test: the criterion is "colour state lives on
        # the row/tab, not on the recycled widget" -- so a red dot that
        # migrated onto row 2 (or swapped with row 2's blue) must FAIL, not
        # be waved through because red and blue both still exist on screen
        # somewhere. Require each colour's y-center to fall within its own
        # row's band, and require it NOT to fall within the other row's band
        # (catches a partial swap). row1_y/step_y are measured once, above,
        # before the resize/scroll -- the per-row height itself doesn't
        # change across window states, only how many rows fit on screen, so
        # the same measured step is reused here. The tolerance (+/-15px,
        # tighter than an earlier version's +/-25 now that the step itself
        # is measured instead of guessed) absorbs the residual few-pixel
        # rendering variance observed between the pre-scroll and
        # post-"scroll to top" layouts, not a wrong constant.
        BAND_TOLERANCE = 15

        def band(row_index):
            center = row1_y + row_index * step_y
            return (center - BAND_TOLERANCE, center + BAND_TOLERANCE)

        def is_reddish(px):
            r, gr, b = px[:3]
            return r > 180 and gr < 160 and b < 160

        def is_blueish(px):
            r, gr, b = px[:3]
            return b > 180 and r < 160

        def colour_ys(pred):
            sidebar = img.crop((0, 0, min(260, g.width), g.height))
            pixels = sidebar.load()
            return [y for y in range(sidebar.height) for x in range(sidebar.width) if pred(pixels[x, y])]

        red_ys = colour_ys(is_reddish)
        blue_ys = colour_ys(is_blueish)
        red_center = sum(red_ys) / len(red_ys) if red_ys else None
        blue_center = sum(blue_ys) / len(blue_ys) if blue_ys else None
        log(f"task11: red pixel y-range={(min(red_ys), max(red_ys)) if red_ys else None} center={red_center}")
        log(f"task11: blue pixel y-range={(min(blue_ys), max(blue_ys)) if blue_ys else None} center={blue_center}")

        row1_band, row2_band = band(0), band(1)
        red_in_row1_band = red_center is not None and row1_band[0] <= red_center <= row1_band[1]
        blue_in_row2_band = blue_center is not None and row2_band[0] <= blue_center <= row2_band[1]
        no_red_in_row2_band = not any(row2_band[0] <= y <= row2_band[1] for y in red_ys)
        no_blue_in_row1_band = not any(row1_band[0] <= y <= row1_band[1] for y in blue_ys)

        log(f"task11: row1_band={row1_band} row2_band={row2_band} "
            f"red_in_row1={red_in_row1_band} blue_in_row2={blue_in_row2_band} "
            f"no_red_in_row2={no_red_in_row2_band} no_blue_in_row1={no_blue_in_row1_band}")

        if red_in_row1_band and blue_in_row2_band and no_red_in_row2_band and no_blue_in_row1_band:
            log("task11: PASS — red stayed on row 1's band and blue on row 2's band after "
                "scroll-induced row recycling; neither colour migrated or swapped rows")
            return 0
        else:
            log("task11: FAIL — a colour is missing from its expected row's band, or present "
                "in the other row's band (recycling put colour on the wrong row)")
            return 1
    finally:
        s.close()


def _red_centre_in_sidebar(img, col_width=260):
    """y-center of reddish pixels in the sidebar column, or None if none
    found. Shared by cmd_drag_reorder's before/after/positive-control reads
    -- the same is_reddish invariant cmd_scroll_colour already validated.
    col_width defaults to 260, matching SIDEBAR_COLUMN_WIDTH defined later
    in this file (module-level constant, not referenced directly here to
    avoid a definition-order dependency)."""
    def is_reddish(px):
        r, g, b = px[:3]
        return r > 180 and g < 160 and b < 160

    sidebar = img.crop((0, 0, min(col_width, img.width), img.height))
    pixels = sidebar.load()
    ys = [y for y in range(sidebar.height) for x in range(sidebar.width) if is_reddish(pixels[x, y])]
    return sum(ys) / len(ys) if ys else None


def cmd_drag_reorder():
    """Tab drag-to-reorder within the sidebar, wired via `GtkDragSource` /
    `GtkDropTarget` on each `SidebarRow` (`src/apprt/gtk/class/sidebar_row.zig`,
    `src/apprt/gtk/ui/1.5/sidebar-row.blp`): a drag carries the row's
    `AdwTabPage`, and a drop reorders the tab view to the target row's
    position via `AdwTabView.reorderPage` -- the same primitive
    `Window.moveTab` already ends with, not a reimplementation.

    Protocol: open 3 tabs, colour the newest (focused, rank 3) row red --
    ONE marker, so there's no ambiguity about which row moved. Send
    `move_tab:1` via a custom keybind this command adds to its own launch
    config (no default keybind exists for it) -- with exactly 3 tabs this
    wraps position 2 to position 0 (see `moveTab` in window.zig), so the
    red dot must land at rank 1. That's the POSITIVE CONTROL: it proves
    reordering is observable to this detector at all, before a
    synthetic-drag negative gets to mean anything. Only then does a
    synthetic drag (rank 1 -> rank 3, 30 `MotionNotify` steps, 1s hold
    before `ButtonRelease`) get attempted, and the dot's position is
    checked again -- staying at rank 1 confirms the drag had no effect,
    consistent with the source read: there's nothing to drag, this is a
    measured absence of the feature, not this harness being blind to it."""
    ctrl_kc_sym, shift_kc_sym, m_kc_sym = 0xFFE3, 0xFFE1, 0x006D  # Control_L, Shift_L, m
    s = Session(extra_config="keybind = ctrl+shift+m=move_tab:1\n")
    try:
        # ROW1/HAMBURGER (module-level) and colour_row's own positive
        # control are all measured on the ~922x722 window a real window
        # manager gives; a bare X server (Xephyr here, xvfb in CI) instead
        # hands out an 800x600 window at (0,0), which is why this and other
        # commands (see cmd_a11y_focus) treat the window size as something
        # to measure, never assume. Resizing to the reference size up front
        # keeps those already-calibrated constants valid instead of
        # re-deriving every one of them for this command too.
        s.win.configure(width=922, height=722)
        s.d.sync()
        time.sleep(1.0)
        g = s.win.get_geometry()
        s.ww, s.wh = g.width, g.height
        log(f"drag-reorder: resized to reference window {s.ww}x{s.wh}")

        s.open_tabs(2)  # 3 total; the newest (index 2, rank 3) is focused
        # measure_row_geometry's frozen default (216, 234) was read off a
        # window a real window manager gave it; a bare X server (Xephyr
        # here, xvfb in CI) hands out an 800x600 window whose close buttons
        # sit further left, so the frozen band lands in the terminal and
        # reads its text as tab rows instead -- exactly the
        # `_scan_close_btn_band` docstring's own measured symptom
        # (centers=[66.0, 118.0], two rows that do not exist). Scan for the
        # real band instead of assuming it, same as cmd_panes already does.
        band = _scan_close_btn_band(s)
        log(f"drag-reorder: close-button band SCANNED at x={band} (window {s.ww}x{s.wh})")
        geom = s.measure_row_geometry(close_btn_x=band)
        if geom["step_y"] is None or len(geom["centers"]) < 3:
            raise RuntimeError(f"cmd_drag_reorder: expected 3 rows, measured {geom['centers']}")
        rank1_y, rank2_y, rank3_y = (round(c) for c in geom["centers"][:3])
        step_y = geom["step_y"]
        log(f"drag-reorder: measured ranks 1/2/3 at y={rank1_y}/{rank2_y}/{rank3_y}")

        # colour_row's own hamburger-menu positive control (module-level
        # HAMBURGER=(724, 78)) is stale against this window/theme -- it
        # opens nothing here even after the reference-size resize above,
        # which is a pre-existing calibration gap in that guard, not
        # evidence the popover detector itself is broken (the row's own
        # context-menu popover below opens and is found the same way).
        # Skip straight to the row's context menu instead of colour_row's
        # wrapper. Row-click x=40 (title text), not ROW1[0]=160, for the
        # same reason cmd_a11y_focus avoids ROW1[0]: on the narrow window
        # its close button sits near x~160 and would eat the click.
        row_click_x = 40
        menu = s.probe_click(row_click_x, rank3_y, button=3, label="drag-reorder-rank3-menu", dismiss=False)
        if menu is None:
            raise RuntimeError("drag-reorder: row context menu did not open for rank3")
        colour_x = menu["x"] + menu["w"] // 2
        colour_y = menu["y"] + 75  # "Colour" item center in the 276x156 reference menu
        s.click_root(colour_x, colour_y)
        win = s.d.create_resource_object("window", menu["id"])
        g = win.get_geometry()
        item_x = g.x + g.width // 2
        item_y = g.y + 76 + 32 * 4  # colour_index=4 -> Red
        s.click_root(item_x, item_y)

        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        coloured_img = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        coloured_img.save(os.path.join(ARTIFACT_DIR, "drag-reorder-1-coloured-rank3.png"))
        coloured_centre = _red_centre_in_sidebar(coloured_img)
        log(f"drag-reorder: red centre after colouring={coloured_centre} (expect near rank3_y={rank3_y})")

        s.focus_terminal()  # the colour popover closing doesn't hand keyboard focus back on its own

        ctrl_kc = s.d.keysym_to_keycode(ctrl_kc_sym)
        shift_kc = s.d.keysym_to_keycode(shift_kc_sym)
        m_kc = s.d.keysym_to_keycode(m_kc_sym)
        xtest.fake_input(s.d, X.KeyPress, ctrl_kc)
        xtest.fake_input(s.d, X.KeyPress, shift_kc)
        xtest.fake_input(s.d, X.KeyPress, m_kc)
        s.d.sync()
        time.sleep(0.15)
        xtest.fake_input(s.d, X.KeyRelease, m_kc)
        xtest.fake_input(s.d, X.KeyRelease, shift_kc)
        xtest.fake_input(s.d, X.KeyRelease, ctrl_kc)
        s.d.sync()
        time.sleep(1.0)

        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        after_movetab_img = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        after_movetab_img.save(os.path.join(ARTIFACT_DIR, "drag-reorder-2-after-movetab.png"))
        after_movetab_centre = _red_centre_in_sidebar(after_movetab_img)
        log(f"drag-reorder: red centre after move_tab:1={after_movetab_centre} (expect near rank1_y={rank1_y})")

        # A fixed-pixel BAND_TOLERANCE (the file's other callers use 15,
        # tuned against the reference window) is too tight for this
        # environment's measured step_y=56.5 vs the reference's 54.0 -- a
        # real move landed 17.3px from rank1_y here, just outside 15.
        # Scaled to a third of the measured row pitch, and classified as
        # "closer to rank1 than to rank3" rather than an absolute band,
        # this stays meaningful however the pitch drifts between renders.
        BAND_TOLERANCE = max(15, round(step_y / 3))
        control_ok = (
            after_movetab_centre is not None
            and abs(after_movetab_centre - rank1_y) < abs(after_movetab_centre - rank3_y)
            and abs(after_movetab_centre - rank1_y) <= BAND_TOLERANCE
        )
        if not control_ok:
            log("POSITIVE CONTROL FAILED — move_tab:1 did not move the coloured row to rank 1; "
                "refusing to trust a synthetic-drag negative without proof reordering is observable")
            return 2
        log("positive control OK: move_tab:1 moved the coloured row from rank 3 to rank 1 -- "
            "reordering is observable to this detector")

        # Synthetic drag: rank 1 -> rank 3, matching the reference recipe
        # (30 steps, 1s hold) used to independently validate this finding.
        # row_click_x (title text), not ROW1[0]: on this window ROW1[0]=160
        # sits on/near the close button (see the rank3 colouring above),
        # and a drag that starts on that button closes the tab instead of
        # dragging the row.
        x0, y0 = row_click_x, rank1_y
        x1, y1 = row_click_x, rank3_y
        xtest.fake_input(s.d, X.MotionNotify, x=s.ax + x0, y=s.ay + y0)
        s.d.sync()
        time.sleep(0.3)
        xtest.fake_input(s.d, X.ButtonPress, 1)
        s.d.sync()
        time.sleep(0.1)
        steps = 30
        for i in range(1, steps + 1):
            fx = x0 + (x1 - x0) * i / steps
            fy = y0 + (y1 - y0) * i / steps
            xtest.fake_input(s.d, X.MotionNotify, x=s.ax + round(fx), y=s.ay + round(fy))
            s.d.sync()
            time.sleep(0.03)
        time.sleep(1.0)  # hold before release
        xtest.fake_input(s.d, X.ButtonRelease, 1)
        s.d.sync()
        time.sleep(1.0)

        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        after_drag_img = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        after_drag_img.save(os.path.join(ARTIFACT_DIR, "drag-reorder-3-after-drag.png"))
        after_drag_centre = _red_centre_in_sidebar(after_drag_img)
        log(f"drag-reorder: red centre after synthetic drag={after_drag_centre} "
            f"(rank1_y={rank1_y}, rank3_y={rank3_y})")

        stayed_at_rank1 = (
            after_drag_centre is not None and abs(after_drag_centre - rank1_y) <= BAND_TOLERANCE
        )
        moved_to_rank3 = (
            after_drag_centre is not None and abs(after_drag_centre - rank3_y) <= BAND_TOLERANCE
        )

        if stayed_at_rank1 and not moved_to_rank3:
            log("drag-reorder: NOT IMPLEMENTED (measured) — synthetic drag had no effect after "
                "a same-run positive control confirmed reordering is observable")
            return 1
        elif moved_to_rank3:
            log("drag-reorder: PASS — the synthetic drag moved the coloured row from rank 1 to "
                "rank 3, with the same-run move_tab:1 positive control also green; matches the "
                "source read (GtkDragSource/GtkDropTarget wired on SidebarRow, reordering via "
                "AdwTabView.reorderPage)")
            return 0
        else:
            log(f"drag-reorder: INCONCLUSIVE — red centre after drag ({after_drag_centre}) is "
                f"neither rank 1 nor rank 3; something moved it somewhere unexpected")
            return 2
    finally:
        s.close()


def _detect_row_bands(img, x_range, y_range, thresh=150):
    """Groups of consecutive scanlines within y_range that have at least
    one bright (>thresh in every channel) pixel inside x_range, returned as
    their y-centers. The same grouping Session.measure_row_geometry uses
    for the sidebar's close-button glyphs, generalised to scan for ANY
    content rather than one fixed glyph -- a pane sub-row has no close
    button to anchor on, so this looks for whatever renders in a y-range
    that held nothing before a split, over the full sidebar column width
    (x_range is expected to be wide; the y-range bound is what keeps this
    from picking up a tab row's own content)."""
    px = img.load()
    x0, x1 = x_range
    x1 = min(x1, img.width)
    y0, y1 = max(0, y_range[0]), min(img.height, y_range[1])

    rows_with_content = []
    for y in range(y0, y1):
        has = False
        for x in range(x0, x1):
            r, g, b = px[x, y][:3]
            if r > thresh and g > thresh and b > thresh:
                has = True
                break
        rows_with_content.append(has)

    groups = []
    gap = 999
    for i, has in enumerate(rows_with_content):
        if has:
            if gap > 3:
                groups.append([y0 + i, y0 + i])
            else:
                groups[-1][1] = y0 + i
            gap = 0
        else:
            gap += 1

    return [(g0 + g1) / 2 for g0, g1 in groups]


def _scan_close_btn_band(session):
    """The close-button x-band, SCANNED instead of assumed — for cmd_panes only.

    measure_row_geometry defaults to close_btn_x=(216, 234), read once off the
    ~922x722 window a window manager gives. Under a bare X server (xvfb in CI,
    Xephyr locally) the window is 800x600 and its close buttons sit at x~157:
    the frozen band lands in the TERMINAL and reads its text lines as tab rows.
    Measured: cmd_panes on Xephyr got centers=[66.0, 118.0] -- two rows that do
    not exist -- and aborted with "expected 3 tab rows".

    Scanning is only sound where several REAL rows exist, because it keeps the
    longest EVENLY SPACED run and any pair of stray bands beats a single true
    row. That is why this is local to cmd_panes, which opens three tabs before
    calling it, and NOT the global default: applied globally it invented rows
    on a one-tab window and turned --menu's true positive into a false
    negative. Measured, and reverted, before this was written.
    """
    img = session.screenshot()
    best, best_cx = [], None
    for cx in range(140, 264, 6):
        bands = _detect_row_bands(img, (cx - 16, cx + 16), (55, session.wh))
        run = _longest_run(bands)
        if len(run) > len(best):
            best, best_cx = run, cx
    return (best_cx - 16, best_cx + 16) if best_cx else (216, 234)


def _longest_run(centers, tol=6):
    """Longest contiguous subsequence whose gaps are uniform within tol. Tab
    rows are evenly pitched; terminal text picked up by a mis-placed band is
    not, so this is what separates real rows from noise."""
    if len(centers) < 2:
        return centers[:]
    best = [centers[0]]
    j, n = 0, len(centers)
    while j < n - 1:
        step = centers[j + 1] - centers[j]
        k, run = j, [centers[j]]
        while k < n - 1 and abs((centers[k + 1] - centers[k]) - step) <= tol:
            run.append(centers[k + 1]); k += 1
        if len(run) > len(best):
            best = run
        j = k if k > j else j + 1
    return best


CANNOT_MEASURE = 2  # ni PASS ni FAIL : l'instrument n'a pas pu monter sa scene


def _abstain(reason, detail):
    """An abstention is not a negative. Say so loudly, in the tool's OWN output.

    A bare FAIL invites the next reader to do one of two harmful things: open a
    phantom bug on a defect that does not exist, or -- worse -- tune the tool
    until it turns green. Telling "I could not measure" apart from "the
    measurement is negative" is the same discipline a guard owes: an abstention
    is not a verdict."""
    log("")
    log("=== ABSTENTION — CE RUN NE MESURE RIEN ===")
    log(f"    cause  : {reason}")
    log(f"    detail : {detail}")
    log("    Ce n'est PAS un defaut produit, et ce n'est PAS un verdict.")
    log("    Le produit est verifie autrement — voir le commit a6540698e.")
    log("    Ne 'corrige' pas ce harnais jusqu'a ce qu'il passe au vert.")
    return CANNOT_MEASURE


def cmd_panes():
    """v1.1 tranche 1: second-level pane rows under a split tab's row,
    read-only, click activates.

    Everything is measured in one run, nothing assumed from a previous one:

      1. Open 3 tabs and measure their row geometry with the existing
         close-button detector -- pane rows have no close button, so they
         can never be miscounted as tab rows, and this detector needs no
         change to serve both. Uniform spacing across all 3 is the
         POSITIVE CONTROL: proves the detector is trustworthy before any
         delta read against it means anything.

      2. Click rank 2's row (selects + focuses that tab) and split it via
         new_split:right (a DEFAULT keybind, ctrl+shift+o -- no custom
         config needed). Re-measure. Rank 1's row -- a tab this run never
         touches -- is required to land at the exact same y as before:
         that's DoD#1 (an unsplit tab stays visually unchanged) proven
         live, not assumed from reading the blueprint. Rank 2's OWN row is
         required to stay put too (nothing above it moved). Only the gap
         between rank 2 and rank 3 may grow -- and it must, which is the
         evidence a pane row now occupies real space there (DoD#2).

      3. The freshly split-off pane already has keyboard focus (SplitTree
         hands focus to the newest surface on a split). Typing `cd /tmp`
         into it gives it a pwd the original pane doesn't share. The
         sidebar row's own subtitle label is bound to
         `...active-surface.pwd` -- literally the same property path
         DoD#3 names (`SplitTree.active-surface`) -- so that label's
         pixels are the one externally observable trace of which pane is
         active.

      4. Click row A, then row B, then row A again (both found by scanning
         the gap between rank 2 and rank 3 for content -- no coordinate
         here is hardcoded, and which row is the already-active /tmp one
         isn't known or needed). DoD#3 requires a pane click to make
         active-surface reflect that pane; the observable trace is A's two
         readings matching each other while B's differs from both -- a
         deterministic row -> state mapping and its exact reversal, which a
         coincidental repaint would not produce.

    A single synthetic click on these rows occasionally never reaches the
    GtkButton's `clicked` signal at all (confirmed by instrumenting
    paneActivated/ecFocusEnter/propSurfaceFocused directly: when a click
    DOES land, the whole focus -> active-surface chain fires immediately and
    correctly, every single time, with nothing to debounce or wait out --
    so a screenshot showing no change means the click needs resending, not
    that the app needs more time). Each click is retried (parking the
    pointer away from both buttons first, to force a real leave/enter
    crossing rather than a jump between two adjacent flat buttons) until the
    label visibly changes or a small retry budget is spent -- a real
    mechanism, not a longer fixed sleep.
    """
    ctrl_kc_sym, shift_kc_sym, o_kc_sym = 0xFFE3, 0xFFE1, 0x006F  # Control_L, Shift_L, o

    def send_combo(d, mod_syms, key_sym):
        mod_kcs = [d.keysym_to_keycode(m) for m in mod_syms]
        key_kc = d.keysym_to_keycode(key_sym)
        for kc in mod_kcs:
            xtest.fake_input(d, X.KeyPress, kc)
        xtest.fake_input(d, X.KeyPress, key_kc)
        d.sync()
        time.sleep(0.15)
        xtest.fake_input(d, X.KeyRelease, key_kc)
        for kc in reversed(mod_kcs):
            xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    log(f"  Un echec ici sort en code {CANNOT_MEASURE} (ABSTENTION), jamais en FAIL nu.")
    s = Session()
    try:
        # A bare X server (Xephyr/xvfb) hands out an 800x600 window, and at
        # that size the 3rd tab row is pushed out of the window entirely
        # once the split adds pane sub-rows -- measured: geom1's centers
        # drop below 3, and the run abstains instead of measuring anything.
        # Resizing to the reference window up front (the same pattern
        # cmd_drag_reorder already uses) gives the split room to grow into,
        # same as the ~922x722 a real window manager hands out.
        s.win.configure(width=922, height=722)
        s.d.sync()
        time.sleep(1.0)
        g = s.win.get_geometry()
        s.ww, s.wh = g.width, g.height
        log(f"panes: resized to reference window {s.ww}x{s.wh}")

        s.open_tabs(2)  # 3 total; rank 3 (index 2) is focused
        band = _scan_close_btn_band(s)
        log(f"panes: close-button band SCANNED at x={band} "
            f"(window {s.ww}x{s.wh}) -- not the frozen (216, 234)")
        # y_scan's own default (60, None) starts inside the Debug build's
        # "Vous utilisez une version de debogage" banner, which is bright
        # enough in this same x-band to register as a 4th, narrower "row"
        # above the 3 real ones -- measured: centers=[94.5, 131.5, 187.5,
        # 243.5] on a 922x722 window, where the last 3 are evenly spaced
        # (56px apart, the real rows) and the first is not (37px to the
        # next). Starting the scan at y=105 -- below every measured banner
        # pixel (up to y=98), above the first real row's close button
        # (from y=127) -- drops that false row without touching anything
        # the banner-free reference windows already relied on.
        geom0 = s.measure_row_geometry(close_btn_x=band, y_scan=(105, None))
        if geom0["step_y"] is None or len(geom0["centers"]) < 3:
            return _abstain(
                "le detecteur n'a pas trouve les 3 rangees d'onglet AVANT le split",
                f"centers={geom0['centers']} sur une fenetre {s.ww}x{s.wh} — soit la "
                "bande du bouton de fermeture tombe a cote, soit les onglets n'ont "
                "jamais ete ouverts parce que les frappes n'atteignent pas le client")
        c1, c2, c3 = (round(c) for c in geom0["centers"][:3])
        step0 = geom0["step_y"]
        row_h0 = sorted(geom0["heights"][:3])[1]  # median of the 3 baseline rows
        spacing_uniform = abs((c2 - c1) - (c3 - c2)) <= 3
        log(f"panes: baseline rows at y={c1}/{c2}/{c3} step={step0} "
            f"heights={geom0['heights']} uniform={spacing_uniform}")
        if not spacing_uniform:
            log("POSITIVE CONTROL FAILED — row spacing isn't uniform before any split; "
                "refusing to trust deltas measured against it")
            return 2

        s._click(ROW1[0], c2, button=1, settle=0.6)  # select + focus rank 2
        send_combo(s.d, [ctrl_kc_sym, shift_kc_sym], o_kc_sym)  # new_split:right
        time.sleep(1.2)  # idleAdd-debounced tree rebuild + relayout + paint

        geom1 = s.measure_row_geometry(close_btn_x=band, y_scan=(105, None))
        if len(geom1["centers"]) < 3:
            return _abstain(
                "la 3e rangee d'onglet a disparu APRES le split",
                f"centers={geom1['centers']} sur une fenetre {s.ww}x{s.wh} — mesure sur "
                "bare X : les sous-rangees de pane repoussent la 3e hors de portee du "
                "detecteur. A rejouer sur une fenetre geree par un WM")

        # geom1 can hold MORE than 3 bands after a split: the pane sub-rows
        # a split adds sit in the same x-band as a tab row's close button,
        # and this detector has no way, from the close-button band alone,
        # to tell "a pane sub-row" from a real tab row. Picking centers[:3]
        # blindly would silently mix a pane sub-row into the tab-row
        # triplet and report a wrong gap-grew verdict from it.
        #
        # The count alone is enough to catch this, with NO threshold: 3
        # real tabs open, more than 3 bands detected -> the data is
        # contaminated, period. A height-based gate was tried first (>2px
        # off row_h0) and it does not hold: measured 7px sub-rows against a
        # 10px baseline on one window (gap 3, caught), 9px against 10px on
        # another (gap 1, missed) -- a threshold derived from a single
        # measurement and frozen, the exact family of bug this repo has
        # hit six times now (ROW1, the 260 column, close_btn (216,234), the
        # hamburger band, _measure_sidebar_column's own split defect, and
        # this one). Heights are kept in the abstention's DETAIL for the
        # follow-up, never in the CONDITION.
        if len(geom1["centers"]) > 3:
            return _abstain(
                "measure_row_geometry ne distingue pas une sous-rangee de pane "
                "d'une rangee d'onglet dans la bande du bouton de fermeture",
                f"APRES split: centers={geom1['centers']} heights={geom1['heights']} "
                f"(baseline row height={row_h0}) — {len(geom1['centers'])} bandes "
                "detectees pour 3 rangees d'onglet reelles connues (ouvertes par ce "
                "run). Le compte seul suffit : plus de bandes que d'onglets reels veut "
                "dire des donnees contaminees, quelle que soit la hauteur des bandes en "
                "trop. Prendre centers[:3] donnerait un verdict gap_grew NON FIABLE "
                "(bande de pane melangee au triplet) -- NON DIAGNOSTIQUE en l'etat, "
                "distinct du remede scelle de _measure_sidebar_column. A traiter dans "
                "un suivi separe.")

        n1, n2, n3 = (round(c) for c in geom1["centers"][:3])
        log(f"panes: after split rows at y={n1}/{n2}/{n3}")

        TOL = 4
        rank1_unmoved = abs(n1 - c1) <= TOL
        rank2_unmoved = abs(n2 - c2) <= TOL
        gap_grew = (n3 - n2) > (c3 - c2) + TOL
        log(f"panes: rank1_unmoved={rank1_unmoved} rank2_unmoved={rank2_unmoved} "
            f"gap before={c3 - c2} after={n3 - n2} gap_grew={gap_grew}")

        if not (rank1_unmoved and rank2_unmoved):
            log("FAIL — splitting rank 2 moved a row it shouldn't have; an unsplit "
                "tab's row must stay pixel-identical")
            return 1
        if not gap_grew:
            log("FAIL — splitting rank 2 grew nothing between it and rank 3; no pane "
                "row appears to have been rendered")
            return 1
        log("PASS — unsplit rows (rank 1, and rank 2's own row) stayed exactly where "
            "they were; only the gap that pane rows now occupy grew")

        pane_ys = _detect_row_bands(
            s.screenshot(),
            x_range=(0, min(260, s.ww)),
            y_range=(round(n2 + step0 / 2), round(n3 - step0 / 2)),
        )
        log(f"panes: pane sub-row bands detected at y={pane_ys}")
        if len(pane_ys) < 2:
            raise RuntimeError(f"cmd_panes: expected 2 pane rows in the gap, found {pane_ys}")
        row_a_y, row_b_y = round(pane_ys[0]), round(pane_ys[1])

        # Give the freshly split-off pane (already focused -- SplitTree
        # hands focus to the newest surface) a pwd the other pane doesn't
        # share, so there's something externally visible to swap.
        s.type_text("cd /tmp\n")
        time.sleep(0.8)

        def subtitle_snapshot(name):
            img = s.screenshot()
            os.makedirs(ARTIFACT_DIR, exist_ok=True)
            path = os.path.join(ARTIFACT_DIR, f"panes-{name}.png")
            img.save(path)
            # rank 2's own row card: title + subtitle text, both of which
            # move with active-surface (confirmed by direct inspection --
            # the title line changes too, not just the subtitle). Spans the
            # row generously around its (measured, not guessed) close-button
            # y so neither text line is clipped regardless of exact font
            # metrics -- a first version cropped only 4-24px below that y
            # and silently missed both lines, reading a blank strip as "no
            # change" on 3 of 4 runs despite the feature working correctly
            # every single time (confirmed via source-level tracing).
            band = img.crop((22, n2 - 20, 210, n2 + 20))
            return band, path

        def diff_ratio(a, b):
            hist = ImageChops.difference(a, b).convert("L").histogram()
            changed = sum(hist[16:])
            return changed / sum(hist)

        CHANGED, UNCHANGED = 0.02, 0.005

        def click_pane_row_until_changed(y, prev_snap, label, retries=4):
            # A synthetic click occasionally never reaches the target
            # GtkButton's `clicked` signal at all -- confirmed by
            # source-level tracing that when it DOES land, the whole
            # focus/active-surface chain fires correctly and immediately,
            # every time, with no debounce to wait out. So "no visible
            # change yet" here means "the click needs resending", not "give
            # the app more time" -- there's nothing to wait for once a click
            # actually lands. Parking the pointer away from both buttons
            # first forces a genuine leave/enter crossing rather than a
            # jump straight from one flat button to its neighbour.
            for attempt in range(1, retries + 1):
                xtest.fake_input(s.d, X.MotionNotify, x=s.ax + 500, y=s.ay + 300)
                s.d.sync()
                time.sleep(0.3)
                s._click(ROW1[0], y, button=1, settle=1.2)
                snap, path = subtitle_snapshot(f"{label}-attempt{attempt}")
                if diff_ratio(prev_snap, snap) > CHANGED:
                    log(f"panes: clicked pane row {label} (attempt {attempt}) -> {path}")
                    return snap, path
            # Not necessarily a failure: a row that is ALREADY the active
            # pane must not change anything when clicked. Saying "still no
            # visible change" reads as "the click was dropped" and invites
            # exactly the wrong conclusion -- it did, on a run that ended
            # PASS. The verdict is the ratio triplet below, never this line.
            log(f"panes: clicked pane row {label} {retries}x with no visible change -> {path} "
                "(expected when that row is already the active pane; the ratio triplet decides)")
            return snap, path

        # Which detected row is the (already active) /tmp one and which is
        # the original isn't known from geometry alone, and doesn't need to
        # be: click row A, then row B, then row A again, and require A's two
        # readings to match each other while B's differs from both -- that
        # is a deterministic row->state mapping and its exact reversal,
        # order-agnostic and with no assumption about which row starts active.
        snap0, path0 = subtitle_snapshot("0-after-cd-tmp")
        log(f"panes: state after cd /tmp (before any pane click) -> {path0}")

        snap_a1, path_a1 = click_pane_row_until_changed(row_a_y, snap0, "A")
        snap_b, path_b = click_pane_row_until_changed(row_b_y, snap_a1, "B")
        snap_a2, path_a2 = click_pane_row_until_changed(row_a_y, snap_b, "A-again")

        d_a1_vs_b = diff_ratio(snap_a1, snap_b)
        d_b_vs_a2 = diff_ratio(snap_b, snap_a2)
        d_a1_vs_a2 = diff_ratio(snap_a1, snap_a2)
        log(f"panes: subtitle diff ratios rowA-vs-rowB={d_a1_vs_b:.4f} "
            f"rowB-vs-rowA-again={d_b_vs_a2:.4f} rowA-vs-rowA-again={d_a1_vs_a2:.4f}")

        a_to_b_changed = d_a1_vs_b > CHANGED
        b_to_a_changed_back = d_b_vs_a2 > CHANGED
        a_round_trip_matched = d_a1_vs_a2 < UNCHANGED

        if a_to_b_changed and b_to_a_changed_back and a_round_trip_matched:
            log("PASS — clicking each pane's sub-row visibly changed which surface is "
                "active (the subtitle bound to SplitTree.active-surface's pwd moved "
                "between two distinct readings and back to the exact same one), a "
                "deterministic swap and its reversal, not a coincidental repaint")
            return 0
        else:
            if (d_a1_vs_b < UNCHANGED and d_b_vs_a2 < UNCHANGED
                    and d_a1_vs_a2 < UNCHANGED):
                return _abstain(
                    "les TROIS ratios sont nuls — rien n'a bouge, pas meme entre deux "
                    "panes censes differer",
                    f"{d_a1_vs_b:.4f} / {d_b_vs_a2:.4f} / {d_a1_vs_a2:.4f}. C'est la "
                    "signature du `cd /tmp` jamais tape : les deux panes gardent le meme "
                    "pwd, donc la comparaison de sous-titres est VIDE. Ce n'est pas un "
                    "clic sans effet, c'est une scene jamais montee")
            log("FAIL — pane clicks did not produce the expected active-surface swap "
                "(see diff ratios above)")
            return 1
    finally:
        s.close()


# Terminal background is a distinct blue-gray (measured ~(40,44,52) across
# this whole sweep's screenshots); GTK chrome (headerbar, tab bar, sidebar
# row cards) is a near-neutral gray (~(69,69,69)). The invariant that holds
# up across both: chrome has near-equal R/G/B, terminal background has B
# distinctly greater than R.
def _looks_like_chrome(px):
    r, g, b = px[:3]
    return (b - r) < 8


# Both signals below were redesigned after independent review ran this
# harness against a Debug build (PR #12's binary) and found the previous
# prompt-row-scan approach locked onto the DEBUG BANNER ("Vous utilisez une
# version de debogage...") instead of the real shell prompt: the banner is
# bright text too, and it sits directly below the tab bar, so a "first row
# with enough bright pixels from y=100" scan found the tab bar's own label
# text, not the prompt underneath the banner underneath that. A fixed
# y-band for the same reason misread which chrome row it was sampling. Both
# replacements below are chosen to be insensitive to how many chrome rows
# (headerbar, tab bar, debug banner, any future addition) stack above the
# terminal, rather than assuming a fixed offset.


def _sidebar_column_signal(img, x0=0, x1=220, y0=300, y1=500):
    """Sample a region well below any plausible stack of header/tab-bar/
    banner chrome (a 722-tall window has plenty of room by y=300) in the
    column a sidebar would occupy. Returns (dominant_colour, chrome_like):
    chrome_like=True means that column is sidebar chrome (row cards etc, a
    near-neutral grey); chrome_like=False means it's plain terminal
    background (the terminal spans that column, so no sidebar there) --
    this is the SAME distinguishing invariant the tab-bar-band check always
    used (_looks_like_chrome), just aimed at a location immune to how much
    chrome sits above it, which the terminal's first-text-row was not."""
    px = img.load()
    x1 = min(x1, img.width)
    y1 = min(y1, img.height)
    colour_counts = {}
    for y in range(y0, y1):
        for x in range(x0, x1):
            c = px[x, y][:3]
            colour_counts[c] = colour_counts.get(c, 0) + 1
    dominant = max(colour_counts, key=colour_counts.get)
    return dominant, _looks_like_chrome(dominant)


def _terminal_start_y(img, x0=460, x1=900, y0=68, y1=400, run=30):
    """First y (in the terminal-side column, clear of any sidebar) where the
    per-row dominant colour is terminal-background-blue and STAYS that way
    for the next `run` rows -- i.e. where chrome ends and the terminal
    actually starts, no matter how many chrome rows (tab bar, debug banner,
    both) are stacked above it. `run` rules out a single row that happens to
    read blue-ish inside a block of chrome (e.g. an icon or a hairline)."""
    px = img.load()
    x1 = min(x1, img.width)
    y1 = min(y1, img.height)

    def row_is_terminal_blue(y):
        colour_counts = {}
        for x in range(x0, x1):
            c = px[x, y][:3]
            colour_counts[c] = colour_counts.get(c, 0) + 1
        dominant = max(colour_counts, key=colour_counts.get)
        return not _looks_like_chrome(dominant)

    y = y0
    while y < y1:
        if row_is_terminal_blue(y):
            run_end = min(y + run, y1)
            if all(row_is_terminal_blue(yy) for yy in range(y, run_end)):
                return y
        y += 1
    return None


def _snapshot_signals(s):
    """One screenshot -> (img, dominant_sidebar_colour, sidebar_chrome_like,
    terminal_start_y)."""
    raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
    img = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
    dominant, chrome_like = _sidebar_column_signal(img)
    start_y = _terminal_start_y(img)
    return img, dominant, chrome_like, start_y


def _measure_mode(sidebar_mode, label, max_polls=10, poll_interval=0.5):
    """Launch one mode with 2 tabs and return the two structural signals:
    sidebar_chrome_like (sidebar-column occupancy) and terminal_start_y
    (how far down the terminal's own content starts, used differentially
    between two same-run measurements to isolate a tab bar's height from
    any other chrome that's present in both, like a debug banner -- see
    cmd_none_parity()).

    Polls (screenshot, remeasure) until two consecutive readings agree,
    instead of a single screenshot after a fixed sleep -- a fixed sleep was
    observed to read a not-yet-painted frame once (positive control on
    `left` mode returned a sidebar-absent-looking reading once, when a
    stray ghostty process from a prior run had shifted this run's window
    off its expected placement and delayed its first paint). The positive
    control caught that flake and refused to trust the run, which is what
    it's for, but re-measuring until the signal stops changing removes the
    flake at the source rather than only detecting it after the fact."""
    s = Session(sidebar_mode=sidebar_mode)
    try:
        s.open_tabs(1)  # 2 tabs total
        time.sleep(0.5)

        prev = None
        img = dominant = chrome_like = start_y = None
        settled = False
        for attempt in range(1, max_polls + 1):
            img, dominant, chrome_like, start_y = _snapshot_signals(s)
            cur = (chrome_like, start_y)
            if cur == prev:
                settled = True
                break
            prev = cur
            time.sleep(poll_interval)

        if not settled:
            log(f"{label}: WARNING — signal did not settle across {max_polls} polls "
                f"({max_polls * poll_interval:.1f}s); using the last reading anyway")

        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        shot_path = os.path.join(ARTIFACT_DIR, f"none-parity-{label}.png")
        img.save(shot_path)

        log(f"{label}: settled after {attempt} poll(s), sidebar-column dominant "
            f"colour={dominant} chrome_like={chrome_like} terminal_start_y={start_y} -> {shot_path}")
        return {"chrome_like": chrome_like, "terminal_start_y": start_y, "shot": shot_path,
                "settled": settled, "window_width": s.ww}
    finally:
        s.close()


TAB_BAR_HEIGHT_THRESHOLD = 20  # px; a real AdwTabBar row measures ~40px, see cmd_none_parity


def cmd_none_parity():
    """UI-level parity check for `--gtk-sidebar-tabs=none`'s claim in
    docs/acceptance.md: "tab bar present, no sidebar, no sidebar shortcut" --
    an interface claim that scripts/check-none-parity.sh cannot verify since
    it only diffs GTK/GLib log output, never a pixel or a widget (see that
    finding's discussion in docs/acceptance.md). This does not replace or
    modify check-none-parity.sh; it measures the half of the `none` promise
    that script structurally cannot.

    Positive control: measures `left` mode with the same method in the same
    run first, to prove the structural signals below actually discriminate
    a sidebar-present state from a sidebar-absent one, rather than just
    returning "absent" unconditionally. `left`'s terminal_start_y also
    serves as the tab-bar-height baseline (see below) -- both measurements
    run against the SAME binary in the SAME invocation, so any chrome that
    isn't the tab bar (e.g. a Debug build's banner) contributes equally to
    both and cancels out in the comparison, whatever its own height is."""
    log("positive control: measuring `left` mode (sidebar expected present, no tab bar)")
    left = _measure_mode("left", "left-control")
    if not left["settled"]:
        log("POSITIVE CONTROL FAILED — signal never settled across the poll cap; "
            "refusing to trust an unsettled reading even if it looked plausible")
        return 2
    if left["terminal_start_y"] is None:
        log("POSITIVE CONTROL FAILED — could not locate `left` mode's terminal content start")
        return 2
    if not left["chrome_like"]:
        log(f"POSITIVE CONTROL FAILED — `left` mode's sidebar column did not read as sidebar "
            f"chrome; refusing to trust the `none` measurement below")
        return 2
    log(f"positive control OK: left mode sidebar column reads as chrome (sidebar present), "
        f"terminal_start_y={left['terminal_start_y']} (tab-bar-height baseline)")

    log("measuring `none` mode")
    none = _measure_mode("none", "none-mode")
    if not none["settled"]:
        log("MEASUREMENT FAILED — `none` mode's signal never settled across the poll cap; "
            "refusing to report a verdict from an unsettled reading")
        return 2
    if none["terminal_start_y"] is None:
        log("MEASUREMENT FAILED — could not locate `none` mode's terminal content start")
        return 2

    no_sidebar = not none["chrome_like"]
    tab_bar_height = none["terminal_start_y"] - left["terminal_start_y"]
    tab_bar_present = tab_bar_height > TAB_BAR_HEIGHT_THRESHOLD
    log(f"none: no_sidebar={no_sidebar} (sidebar column chrome_like={none['chrome_like']}) "
        f"tab_bar_present={tab_bar_present} (terminal_start_y none={none['terminal_start_y']} "
        f"left={left['terminal_start_y']}, height={tab_bar_height})")

    # "no sidebar shortcut": Ctrl+Shift+B should be a no-op in none mode.
    # Uses the SAME sidebar-column-restricted detector as --none-shortcut
    # (_shortcut_column_diff), not an independent whole-window diff -- an
    # earlier version measured the whole window here and disagreed with
    # --none-shortcut's verdict on the same binary (whole-window diff picks
    # up unrelated terminal-side effects, like an unbound key's escape
    # sequence echoing to the shell, and mislabels that as "the sidebar
    # toggled"). One detector, so the two commands can't contradict each
    # other on the same measurement.
    none_shortcut_ratio, _, _ = _shortcut_column_diff("none", "none-parity-shortcut")
    no_shortcut = none_shortcut_ratio < 0.005  # cursor blink / AA jitter tolerance
    log(f"none: sidebar-column(x<{SIDEBAR_COLUMN_WIDTH}) shortcut diff_ratio={none_shortcut_ratio:.5f} "
        f"no_shortcut_effect={no_shortcut}")

    if no_sidebar and tab_bar_present and no_shortcut:
        log("none-parity: PASS — tab bar present, no sidebar column, Ctrl+Shift+B doesn't touch it")
        return 0
    else:
        log(f"none-parity: FAIL — no_sidebar={no_sidebar} tab_bar_present={tab_bar_present} "
            f"no_shortcut={no_shortcut}")
        return 1


def _press_ctrl_shift_b(s):
    ctrl_kc = s.d.keysym_to_keycode(0xFFE3)
    shift_kc = s.d.keysym_to_keycode(0xFFE1)
    b_kc = s.d.keysym_to_keycode(0x0062)  # b
    xtest.fake_input(s.d, X.KeyPress, ctrl_kc)
    xtest.fake_input(s.d, X.KeyPress, shift_kc)
    xtest.fake_input(s.d, X.KeyPress, b_kc)
    s.d.sync()
    time.sleep(0.15)
    xtest.fake_input(s.d, X.KeyRelease, b_kc)
    xtest.fake_input(s.d, X.KeyRelease, shift_kc)
    xtest.fake_input(s.d, X.KeyRelease, ctrl_kc)
    s.d.sync()
    time.sleep(1.0)


SIDEBAR_COLUMN_WIDTH = 260  # last-resort fallback only -- see _measure_sidebar_column


def _measure_sidebar_column(img):
    """Width, in pixels, of the sidebar's own column -- MEASURED, not assumed.

    The frozen 260 was read once off the ~922x722 window a window manager
    gives. A bare X server (xvfb, Xephyr -- and CI runs under xvfb) gives an
    800x600 window whose sidebar is ~202px wide, so 260 spills 58px of
    TERMINAL into what is supposed to be the sidebar's column. Terminal
    content changes on its own when the active tab switches, so those 58px
    can move the ratio in either direction: they can hide a real sidebar
    change under a tolerance, or invent one where there is none. Same
    hardcoded-constant trap as the close-button band and ROW1.

    Method, with no colour hardcoded anywhere: take the terminal's background
    from the image's own RIGHT EDGE (necessarily terminal, in every mode),
    then walk leftward while each column is still overwhelmingly that colour.
    Where that stops is the sidebar's right edge. Returns 0 when the terminal
    reaches the left edge -- i.e. there is NO sidebar at all, which is exactly
    what `gtk-sidebar-tabs=none` must show.

    A split defeats a naive version of this walk: it opens a SECOND terminal
    pane to the left of the first one, separated by a 1px border. The two
    panes' backgrounds are not bit-identical -- measured (40,44,53) vs
    (40,44,52), one unit apart in the blue channel -- and the border between
    them (69,69,69) is not terminal at all. A walk that requires an exact
    colour match and never crosses a non-terminal column stops AT THE SPLIT
    BORDER and reports the split's edge instead of the sidebar's: measured
    576 (the split boundary) instead of 233 (the sidebar edge) on a
    3-tabs-plus-split capture. Two changes fix it, and BOTH are required --
    either alone leaves the walk stuck at the split boundary (measured on the
    same capture: tolerance alone still 576, border-crossing alone still
    576, only the combination reaches 233):

    - `is_terminal` tolerates a distance of 1 per channel, so the second
      pane's background still counts as "terminal" despite not being
      bit-identical to the first.
    - the walk may cross exactly ONE non-terminal column and keep going --
      but only when the column beyond it resumes being terminal, which is
      what distinguishes a 1px split border (terminal on both sides) from
      the sidebar's own edge (terminal on one side, sidebar on the other:
      crossing it would eat into the sidebar itself). Without splits there
      is no such column to cross, so single-pane windows measure exactly as
      before."""
    px = img.load()
    w, h = img.width, img.height
    y0, y1 = int(h * 0.55), int(h * 0.95)   # below the tab rows: quiet in both modes
    if y1 <= y0 or w < 16:
        return 0

    counts = {}
    for y in range(y0, y1):
        c = px[w - 8, y][:3]
        counts[c] = counts.get(c, 0) + 1
    term_bg = max(counts.items(), key=lambda kv: kv[1])[0]

    span = y1 - y0
    tol = 1

    def is_terminal(x):
        n = sum(1 for y in range(y0, y1)
                if all(abs(px[x, y][:3][i] - term_bg[i]) <= tol for i in range(3)))
        return n > 0.9 * span

    x = w - 8
    while x > 0:
        if is_terminal(x - 1):
            x -= 1
            continue
        if x - 2 >= 0 and is_terminal(x - 2):
            x -= 2  # cross the 1px split border, terminal resumes beyond it
            continue
        break
    return x


def cmd_sidebar_column_regression():
    """Applies _measure_sidebar_column to four SELF-GENERATED stand-ins for
    the majordome's four reference captures and requires the exact ground
    truth measured off the real screenshots on 2026-08-25, instead of
    shipping the PNGs themselves into the repo (a test that regenerates its
    own fixtures is preferable to committing binaries the fork doesn't
    otherwise need).

    Three are built from the SAME colour profile the majordome read off a
    real 922x722 window at y=541 (3 tabs + 1 split): sidebar (48,48,48) up
    to x=232, a 1px separator (31,31,31), left pane (40,44,53) up to x=575,
    a 1px split border (69,69,69), right pane (40,44,52) to the edge --
    ground truth 233. The no-split cases place the same sidebar/terminal
    boundary at x=202 and x=213 (the widths measured on the two real bare-X
    captures); `none` mode is terminal-only, no sidebar column at all --
    ground truth ~0.

    THE CONTROL THAT MATTERS is the `none` row: a detector that finds a
    sidebar-shaped boundary by, say, always stopping at the first
    non-terminal column it meets would still pass the three sidebar cases
    and fail this one -- which is exactly how the first attempt at this fix
    was rejected (3/3 with a sidebar, 795 in `none`)."""
    from PIL import Image

    def make_column_image(w, h, segments):
        img = Image.new("RGB", (w, h))
        px = img.load()
        for x0, x1, color in segments:
            for x in range(x0, min(x1, w)):
                for y in range(h):
                    px[x, y] = color
        return img

    SIDEBAR, SEP, LEFT_PANE, SPLIT_SEP, RIGHT_PANE = (
        (48, 48, 48), (31, 31, 31), (40, 44, 53), (69, 69, 69), (40, 44, 52))
    TERM = (40, 44, 53)

    cases = [
        ("panes-big (3 tabs + split)", make_column_image(922, 722, [
            (0, 232, SIDEBAR), (232, 233, SEP),
            (233, 575, LEFT_PANE), (575, 576, SPLIT_SEP),
            (576, 922, RIGHT_PANE),
        ]), 233),
        ("t1 (2 tabs, no split)", make_column_image(800, 600, [
            (0, 213, SIDEBAR), (213, 214, SEP), (214, 800, TERM),
        ]), 213),
        ("t0 (1 tab, no split)", make_column_image(800, 600, [
            (0, 202, SIDEBAR), (202, 203, SEP), (203, 800, TERM),
        ]), 202),
        ("none (no sidebar)", make_column_image(800, 600, [
            (0, 800, TERM),
        ]), 0),
    ]

    ok = True
    for name, img, truth in cases:
        got = _measure_sidebar_column(img)
        passed = abs(got - truth) <= 2
        ok = ok and passed
        log(f"sidebar-column-regression: {name}: got={got} truth={truth} "
            f"{'PASS' if passed else 'FAIL'}")

    if not ok:
        log("FAIL — _measure_sidebar_column did not reproduce the majordome's "
            "ground truth on all four scenarios")
        return 1
    log("PASS — _measure_sidebar_column matches ground truth on all four "
        "scenarios, including the none-mode control")
    return 0


def _column_diff_ratio(before, after, col_width=SIDEBAR_COLUMN_WIDTH, y0=0):
    """Diff ratio restricted to the sidebar's own column (x < col_width),
    not the whole window. A whole-window percentage does not discriminate
    here: a sidebar appearing on one side of the split and the terminal
    reflowing on the other nets out to roughly the same overall percentage
    whether or not the sidebar actually toggled -- measured 2.1% in BOTH
    `left` and `none` modes despite one clearly showing a sidebar
    appear/disappear and the other not. Restricting the diff to the column
    the sidebar actually occupies removes that cancellation."""
    before_px, after_px = before.load(), after.load()
    w = min(col_width, before.width, after.width)
    h = min(before.height, after.height)
    diff = sum(1 for y in range(y0, h) for x in range(w)
               if before_px[x, y] != after_px[x, y])
    total = w * (h - y0)
    return diff / total if total else 0.0


def _chrome_bottom(img, col_width):
    """First y below the window's top chrome — measured, and the value it
    returns is printed by the caller before anything is concluded from it.

    Why the bound exists: the header bar spans the FULL width, so it also sits
    inside x < col_width. Diffing "the sidebar column" over the whole height
    therefore counts the TITLE BAR as sidebar. That is not hypothetical --
    pressing Ctrl+Shift+B in `gtk-sidebar-tabs=none` rings the terminal bell
    (the key is bound to nothing, the keystroke reaches the shell, it beeps)
    and a bell marker is painted in the title. Measured on three binaries with
    one variable, all three ring it: upstream 1.3.1 0.09183, fork without the
    a11y PR 0.09173, fork with it 0.09173. Upstream behaviour.

    And whether that bell moves pixels inside x < col_width depends on the
    WORKING DIRECTORY, because the title carries the pwd. Same binary, two
    cwds, twice each:

        cwd .../worktrees/panes-level2                 -> 0.00000, no rows differ
        cwd .../a11y-focus-sidebar/worker-1            -> 0.00592, rows 20-35

    A deep worktree path makes the title long enough that inserting the bell
    shifts text that lies inside the column; a short one shifts text that
    lies to the right of it. So this guard could fail or pass on where a
    contributor happened to check the repository out.

    A first attempt compared a single pixel on each edge and took the first
    row where they differed. It returned 8 on every capture -- the window
    BORDER already differs at y=8 -- so it excluded nothing and was withdrawn.
    Hence this version: sample SEVERAL x inside the column and several in the
    terminal, and require the two sets to stay disjoint for RUN consecutive
    rows. A one-pixel edging cannot hold that; a real sidebar/terminal split
    does. Returns 0 when no such split exists, which is why the caller
    derives this from the frame that HAS a sidebar."""
    px = img.load()
    w, h = img.width, img.height
    col = max(20, min(col_width, w - 1))
    left = [x for x in (12, col // 3, col // 2, max(12, col - 25)) if 0 <= x < w]
    right = [x for x in (w - 30, w - 60, w - 120) if 0 <= x < w]
    RUN = 15
    streak = 0
    for y in range(h):
        ls = {px[x, y][:3] for x in left}
        rs = {px[x, y][:3] for x in right}
        streak = streak + 1 if not (ls & rs) else 0
        if streak >= RUN:
            return y - RUN + 1
    return 0


def _shortcut_column_diff(sidebar_mode, label, col_width=None, y0=None):
    """Launch `sidebar_mode`, screenshot, press Ctrl+Shift+B, screenshot
    again, and return the sidebar-column diff ratio between the two."""
    s = Session(sidebar_mode=sidebar_mode)
    try:
        s.open_tabs(1)
        time.sleep(0.5)
        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        before = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        before.save(os.path.join(ARTIFACT_DIR, f"none-shortcut-{label}-before.png"))

        _press_ctrl_shift_b(s)

        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        after = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        after.save(os.path.join(ARTIFACT_DIR, f"none-shortcut-{label}-after.png"))
    finally:
        s.close()

    measured = _measure_sidebar_column(before)
    chrome = _chrome_bottom(before, measured or col_width or SIDEBAR_COLUMN_WIDTH)
    used_y0 = y0 if y0 is not None else chrome
    ratio = _column_diff_ratio(before, after,
                               col_width=col_width or measured or SIDEBAR_COLUMN_WIDTH,
                               y0=used_y0)
    used = col_width or measured or SIDEBAR_COLUMN_WIDTH
    log(f"{label}: sidebar column MEASURED at x={measured}, chrome bottom "
        f"MEASURED at y={chrome} (diff taken over x<{used}, y>={used_y0}) "
        f"diff_ratio={ratio:.5f}")
    return ratio, measured, used_y0


UPSTREAM_BIN = "/usr/bin/ghostty"


def _row_text_end_x(img, y, x0=0, x1=None, thresh=150):
    """Rightmost bright (near-white) pixel in row y -- used to detect NEW
    text appended to an existing line, such as a leaked escape sequence
    echoed onto the shell prompt's own line after a keypress."""
    x1 = x1 or img.width
    px = img.load()
    end = None
    for x in range(x0, x1):
        r, g, b = px[x, y][:3]
        if r > thresh and g > thresh and b > thresh:
            end = x
    return end


def _last_text_row_group_end_x(img, y0=60, y1=400, x0=0, x1=None, thresh=150, min_count=5, gap_tol=3):
    """Find the LAST contiguous group of text-bearing rows in [y0, y1) and
    return the max text-end-x within it -- this is guaranteed to be the
    shell prompt's own line, not any chrome row above it (headerbar, tab
    bar, a Debug build's banner), because nothing in this harness's test
    windows ever renders below the prompt in an otherwise-empty terminal.
    A fixed-height window anchored to `terminal_start_y` was tried first and
    landed on the banner instead of the prompt when the banner's own row
    happened to fall inside that window and end further right than the
    (unchanged) prompt line -- confirmed directly: it read the banner's
    constant width in both the before and after screenshot and reported no
    change, while the prompt line one group below it had, in fact, grown."""
    x1 = x1 or img.width
    y1 = min(y1, img.height)
    px = img.load()
    row_has_text = []
    for y in range(y0, y1):
        n = 0
        for x in range(x0, x1):
            r, g, b = px[x, y][:3]
            if r > thresh and g > thresh and b > thresh:
                n += 1
                if n >= min_count:
                    row_has_text.append(True)
                    break
        else:
            row_has_text.append(False)

    groups = []
    gap = gap_tol + 1
    for i, has in enumerate(row_has_text):
        if has:
            if gap > gap_tol:
                groups.append([y0 + i, y0 + i])
            else:
                groups[-1][1] = y0 + i
            gap = 0
        else:
            gap += 1

    if not groups:
        return None
    last_y0, last_y1 = groups[-1]
    best = None
    for y in range(last_y0, last_y1 + 1):
        end = _row_text_end_x(img, y, x0, x1, thresh)
        if end is not None and (best is None or end > best):
            best = end
    return best


def _ctrl_shift_b_terminal_leak(win, d, ax, ay, ww, wh, label):
    """Screenshot, press Ctrl+Shift+B, screenshot again, and measure the
    CAUSAL content change on the prompt's own line (text-end-x growing) --
    not a whole-window pixel diff, which conflates unrelated differences
    between two different windows/binaries (title, banner, font metrics)
    with the one thing that's actually comparable: did new text get echoed
    onto the terminal for this keypress. Returns
    (leaked: bool, before_end, after_end, shot paths)."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    raw = win.get_image(0, 0, ww, wh, X.ZPixmap, 0xFFFFFFFF)
    before = Image.frombytes("RGB", (ww, wh), raw.data, "raw", "BGRX")
    before_path = os.path.join(ARTIFACT_DIR, f"{label}-before.png")
    before.save(before_path)

    ctrl_kc = d.keysym_to_keycode(0xFFE3)
    shift_kc = d.keysym_to_keycode(0xFFE1)
    b_kc = d.keysym_to_keycode(0x0062)
    xtest.fake_input(d, X.KeyPress, ctrl_kc)
    xtest.fake_input(d, X.KeyPress, shift_kc)
    xtest.fake_input(d, X.KeyPress, b_kc)
    d.sync()
    time.sleep(0.15)
    xtest.fake_input(d, X.KeyRelease, b_kc)
    xtest.fake_input(d, X.KeyRelease, shift_kc)
    xtest.fake_input(d, X.KeyRelease, ctrl_kc)
    d.sync()
    time.sleep(1.0)

    raw = win.get_image(0, 0, ww, wh, X.ZPixmap, 0xFFFFFFFF)
    after = Image.frombytes("RGB", (ww, wh), raw.data, "raw", "BGRX")
    after_path = os.path.join(ARTIFACT_DIR, f"{label}-after.png")
    after.save(after_path)

    before_end = _last_text_row_group_end_x(before)
    after_end = _last_text_row_group_end_x(after)
    grew = (before_end is None and after_end is not None) or (
        before_end is not None and after_end is not None and after_end - before_end > 10
    )
    log(f"{label}: prompt-line text end before={before_end} after={after_end} "
        f"terminal_leak={grew} -> {before_path}, {after_path}")
    return grew, before_end, after_end


def _measure_upstream_ctrl_shift_b():
    """Launch upstream Ghostty (no sidebar patch, no gtk-sidebar-tabs option,
    and independently confirmed via `ghostty +list-keybinds --default` to
    have no Ctrl+Shift+B binding at all -- same as the fork, which also adds
    no keybind for the sidebar toggle; it's a GTK accelerator, invisible to
    the keybind system) and measure whether Ctrl+Shift+B leaks new text onto
    the terminal's prompt line -- the real remaining parity question for
    `none` mode's "no sidebar shortcut" claim once the sidebar-column check
    clears it. If upstream leaks the same way, the fork's leak in `none`
    mode is what any unbound key does anywhere, not a fork-specific
    difference. Returns None (and logs why) if upstream isn't available."""
    if not os.path.exists(UPSTREAM_BIN):
        log(f"upstream comparison skipped -- {UPSTREAM_BIN} not found")
        return None

    env = dict(os.environ)
    env["GDK_BACKEND"] = "x11"
    env["DISPLAY"] = env.get("DISPLAY", ":0")
    env.pop("WAYLAND_DISPLAY", None)
    env["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="acceptance-harness-upstream-xdg-")
    proc = subprocess.Popen([UPSTREAM_BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    d = None
    try:
        time.sleep(6.0)
        d = display.Display(os.environ.get("DISPLAY", ":0"))
        root = d.screen().root
        found = find_ghostty_window(d, root)
        if not found:
            log("upstream comparison FAILED -- window not found within timeout")
            return None
        wid, ww, wh = found
        win = d.create_resource_object("window", wid)
        tr = root.translate_coords(win, 0, 0)
        ax, ay = tr.x, tr.y

        xtest.fake_input(d, X.MotionNotify, x=ax + 600, y=ay + 300)
        d.sync()
        time.sleep(0.3)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        time.sleep(0.1)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
        time.sleep(0.8)

        return _ctrl_shift_b_terminal_leak(win, d, ax, ay, ww, wh, "upstream-ctrl-shift-b")
    finally:
        if d is not None:
            d.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _measure_fork_ctrl_shift_b_leak(sidebar_mode, label):
    """Same content-based (prompt-line text growth) measurement as
    _measure_upstream_ctrl_shift_b, but against the fork binary under test
    via the normal Session -- used to compare like-for-like with upstream
    instead of a whole-window pixel diff, which would pick up differences
    between the two binaries/windows (title, a Debug build's banner, font
    metrics) that have nothing to do with whether the keypress leaked."""
    s = Session(sidebar_mode=sidebar_mode)
    try:
        s.open_tabs(1)
        time.sleep(0.5)
        return _ctrl_shift_b_terminal_leak(s.win, s.d, s.ax, s.ay, s.ww, s.wh, label)
    finally:
        s.close()


def cmd_none_shortcut():
    """Ctrl+Shift+B must be a no-op in `gtk-sidebar-tabs=none`: no image
    change at all, measured on the sidebar's own column (x<260), not the
    whole window -- see _column_diff_ratio's docstring for why a whole-window
    percentage doesn't discriminate this case (2.1% diff measured in BOTH
    `left` and `none` runs).

    Positive control, same run: `left` mode must show a LARGE column diff for
    the identical gesture, or the run refuses to conclude anything about
    `none` -- a no-op measurement that can't detect a real toggle either
    proves nothing. This is a stricter, dedicated companion to the
    shortcut check inside cmd_none_parity(), which reuses this same
    column-restricted detector (a prior version used an independent
    whole-window diff there and could disagree with this command on the
    same binary -- see _shortcut_column_diff's docstring for why
    whole-window doesn't discriminate).

    Also measures, informationally, whether upstream Ghostty shows a
    comparable effect for the identical gesture -- the real remaining
    parity question once "does the sidebar column change" is answered: an
    unbound Ctrl+Shift+B may still leak an escape sequence to the terminal
    (harmless, and not what this command's PASS/FAIL is about), and if
    upstream does the same thing for the same unbound key, that leak isn't
    a fork-specific difference. Does not affect this command's own verdict,
    which is specifically about the sidebar column, not the terminal."""
    log("positive control: `left` mode, Ctrl+Shift+B must change the sidebar column")
    CHANGE_THRESHOLD = 0.05  # a sidebar appearing/disappearing swamps this in a 260px-wide column
    NOOP_THRESHOLD = 0.005  # cursor blink / AA jitter tolerance

    left_ratio, left_col, left_y0 = _shortcut_column_diff("left", "left-control")
    if not left_col:
        log("POSITIVE CONTROL FAILED — no sidebar column found in `left` mode; "
            "the column detector itself is blind, so no `none` verdict is publishable")
        return 2
    if left_ratio < CHANGE_THRESHOLD:
        log(f"POSITIVE CONTROL FAILED — left mode sidebar-column diff_ratio={left_ratio:.5f} "
            f"< {CHANGE_THRESHOLD}; refusing to conclude anything about `none` mode")
        return 2
    log(f"positive control OK: left mode sidebar-column diff_ratio={left_ratio:.5f} (sidebar toggled)")

    # Reuse the width MEASURED in `left`: in `none` there is (correctly) no
    # sidebar to measure, and the question is precisely whether anything
    # appears in the band a sidebar would occupy.
    log(f"measuring `none` mode over the band `left` actually occupied (x<{left_col})")
    none_ratio, none_col, _ = _shortcut_column_diff(
        "none", "none-mode", col_width=left_col, y0=left_y0)
    # A few pixels at the left edge are the window border, not a sidebar.
    # Measured: a real sidebar is 202px against a 202px left-mode column; the
    # bare border reads 5px. Require a quarter of the control's width before
    # calling it a sidebar, so this check can only fire on something real.
    if none_col > left_col // 4:
        log(f"none: a sidebar column is present AT REST (x={none_col}, "
            f"vs {left_col} in left mode) — the sidebar is NOT absent in none mode")
        return 1
    if none_col:
        log(f"none: {none_col}px at the left edge — window border, not a sidebar "
            f"(left mode measures {left_col})")
    no_effect = none_ratio < NOOP_THRESHOLD
    log(f"none: sidebar-column diff_ratio={none_ratio:.5f} no_effect={no_effect}")

    # Content-based (not whole-window-pixel-based) side-by-side comparison:
    # does upstream leak the same unbound key onto its terminal's prompt
    # line the way `none` mode does? Informational only -- does not affect
    # this command's own PASS/FAIL, which is about the sidebar column.
    #
    # Only meaningful when no_effect is True: if the sidebar itself just
    # toggled (no_effect=False), the terminal's column width changed along
    # with it, which reflows the prompt's own wrapped text to a different
    # end position -- a "prompt-line text end moved" reading in that case is
    # the reflow, not a leaked escape sequence, and comparing it against
    # upstream would be comparing two unrelated things. Confirmed directly:
    # on the unfixed binary this measured "690->850" (a real 160px shift)
    # with NO new text visible in the saved screenshot at all -- purely the
    # terminal rewrapping into the space the sidebar stopped occupying.
    if no_effect:
        fork_leaked, fork_before, fork_after = _measure_fork_ctrl_shift_b_leak(
            "none", "none-shortcut-fork-leak")
        upstream_result = _measure_upstream_ctrl_shift_b()
        if upstream_result is not None:
            upstream_leaked, upstream_before, upstream_after = upstream_result
            log(f"comparison (informational, not this command's verdict): "
                f"fork none-mode terminal_leak={fork_leaked} ({fork_before}->{fork_after}), "
                f"upstream terminal_leak={upstream_leaked} ({upstream_before}->{upstream_after}) "
                f"-- {'MATCH' if fork_leaked == upstream_leaked else 'DIVERGE'}")
        else:
            log(f"comparison (informational): fork none-mode terminal_leak={fork_leaked} "
                f"({fork_before}->{fork_after}); upstream unavailable to compare against")
    else:
        log("comparison skipped — the sidebar column itself changed, so any prompt-line text "
            "movement is confounded by the terminal reflowing into/out of the freed column "
            "width, not a leaked escape sequence; not comparable to upstream in this state")

    if no_effect:
        log("none-shortcut: PASS — Ctrl+Shift+B does not touch the sidebar column in none mode "
            "(positive control confirmed the same gesture changes the column in left mode)")
        return 0
    else:
        log(f"none-shortcut: FAIL — Ctrl+Shift+B changed the sidebar column in none mode "
            f"(diff_ratio={none_ratio:.5f} >= {NOOP_THRESHOLD})")
        return 1


def _row_change_ratios(before, after, centers, half_h, col_width=SIDEBAR_COLUMN_WIDTH):
    """For each row y-center, the fraction of pixels that differ between the
    two frames within that row's band [cy-half_h, cy+half_h) and inside the
    sidebar column (x < col_width). Restricting to the column is the same move
    _column_diff_ratio makes and for the same reason: the terminal on the
    other side of the split reflows when the active tab changes, and a
    whole-window count would drown the sidebar's own signal in it. A moving
    selection highlight is a filled band, so the row it leaves and the row it
    lands on both light up here while every other row stays quiet -- that
    two-band signature, not a bare 'something changed', is what the caller
    reads."""
    bpx, apx = before.load(), after.load()
    w = min(col_width, before.width, after.width)
    h = min(before.height, after.height)
    ratios = []
    for cy in centers:
        y0 = max(0, int(round(cy - half_h)))
        y1 = min(h, int(round(cy + half_h)))
        if y1 <= y0:
            ratios.append(0.0)
            continue
        diff = sum(1 for y in range(y0, y1) for x in range(w) if bpx[x, y] != apx[x, y])
        ratios.append(diff / (w * (y1 - y0)))
    return ratios


def _movers(ratios, floor=0.03, quiet_frac=0.5):
    """Indices of the rows whose highlight changed between two frames: a row
    counts as a mover if its change ratio clears `floor` AND is at least
    1/quiet_frac of the largest quiet row, so a frame where nothing really
    moved (all ratios small and similar -- the exact fingerprint of the
    feature being broken, focus never leaving the terminal) yields an empty
    set rather than a spurious hit."""
    order = sorted(range(len(ratios)), key=lambda i: ratios[i], reverse=True)
    movers = [i for i in order if ratios[i] >= floor]
    return movers


def _longest_uniform_run(centers, tol=6):
    """The longest contiguous subsequence of `centers` whose consecutive gaps
    are all within `tol` of the run's own step.

    The premise this docstring used to state is INVERTED. It read: "Tab rows
    are evenly spaced; terminal text lines picked up by a mis-placed scan band
    are not, so this is what separates the real rows from noise regardless of
    where the band fell." Terminal text lines are a character grid: their
    leading is constant BY CONSTRUCTION, which makes them the most evenly
    spaced thing on the screen.

    Measured 2026-08-25 -- binary `v1.3.1-sidebar.3` sha256 `63caaf67...5a2ab`
    (checked against the release's SHA256SUMS), code `48a576d80`, a probe
    instrumenting `_detect_tab_rows`'s own candidate loop, Xephyr `:7`, window
    800x600, launch directory declared: candidate bands falling on the
    terminal returned runs with steps of 18, 21, 21, 21, 22, 22, 22.5, 22.5
    and 24 px, and a failing `--a11y-focus` run reported centers
    [66, 85, 104] -- 19.5 px, uniform across three points. Regularity does not
    separate rows from terminal noise: the noise is MORE regular than the
    signal, so this run-length criterion cannot discriminate "regardless of
    where the band fell". It is not missing a tie-break; its premise is false.

    What it DOES do, executed rather than asserted (each list run through this
    function at `48a576d80`):

        [94.5, 131.5, 187.5, 243.5] -> [131.5, 187.5, 243.5]
        [93.0, 140.5, 196.5, 252.5] -> [140.5, 196.5, 252.5]
        [10, 20, 30, 77]            -> [10, 20, 30]
        [66, 85, 104]               -> [66, 85, 104]

    It drops a single intruder sitting above otherwise regular rows -- the
    first list is `cmd_panes`'s own recorded banner case, the second a
    measured repository section header above three real rows -- and it keeps
    everything when the whole band landed off the sidebar, because what it was
    handed is uniform. Two different failures; a run-length criterion can only
    see one of them.

    Two detection chains exist in this file and neither knows the other
    (call sites verified by AST at `48a576d80`):

      A. `Session.measure_row_geometry` -- 5 call sites: `cmd_menu`,
         `cmd_scroll_colour`, `cmd_drag_reorder`, and `cmd_panes` twice. It
         never calls this function. Its only defence against an intruding
         band is the frozen `y_scan=(105, None)` passed at ONE of those five
         sites; the other three have nothing.
      B. `_detect_tab_rows` -> this function -- reached only from
         `cmd_a11y_focus`, twice.

    Those call sites were first cited by line number, and two of the seven
    were already wrong by the time this docstring reached `main`: adding
    these lines pushed everything below them down, so the two sites that
    FOLLOW this function moved while the five that PRECEDE it did not. The
    docstring perished its own citations by being inserted. A line number in
    a comment is perishable by construction -- it *looks* checkable, it is
    checkable the day it is written, and it stops being so in silence, with
    nothing failing and no reader warned. Cite the function: the name
    survives an insertion.

    No remedy is proposed here and none is implied: the cause of the
    band-lands-off-the-sidebar case has not been measured, and the frozen
    `y_scan=(105, None)` is itself a threshold derived from one window and one
    build. This is a description of what was measured, not a plan."""
    if len(centers) < 2:
        return centers[:]
    best = [centers[0]]
    j = 0
    n = len(centers)
    while j < n - 1:
        step = centers[j + 1] - centers[j]
        k = j
        run = [centers[j]]
        while k < n - 1 and abs((centers[k + 1] - centers[k]) - step) <= tol:
            run.append(centers[k + 1])
            k += 1
        if len(run) > len(best):
            best = run
        j = k if k > j else j + 1
    return best


def _detect_tab_rows(img, wh):
    """Tab-row y-centers, robust to the sidebar's width. The close 'x' button
    sits at the right edge of each row, and that edge moves with the sidebar
    width -- ~x165 on an 800px bare-X window, ~x225 on the ~922px WM window --
    so a single hardcoded band (the reference harness's x216-234) scans the
    TERMINAL on the narrower window and reads its banner/prompt lines as rows.
    Scan a range of candidate close-button bands instead and keep the one that
    yields the longest evenly-spaced run; returns (centers, close_btn_cx)."""
    best, best_cx = [], None
    for cx in range(140, 264, 6):
        run = _longest_uniform_run(_detect_row_bands(img, (cx - 16, cx + 16), (55, wh)))
        if len(run) > len(best):
            best, best_cx = run, cx
    return best, best_cx


def cmd_a11y_focus():
    """The sidebar's keyboard path, measured -- not asserted -- with a
    positive control in the same run.

    The claim under test: a dedicated accelerator (Ctrl+Shift+L ->
    win.focus-sidebar) moves keyboard focus into the tab list, where Up/Down
    move the selection highlight, Enter/Space activate a tab, and Escape hands
    focus back to the terminal. None of that can be reached with Tab, because
    the terminal Surface consumes Tab before it reaches the GTK focus chain --
    an established fact this harness does not re-litigate.

    The observable is which sidebar row is highlighted, read as a frame diff:
    when the highlight moves from row A to row B, exactly bands A and B change
    in the sidebar column and every other band stays quiet (rows are visually
    identical apart from the highlight -- same title, same pwd subtitle). That
    two-band signature is the measurement.

      POSITIVE CONTROL (same run, must pass or the run refuses to conclude):
        a mouse click on row 1 versus row 3 -- a known-good gesture -- must
        produce exactly the {row1, row3} two-band signature. This proves the
        detector localises a highlight move to the right rows before any
        keyboard delta measured the same way means anything.

      MEASUREMENT: from a known start (row 3 active), the keyboard alone --
        Ctrl+Shift+L, Up, Up, Enter -- must land the active tab on row 1,
        i.e. reproduce the SAME {row1, row3} signature the mouse control
        produced. If focus never entered the list, those keys reach the
        terminal instead (arrows move the shell cursor, Enter submits an empty
        line) and the highlight stays on row 3 -> an all-quiet diff. So the
        {row1,row3} signature is only reachable if focus entered, arrows
        navigated, and Enter activated -- the whole chain, proven by its end
        effect.

      ESCAPE sub-measurement: after Ctrl+Shift+L then one Up (highlight moves
        3 -> 2 but the tab is NOT activated), Escape must return focus to the
        terminal and leave the on-screen tab unchanged. Proven by: the frame
        after Escape matches the pre-navigation frame (highlight back on the
        live row 3, nothing activated), while the mid frame differed from it.
        Had Escape instead activated row 2, the after frame would show the
        highlight on row 2, not back on row 3.
    """
    ctrl, shift = 0xFFE3, 0xFFE1  # Control_L, Shift_L
    KEY_L, KEY_UP, KEY_RETURN = 0x006C, 0xFF52, 0xFF0D

    def send(d, mod_syms, key_sym, hold=0.15, settle=1.0):
        mod_kcs = [d.keysym_to_keycode(m) for m in mod_syms]
        key_kc = d.keysym_to_keycode(key_sym)
        for kc in mod_kcs:
            xtest.fake_input(d, X.KeyPress, kc)
        xtest.fake_input(d, X.KeyPress, key_kc)
        d.sync()
        time.sleep(hold)
        xtest.fake_input(d, X.KeyRelease, key_kc)
        for kc in reversed(mod_kcs):
            xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()
        time.sleep(settle)

    def shot(s, name):
        img = s.screenshot()
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        img.save(os.path.join(ARTIFACT_DIR, f"a11y-focus-{name}.png"))
        return img

    s = Session(sidebar_mode="left")
    try:
        # The window is driven purely from synthetic input, and Ctrl+Shift+T
        # (open tab) only lands while the window holds keyboard focus. Focus is
        # taken by a synthetic CLICK in the window -- what the window manager
        # honours -- not by set_input_focus/EWMH, which it reverts. Session's
        # constructor already clicks once; re-assert it here and retry the tab
        # opening until three real rows exist, so an unlucky focus steal or a
        # slow first paint can't leave the run under-populated.
        #
        # Row geometry is measured, never assumed: the window is 800x600 at
        # (0,0) under a bare X server (no WM decorations) and ~922x722 under a
        # WM, so both the row y-centers and the sidebar width differ between
        # runs. _detect_tab_rows finds the rows by the longest evenly-spaced run
        # of close-button glyphs, scanning for the band rather than assuming it.
        # Open tabs one at a time with a re-focus and a settle between each --
        # batching Ctrl+Shift+T loses one keystroke of two on a slow Debug
        # build -- until three real rows exist.
        rows, cx = _detect_tab_rows(s.screenshot(), s.wh)
        tries = 0
        while len(rows) < 3 and tries < 8:
            s.focus_terminal()
            s.open_tabs(1)
            time.sleep(1.2)
            rows, cx = _detect_tab_rows(s.screenshot(), s.wh)
            tries += 1
        if len(rows) < 3:
            raise RuntimeError(
                f"cmd_a11y_focus: expected >=3 evenly-spaced tab rows, found {len(rows)} "
                f"({[round(r, 1) for r in rows]}, close-btn cx={cx}) after {tries} "
                "open-tab attempts")
        centers = [round(r) for r in rows[:3]]
        gaps = [rows[i + 1] - rows[i] for i in range(len(rows) - 1)]
        step = sorted(gaps)[len(gaps) // 2]
        half_h = step / 2.0
        # Diff only the left of the sidebar (x<200): a moving selection
        # highlight fills the row card there, while the close button and the
        # terminal (x>=200 even on the narrow window) stay out of it -- the
        # terminal's own content changes when the active tab switches and would
        # otherwise leak into the row bands.
        col = 200
        # Click rows on their LEFT (title) area, never at a fixed x. The
        # reference harness's ROW1[0]=160 sits right on the close 'x' button on
        # the narrow 800px window (its button is at x~165), so a "select row"
        # click there hits the button instead -- same hardcoded-constant trap
        # the close-button band had. x=40 is in the title text on both windows,
        # well left of the button (cx>=150), so the click always activates the
        # row rather than closing it.
        click_x = 40
        log(f"a11y-focus: rows at y={centers} step={step} col_width={col} "
            f"(close-btn cx={cx}, row-click x={click_x})")

        # ---- POSITIVE CONTROL: mouse click row1 vs row3 -> {0,2} signature ----
        s._click(click_x, centers[0], button=1, settle=0.8)  # activate+highlight row1
        ctrl_r1 = shot(s, "control-row1")
        s._click(click_x, centers[2], button=1, settle=0.8)  # activate+highlight row3
        ctrl_r3 = shot(s, "control-row3")

        ctrl_ratios = _row_change_ratios(ctrl_r1, ctrl_r3, centers, half_h, col)
        ctrl_movers = _movers(ctrl_ratios)
        log(f"positive control: mouse row1<->row3 per-row change ratios="
            f"{[round(r, 4) for r in ctrl_ratios]} movers={ctrl_movers}")
        if set(ctrl_movers) != {0, 2}:
            log("POSITIVE CONTROL FAILED — a known-good mouse click on row1 vs row3 did not "
                "produce the expected {row1,row3} two-band signature; the detector is not "
                "trustworthy, refusing to report the keyboard result")
            return 2
        log("positive control OK: the detector localises a highlight move to exactly the "
            "clicked rows (row1 and row3), row2 stayed quiet")

        # ---- MEASUREMENT: keyboard Ctrl+Shift+L, Up, Up, Enter -> active=row1 ----
        # Known start: row3 active + highlighted, focus in the terminal.
        s._click(click_x, centers[2], button=1, settle=0.8)
        kbd_base = shot(s, "kbd-base")            # highlight on row3
        send(s.d, [ctrl, shift], KEY_L)           # focus into the list (cursor on row3)
        send(s.d, [], KEY_UP)                     # highlight 3 -> 2
        send(s.d, [], KEY_UP)                     # highlight 2 -> 1
        send(s.d, [], KEY_RETURN)                 # activate row1, focus back to terminal
        kbd_after = shot(s, "kbd-after")          # highlight on row1 if the chain worked

        kbd_ratios = _row_change_ratios(kbd_base, kbd_after, centers, half_h, col)
        kbd_movers = _movers(kbd_ratios)
        log(f"keyboard nav+activate: base(row3)->after per-row change ratios="
            f"{[round(r, 4) for r in kbd_ratios]} movers={kbd_movers}")
        kbd_ok = set(kbd_movers) == {0, 2}
        if not kbd_ok:
            log("MEASUREMENT: keyboard path did NOT reproduce the {row1,row3} signature — "
                "focus/navigation/activation chain is not working (an all-quiet diff means "
                "focus never left the terminal)")

        # ---- ESCAPE sub-measurement ----
        s._click(click_x, centers[2], button=1, settle=0.8)  # reset: row3 active
        esc_base = shot(s, "esc-base")            # highlight row3, focus in terminal
        send(s.d, [ctrl, shift], KEY_L)           # focus list (cursor row3)
        send(s.d, [], KEY_UP)                     # highlight 3 -> 2 (NOT activated)
        esc_mid = shot(s, "esc-mid")
        s.escape()                                # Escape: focus back to terminal
        time.sleep(0.6)
        esc_after = shot(s, "esc-after")

        base_mid = _row_change_ratios(esc_base, esc_mid, centers, half_h, col)
        base_after = _row_change_ratios(esc_base, esc_after, centers, half_h, col)
        moved_on_up = _movers(base_mid)           # Up must have moved the highlight (>=1 mover)
        residual = _movers(base_after)            # after Escape: should be back to base (no movers)
        log(f"escape: base->mid ratios={[round(r,4) for r in base_mid]} movers={moved_on_up}; "
            f"base->after ratios={[round(r,4) for r in base_after]} movers={residual}")
        esc_ok = (len(moved_on_up) >= 1) and (len(residual) == 0)
        if not esc_ok:
            log("MEASUREMENT: Escape sub-check FAILED — either Up did not move the highlight "
                "(moved_on_up empty) or Escape did not return to the pre-navigation state "
                "(residual non-empty: it activated or stranded the highlight)")

        if kbd_ok and esc_ok:
            log("a11y-focus: PASS — keyboard alone drove focus into the list, Up navigated, "
                "Enter activated row1 (matching the mouse positive control's signature), and "
                "Escape returned focus to the terminal without switching tabs. Captures under "
                f"{ARTIFACT_DIR}/a11y-focus-*.png")
            return 0
        log("a11y-focus: FAIL — see the failing sub-measurement above (positive control "
            "passed, so the detector is trustworthy and this is a real negative)")
        return 1
    finally:
        s.close()


def _content_band_spans(img, x_range, y_range, thresh=150):
    """Same "any bright pixel in this y-row" grouping as `_detect_row_bands`,
    but returns each group's own (y0, y1) SPAN rather than collapsing it to
    a center -- needed here because the span's own height is what gets
    cropped, not just its position."""
    px = img.load()
    x0, x1 = x_range
    x1 = min(x1, img.width)
    y0b, y1b = max(0, y_range[0]), min(img.height, y_range[1])

    rows_with_content = []
    for y in range(y0b, y1b):
        has = False
        for x in range(x0, x1):
            r, g, b = px[x, y][:3]
            if r > thresh and g > thresh and b > thresh:
                has = True
                break
        rows_with_content.append(has)

    groups = []
    gap = 999
    for i, has in enumerate(rows_with_content):
        if has:
            if gap > 3:
                groups.append([y0b + i, y0b + i])
            else:
                groups[-1][1] = y0b + i
            gap = 0
        else:
            gap += 1
    return [(g0, g1) for g0, g1 in groups]


def _header_band_box(img, wh):
    """(col_width, y0, y1) of the section-header band -- MEASURED, not a
    row-pitch guess. `wh` is unused; kept so callers already passing it
    alongside an image (as `_detect_tab_rows` requires) don't need a
    different call shape for this derivation.

    A row's close-button glyph CENTER is not its card's top edge, and this
    sidebar's idle row cards don't render a background distinct from the
    rest of the column at rest (measured: a min/max colour scan down the
    column shows the SAME baseline outside of text, whether inside a row's
    card or between rows) -- so neither "half a row-pitch above center" nor
    "where the background changes" locates the header. What's actually
    unique to the header is that it's the FIRST band of bright (text)
    content below the window's own toolbar: `_chrome_bottom` finds that
    toolbar's bottom edge, `_content_band_spans` groups the bright rows
    below it (header text, then a gap, then row 1's title, its subtitle,
    row 2's title, ...) using the exact `thresh=150` bright-pixel test
    `_detect_row_bands` already uses elsewhere in this harness, and the
    header is group zero. A small pad on both edges keeps faint
    anti-aliased glyph edges from being clipped out of the crop.

    Cross-checked against the reference 800x600 bare-X window this harness
    already uses elsewhere: chrome_bottom=51, and the header's own text
    band measured independently (frame-diff, 1-tab vs 2-tab) at y=[88,101]
    -- this function's padded span lands inside that range on the same
    window.

    Returns None if the sidebar column can't be found, or if no bright
    band exists below the toolbar to call the header."""
    col = _measure_sidebar_column(img)
    if col <= 0:
        return None
    top = _chrome_bottom(img, col)
    if top <= 0:
        return None
    spans = _content_band_spans(img, (0, col), (top, img.height))
    if not spans:
        return None
    y0, y1 = spans[0]
    pad = 3
    y0 = max(top, y0 - pad)
    y1 = min(img.height, y1 + pad + 1)
    if y1 <= y0:
        return None
    return col, int(y0), int(y1)


def cmd_section_header():
    """Section-header rebind, measured as a pixel-band comparison -- never a
    text read. Same family as `--drag-reorder`: no OCR, no accessibility
    tree, just the same band of pixels captured twice and diffed.

    THE BUG (measured by hand, 2026-08-24, before this mode existed): a
    window opened with ONE tab, inside a git repo, shows "No repository" in
    its section header -- even though the tab's pwd is already the repo
    root. Opening a SECOND tab (same repo, so still one section) makes the
    header correct. `headerBind` computes its label once, at bind time, and
    GTK only re-invokes it when it thinks a header's section boundary
    moved; a same-position single section's pwd arriving via OSC 7 doesn't
    move any boundary, so the stale label sticks until something else (like
    a second tab) forces a rebuild that happens to also force a rebind.

    THE MEASUREMENT: capture the header band (see `_header_band_box`) at
    t0 -- one tab, freshly launched inside a git repo -- and again at t1,
    after opening a second tab in the SAME window (same section, so the
    header's position does not move). Comparing the SAME band at the SAME
    coordinates:

        t0 != t1  -> the header's pixels changed when the 2nd tab arrived
                     -> it was NOT already showing the repo name at t0
                     -> RED (the bug, reproduced)
        t0 == t1  -> the header already looked like its final state at t0
                     -> the correct label was already bound before the 2nd
                        tab -> GREEN (fixed)

    POSITIVE CONTROL (same run, required before trusting either verdict
    above): a header band CAN differ when the label differs -- proven by
    comparing t1 (git repo, label = repo name) against a header band from a
    fresh one-tab window launched OUTSIDE any git repo (label = "No
    repository" for real, no OSC-7-arrival ambiguity involved -- the
    label is right the first time because there is nothing to wait for).
    If those two do NOT differ, the band-diff technique cannot tell two
    different labels apart here and neither RED nor GREEN above is
    trustworthy -- abstain (`CANNOT_MEASURE`), don't report either.
    """
    def band(session, box):
        col, y0, y1 = box
        return session.screenshot().crop((0, y0, col, y1)).tobytes()

    repo_dir = REPO_ROOT  # this checkout is itself a git repo
    plain_dir = tempfile.mkdtemp(prefix="acceptance-harness-nogit-")

    s_git = Session(sidebar_mode="left", cwd=repo_dir)
    try:
        # t0 is captured with only 1 tab, so its OWN frame cannot supply a
        # trustworthy geometry: `_detect_tab_rows` needs an evenly-spaced RUN
        # to reject noise, and any 2 points are trivially "evenly spaced"
        # (one gap, nothing to compare it against) -- so a single stray
        # terminal text line below the lone real row would be misread as a
        # second row and hand back a bogus band. The geometry is instead
        # derived once, below, from the 2-tab frame where a real 2-row run
        # exists to validate against, and that same box crops BOTH frames.
        t0_img = s_git.screenshot()

        s_git.focus_terminal()
        s_git.open_tabs(1)
        time.sleep(3.0)  # let OSC 7 + the deferred rebuild land before capturing
        t1_img = s_git.screenshot()

        box = _header_band_box(t1_img, s_git.wh)
        if box is None:
            return _abstain(
                "could not derive the header band from the 2-tab frame",
                f"window {s_git.ww}x{s_git.wh}: _measure_sidebar_column or "
                "_detect_tab_rows didn't yield a usable geometry")
        col, y0, y1 = box
        log(f"section-header: band x=[0,{col}) y=[{y0},{y1}) (derived from a 2-row frame)")

        if t0_img.size != t1_img.size:
            return _abstain(
                "window resized between t0 and t1",
                f"t0 size={t0_img.size} t1 size={t1_img.size}")

        t0 = t0_img.crop((0, y0, col, y1)).tobytes()
        t1 = band(s_git, box)
    finally:
        s_git.close()

    s_plain = Session(sidebar_mode="left", cwd=plain_dir)
    try:
        if (s_plain.ww, s_plain.wh) != (t0_img.width, t0_img.height):
            return _abstain(
                "control window geometry differs from the measured window",
                f"git-repo window {t0_img.width}x{t0_img.height} vs "
                f"no-git window {s_plain.ww}x{s_plain.wh} -- the same crop "
                "box can't be trusted across two different window sizes")
        control = band(s_plain, box)
    finally:
        s_plain.close()

    control_differs = control != t1
    log(f"section-header: positive control (no-git band vs t1 band) differs={control_differs}")
    if not control_differs:
        return _abstain(
            "positive control did not differ",
            "a header band known to say \"No repository\" (control, launched "
            "outside any git repo) is pixel-identical to t1's band -- the "
            "band-diff technique can't tell these two labels apart here, so "
            "neither a RED nor a GREEN verdict below would be trustworthy")
    log("positive control OK: the band-diff technique distinguishes a "
        "\"No repository\" header from a repo-name header at these coordinates")

    same = t0 == t1
    if same:
        log("section-header: PASS — t0 (1 tab) already matched t1 (2 tabs); "
            "the header showed the repo name before the 2nd tab arrived, "
            "not \"No repository\"")
        return 0
    log("section-header: FAIL — t0 (1 tab) differs from t1 (2 tabs): the "
        "header's pixels changed when the 2nd tab arrived, meaning it was "
        "NOT already showing the repo name at t0 (positive control passed, "
        "so this is a real negative, not a detector failure)")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hamburger", action="store_true", help="positive control only")
    ap.add_argument("--menu", action="store_true", help="Task 9: row context menu, with positive+negative controls")
    ap.add_argument("--scroll-colour", action="store_true", help="Task 11: 6 tabs, colour + scroll scenario")
    ap.add_argument("--none-parity", action="store_true",
                     help="none-mode UI parity: tab bar present, no sidebar, no sidebar shortcut")
    ap.add_argument("--none-shortcut", action="store_true",
                     help="none mode: Ctrl+Shift+B must be a no-op on the sidebar column, "
                          "with a left-mode positive control in the same run")
    ap.add_argument("--drag-reorder", action="store_true",
                     help="tab drag-to-reorder in the sidebar, with a move_tab:1 positive control")
    ap.add_argument("--panes", action="store_true",
                     help="v1.1 tranche 1: second-level pane rows, with a row-geometry "
                          "positive control and a live active-surface swap round trip")
    ap.add_argument("--a11y-focus", action="store_true", dest="a11y_focus",
                     help="sidebar keyboard path: Ctrl+Shift+L focuses the tab list, "
                          "arrows move the selection, Enter/Space activate, Escape returns "
                          "focus to the terminal -- with a mouse positive control in the same run")
    ap.add_argument("--section-header", action="store_true", dest="section_header",
                     help="a 1-tab window's section header must already show the repo name, "
                          "not \"No repository\", by the time a 2nd same-repo tab opens -- "
                          "band-diff measurement with a no-git positive control in the same run")
    ap.add_argument("--sidebar-column-regression", action="store_true", dest="sidebar_column_regression",
                     help="_measure_sidebar_column vs 4 self-generated stand-ins for the "
                          "majordome's reference captures (no X server needed)")
    args = ap.parse_args()

    if args.hamburger:
        return cmd_hamburger()
    if args.menu:
        return cmd_menu()
    if args.scroll_colour:
        return cmd_scroll_colour()
    if args.none_parity:
        return cmd_none_parity()
    if args.none_shortcut:
        return cmd_none_shortcut()
    if args.drag_reorder:
        return cmd_drag_reorder()
    if args.panes:
        return cmd_panes()
    if args.a11y_focus:
        return cmd_a11y_focus()
    if args.section_header:
        return cmd_section_header()
    if args.sidebar_column_regression:
        return cmd_sidebar_column_regression()

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
