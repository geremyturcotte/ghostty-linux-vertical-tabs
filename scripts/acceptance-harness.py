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
cmd_drag_reorder()'s docstring.
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
        if not centers:
            raise RuntimeError("measure_row_geometry: no sidebar rows detected -- "
                                "is the sidebar visible and at least one tab open?")

        step_y = None
        if len(centers) >= 2:
            diffs = sorted(centers[i + 1] - centers[i] for i in range(len(centers) - 1))
            step_y = diffs[len(diffs) // 2]  # median

        log(f"measure_row_geometry: centers={centers} step_y={step_y}")
        return {"row1_y": centers[0], "step_y": step_y, "centers": centers}

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

        geom = s.measure_row_geometry()
        if geom["step_y"] is None:
            raise RuntimeError("cmd_scroll_colour: could not measure row spacing "
                                "(fewer than 2 rows detected with 6 tabs open)")
        row1_y, step_y = geom["row1_y"], geom["step_y"]
        log(f"task11: measured row1_y={row1_y} step_y={step_y} (not the hardcoded "
            "ROW_STEP_Y=66 an earlier version used, which was wrong -- true spacing is 54px)")

        s.colour_row((ROW1[0], round(row1_y)), 4, "row1")  # Red
        s.colour_row((ROW1[0], round(row1_y + step_y)), 1, "row2")  # Blue

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
    """Tab drag-to-reorder within the sidebar. Also the deciding evidence
    for the checklist's separate "drag a tab into its own window" item
    (`docs/acceptance.md`), since both are driven by the same DND wiring on
    the tab widget -- confirmed absent by reading the source directly:
    `src/apprt/gtk/class/sidebar.zig` and `sidebar_row.zig` wire no
    `GtkDragSource`/`GtkDropTarget` at all; the only `DropTarget` anywhere
    in the GTK apprt is `surface.zig`'s, for file drops onto the terminal,
    unrelated to tabs.

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
        s.open_tabs(2)  # 3 total; the newest (index 2, rank 3) is focused
        geom = s.measure_row_geometry()
        if geom["step_y"] is None or len(geom["centers"]) < 3:
            raise RuntimeError(f"cmd_drag_reorder: expected 3 rows, measured {geom['centers']}")
        rank1_y, rank2_y, rank3_y = (round(c) for c in geom["centers"][:3])
        log(f"drag-reorder: measured ranks 1/2/3 at y={rank1_y}/{rank2_y}/{rank3_y}")

        s.colour_row((ROW1[0], rank3_y), 4, "drag-reorder-rank3")  # Red

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

        BAND_TOLERANCE = 15
        control_ok = (
            after_movetab_centre is not None
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
        x0, y0 = ROW1[0], rank1_y
        x1, y1 = ROW1[0], rank3_y
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
                "a same-run positive control confirmed reordering is observable; matches the "
                "source read (no GtkDragSource/GtkDropTarget wired in sidebar.zig/sidebar_row.zig)")
            return 0
        elif moved_to_rank3:
            log("drag-reorder: REORDERING HAPPENED — the drag moved the row to rank 3. "
                "Contradicts the source read; needs a human to confirm before trusting this.")
            return 1
        else:
            log(f"drag-reorder: INCONCLUSIVE — red centre after drag ({after_drag_centre}) is "
                f"neither rank 1 nor rank 3; something moved it somewhere unexpected")
            return 2
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
    none_shortcut_ratio = _shortcut_column_diff("none", "none-parity-shortcut")
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
    what `gtk-sidebar-tabs=none` must show."""
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

    def is_terminal(x):
        n = sum(1 for y in range(y0, y1) if px[x, y][:3] == term_bg)
        return n > 0.9 * span

    x = w - 8
    while x > 0 and is_terminal(x - 1):
        x -= 1
    return x


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


def _shortcut_column_diff(sidebar_mode, label, col_width=None):
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
    ratio = _column_diff_ratio(before, after, col_width=col_width or measured
                               or SIDEBAR_COLUMN_WIDTH)
    used = col_width or measured or SIDEBAR_COLUMN_WIDTH
    log(f"{label}: sidebar column MEASURED at x={measured} "
        f"(diff taken over x<{used}) diff_ratio={ratio:.5f}")
    return ratio, measured


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

    left_ratio, left_col = _shortcut_column_diff("left", "left-control")
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
    none_ratio, none_col = _shortcut_column_diff("none", "none-mode", col_width=left_col)
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

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
