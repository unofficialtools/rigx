# rigx-cpp-tutorial — Remotion script

A short video walkthrough of building a C++ project from scratch with rigx —
ten scenes, ~85 seconds at 30fps / 1920×1080.

The content (commands, code snippets, narration) lives in
[`src/content.ts`](src/content.ts) as plain data; the renderer in
[`src/Video.tsx`](src/Video.tsx) just plays it back with typewriter-style
terminals and progressively-revealed code panels.

## Scenes

1. Create the project folder
2. Write the C++ code (header + library + executable, the `fmt`-based
   `hello world` from `example-project/src/`)
3. Install rigx via PyPI
4. Add `rigx.toml` with `static_library` + `executable` targets
5. Build and run
6. Add a `kind = "run"` zip target
7. Add a sandboxed `kind = "test"` target
8. Cross-compile for ARM via `target = "aarch64-linux"`
9. Package as a `kind = "capsule"` lite container
10. Drive the capsule from a `rigx.testbed.Network` integration test

## Running

```bash
cd remotion-tutorial
npm install              # or pnpm / yarn
npm run studio           # interactive preview at http://localhost:3000
npm run render           # writes out/rigx-cpp-tutorial.mp4
npm run render:webm      # webm/vp9 instead of h264
```

## Editing the content

To change a command, code snippet, or scene length, edit
[`src/content.ts`](src/content.ts) — it's the single source of truth.
The renderer reads `seconds` per scene and converts to frames, so making a
scene longer/shorter is just a number change.

To restyle (colors, fonts, panel chrome), touch
[`src/components.tsx`](src/components.tsx) — `COLORS`, `FONT_BODY`,
`FONT_MONO`, and the `TerminalPanel` / `FilePanel` components.

## Notes

- The C++ source shown in scene 2 matches `example-project/src/{main,greet}.cpp`
  and `example-project/include/greet.h` byte-for-byte.
- Scene 4's `rigx.toml` matches the `[targets.greet]` static library and
  `[targets.hello]` executable from `example-project/rigx.toml`.
- Later scenes show *additions* to the same `rigx.toml`, not replacements.
- The testbed example in scene 10 is a minimal one-capsule case; for a richer
  example with fault injection across multiple capsules see the
  README's "Multi-capsule tests with faults" section.

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
0.12.0 / nixos-24.11 (clang 18.1.8, Eigen 3.4.0).
