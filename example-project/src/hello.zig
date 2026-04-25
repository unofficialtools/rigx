const std = @import("std");

pub fn main() !void {
    const stdout = std.io.getStdOut().writer();
    var args = std.process.args();
    _ = args.skip();
    const who = args.next() orelse "world";
    try stdout.print("Hello, {s}! (from Zig)\n", .{who});
}
