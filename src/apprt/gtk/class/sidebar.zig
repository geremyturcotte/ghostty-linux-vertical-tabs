const std = @import("std");

const adw = @import("adw");
const gio = @import("gio");
const glib = @import("glib");
const gobject = @import("gobject");
const gtk = @import("gtk");

const gresource = @import("../build/gresource.zig");
const Common = @import("../class.zig").Common;
const Application = @import("application.zig").Application;
const Tab = @import("tab.zig").Tab;
const Surface = @import("surface.zig").Surface;
const SidebarRow = @import("sidebar_row.zig").SidebarRow;
const group = @import("sidebar_group.zig");

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

        /// Selection wrapper over the tab view's pages, grouped by
        /// repository.
        ///
        /// The wrapping is the whole point. `AdwTabPages` proxies
        /// `AdwTabView:selected-page`, so in that model *selecting is
        /// switching*. Handing it to the list view directly — as an earlier
        /// design did — would switch the user's terminal on mouse hover,
        /// because `single-click-activate` selects rows on hover. Wrapping it
        /// in a `GtkSingleSelection` gives the list a selection that only
        /// means "highlighted", and tab switching happens on activation only.
        ///
        /// What `model` actually wraps is not `AdwTabPages` directly, but a
        /// `GtkSortListModel` built in `setTabView` that reorders it into
        /// contiguous per-repository runs (see `sortCompare`/`sectionCompare`)
        /// and exposes those runs as sections via `GtkSectionModel`, which
        /// `GtkSingleSelection` forwards. This means row *positions* here are
        /// positions in that grouped order, not `AdwTabPages` order — see
        /// `rowActivated` and `syncSelection`, neither of which may assume
        /// the two coincide.
        model: *gtk.SingleSelection,

        /// The tab view we mirror. Borrowed, never owned.
        tab_view: ?*adw.TabView = null,

        /// Handler id for our notify::selected-page connection on the tab
        /// view. It MUST be disconnected in dispose: zig-gobject's connect
        /// maps to g_signal_connect_data, not g_signal_connect_object, so
        /// nothing ties the handler's life to ours. The tab view is borrowed
        /// and can outlive us, and it would then call back into freed memory.
        selected_page_handler: c_ulong = 0,

        /// Handler id for our items-changed connection on the tab view's
        /// pages model. Same disconnect obligation as
        /// `selected_page_handler`, same reason.
        pages_changed_handler: c_ulong = 0,

        /// The main and section sorters currently installed on the model,
        /// kept here (an extra ref each, beyond the one
        /// `GtkSortListModel.new`/`setSectionSorter` consumed) purely so
        /// `rebuildSortModel` can find and release the previous pair when it
        /// builds a new one. See that function for why a live `pwd` change
        /// rebuilds rather than invalidates in place.
        sorter: ?*gtk.CustomSorter = null,
        section_sorter: ?*gtk.CustomSorter = null,

        /// One (object, handler id) pair per `notify::pwd` /
        /// `notify::active-surface` connection made in `rebuildPwdWatches`.
        /// Rebuilt from scratch, in full, every time the tab set changes —
        /// see that function for why a partial diff isn't worth it.
        pwd_watches: std.ArrayListUnmanaged(Watch) = .{},

        pub var offset: c_int = 0;
    };

    const Watch = struct {
        object: *gobject.Object,
        handler: c_ulong,
    };

    fn init(self: *Self, _: *Class) callconv(.c) void {
        gtk.Widget.initTemplate(self.as(gtk.Widget));
    }

    /// Build (or rebuild) the sorter pair and the `GtkSortListModel` that
    /// applies them, and hand the result to `priv.model`.
    ///
    /// This is a full rebuild, not an in-place invalidation. An earlier
    /// version tried `GtkSorter.changed(.different)` on already-installed
    /// sorters to react to a live `pwd` change — correctly connected and
    /// observed to fire (traced with a debug log), but it did not visibly
    /// move an already-modeled row into a new section. Whether that's a
    /// genuine `GtkSortListModel` limitation for a same-position section
    /// change or a misuse on this end wasn't fully run to ground; replacing
    /// the model outright sidesteps the question by construction — it is
    /// the same path already proven correct for initial setup and for a tab
    /// being added — at the cost of one more allocation than an in-place
    /// invalidation would need. Cheap next to the git-root walk each
    /// comparison already does.
    fn rebuildSortModel(self: *Self) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;

        // The main sorter defines the full order: group first (so same-repo
        // tabs land together), then original tab-view position as a
        // tiebreak (so tabs within a group stay in the order the user
        // actually opened them, rather than at the mercy of whatever
        // ordering the underlying sort happens to produce for equal keys).
        const sorter = gtk.CustomSorter.new(sortCompare, tab_view, null);

        // The section sorter defines section BOUNDARIES: two adjacent items
        // are in the same section iff this considers them equal. It must
        // agree with the main sorter's grouping (same key, just without the
        // tiebreak) or GTK's section detection and the main order disagree
        // with each other.
        const section_sorter = gtk.CustomSorter.new(sectionCompare, null, null);

        // An extra ref each: CustomSorter.new() and SortListModel.new()'s
        // sorter parameter (transfer-full) between them account for the
        // first ref on `sorter`, consumed on the way in; setSectionSorter is
        // transfer-none and takes its own ref on `section_sorter` below.
        // These second refs are ours, held so a later rebuild can find and
        // release the previous pair — see the unrefs just below.
        sorter.ref();
        section_sorter.ref();

        if (priv.sorter) |old| old.unref();
        priv.sorter = sorter;
        if (priv.section_sorter) |old| old.unref();
        priv.section_sorter = section_sorter;

        // getPages() and CustomSorter.new() both hand us an owned ref, and
        // SortListModel.new()'s (model, sorter) parameters are both
        // transfer-full — it consumes both, so neither is unreffed here.
        const pages = tab_view.getPages().as(gio.ListModel);
        const sort_model = gtk.SortListModel.new(pages, sorter.as(gtk.Sorter));
        // set_section_sorter is a plain property setter (transfer-none): it
        // takes its own ref, so our local one must still be released.
        sort_model.setSectionSorter(section_sorter.as(gtk.Sorter));
        section_sorter.unref();

        // setModel is likewise transfer-none.
        priv.model.setModel(sort_model.as(gio.ListModel));
        sort_model.unref();

        self.syncSelection();
    }

    /// Point the sidebar at a tab view. Called once, by the window.
    pub fn setTabView(self: *Self, tab_view: *adw.TabView) void {
        const priv = self.private();
        priv.tab_view = tab_view;

        self.rebuildSortModel();

        // The grouping key depends on each tab's *live* pwd, so the tab set
        // changing (open/close/drag) means the watch list needs rebuilding,
        // and any watched pwd/active-surface changing means the sort itself
        // is stale. This connection is made once, here, against the tab
        // view's one persistent pages object — rebuildSortModel building a
        // fresh GtkSortListModel each time does not touch it.
        const pages = tab_view.getPages().as(gio.ListModel);
        priv.pages_changed_handler = gio.ListModel.signals.items_changed.connect(
            pages,
            *Self,
            pagesChanged,
            self,
            .{},
        );
        self.rebuildPwdWatches();

        const header_factory = gtk.SignalListItemFactory.new();
        _ = gtk.SignalListItemFactory.signals.setup.connect(
            header_factory,
            *Self,
            headerSetup,
            self,
            .{},
        );
        _ = gtk.SignalListItemFactory.signals.bind.connect(
            header_factory,
            *Self,
            headerBind,
            self,
            .{},
        );
        // set_header_factory is transfer-none too.
        priv.view.setHeaderFactory(header_factory.as(gtk.ListItemFactory));
        header_factory.unref();

        priv.selected_page_handler = gobject.Object.signals.notify.connect(
            tab_view,
            *Self,
            notifySelectedPage,
            self,
            .{ .detail = "selected-page" },
        );

        self.syncSelection();
    }

    /// The row/section grouping key for one tab, per `sidebar_group.zig`.
    ///
    /// Keyed on the active surface's *live* `pwd` — the directory the shell
    /// is actually reporting right now, the same property
    /// `sidebar-row.blp`'s subtitle already reads — not anything fixed at
    /// tab creation. It moves when the user `cd`s, which is why this sidebar
    /// tracks it via `rebuildPwdWatches` rather than computing it once.
    ///
    /// A page whose child isn't a `Tab`, or a tab with no active surface yet
    /// (should not happen in practice; defensive only), groups as if it had
    /// no working directory. Never crashes either way.
    fn keyFor(alloc: std.mem.Allocator, page: *adw.TabPage) ![]const u8 {
        const tab = gobject.ext.cast(Tab, page.getChild()) orelse
            return group.groupKey(alloc, null);
        const surface = tab.getActiveSurface() orelse
            return group.groupKey(alloc, null);
        return group.groupKey(alloc, surface.getPwd());
    }

    fn castPage(p: ?*const anyopaque) ?*adw.TabPage {
        const raw = p orelse return null;
        const obj: *gobject.Object = @ptrCast(@constCast(@alignCast(raw)));
        return gobject.ext.cast(adw.TabPage, obj);
    }

    fn orderToC(order: std.math.Order) c_int {
        return switch (order) {
            .lt => -1,
            .eq => 0,
            .gt => 1,
        };
    }

    /// Full ordering: group key, then original tab-view position.
    fn sortCompare(
        a_: ?*const anyopaque,
        b_: ?*const anyopaque,
        data: ?*anyopaque,
    ) callconv(.c) c_int {
        const a = castPage(a_) orelse return 0;
        const b = castPage(b_) orelse return 0;
        const tab_view: *adw.TabView = @ptrCast(@alignCast(data orelse return 0));

        const alloc = Application.default().allocator();
        const key_a = keyFor(alloc, a) catch return 0;
        defer alloc.free(key_a);
        const key_b = keyFor(alloc, b) catch return 0;
        defer alloc.free(key_b);

        const key_order = std.mem.order(u8, key_a, key_b);
        if (key_order != .eq) return orderToC(key_order);

        return orderToC(std.math.order(
            tab_view.getPagePosition(a),
            tab_view.getPagePosition(b),
        ));
    }

    /// Section boundaries: group key only, no tiebreak. Two items compare
    /// equal here iff they belong to the same section.
    fn sectionCompare(
        a_: ?*const anyopaque,
        b_: ?*const anyopaque,
        _: ?*anyopaque,
    ) callconv(.c) c_int {
        const a = castPage(a_) orelse return 0;
        const b = castPage(b_) orelse return 0;

        const alloc = Application.default().allocator();
        const key_a = keyFor(alloc, a) catch return 0;
        defer alloc.free(key_a);
        const key_b = keyFor(alloc, b) catch return 0;
        defer alloc.free(key_b);

        return orderToC(std.mem.order(u8, key_a, key_b));
    }

    /// First time a header widget is created: give it a label child.
    fn headerSetup(
        _: *gtk.SignalListItemFactory,
        object: *gobject.Object,
        _: *Self,
    ) callconv(.c) void {
        const header = gobject.ext.cast(gtk.ListHeader, object) orelse return;
        const label = gtk.Label.new(null);
        label.as(gtk.Widget).addCssClass("heading");
        label.as(gtk.Widget).addCssClass("dim-label");
        label.as(gtk.Label).setXalign(0);
        label.as(gtk.Widget).setMarginStart(12);
        label.as(gtk.Widget).setMarginTop(8);
        label.as(gtk.Widget).setMarginBottom(4);
        header.setChild(label.as(gtk.Widget));
    }

    /// A header is about to represent a section: compute and set its label.
    fn headerBind(
        _: *gtk.SignalListItemFactory,
        object: *gobject.Object,
        _: *Self,
    ) callconv(.c) void {
        const header = gobject.ext.cast(gtk.ListHeader, object) orelse return;
        const widget = header.getChild() orelse return;
        const label = gobject.ext.cast(gtk.Label, widget) orelse return;

        const item = header.getItem() orelse return;
        const page = gobject.ext.cast(adw.TabPage, item) orelse return;

        const alloc = Application.default().allocator();
        const key = keyFor(alloc, page) catch return;
        defer alloc.free(key);
        const text = group.groupLabel(alloc, key) catch return;
        defer alloc.free(text);

        label.setLabel(text);
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

        // A null page is window teardown.
        const page = tab_view.getSelectedPage() orelse return;

        // Position in `priv.model`, NOT `tab_view.getPagePosition(page)`:
        // the model is grouped/reordered (see `setTabView`), so the two only
        // coincide by accident. `AdwTabPages` has no "find" API, so this is
        // a linear scan — bounded by the tab count, which is never large
        // enough for this to matter.
        const pos = findPosition(priv.model.as(gio.ListModel), page) orelse return;
        priv.model.setSelected(pos);
    }

    /// Find `page`'s position in `model` by identity. Returns `null` if it
    /// is not present — a tab dragged out to another window fires
    /// items-changed and notify::selected-page together, and an unguarded
    /// lookup here would be a crash or a silently wrong highlight.
    fn findPosition(model: *gio.ListModel, page: *adw.TabPage) ?c_uint {
        const n = model.getNItems();
        var i: c_uint = 0;
        while (i < n) : (i += 1) {
            const raw = model.getItem(i) orelse continue;
            const item: *gobject.Object = @ptrCast(@alignCast(raw));
            defer item.unref();
            if (item == page.as(gobject.Object)) return i;
        }
        return null;
    }

    /// The tab set changed (opened, closed, dragged out): the watch list
    /// from `rebuildPwdWatches` is stale (it names pages that may no longer
    /// exist, and misses new ones), and the grouping itself needs
    /// recomputing since it may now be wrong regardless.
    ///
    /// Deferred, not immediate: this fires *during* `AdwTabView.insert()`'s
    /// own signal cascade (items-changed on its pages model, emitted before
    /// the insert call returns). Calling `rebuildSortModel` — which replaces
    /// `priv.model`'s model — synchronously from in there is reentering
    /// GTK's list-model machinery mid-mutation, and it crashed: a real
    /// segfault, `libgtk-4.so.1` several frames down from
    /// `GtkSingleSelection.set_model`, called from `rebuildSortModel`,
    /// called from this handler, called from `AdwTabView.insert`. Scheduling
    /// the rebuild on an idle callback lets `insert()` finish and the stack
    /// unwind first.
    fn pagesChanged(
        _: *gio.ListModel,
        _: c_uint,
        _: c_uint,
        _: c_uint,
        self: *Self,
    ) callconv(.c) void {
        self.scheduleRefresh();
    }

    /// A watched tab's active surface changed (e.g. split navigation): the
    /// watch list now points at the wrong surface for that tab, and the
    /// group that tab belongs to may have changed along with it. Deferred
    /// for the same reentrancy reason as `pagesChanged`.
    fn tabActiveSurfaceChanged(
        _: *Tab,
        _: *gobject.ParamSpec,
        self: *Self,
    ) callconv(.c) void {
        self.scheduleRefresh();
    }

    /// A watched surface's pwd changed (the user `cd`d): the group a tab
    /// belongs to may have changed. Deferred for the same reentrancy reason
    /// as `pagesChanged`, even though this particular signal isn't known to
    /// fire from inside a GTK mutation — better a redundant idle callback
    /// than finding out the hard way that some code path does.
    fn surfacePwdChanged(
        _: *Surface,
        _: *gobject.ParamSpec,
        self: *Self,
    ) callconv(.c) void {
        self.scheduleRefresh();
    }

    /// Schedule `rebuildPwdWatches` + `rebuildSortModel` to run once the
    /// current call stack unwinds, via `g_idle_add`. Takes its own ref on
    /// `self` so the sidebar can't be freed out from under the pending
    /// callback (e.g. the window closing between the signal firing and the
    /// idle running) — released when the callback runs.
    fn scheduleRefresh(self: *Self) void {
        _ = self.ref();
        _ = glib.idleAddOnce(deferredRefresh, self);
    }

    fn deferredRefresh(data: ?*anyopaque) callconv(.c) void {
        const self: *Self = @ptrCast(@alignCast(data orelse return));
        defer self.unref();
        self.rebuildPwdWatches();
        self.rebuildSortModel();
    }

    /// Disconnect every watch from the last call, then connect a fresh
    /// `notify::active-surface` on each current tab and `notify::pwd` on
    /// each tab's current active surface.
    ///
    /// Rebuilt from scratch rather than diffed against the previous set: a
    /// tab's active surface can itself change (split navigation), so a
    /// pwd-watch keyed by tab can go stale independently of the tab set
    /// changing. Tracking that incrementally means either watching a third
    /// thing (active-surface changes trigger a pwd-watch swap) or accepting
    /// the same bug this whole mechanism exists to avoid — the sort quietly
    /// going stale. Full rebuild is O(tab count), which is never large
    /// enough for that to matter, and it can't drift.
    fn rebuildPwdWatches(self: *Self) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;
        const alloc = Application.default().allocator();

        for (priv.pwd_watches.items) |w| {
            gobject.signalHandlerDisconnect(w.object, w.handler);
        }
        priv.pwd_watches.clearRetainingCapacity();

        const n = tab_view.getNPages();
        var i: c_int = 0;
        while (i < n) : (i += 1) {
            const page = tab_view.getNthPage(i);
            const tab = gobject.ext.cast(Tab, page.getChild()) orelse continue;

            const tab_handler = gobject.Object.signals.notify.connect(
                tab,
                *Self,
                tabActiveSurfaceChanged,
                self,
                .{ .detail = "active-surface" },
            );
            priv.pwd_watches.append(alloc, .{
                .object = tab.as(gobject.Object),
                .handler = tab_handler,
            }) catch continue;

            const surface = tab.getActiveSurface() orelse continue;
            const surface_handler = gobject.Object.signals.notify.connect(
                surface,
                *Self,
                surfacePwdChanged,
                self,
                .{ .detail = "pwd" },
            );
            priv.pwd_watches.append(alloc, .{
                .object = surface.as(gobject.Object),
                .handler = surface_handler,
            }) catch continue;
        }
    }

    /// row -> tab, plus the keyboard handoff.
    ///
    /// `tabViewSelectedPage` in Window never calls `grabFocus`, so nothing
    /// else will: without this the GtkListItem keeps keyboard focus and the
    /// terminal silently stops receiving keystrokes after every click.
    ///
    /// `pos` is a position in `priv.model` (grouped order), so the page it
    /// names is read directly off that model rather than assumed to be
    /// `tab_view`'s `pos`-th page — those two orders agree only by
    /// accident once rows are grouped. See `setTabView`.
    fn rowActivated(_: *gtk.ListView, pos: c_uint, self: *Self) callconv(.c) void {
        const priv = self.private();
        const tab_view = priv.tab_view orelse return;

        const raw = priv.model.as(gio.ListModel).getItem(pos) orelse return;
        const item: *gobject.Object = @ptrCast(@alignCast(raw));
        defer item.unref();
        const page = gobject.ext.cast(adw.TabPage, item) orelse return;

        tab_view.setSelectedPage(page);

        const tab = gobject.ext.cast(Tab, page.getChild()) orelse return;
        if (tab.getActiveSurface()) |surface| surface.grabFocus();
    }

    fn dispose(self: *Self) callconv(.c) void {
        const priv = self.private();

        // Every notify::pwd / notify::active-surface watch names a Tab or
        // Surface that (like tab_view) can outlive the sidebar. Same
        // use-after-free hazard as selected_page_handler below, same fix.
        for (priv.pwd_watches.items) |w| {
            gobject.signalHandlerDisconnect(w.object, w.handler);
        }
        priv.pwd_watches.deinit(Application.default().allocator());

        // Disconnect before dropping the tab view. Leaving this connected is a
        // use-after-free: the tab view outlives the sidebar during window
        // teardown, and would call notifySelectedPage on freed memory.
        if (priv.tab_view) |tv| {
            if (priv.selected_page_handler != 0) {
                gobject.signalHandlerDisconnect(
                    tv.as(gobject.Object),
                    priv.selected_page_handler,
                );
                priv.selected_page_handler = 0;
            }
            if (priv.pages_changed_handler != 0) {
                // getPages() returns tab_view's one persistent pages object
                // (a fresh, owned ref each call) — the same object the
                // items-changed handler was connected to in setTabView.
                const pages = tv.getPages().as(gobject.Object);
                gobject.signalHandlerDisconnect(
                    pages,
                    priv.pages_changed_handler,
                );
                pages.unref();
                priv.pages_changed_handler = 0;
            }
        }
        priv.tab_view = null;

        if (priv.sorter) |s| {
            s.unref();
            priv.sorter = null;
        }
        if (priv.section_sorter) |s| {
            s.unref();
            priv.section_sorter = null;
        }

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
