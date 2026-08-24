const std = @import("std");

const adw = @import("adw");
const gdk = @import("gdk");
const gio = @import("gio");
const glib = @import("glib");
const gobject = @import("gobject");
const gtk = @import("gtk");

const ext = @import("../ext.zig");
const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
const SidebarPaneRow = @import("sidebar_pane_row.zig").SidebarPaneRow;
const SplitTree = @import("split_tree.zig").SplitTree;
const Surface = @import("surface.zig").Surface;
const Tab = @import("tab.zig").Tab;
const Window = @import("window.zig").Window;

const log = std.log.scoped(.gtk_ghostty_sidebar_row);

/// One row of the tab sidebar.
///
/// This exists as its own widget for a specific reason. A
/// `GtkBuilderListItemFactory` template cannot connect a signal to a Zig
/// handler, so a close button inside the factory template has no way to act.
/// Binding the action target to `GtkListItem:position` looks like it works —
/// it even compiles — but GObject has no guint-to-GVariant transform, and it
/// fails at runtime with:
///
///     Unable to convert a value of type guint to a value of type GVariant
///
/// A row that *owns its page* needs neither a position nor a variant. It is
/// also the ordinary GTK4 way to do this.
pub const SidebarRow = extern struct {
    const Self = @This();
    parent_instance: Parent,
    pub const Parent = adw.Bin;
    pub const getGObjectType = gobject.ext.defineClass(Self, .{
        .name = "GhosttySidebarRow",
        .instanceInit = &init,
        .classInit = &Class.init,
        .parent_class = &Class.parent,
        .private = .{ .Type = Private, .offset = &Private.offset },
    });

    pub const properties = struct {
        pub const page = struct {
            pub const name = "page";
            const impl = gobject.ext.defineProperty(
                name,
                Self,
                ?*adw.TabPage,
                .{
                    .accessor = C.privateObjFieldAccessor("page"),
                },
            );
        };
    };

    const Private = struct {
        /// The tab this row stands for. Borrowed from the tab view.
        page: ?*adw.TabPage = null,

        /// Right-click menu. The horizontal tab bar carries one of these
        /// upstream; hiding that bar took it away, so the sidebar restores it.
        menu: *gtk.PopoverMenu,

        /// Actions the menu items target, scoped to this row under "row".
        action_group: ?*gio.SimpleActionGroup = null,

        /// The coloured dot that marks a tab.
        colour_dot: *gtk.Image,

        /// Second-level pane rows, one per leaf in the tab's split tree.
        panes_box: *gtk.Box,

        /// The split tree we're currently listening to, borrowed from the
        /// bound tab. Rows are recycled, so this must be disconnected
        /// before rebinding to a different page's tree, same reasoning as
        /// `Sidebar.selected_page_handler` in sidebar.zig: zig-gobject's
        /// connect doesn't tie the handler's life to ours.
        split_tree: ?*SplitTree = null,
        split_tree_changed_handler: c_ulong = 0,

        pub var offset: c_int = 0;
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
        self.initActionMap();

        // Rows are recycled: when the factory rebinds this row to another
        // tab, the dot must follow the new tab, not keep the old one's colour.
        _ = gobject.Object.signals.notify.connect(
            self,
            *Self,
            notifyPage,
            self,
            .{ .detail = "page" },
        );
    }

    /// The row's subtitle: the last segment of the working directory.
    ///
    /// The full path is what `pwd` carries, and it is far too long for a
    /// sidebar column — `~/Github/prokai-plugins` ellipsizes to noise. The
    /// last segment is what actually distinguishes one tab from another.
    fn closureCwd(
        _: *Self,
        pwd_: ?[*:0]const u8,
    ) callconv(.c) ?[*:0]const u8 {
        const pwd = pwd_ orelse return null;
        const span = std.mem.span(pwd);
        if (span.len == 0) return null;

        // MUST allocate. Three independent reviewers said this dupeZ leaks —
        // that GTK copies the returned string and never frees it. It was tried
        // their way and the binary aborted on exit with
        // `munmap_chunk(): invalid pointer`, in every mode: something DOES
        // free this pointer, so returning an interior pointer into the
        // caller's buffer hands free() an address it never allocated.
        // Unanimous review lost to one run of the program.
        const base = std.fs.path.basename(span);
        return glib.ext.dupeZ(u8, if (base.len == 0) span else base);
    }

    /// Adwaita's semantic style classes, which tint a symbolic icon and follow
    /// the user's theme. Using these rather than a palette of our own is a
    /// deliberate trade: a custom palette would mean editing upstream's
    /// style.css and its three variants, and this fork does not rewrite
    /// upstream files.
    const colours = [_][:0]const u8{ "accent", "success", "warning", "error" };

    /// The colour lives on the AdwTabPage, not on the row. Rows are recycled
    /// by the list factory, so row-local state would follow the wrong tab as
    /// soon as the list scrolled. Stored as an index+1 so that null is "none".
    const colour_key = "ghostty-sidebar-colour";

    fn colourIndex(page: *adw.TabPage) usize {
        const data = page.as(gobject.Object).getData(colour_key) orelse return 0;
        return @intFromPtr(data);
    }

    /// `idx == 0` deliberately passes a null pointer, and GLib documents that
    /// `g_object_set_data(obj, key, NULL)` REMOVES the key rather than storing
    /// zero. That is exactly the "no colour" semantics wanted here — absent and
    /// zero both read back as 0 — but it is load-bearing rather than
    /// incidental: anyone who later needs to distinguish "explicitly zero" from
    /// "never set" cannot do it through this door.
    fn setColour(self: *Self, idx: usize) void {
        const priv = self.private();
        const page = priv.page orelse return;
        page.as(gobject.Object).setData(colour_key, @ptrFromInt(idx));
        self.syncColour();
    }

    fn syncColour(self: *Self) void {
        const priv = self.private();
        const widget = priv.colour_dot.as(gtk.Widget);
        for (colours) |c| widget.removeCssClass(c);

        const page = priv.page orelse {
            widget.setVisible(0);
            return;
        };
        const idx = colourIndex(page);
        if (idx == 0 or idx > colours.len) {
            widget.setVisible(0);
            return;
        }
        widget.addCssClass(colours[idx - 1]);
        widget.setVisible(1);
    }

    fn notifyPage(_: *Self, _: *gobject.ParamSpec, self: *Self) callconv(.c) void {
        self.syncColour();
        self.reconnectSplitTree();
    }

    /// Follow the bound tab's split tree, so panes rebuild live off its
    /// `changed` signal instead of being polled.
    fn reconnectSplitTree(self: *Self) void {
        const priv = self.private();

        if (priv.split_tree) |st| {
            if (priv.split_tree_changed_handler != 0) {
                gobject.signalHandlerDisconnect(
                    st.as(gobject.Object),
                    priv.split_tree_changed_handler,
                );
                priv.split_tree_changed_handler = 0;
            }
            priv.split_tree = null;
        }

        const st: ?*SplitTree = st: {
            const page = priv.page orelse break :st null;
            const tab = gobject.ext.cast(Tab, page.getChild()) orelse break :st null;
            break :st tab.getSplitTree();
        };

        const tree: ?*Surface.Tree = tree: {
            const s = st orelse break :tree null;
            priv.split_tree = s;
            priv.split_tree_changed_handler = SplitTree.signals.changed.connect(
                s,
                *Self,
                splitTreeChanged,
                self,
                .{},
            );
            break :tree s.getTree();
        };

        self.syncPanes(tree);
    }

    fn splitTreeChanged(
        _: *SplitTree,
        _: ?*const Surface.Tree,
        new_tree: ?*const Surface.Tree,
        self: *Self,
    ) callconv(.c) void {
        self.syncPanes(new_tree);
    }

    /// Rebuild the second-level pane rows from the tree. A non-split tab
    /// (root is a leaf, or the tree is empty/null) leaves `panes_box`
    /// empty -- an empty GtkBox contributes no height, which is what keeps
    /// an unsplit tab's row pixel-identical to before this existed.
    fn syncPanes(self: *Self, tree: ?*const Surface.Tree) void {
        const priv = self.private();
        const box = priv.panes_box;

        var child_ = box.as(gtk.Widget).getFirstChild();
        while (child_) |child| {
            const next = child.getNextSibling();
            box.remove(child);
            child_ = next;
        }

        const t = tree orelse return;
        if (t.isEmpty()) return;
        if (t.nodes[0] == .leaf) return;

        var it = t.iterator();
        while (it.next()) |entry| {
            const row = gobject.ext.newInstance(SidebarPaneRow, .{ .surface = entry.view });
            _ = SidebarPaneRow.signals.activated.connect(
                row,
                *Self,
                paneActivated,
                self,
                .{},
            );
            box.append(row.as(gtk.Widget));
        }
    }

    fn paneActivated(row: *SidebarPaneRow, self: *Self) callconv(.c) void {
        const surface = row.getSurface() orelse return;

        // Same shape as Sidebar.rowActivated: select the tab this pane
        // belongs to before grabbing focus, so the surface is actually
        // visible when it becomes SplitTree.active-surface.
        const page = self.livePage() orelse return;
        const window = ext.getAncestor(Window, self.as(gtk.Widget)) orelse return;
        window.getTabView().setSelectedPage(page);
        surface.grabFocus();
    }

    fn actionColourNone(_: *gio.SimpleAction, _: ?*glib.Variant, self: *Self) callconv(.c) void {
        self.setColour(0);
    }
    fn actionColourAccent(_: *gio.SimpleAction, _: ?*glib.Variant, self: *Self) callconv(.c) void {
        self.setColour(1);
    }
    fn actionColourSuccess(_: *gio.SimpleAction, _: ?*glib.Variant, self: *Self) callconv(.c) void {
        self.setColour(2);
    }
    fn actionColourWarning(_: *gio.SimpleAction, _: ?*glib.Variant, self: *Self) callconv(.c) void {
        self.setColour(3);
    }
    fn actionColourError(_: *gio.SimpleAction, _: ?*glib.Variant, self: *Self) callconv(.c) void {
        self.setColour(4);
    }

    fn initActionMap(self: *Self) void {
        const actions = [_]ext.actions.Action(Self){
            .init("rename", actionRename, null),
            .init("close", actionClose, null),
            .init("colour-none", actionColourNone, null),
            .init("colour-accent", actionColourAccent, null),
            .init("colour-success", actionColourSuccess, null),
            .init("colour-warning", actionColourWarning, null),
            .init("colour-error", actionColourError, null),
        };
        self.private().action_group =
            ext.actions.addAsGroup(Self, self, "row", &actions);
    }

    /// Open the menu where the pointer is.
    fn rightClick(
        _: *gtk.GestureClick,
        _: c_int,
        x: f64,
        y: f64,
        self: *Self,
    ) callconv(.c) void {
        const priv = self.private();

        // @intFromFloat is illegal behaviour on NaN, infinity, or anything
        // outside c_int. Gesture coordinates are finite in practice, but
        // "in practice" is not a guarantee the language makes.
        const safe = struct {
            fn coord(v: f64) c_int {
                if (!std.math.isFinite(v)) return 0;
                return @intFromFloat(std.math.clamp(v, 0, 100_000));
            }
        };
        const rect: gdk.Rectangle = .{
            .f_x = safe.coord(x),
            .f_y = safe.coord(y),
            .f_width = 1,
            .f_height = 1,
        };
        priv.menu.as(gtk.Popover).setPointingTo(&rect);
        priv.menu.as(gtk.Popover).popup();
    }

    /// Rename this row's tab, without switching to it. `Tab.promptTabTitle`
    /// is public, so the row does not have to select the page first and then
    /// route through a window action.
    fn actionRename(
        _: *gio.SimpleAction,
        _: ?*glib.Variant,
        self: *Self,
    ) callconv(.c) void {
        const page = self.livePage() orelse return;
        const tab = gobject.ext.cast(Tab, page.getChild()) orelse return;
        tab.promptTabTitle();
    }

    /// This row's page, but only if the tab view still holds it.
    ///
    /// Rows are recycled and a menu can stay open across a tab closing, so
    /// `priv.page` alone is not proof the page is alive. Acting on a departed
    /// page means either closing the wrong tab or dereferencing a child widget
    /// that has already been destroyed.
    fn livePage(self: *Self) ?*adw.TabPage {
        const priv = self.private();
        const page = priv.page orelse return null;
        const window = ext.getAncestor(Window, self.as(gtk.Widget)) orelse return null;
        if (window.getTabView().getPagePosition(page) < 0) return null;
        return page;
    }

    fn actionClose(
        _: *gio.SimpleAction,
        _: ?*glib.Variant,
        self: *Self,
    ) callconv(.c) void {
        self.closeTab();
    }

    /// Close this row's tab.
    ///
    /// The tab view is reached by walking up to the window rather than being
    /// injected: a factory template can pass the row its item, but nothing
    /// else from the outer scope.
    fn closeClicked(_: *gtk.Button, self: *Self) callconv(.c) void {
        self.closeTab();
    }

    fn closeTab(self: *Self) void {
        const page = self.livePage() orelse return;
        const window = ext.getAncestor(
            Window,
            self.as(gtk.Widget),
        ) orelse return;
        window.getTabView().closePage(page);
    }

    fn dispose(self: *Self) callconv(.c) void {
        const priv = self.private();
        if (priv.split_tree) |st| {
            if (priv.split_tree_changed_handler != 0) {
                gobject.signalHandlerDisconnect(
                    st.as(gobject.Object),
                    priv.split_tree_changed_handler,
                );
                priv.split_tree_changed_handler = 0;
            }
            priv.split_tree = null;
        }
        priv.page = null;
        gtk.Widget.disposeTemplate(self.as(gtk.Widget), getGObjectType());
        gobject.Object.virtual_methods.dispose.call(
            Class.parent,
            self.as(Parent),
        );
    }

    const C = Common(Self, Private);
    pub const as = C.as;
    pub const ref = C.ref;
    pub const unref = C.unref;
    const private = C.private;

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
                    .name = "sidebar-row",
                }),
            );

            gobject.ext.ensureType(SidebarPaneRow);

            class.bindTemplateChildPrivate("menu", .{});
            class.bindTemplateChildPrivate("colour_dot", .{});
            class.bindTemplateChildPrivate("panes_box", .{});
            class.bindTemplateCallback("close_clicked", &closeClicked);
            class.bindTemplateCallback("right_click", &rightClick);
            class.bindTemplateCallback("computed_cwd", &closureCwd);

            gobject.ext.registerProperties(class, &.{
                properties.page.impl,
            });

            gobject.Object.virtual_methods.dispose.implement(class, &dispose);
        }

        pub const as = C.Class.as;
        pub const bindTemplateChildPrivate = C.Class.bindTemplateChildPrivate;
        pub const bindTemplateCallback = C.Class.bindTemplateCallback;
    };
};
