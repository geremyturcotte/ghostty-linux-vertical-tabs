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
        const rect: gdk.Rectangle = .{
            .f_x = @intFromFloat(x),
            .f_y = @intFromFloat(y),
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
        const priv = self.private();
        const page = priv.page orelse return;
        const tab = gobject.ext.cast(Tab, page.getChild()) orelse return;
        tab.promptTabTitle();
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
        const priv = self.private();
        const page = priv.page orelse return;
        const window = ext.getAncestor(
            Window,
            self.as(gtk.Widget),
        ) orelse return;
        window.getTabView().closePage(page);
    }

    fn dispose(self: *Self) callconv(.c) void {
        const priv = self.private();
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

            class.bindTemplateChildPrivate("menu", .{});
            class.bindTemplateChildPrivate("colour_dot", .{});
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
