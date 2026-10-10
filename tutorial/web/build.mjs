// Builds the GitHub Pages site: the hand-written pages in ../../www,
// the rigx logo, and one live-player page per tutorial.
//
//   node web/build.mjs [outdir]     (default: ../_site, at the repo root)

import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../..");
const out = resolve(process.argv[2] ?? join(repo, "_site"));

const TUTORIALS = [
  {
    slug: "hello-cpp",
    title: "One C++ program, many targets",
    blurb: "A library and program using fmt, shipped as a zip, tested, cross-compiled for ARM, and packaged as a capsule.",
  },
  {
    slug: "matmul-cluster",
    title: "A matrix-multiplication cluster",
    blurb: "Pin clang 18, use Eigen, and drive three copies of one capsule from a Python testbed.",
  },
  {
    slug: "fib-cache",
    title: "A Fibonacci service with a C++ cache",
    blurb: "A JavaScript service caches results in a C++ key-value store; a testbed links them and checks 100 numbers.",
  },
];

rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, "tutorials"), { recursive: true });
cpSync(join(repo, "www"), out, { recursive: true });
cpSync(join(repo, "rigx.png"), join(out, "rigx.png"));

await build({
  entryPoints: [join(here, "player.tsx")],
  bundle: true,
  minify: true,
  format: "iife",
  target: "es2020",
  define: { "process.env.NODE_ENV": '"production"' },
  outfile: join(out, "tutorials", "player.js"),
  logLevel: "warning",
});

const page = (t) => `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${t.title} · rigx</title>
<meta name="description" content="${t.blurb}">
<link rel="icon" href="../../rigx.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root { --bg: #0c0e14; --text: #e3e6f3; --dim: #a9b1d6; --line: #262936; --panel: #161922; --accent: #ff5f05; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.5 Inter, system-ui, sans-serif; }
  main { max-width: 1280px; margin: 0 auto; padding: 24px 16px 64px; }
  header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
  header a { color: var(--dim); text-decoration: none; }
  header a:hover { color: var(--accent); }
  h1 { font-size: clamp(22px, 3vw, 32px); margin: 0; letter-spacing: -0.5px; }
  .chapters { list-style: none; padding: 0; margin: 20px 0 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px; }
  .chapters button { width: 100%; text-align: left; font: inherit; color: var(--dim); background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; cursor: pointer; }
  .chapters button:hover { color: var(--text); border-color: var(--dim); }
  .chapters button.on { color: var(--text); border-color: var(--accent); }
  .t { font-family: "JetBrains Mono", monospace; font-size: 13px; color: var(--accent); margin-right: 6px; }
  footer { margin-top: 28px; color: var(--dim); font-size: 14px; }
  footer a { color: var(--dim); }
</style>
</head>
<body>
<main>
  <header>
    <a href="../../">← rigx</a>
    <h1>${t.title}</h1>
  </header>
  <div id="player"></div>
  <footer>
    The finished project is in
    <a href="https://github.com/unofficialtools/rigx/tree/main/tutorial/${t.slug}">tutorial/${t.slug}</a>.
    Click the video or press space to play.
  </footer>
</main>
<script src="../player.js" data-video="${t.slug}"></script>
</body>
</html>
`;

for (const t of TUTORIALS) {
  mkdirSync(join(out, "tutorials", t.slug), { recursive: true });
  writeFileSync(join(out, "tutorials", t.slug, "index.html"), page(t));
}
console.log(`site written to ${out}`);
