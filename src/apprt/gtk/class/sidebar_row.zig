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

        pub var offset: c_int = 0;
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
        self.initActionMap();
    }

    fn initActionMap(self: *Self) void {
        const actions = [_]ext.actions.Action(Self){
            .init("rename", actionRename, null),
            .init("close", actionClose, null),
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
            class.bindTemplateCallback("close_clicked", &closeClicked);
            class.bindTemplateCallback("right_click", &rightClick);

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
