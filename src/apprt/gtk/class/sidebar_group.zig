const std = @import("std");

/// The single place that decides how sidebar rows are grouped into
/// sections. If the grouping criterion ever needs to change — full path
/// instead of repo root, some other notion of "project" — this is the only
/// function that should need to change.
///
/// The operator asked to segment tabs "by repository" ("dossier courant"),
/// not "by directory" — the caller passes the active surface's *live*
/// `pwd` (`sidebar-row.blp`'s subtitle already reads the same property), so
/// the key follows a tab across a `cd`, not just at creation. The key is
/// that directory's git repository root, found by walking up looking for
/// `.git`. Two fallbacks, both deliberate:
///
///   - `working_directory == null` (no surface yet, or the shell hasn't
///     reported a pwd) maps to the empty string, the "ungrouped" sentinel
///     key.
///   - a working directory outside any git repository maps to itself, so
///     tabs in unrelated non-repo directories don't collapse into one
///     group.
///
/// Returned slices are allocator-owned; the caller frees them.
pub fn groupKey(
    alloc: std.mem.Allocator,
    working_directory: ?[]const u8,
) std.mem.Allocator.Error![]const u8 {
    const wd = working_directory orelse return alloc.dupe(u8, "");
    if (try gitRoot(alloc, wd)) |root| return root;
    return alloc.dupe(u8, wd);
}

/// The label shown in a section's header, derived from a `groupKey` result.
pub fn groupLabel(
    alloc: std.mem.Allocator,
    key: []const u8,
) std.mem.Allocator.Error![:0]const u8 {
    if (key.len == 0) return alloc.dupeZ(u8, "No repository");
    const base = std.fs.path.basename(key);
    return alloc.dupeZ(u8, if (base.len == 0) key else base);
}

/// Walk `start` and its ancestors looking for a `.git` entry (a directory
/// for an ordinary checkout, a file for a worktree — either is proof enough
/// this is a repository root). Returns `null` if none is found, including
/// when `start` is not an absolute path (nothing to walk).
fn gitRoot(alloc: std.mem.Allocator, start: []const u8) std.mem.Allocator.Error!?[]const u8 {
    if (!std.fs.path.isAbsolute(start)) return null;

    var dir: []const u8 = start;
    while (true) {
        const marker = try std.fs.path.join(alloc, &.{ dir, ".git" });
        defer alloc.free(marker);

        if (std.fs.accessAbsolute(marker, .{})) {
            return try alloc.dupe(u8, dir);
        } else |_| {}

        const parent = std.fs.path.dirname(dir) orelse return null;
        if (std.mem.eql(u8, parent, dir)) return null;
        dir = parent;
    }
}

test "groupKey: null working directory is the ungrouped sentinel" {
    const key = try groupKey(std.testing.allocator, null);
    defer std.testing.allocator.free(key);
    try std.testing.expectEqualStrings("", key);
}

test "groupLabel: ungrouped sentinel gets a readable label" {
    const label = try groupLabel(std.testing.allocator, "");
    defer std.testing.allocator.free(label);
    try std.testing.expectEqualStrings("No repository", label);
}

test "groupKey: a directory with no ancestor .git falls back to itself" {
    // /tmp is not (in any sane test environment) inside a git repo.
    const key = try groupKey(std.testing.allocator, "/tmp");
    defer std.testing.allocator.free(key);
    try std.testing.expectEqualStrings("/tmp", key);
}

test "groupKey: a relative working directory does not walk the real filesystem" {
    // isAbsolute() bails before any access() call, so this can't
    // accidentally match an ancestor of the test runner's cwd.
    const key = try groupKey(std.testing.allocator, "relative/path");
    defer std.testing.allocator.free(key);
    try std.testing.expectEqualStrings("relative/path", key);
}

test "groupKey: finds the repository root of this very checkout" {
    // This test file lives inside this fork's git repo, so walking up from
    // its own source directory must find a real .git.
    const this_dir = std.fs.path.dirname(@src().file) orelse return error.SkipZigTest;
    if (!std.fs.path.isAbsolute(this_dir)) return error.SkipZigTest;

    const key = try groupKey(std.testing.allocator, this_dir);
    defer std.testing.allocator.free(key);
    try std.testing.expect(key.len > 0);

    const marker = try std.fs.path.join(std.testing.allocator, &.{ key, ".git" });
    defer std.testing.allocator.free(marker);
    try std.fs.accessAbsolute(marker, .{});
}

test "groupLabel: a repo root's label is its basename" {
    const label = try groupLabel(std.testing.allocator, "/home/prokai/Github/ghostty-linux-vertical-tabs");
    defer std.testing.allocator.free(label);
    try std.testing.expectEqualStrings("ghostty-linux-vertical-tabs", label);
}
