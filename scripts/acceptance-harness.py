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

Every mode launches its own ghostty process against an isolated
XDG_CONFIG_HOME and terminates it on exit. --menu and --scroll-colour always
fire the hamburger positive control first in the same run and refuse to
report a negative if it fails.
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
GHOSTTY_BIN = "/home/prokai/Github/ghostty-linux-vertical-tabs/zig-out/bin/ghostty"
ARTIFACT_DIR = os.path.join(REPO_ROOT, ".prokai", "tmp", "dispatch-artifacts")

# Coordinates measured on the 922x722 reference window (docs/acceptance.md).
ROW1 = (160, 133)
HAMBURGER = (724, 78)
HAMBURGER_MIN_SIZE = (200, 200)  # the real menu is 349x662; a "Menu principal" tooltip is 121x32
TAB_OVERVIEW = (690, 78)  # negative control: in-window, opens 0 new X windows
TERMINAL_FOCUS = (600, 300)


def log(msg):
    print(f"[harness] {msg}", flush=True)


def launch_ghostty(extra_config=""):
    env = dict(os.environ)
    env["GDK_BACKEND"] = "x11"
    env["DISPLAY"] = env.get("DISPLAY", ":0")
    env.pop("WAYLAND_DISPLAY", None)
    cfgdir = tempfile.mkdtemp(prefix="acceptance-harness-xdg-")
    os.makedirs(os.path.join(cfgdir, "ghostty"), exist_ok=True)
    with open(os.path.join(cfgdir, "ghostty", "config"), "w") as f:
        f.write("gtk-sidebar-tabs = left\n")
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
    """Match by WM_CLASS ("ghostty", "com.mitchellh.ghostty"), not WM_NAME --
    the window title is the shell's cwd, not the app name, so it varies with
    the launch directory and cannot be relied on."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for (wid, nm, w, h, x, y, orr, ms) in tree(root):
            if w > 300 and h > 200 and ms == X.IsViewable and not orr:
                try:
                    win = d.create_resource_object("window", wid)
                    cls = win.get_wm_class()
                except Exception:
                    continue
                if cls and any(c == "ghostty" for c in cls):
                    return wid, w, h
        time.sleep(0.3)
    return None


class Session:
    """One ghostty process + its X connection, window handle, and absolute origin."""

    def __init__(self, extra_config=""):
        self.proc = launch_ghostty(extra_config)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hamburger", action="store_true", help="positive control only")
    ap.add_argument("--menu", action="store_true", help="Task 9: row context menu, with positive+negative controls")
    ap.add_argument("--scroll-colour", action="store_true", help="Task 11: 6 tabs, colour + scroll scenario")
    args = ap.parse_args()

    if args.hamburger:
        return cmd_hamburger()
    if args.menu:
        return cmd_menu()
    if args.scroll_colour:
        return cmd_scroll_colour()

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
