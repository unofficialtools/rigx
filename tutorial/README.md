# rigx tutorials — Remotion

Three video tutorials, all sharing one design: a sidebar with the step
timeline and a live project file tree, an explainer page per step, one
editor slide per file with numbered callouts, and terminal slides.

| Composition | Source | Finished project |
|---|---|---|
| `rigx-cpp-tutorial` | [`src/hellocpp/`](src/hellocpp/) | [`hello-cpp/`](hello-cpp/) |
| `rigx-matmul-cluster` | [`src/cluster/`](src/cluster/) | [`matmul-cluster/`](matmul-cluster/) |
| `rigx-fib-cache` | [`src/fibcache/`](src/fibcache/) | [`fib-cache/`](fib-cache/) |

Each folder has a `content.ts` (the single source of truth: steps,
files, callouts, commands and their real output), a `timeline.ts` that
derives every scene's length from it, and a `Video.tsx` renderer. The
finished projects are generated from `finalFiles()` in each
`timeline.ts`, and every command and output was run for real against
rigx 0.13.0 / nixos-24.11.

```bash
npm install
npm run studio            # interactive preview
npm run render            # out/rigx-cpp-tutorial.mp4
npm run render:cluster    # out/rigx-matmul-cluster.mp4
npm run render:fibcache   # out/rigx-fib-cache.mp4
```

## Website

The same compositions play live in the browser (no video files) on the
project site, built by [`web/build.mjs`](web/build.mjs): it bundles
[`web/player.tsx`](web/player.tsx) (`@remotion/player` plus a chapter
list per step), writes one page per tutorial, and copies in the
hand-written [`../www/`](../www/) and the logo.

```bash
node web/build.mjs /tmp/site    # then serve /tmp/site with any static server
```

`.github/workflows/pages.yml` runs this on every push to `main` and
publishes the result to GitHub Pages.

# rigx-cpp-tutorial — one C++ program, many targets

1. Start a project
2. Write a library and a program (`include/greet.h`, `src/greet.cpp`, `src/main.cpp`)
3. Build it with fmt (`static_library` + `executable`, `deps.nixpkgs = ["fmt"]`)
4. Ship it as a zip (`kind = "run"`)
5. Lock it in with a sandboxed `kind = "test"`
6. Cross-compile for ARM (`target = "aarch64-linux"`, checked with `file`)
7. Package it as a `lite` capsule
8. Check the capsule with a host-side test (`sandbox = false`), then `rigx test`

It closes with pointers to the other two tutorials.

---

# rigx-matmul-cluster — second composition

A longer walkthrough (~7m20s) that grows one project from a hello world
into a small matrix-multiplication cluster:

1. Start a project
2. Hello, world (default gcc)
3. Pin a specific clang (`compiler = "clang18"`)
4. Multiply matrices with Eigen (`deps.nixpkgs = ["eigen"]`)
5. Lock it in with a sandboxed `kind = "test"`
6. Package it as a `lite` capsule (socat → TCP :7000)
7. Write the Python orchestrator (`rigx.testbed.Network` + `rigx.capsule.start`)
8. Run the `kind = "testbed"` that drives three copies of the capsule

It opens with a title card, a page introducing rigx (hermetic, built on
Nix with no Nix knowledge required, dependencies from nixpkgs.org), and
a "what we're going to build" page with an animated diagram. The video
stresses that you need only a shell, a text editor, and rigx; the one
exception, docker or podman for running the containers, is stated where
it applies. Each step then has:

- an explainer page saying *what* the step achieves (not how);
- one editor slide per file touched: a black editor window types the
  file in, with numbered callouts explaining the lines as they appear
  (typing pauses so each callout can be read); appends show the existing
  file dimmed with the new lines marked in the gutter;
- a terminal slide that only runs commands. Files are never written
  from the terminal.

It closes with a "going further" slide on testbed UDP links and fault
injection (loss, latency/jitter, corruption, partitions), then a recap.

A sidebar on every page shows the step timeline and a live project
file tree with new/editing badges.

```bash
npm run studio           # pick "rigx-matmul-cluster"
npm run render:cluster   # writes out/rigx-matmul-cluster.mp4
```

Content lives in [`src/cluster/content.ts`](src/cluster/content.ts):
each step's explainer text plus its slides, and each editor slide's
callouts (`notes`, anchored by a substring of the line they explain).
Scene lengths are derived from it in
[`src/cluster/timeline.ts`](src/cluster/timeline.ts) (typing speed,
callout reading time), so editing text never leaves stale durations
behind. [`matmul-cluster/`](matmul-cluster/) is the finished project,
generated from the same content.
Every file, command, and output shown was run for real against rigx
0.13.0 / nixos-24.11 (clang 18.1.8, Eigen 3.4.0).

# rigx-fib-cache — third composition

Same design as `rigx-matmul-cluster`: the same sidebar, explainer pages,
one-file editor slides with callouts, and terminal slides. It solves a
different problem:

1. Start a project
2. Write the key-value store (C++, `compiler = "clang18"`)
3. Lock it in with a sandboxed `kind = "test"` (socat against the store)
4. Write the Fibonacci service (JavaScript, `kind = "custom"`, Node.js from nixpkgs)
5. Package each program as a `lite` capsule
6. Write the Python orchestrator (`net.link` from the service to the store)
7. Run the `kind = "testbed"` that checks fib(0)..fib(99)

The "going further" slide applies UDP links and fault injection to the
service → store link.

```bash
npm run studio            # pick "rigx-fib-cache"
npm run render:fibcache   # writes out/rigx-fib-cache.mp4
```

The finished project is in `fib-cache/`.
