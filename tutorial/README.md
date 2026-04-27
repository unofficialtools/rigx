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
