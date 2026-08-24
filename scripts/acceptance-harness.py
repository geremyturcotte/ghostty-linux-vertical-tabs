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

Every mode launches its own ghostty process against an isolated
XDG_CONFIG_HOME and terminates it on exit. --menu and --scroll-colour always
fire the hamburger positive control first in the same run and refuse to
report a negative if it fails. --none-parity and --none-shortcut are a
separate concern: they measure the UI-level half of `--gtk-sidebar-tabs=none`'s
promise (tab bar present, no sidebar, no sidebar shortcut) via pixel
structure rather than popover detection -- see cmd_none_parity()'s and
cmd_none_shortcut()'s docstrings. Neither modifies or replaces
scripts/check-none-parity.sh, which only diffs GTK/GLib log output and
cannot see a pixel or a widget.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

from Xlib import X, display
from Xlib.ext import xtest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GHOSTTY_BIN = os.path.join(REPO_ROOT, "zig-out", "bin", "ghostty")
ARTIFACT_DIR = os.path.join(REPO_ROOT, ".prokai", "tmp", "dispatch-artifacts")

# Coordinates measured on the 922x722 reference window (docs/acceptance.md).
ROW1 = (160, 133)
HAMBURGER = (724, 78)
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


def launch_ghostty(extra_config="", sidebar_mode="left"):
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
        [GHOSTTY_BIN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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

    def __init__(self, extra_config="", sidebar_mode="left"):
        self.proc = launch_ghostty(extra_config, sidebar_mode)
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


def cmd_hamburger():
    s = Session()
    try:
        result = s.probe_click(*HAMBURGER, button=1, label="hamburger", min_size=HAMBURGER_MIN_SIZE)
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
        ctrl = s.probe_click(*HAMBURGER, button=1, label="hamburger-control", min_size=HAMBURGER_MIN_SIZE)
        if ctrl is None:
            log("POSITIVE CONTROL FAILED — refusing to report the row-menu result")
            return 2
        neg = s.probe_click(*TAB_OVERVIEW, button=1, label="tab-overview-in-window-control")
        if neg is not None:
            log("NOTE: in-window negative control unexpectedly produced an override-redirect window")
        result = s.probe_click(*ROW1, button=3, label="row-context-menu")
        if result is None:
            log("row-context-menu: confirmed ABSENT (positive control succeeded in the same run)")
            return 1
        log(f"row-context-menu: PRESENT — 0x{result['id']:x} {result['w']}x{result['h']} -> {result['png']}")
        return 0
    finally:
        s.close()


ROW_STEP_Y = 66  # vertical spacing between sidebar rows, measured on the reference window


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
    which don't require input focus)."""
    s = Session()
    try:
        s.open_tabs(5)  # 6 total with the initial tab

        s.colour_row(ROW1, 4, "row1")  # Red
        s.colour_row((ROW1[0], ROW1[1] + ROW_STEP_Y), 1, "row2")  # Blue

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
        # (catches a partial swap). ROW_STEP_Y is measured on the pre-resize,
        # pre-scroll layout; after "scroll to top" the observed row spacing
        # has drifted somewhat (measured 54px vs the nominal 66px in one
        # captured run), so the band is wide (+/-25px) to absorb that rather
        # than assume exact pixel reproduction across window states.
        BAND_TOLERANCE = 25

        def band(row_index):
            center = ROW1[1] + row_index * ROW_STEP_Y
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


# Terminal background is a distinct blue-gray (measured ~(40,44,52) across
# this whole sweep's screenshots); GTK chrome (headerbar, tab bar, sidebar
# row cards) is a near-neutral gray (~(69,69,69)). The invariant that holds
# up across both: chrome has near-equal R/G/B, terminal background has B
# distinctly greater than R.
def _looks_like_chrome(px):
    r, g, b = px[:3]
    return (b - r) < 8


def _prompt_text_x(img, y, thresh=150):
    """First bright (near-white) pixel in row y -- the shell prompt's text
    start. Used as the sidebar-column-occupancy signal: with a sidebar, the
    terminal (and its prompt) starts well to the right of the column; with
    none, the terminal spans the full width and the prompt starts near the
    left edge."""
    px = img.load()
    for x in range(img.width):
        r, g, b = px[x, y][:3]
        if r > thresh and g > thresh and b > thresh:
            return x
    return None


def _find_prompt_row(img, y0=100, y1=250, x_scan=(0, 900), thresh=150, min_count=5):
    """Scan downward for the first row with substantial bright-pixel content
    -- used to locate the shell prompt's y so _prompt_text_x can be measured
    at a real text row rather than a guessed offset (fragile across window
    states, as Task 11's post-scroll drift showed)."""
    px = img.load()
    for y in range(y0, y1):
        n = 0
        for x in range(*x_scan):
            r, g, b = px[x, y][:3]
            if r > thresh and g > thresh and b > thresh:
                n += 1
                if n >= min_count:
                    return y
    return None


def _snapshot_signals(s):
    """One screenshot -> (img, prompt_y, prompt_x, dominant_band_colour, chrome_band)."""
    raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
    img = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")

    prompt_y = _find_prompt_row(img)
    prompt_x = _prompt_text_x(img, prompt_y) if prompt_y is not None else None

    # Sample the row-bar band (y=100-140, the AdwTabBar's expected position
    # measured on the reference window) on the terminal side (x=460-900,
    # clear of any sidebar column regardless of mode) and check whether the
    # dominant colour there is chrome-grey (tab bar present) or
    # terminal-background-blue (no tab bar, content already started).
    px = img.load()
    colour_counts = {}
    for y in range(100, 140):
        for x in range(460, min(900, s.ww)):
            c = px[x, y][:3]
            colour_counts[c] = colour_counts.get(c, 0) + 1
    dominant = max(colour_counts, key=colour_counts.get)
    chrome_band = _looks_like_chrome(dominant)

    return img, prompt_y, prompt_x, dominant, chrome_band


def _measure_mode(sidebar_mode, label, max_polls=10, poll_interval=0.5):
    """Launch one mode with 2 tabs and return the two structural signals:
    prompt_x (sidebar-column occupancy) and chrome_band (whether a
    horizontal tab-bar-style chrome row sits between the headerbar and the
    terminal).

    Polls (screenshot, remeasure) until two consecutive readings agree,
    instead of a single screenshot after a fixed sleep -- a fixed sleep was
    observed to read a not-yet-painted frame once (positive control on
    `left` mode returned prompt_text_x=117 instead of ~317, when a stray
    ghostty process from a prior run had shifted this run's window off its
    expected placement and delayed its first paint). The positive control
    caught that flake and refused to trust the run, which is what it's for,
    but re-measuring until the signal stops changing removes the flake at
    the source rather than only detecting it after the fact."""
    s = Session(sidebar_mode=sidebar_mode)
    try:
        s.open_tabs(1)  # 2 tabs total
        time.sleep(0.5)

        prev = None
        img = prompt_y = prompt_x = dominant = chrome_band = None
        settled = False
        for attempt in range(1, max_polls + 1):
            img, prompt_y, prompt_x, dominant, chrome_band = _snapshot_signals(s)
            cur = (prompt_x, chrome_band)
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

        log(f"{label}: settled after {attempt} poll(s), prompt_row_y={prompt_y} "
            f"prompt_text_x={prompt_x} tab-bar-band dominant colour={dominant} "
            f"chrome_like={chrome_band} -> {shot_path}")
        return {"prompt_x": prompt_x, "chrome_band": chrome_band, "shot": shot_path,
                "settled": settled, "window_width": s.ww}
    finally:
        s.close()


def cmd_none_parity():
    """UI-level parity check for `--gtk-sidebar-tabs=none`'s claim in
    docs/acceptance.md: "tab bar present, no sidebar, no sidebar shortcut" --
    an interface claim that scripts/check-none-parity.sh cannot verify since
    it only diffs GTK/GLib log output, never a pixel or a widget (see that
    finding's discussion in docs/acceptance.md). This does not replace or
    modify check-none-parity.sh; it measures the half of the `none` promise
    that script structurally cannot.

    Positive control: measures `left` mode with the same method in the same
    run first, to prove the two structural signals below actually
    discriminate a sidebar-present state from a sidebar-absent one, rather
    than just returning "absent" unconditionally."""
    log("positive control: measuring `left` mode (sidebar expected present, no tab bar)")
    left = _measure_mode("left", "left-control")
    if not left["settled"]:
        log("POSITIVE CONTROL FAILED — signal never settled across the poll cap; "
            "refusing to trust an unsettled reading even if it looked plausible")
        return 2
    if left["prompt_x"] is None:
        log("POSITIVE CONTROL FAILED — could not even locate `left` mode's terminal prompt")
        return 2
    # A pixel-absolute threshold only holds for the one window width this
    # harness happens to launch at; normalize to a fraction of the measured
    # window width instead so this keeps working if that ever changes. The
    # sidebar column measures ~260/922 (~28%) of the reference window; the
    # threshold sits well below that, at 16%, with margin on both sides
    # (no-sidebar prompts land near 0-16%, sidebar-present prompts near 28%+).
    no_sidebar_x_threshold = left["window_width"] * 0.16
    if not (left["prompt_x"] > no_sidebar_x_threshold and not left["chrome_band"]):
        log(f"POSITIVE CONTROL FAILED — `left` mode did not show the expected sidebar signal "
            f"(prompt_x={left['prompt_x']}, threshold={no_sidebar_x_threshold:.0f}, "
            f"chrome_band={left['chrome_band']}); refusing to trust the `none` measurement below")
        return 2
    log(f"positive control OK: left mode prompt_x={left['prompt_x']} (sidebar occupies the column), "
        f"chrome_band={left['chrome_band']} (no tab bar)")

    log("measuring `none` mode")
    none = _measure_mode("none", "none-mode")
    if not none["settled"]:
        log("MEASUREMENT FAILED — `none` mode's signal never settled across the poll cap; "
            "refusing to report a verdict from an unsettled reading")
        return 2

    no_sidebar_x_threshold = none["window_width"] * 0.16
    no_sidebar = none["prompt_x"] is not None and none["prompt_x"] < no_sidebar_x_threshold
    tab_bar_present = none["chrome_band"] is True
    log(f"none: no_sidebar={no_sidebar} (prompt_x={none['prompt_x']}) "
        f"tab_bar_present={tab_bar_present} (chrome_band={none['chrome_band']})")

    # "no sidebar shortcut": Ctrl+Shift+B should be a no-op in none mode.
    s = Session(sidebar_mode="none")
    try:
        s.open_tabs(1)
        time.sleep(0.5)
        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        before = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        before.save(os.path.join(ARTIFACT_DIR, "none-parity-shortcut-before.png"))

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

        raw = s.win.get_image(0, 0, s.ww, s.wh, X.ZPixmap, 0xFFFFFFFF)
        after = Image.frombytes("RGB", (s.ww, s.wh), raw.data, "raw", "BGRX")
        after.save(os.path.join(ARTIFACT_DIR, "none-parity-shortcut-after.png"))
    finally:
        s.close()

    before_px, after_px = before.load(), after.load()
    diff_pixels = sum(
        1 for y in range(before.height) for x in range(before.width)
        if before_px[x, y] != after_px[x, y]
    )
    total_pixels = before.width * before.height
    diff_ratio = diff_pixels / total_pixels
    no_shortcut = diff_ratio < 0.005  # cursor blink / AA jitter tolerance
    log(f"none: Ctrl+Shift+B diff_ratio={diff_ratio:.5f} ({diff_pixels}/{total_pixels} px) "
        f"no_shortcut_effect={no_shortcut}")

    if no_sidebar and tab_bar_present and no_shortcut:
        log("none-parity: PASS — tab bar present, no sidebar column, Ctrl+Shift+B is a no-op")
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


SIDEBAR_COLUMN_WIDTH = 260  # matches cmd_scroll_colour's sidebar-crop convention


def _column_diff_ratio(before, after, col_width=SIDEBAR_COLUMN_WIDTH):
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
    diff = sum(1 for y in range(h) for x in range(w) if before_px[x, y] != after_px[x, y])
    total = w * h
    return diff / total if total else 0.0


def _shortcut_column_diff(sidebar_mode, label):
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

    ratio = _column_diff_ratio(before, after)
    log(f"{label}: sidebar-column(x<{SIDEBAR_COLUMN_WIDTH}) diff_ratio={ratio:.5f}")
    return ratio


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
    whole-window shortcut check already inside cmd_none_parity(); that one
    already caught the sidebar-shortcut regression, but has no positive
    control of its own for the shortcut sub-check specifically."""
    log("positive control: `left` mode, Ctrl+Shift+B must change the sidebar column")
    CHANGE_THRESHOLD = 0.05  # a sidebar appearing/disappearing swamps this in a 260px-wide column
    NOOP_THRESHOLD = 0.005  # cursor blink / AA jitter tolerance

    left_ratio = _shortcut_column_diff("left", "left-control")
    if left_ratio < CHANGE_THRESHOLD:
        log(f"POSITIVE CONTROL FAILED — left mode sidebar-column diff_ratio={left_ratio:.5f} "
            f"< {CHANGE_THRESHOLD}; refusing to conclude anything about `none` mode")
        return 2
    log(f"positive control OK: left mode sidebar-column diff_ratio={left_ratio:.5f} (sidebar toggled)")

    log("measuring `none` mode: Ctrl+Shift+B must be a no-op")
    none_ratio = _shortcut_column_diff("none", "none-mode")
    no_effect = none_ratio < NOOP_THRESHOLD
    log(f"none: sidebar-column diff_ratio={none_ratio:.5f} no_effect={no_effect}")

    if no_effect:
        log("none-shortcut: PASS — Ctrl+Shift+B is a no-op in none mode "
            "(positive control confirmed the same gesture changes the column in left mode)")
        return 0
    else:
        log(f"none-shortcut: FAIL — Ctrl+Shift+B changed the sidebar column in none mode "
            f"(diff_ratio={none_ratio:.5f} >= {NOOP_THRESHOLD})")
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

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
