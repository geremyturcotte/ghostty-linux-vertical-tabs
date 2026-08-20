const std = @import("std");

const adw = @import("adw");
const gobject = @import("gobject");
const gtk = @import("gtk");

const ext = @import("../ext.zig");
const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
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

        pub var offset: c_int = 0;
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
    }

    /// Close this row's tab.
    ///
    /// The tab view is reached by walking up to the window rather than being
    /// injected: a factory template can pass the row its item, but nothing
    /// else from the outer scope.
    fn closeClicked(_: *gtk.Button, self: *Self) callconv(.c) void {
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

            class.bindTemplateCallback("close_clicked", &closeClicked);

            gobject.ext.registerProperties(class, &.{
                properties.page.impl,
            });

            gobject.Object.virtual_methods.dispose.implement(class, &dispose);
        }

        pub const as = C.Class.as;
        pub const bindTemplateCallback = C.Class.bindTemplateCallback;
    };
};
