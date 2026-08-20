const std = @import("std");

const adw = @import("adw");
const gio = @import("gio");
const gobject = @import("gobject");
const gtk = @import("gtk");

const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
const Tab = @import("tab.zig").Tab;
const SidebarRow = @import("sidebar_row.zig").SidebarRow;

const log = std.log.scoped(.gtk_ghostty_sidebar);

/// A vertical list of the window's tabs.
///
/// The widget takes an `AdwTabView` and knows nothing else about Ghostty.
/// That boundary is deliberate: it keeps the edits to Window small, and small
/// edits are what make this fork's rebases survivable.
pub const Sidebar = extern struct {
    const Self = @This();
    parent_instance: Parent,
    pub const Parent = adw.Bin;
    pub const getGObjectType = gobject.ext.defineClass(Self, .{
        .name = "GhosttySidebar",
        .instanceInit = &init,
        .classInit = &Class.init,
        .parent_class = &Class.parent,
        .private = .{ .Type = Private, .offset = &Private.offset },
    });

    const Private = struct {
        /// One row per tab.
        view: *gtk.ListView,

        /// Selection wrapper over the tab view's pages.
        ///
        /// The wrapping is the whole point. `AdwTabPages` proxies
        /// `AdwTabView:selected-page`, so in that model *selecting is
        /// switching*. Handing it to the list view directly — as an earlier
        /// design did — would switch the user's terminal on mouse hover,
        /// because `single-click-activate` selects rows on hover. Wrapping it
        /// in a `GtkSingleSelection` gives the list a selection that only
        /// means "highlighted", and tab switching happens on activation only.
        model: *gtk.SingleSelection,

        /// The tab view we mirror. Borrowed, never owned.
        tab_view: ?*adw.TabView = null,

        pub var offset: c_int = 0;
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
    }

    /// Point the sidebar at a tab view. Called once, by the window.
    pub fn setTabView(self: *Self, tab_view: *adw.TabView) void {
        const priv = self.private();
        priv.tab_view = tab_view;
        priv.model.setModel(tab_view.getPages().as(gio.ListModel));

        _ = gobject.Object.signals.notify.connect(
            tab_view,
            *Self,
            notifySelectedPage,
            self,
            .{ .detail = "selected-page" },
        );

        self.syncSelection();
    }

    /// tab -> row. Keeps the highlight on the live tab when the switch came
    /// from anywhere else: a keybind, the tab overview, a closed tab.
    fn notifySelectedPage(
        _: *adw.TabView,
        _: *gobject.ParamSpec,
        self: *Self,
    ) callconv(.c) void {
        self.syncSelection();
    }

    fn syncSelection(self: *Self) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;

        // Two guards, both real. A null page is window teardown. A negative
        // position means the page is no longer in this model — a tab dragged
        // out to another window fires items-changed and notify::selected-page
        // together, and an unguarded lookup here is a crash or a silently
        // wrong highlight.
        const page = tab_view.getSelectedPage() orelse return;
        const pos = tab_view.getPagePosition(page);
        if (pos < 0) return;

        priv.model.setSelected(@intCast(pos));
    }

    /// row -> tab, plus the keyboard handoff.
    ///
    /// `tabViewSelectedPage` in Window never calls `grabFocus`, so nothing
    /// else will: without this the GtkListItem keeps keyboard focus and the
    /// terminal silently stops receiving keystrokes after every click.
    fn rowActivated(_: *gtk.ListView, pos: c_uint, self: *Self) callconv(.c) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;
        if (pos >= @as(c_uint, @intCast(tab_view.getNPages()))) return;

        const page = tab_view.getNthPage(@intCast(pos));
        tab_view.setSelectedPage(page);

        const tab = gobject.ext.cast(Tab, page.getChild()) orelse return;
        if (tab.getActiveSurface()) |surface| surface.grabFocus();
    }

    fn dispose(self: *Self) callconv(.c) void {
        const priv = self.private();
        priv.tab_view = null;
        gtk.Widget.disposeTemplate(
            self.as(gtk.Widget),
            getGObjectType(),
        );
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
            gobject.ext.ensureType(SidebarRow);
            gtk.Widget.Class.setTemplateFromResource(
                class.as(gtk.Widget.Class),
                comptime gresource.blueprint(.{
                    .major = 1,
                    .minor = 5,
                    .name = "sidebar",
                }),
            );

            // Bindings
            class.bindTemplateChildPrivate("view", .{});
            class.bindTemplateChildPrivate("model", .{});

            // Template Callbacks
            class.bindTemplateCallback("row_activated", &rowActivated);

            // Virtual methods
            gobject.Object.virtual_methods.dispose.implement(class, &dispose);
        }

        pub const as = C.Class.as;
        pub const bindTemplateChildPrivate = C.Class.bindTemplateChildPrivate;
        pub const bindTemplateCallback = C.Class.bindTemplateCallback;
    };
};
