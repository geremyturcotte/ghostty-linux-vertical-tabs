const std = @import("std");

const adw = @import("adw");
const gobject = @import("gobject");
const gtk = @import("gtk");

const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
const Surface = @import("surface.zig").Surface;

const log = std.log.scoped(.gtk_ghostty_sidebar_pane_row);

/// One pane, nested as a second-level row under its tab's row in the
/// sidebar. Read-only: no rename, no reorder, no close -- clicking it only
/// activates the pane it stands for.
///
/// This owns none of the tab-selection logic. It can't reach the window
/// (it isn't parented under one until the sidebar row appends it), so it
/// only reports that it was clicked; `SidebarRow` -- which already knows
/// how to walk up to the window for its own row-activation and close-tab
/// paths -- decides what activation means.
pub const SidebarPaneRow = extern struct {
    const Self = @This();
    parent_instance: Parent,
    pub const Parent = adw.Bin;
    pub const getGObjectType = gobject.ext.defineClass(Self, .{
        .name = "GhosttySidebarPaneRow",
        .instanceInit = &init,
        .classInit = &Class.init,
        .parent_class = &Class.parent,
        .private = .{ .Type = Private, .offset = &Private.offset },
    });

    pub const properties = struct {
        pub const surface = struct {
            pub const name = "surface";
            const impl = gobject.ext.defineProperty(
                name,
                Self,
                ?*Surface,
                .{
                    .accessor = C.privateObjFieldAccessor("surface"),
                },
            );
        };
    };

    pub const signals = struct {
        /// Emitted when the row is clicked.
        pub const activated = struct {
            pub const name = "activated";
            pub const connect = impl.connect;
            const impl = gobject.ext.defineSignal(name, Self, &.{}, void);
        };
    };

    const Private = struct {
        /// The pane this row stands for. Borrowed, rebuilt every time the
        /// owning tab's split tree changes.
        surface: ?*Surface = null,

        pub var offset: c_int = 0;
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
    }

    /// The surface this row stands for.
    pub fn getSurface(self: *Self) ?*Surface {
        return self.private().surface;
    }

    fn clicked(_: *gtk.Button, self: *Self) callconv(.c) void {
        signals.activated.impl.emit(self, null, .{}, null);
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
                    .name = "sidebar-pane-row",
                }),
            );

            gobject.ext.registerProperties(class, &.{
                properties.surface.impl,
            });

            class.bindTemplateCallback("clicked", &clicked);

            signals.activated.impl.register(.{});
        }

        pub const as = C.Class.as;
        pub const bindTemplateChildPrivate = C.Class.bindTemplateChildPrivate;
        pub const bindTemplateCallback = C.Class.bindTemplateCallback;
    };
};
