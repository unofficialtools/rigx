import React from "react";
import { AbsoluteFill, Sequence, interpolate, useCurrentFrame } from "remotion";
import { COLORS } from "../colors";
import { Lang, STEPS } from "./content";
import {
  EditTimeline,
  FileStatus,
  Row,
  SCENES,
  SceneSpec,
  TerminalTimeline,
  filesTouched,
  pathsBefore,
} from "./timeline";

const FONT_BODY = '"Inter", "SF Pro Display", system-ui, -apple-system, sans-serif';
const FONT_MONO = '"JetBrains Mono", "Fira Code", "Menlo", monospace';
const SIDEBAR_W = 440;
const PROJECT = "hello-cpp";

function fadeIn(frame: number, start: number, len = 18): number {
  return interpolate(frame, [start, start + len], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
}

function Background() {
  return (
    <AbsoluteFill
      style={{ background: `radial-gradient(circle at 70% 0%, #1a1f33 0%, ${COLORS.bg} 60%)` }}
    />
  );
}

// ---------------------------------------------------------------- inline code

// Renders `backtick` spans as code chips inside running text.
function RichText({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith("`") && p.endsWith("`") ? (
          <code
            key={i}
            style={{
              fontFamily: FONT_MONO,
              fontSize: "0.86em",
              color: COLORS.func,
              background: "#1d2233",
              border: `1px solid ${COLORS.panelBorder}`,
              borderRadius: 6,
              padding: "1px 8px",
            }}
          >
            {p.slice(1, -1)}
          </code>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

// ---------------------------------------------------------------- sidebar

type SidebarProps = {
  // -1 = before step 1 (title/overview); STEPS.length = all done.
  step: number;
  phase?: "explain" | "code";
  files: Map<string, FileStatus>;
};

function Timeline({ step, phase }: { step: number; phase?: "explain" | "code" }) {
  const frame = useCurrentFrame();
  const pulse = 0.55 + 0.45 * Math.sin(frame / 6);
  return (
    <div style={{ position: "relative" }}>
      <div
        style={{
          position: "absolute",
          left: 15,
          top: 18,
          bottom: 18,
          width: 2,
          background: COLORS.panelBorder,
        }}
      />
      {STEPS.map((s, i) => {
        const done = i < step;
        const current = i === step;
        return (
          <div
            key={i}
            style={{ display: "flex", alignItems: "center", gap: 18, height: 50, position: "relative" }}
          >
            <div
              style={{
                width: 32,
                height: 32,
                borderRadius: "50%",
                flexShrink: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 16,
                fontWeight: 700,
                fontFamily: FONT_BODY,
                background: done ? COLORS.string : current ? COLORS.prompt : COLORS.panel,
                color: done || current ? COLORS.bg : COLORS.comment,
                border: `2px solid ${done ? COLORS.string : current ? COLORS.prompt : COLORS.panelBorder}`,
                boxShadow: current ? `0 0 ${18 * pulse}px ${COLORS.prompt}` : "none",
              }}
            >
              {done ? "✓" : i + 1}
            </div>
            <div style={{ fontFamily: FONT_BODY, lineHeight: 1.15 }}>
              <div
                style={{
                  fontSize: 21,
                  fontWeight: current ? 700 : 500,
                  color: current ? COLORS.text : done ? COLORS.textDim : COLORS.comment,
                }}
              >
                {s.title}
              </div>
              {current && phase ? (
                <div style={{ fontSize: 15, color: COLORS.prompt, letterSpacing: 1.5, marginTop: 2 }}>
                  {phase === "explain" ? "EXPLAIN" : "CODE"}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

const BADGES: Record<FileStatus, { label: string; color: string } | null> = {
  idle: null,
  "writing-new": { label: "writing", color: COLORS.string },
  "writing-edit": { label: "editing", color: COLORS.ident },
  created: { label: "new", color: COLORS.string },
  modified: { label: "edited", color: COLORS.ident },
  "todo-new": { label: "to create", color: COLORS.comment },
  "todo-edit": { label: "to edit", color: COLORS.comment },
};

// Directories first, then files, alphabetically at each level.
function treeOrder(a: string, b: string): number {
  const pa = a.replace(/\/$/, "").split("/");
  const pb = b.replace(/\/$/, "").split("/");
  for (let i = 0; i < Math.min(pa.length, pb.length); i++) {
    if (pa[i] === pb[i]) continue;
    const aDir = i < pa.length - 1 || a.endsWith("/");
    const bDir = i < pb.length - 1 || b.endsWith("/");
    if (aDir !== bDir) return aDir ? -1 : 1;
    return pa[i] < pb[i] ? -1 : 1;
  }
  return pa.length - pb.length;
}

function FileTree({ files }: { files: Map<string, FileStatus> }) {
  const frame = useCurrentFrame();
  // Add implied parent directories so `src/x.cpp` shows under `src/`.
  const all = new Map(files);
  for (const p of files.keys()) {
    const parts = p.replace(/\/$/, "").split("/");
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/") + "/";
      if (!all.has(dir)) all.set(dir, "idle");
    }
  }
  const paths = [...all.keys()].sort(treeOrder);
  const blink = Math.floor(frame / 10) % 2 === 0;
  return (
    <div style={{ fontFamily: FONT_MONO, fontSize: 19, lineHeight: 1.75 }}>
      <div style={{ color: COLORS.textDim }}>📁 {PROJECT}/</div>
      {paths.length === 0 ? (
        <div style={{ color: COLORS.comment, paddingLeft: 28, fontStyle: "italic" }}>(empty)</div>
      ) : null}
      {paths.map((p) => {
        const isDir = p.endsWith("/");
        const depth = p.replace(/\/$/, "").split("/").length;
        const name = p.replace(/\/$/, "").split("/").pop() + (isDir ? "/" : "");
        const status = all.get(p) ?? "idle";
        const badge = BADGES[status];
        const writing = status === "writing-new" || status === "writing-edit";
        const todo = status === "todo-new";
        return (
          <div
            key={p}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              paddingLeft: depth * 26,
              marginLeft: -10,
              marginRight: -10,
              paddingRight: 10,
              borderRadius: 6,
              background: writing ? "rgba(122,162,247,0.14)" : "transparent",
              color: todo ? COLORS.comment : writing ? COLORS.text : isDir ? COLORS.textDim : COLORS.out,
              opacity: todo ? 0.7 : 1,
              fontStyle: todo ? "italic" : "normal",
            }}
          >
            <span style={{ width: 22, color: isDir ? COLORS.ident : COLORS.comment }}>
              {isDir ? "▸" : writing ? (blink ? "✎" : " ") : "·"}
            </span>
            <span style={{ flex: 1 }}>{name}</span>
            {badge ? (
              <span
                style={{
                  fontFamily: FONT_BODY,
                  fontSize: 13,
                  fontWeight: 700,
                  letterSpacing: 1,
                  textTransform: "uppercase",
                  color: badge.color,
                  border: `1px solid ${badge.color}`,
                  borderRadius: 5,
                  padding: "0 7px",
                  lineHeight: "22px",
                }}
              >
                {badge.label}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontFamily: FONT_BODY,
        fontSize: 15,
        fontWeight: 700,
        letterSpacing: 2.5,
        color: COLORS.comment,
        marginBottom: 14,
      }}
    >
      {children}
    </div>
  );
}

function Sidebar({ step, phase, files }: SidebarProps) {
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        top: 0,
        bottom: 0,
        width: SIDEBAR_W,
        background: "rgba(16,18,26,0.92)",
        borderRight: `1px solid ${COLORS.panelBorder}`,
        padding: "44px 40px",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div style={{ fontFamily: FONT_BODY, fontSize: 30, fontWeight: 800, color: COLORS.text, letterSpacing: -0.5 }}>
        rigx <span style={{ color: COLORS.comment, fontWeight: 500 }}>tutorial</span>
      </div>
      <div style={{ fontFamily: FONT_BODY, fontSize: 17, color: COLORS.textDim, marginTop: 4, marginBottom: 34 }}>
        C++ · fmt · ARM · capsules
      </div>
      <SectionLabel>TIMELINE</SectionLabel>
      <Timeline step={step} phase={phase} />
      <div style={{ height: 1, background: COLORS.panelBorder, margin: "28px 0 24px" }} />
      <SectionLabel>PROJECT FILES</SectionLabel>
      <FileTree files={files} />
    </div>
  );
}

// Main content area to the right of the sidebar.
function Main({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div
      style={{
        position: "absolute",
        left: SIDEBAR_W,
        top: 0,
        right: 0,
        bottom: 0,
        padding: "56px 72px 52px 72px",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
        ...style,
      }}
    >
      {children}
    </div>
  );
}

function idleFiles(paths: string[]): Map<string, FileStatus> {
  return new Map(paths.map((p) => [p, "idle" as FileStatus]));
}

// ---------------------------------------------------------------- syntax

type Seg = { text: string; color: string };

const KEYWORDS: Record<Lang, string[]> = {
  cpp: ["int", "return", "if", "for", "while", "auto", "const", "char", "void", "true", "main"],
  js: ["const", "let", "function", "async", "await", "return", "if", "new", "require"],
  py: ["import", "from", "def", "return", "with", "as", "for", "in", "assert", "and", "or", "not", "None", "True", "False"],
  toml: ["true", "false"],
  txt: [],
};

// Small hand-rolled lexer: comments, strings, keywords, numbers, TOML
// keys/sections, and C preprocessor lines. Enough for video-grade
// highlighting that can be cut at any character for typing.
function tokenize(line: string, lang: Lang): Seg[] {
  if (lang === "toml" && /^\s*\[/.test(line)) return [{ text: line, color: COLORS.func }];
  if (lang === "cpp" && /^\s*#/.test(line)) {
    const m = /^(\s*#\w+)(.*)$/.exec(line);
    if (m) {
      const rest = m[2].replace(/\/\/.*$/, "");
      const comment = m[2].slice(rest.length);
      return [
        { text: m[1], color: COLORS.keyword },
        { text: rest, color: COLORS.string },
        { text: comment, color: COLORS.comment },
      ];
    }
  }
  const segs: Seg[] = [];
  let i = 0;
  if (lang === "toml") {
    const m = /^(\s*[\w.]+)(?=\s*=)/.exec(line);
    if (m) {
      segs.push({ text: m[1], color: COLORS.ident });
      i = m[1].length;
    }
  }
  const commentStart = lang === "cpp" || lang === "js" ? "//" : "#";
  const kw = new Set(KEYWORDS[lang]);
  let plain = "";
  const flush = () => {
    if (plain) segs.push({ text: plain, color: COLORS.text });
    plain = "";
  };
  while (i < line.length) {
    const c = line[i];
    if (line.startsWith(commentStart, i)) {
      flush();
      segs.push({ text: line.slice(i), color: COLORS.comment });
      return segs;
    }
    if (c === '"' || c === "'") {
      flush();
      const triple = line.startsWith(c.repeat(3), i) ? c.repeat(3) : c;
      let j = i + triple.length;
      while (j < line.length && !line.startsWith(triple, j)) j += line[j] === "\\" ? 2 : 1;
      j = Math.min(line.length, j + triple.length);
      segs.push({ text: line.slice(i, j), color: COLORS.string });
      i = j;
      continue;
    }
    const word = /^[A-Za-z_]\w*/.exec(line.slice(i));
    if (word) {
      if (kw.has(word[0])) {
        flush();
        segs.push({ text: word[0], color: COLORS.keyword });
      } else {
        plain += word[0];
      }
      i += word[0].length;
      continue;
    }
    const num = /^\d+(\.\d+)?/.exec(line.slice(i));
    if (num && !/\w/.test(line[i - 1] ?? "")) {
      flush();
      segs.push({ text: num[0], color: COLORS.accent });
      i += num[0].length;
      continue;
    }
    plain += c;
    i++;
  }
  flush();
  return segs;
}

// Shell commands: program name highlighted, strings green.
function tokenizeCmd(text: string): Seg[] {
  const segs = tokenize(text, "txt");
  const first = segs[0];
  if (first && first.color === COLORS.text) {
    const m = /^(\S+)(.*)$/s.exec(first.text);
    if (m) return [{ text: m[1], color: COLORS.func }, { text: m[2], color: COLORS.text }, ...segs.slice(1)];
  }
  return segs;
}

function outColor(text: string): string {
  if (/PASS|^OK:/.test(text.trim())) return COLORS.string;
  if (/FAIL|Error/.test(text)) return COLORS.accent;
  if (text.startsWith("[rigx")) return COLORS.textDim;
  return COLORS.out;
}

function renderSegs(segs: Seg[], n: number): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  let left = n;
  for (let i = 0; i < segs.length && left > 0; i++) {
    const s = segs[i].text.slice(0, left);
    left -= s.length;
    out.push(
      <span key={i} style={{ color: segs[i].color }}>
        {s}
      </span>,
    );
  }
  return out;
}

// ---------------------------------------------------------------- terminal

const TERM_FONT = 22;
const TERM_LH = 1.5;
const TERM_ROW = TERM_FONT * TERM_LH;
const TERM_MAX_ROWS = 24;

function Cursor() {
  const frame = useCurrentFrame();
  const on = Math.floor(frame / 15) % 2 === 0;
  return (
    <span
      style={{
        display: "inline-block",
        width: "0.55em",
        height: "1.1em",
        background: on ? COLORS.text : "transparent",
        verticalAlign: "text-bottom",
        marginLeft: 1,
      }}
    />
  );
}

function rowSegs(row: Row): Seg[] {
  switch (row.kind) {
    case "cmd":
      return tokenizeCmd(row.text);
    case "comment":
      return [{ text: row.text, color: COLORS.comment }];
    case "out":
      return [{ text: row.text, color: outColor(row.text) }];
  }
}

function Prompt({ kind }: { kind: Row["kind"] }) {
  if (kind === "cmd" || kind === "comment")
    return <span style={{ color: COLORS.prompt, marginRight: 12 }}>$</span>;
  return null;
}

function TypingTerminal({ tl }: { tl: TerminalTimeline }) {
  const frame = useCurrentFrame();
  const visible = tl.rows.filter((r) => frame >= r.start);
  // Fractional row count so scrolling keeps pace with typing.
  let revealed = 0;
  for (const r of visible) {
    revealed += r.typeFrames === 0 ? 1 : Math.min(1, (frame - r.start) / r.typeFrames);
  }
  const allDone = tl.rows.length > 0 && frame >= tl.rows[tl.rows.length - 1].start + tl.rows[tl.rows.length - 1].typeFrames;
  if (allDone) revealed += 1; // the idle prompt row
  const scroll = Math.max(0, revealed - TERM_MAX_ROWS) * TERM_ROW;

  return (
    <div
      style={{
        flex: 1,
        background: COLORS.panel,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 14,
        boxShadow: "0 30px 80px rgba(0,0,0,0.45)",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        fontFamily: FONT_MONO,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "14px 20px",
          background: "#1b1e2b",
          borderBottom: `1px solid ${COLORS.panelBorder}`,
        }}
      >
        {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
          <span key={c} style={{ width: 13, height: 13, borderRadius: "50%", background: c }} />
        ))}
        <span style={{ marginLeft: 16, fontSize: 17, color: COLORS.comment, fontFamily: FONT_BODY }}>
          ~/{PROJECT} — bash
        </span>
      </div>
      <div style={{ flex: 1, overflow: "hidden", position: "relative" }}>
        <div
          style={{
            padding: "16px 28px",
            fontSize: TERM_FONT,
            lineHeight: TERM_LH,
            whiteSpace: "pre",
            transform: `translateY(${-scroll}px)`,
          }}
        >
          {visible.map((r, i) => {
            const n = r.typeFrames === 0 ? r.text.length : Math.floor(((frame - r.start) / r.typeFrames) * r.text.length);
            const typing = n < r.text.length;
            return (
              <div key={i} style={{ height: TERM_ROW }}>
                <Prompt kind={r.kind} />
                {renderSegs(rowSegs(r), n)}
                {typing ? <Cursor /> : null}
              </div>
            );
          })}
          {allDone ? (
            <div style={{ height: TERM_ROW }}>
              <Prompt kind="cmd" />
              <Cursor />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// Editor and terminal slides share a layout so the header doesn't jump.
const CODE_PADDING = "56px 48px 52px 56px";

// Step header shared by the editor and terminal slides.
function CodeHeader({ step, sub }: { step: number; sub: string }) {
  const frame = useCurrentFrame();
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 20, marginBottom: 22, opacity: fadeIn(frame, 0, 12) }}>
      <span style={{ fontFamily: FONT_BODY, fontSize: 22, fontWeight: 700, color: COLORS.prompt, letterSpacing: 2 }}>
        STEP {step + 1} · CODE
      </span>
      <span style={{ fontFamily: FONT_BODY, fontSize: 40, fontWeight: 700, color: COLORS.text }}>{STEPS[step].title}</span>
      <span style={{ fontFamily: FONT_BODY, fontSize: 24, color: COLORS.textDim, marginLeft: "auto" }}>{sub}</span>
    </div>
  );
}

function TerminalScene({ step, tl, files }: { step: number; tl: TerminalTimeline; files: Map<string, FileStatus> }) {
  const frame = useCurrentFrame();
  const live = new Map(files);
  for (const d of tl.dirs) if (frame >= d.start) live.set(d.path, "idle");
  return (
    <>
      <Sidebar step={step} phase="code" files={live} />
      <Main style={{ padding: CODE_PADDING }}>
        <CodeHeader step={step} sub="run it" />
        <TypingTerminal tl={tl} />
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- editor

const ED = {
  bg: "#050505",
  chrome: "#101010",
  border: "#262626",
  gutter: "#4a4f63",
  lineHi: "rgba(122,162,247,0.10)",
  added: COLORS.string,
};
const ED_FONT = 20;
const ED_LH = 1.6;
const ED_ROW = ED_FONT * ED_LH;
const ED_CHAR = ED_FONT * 0.602; // monospace advance width
const ED_GUTTER = 64;
const ED_PAD_X = 20;
const ED_PAD_Y = 14;
const ED_MAX_ROWS = 25;
const CALLOUT_W = 380;
const CALLOUT_CHARS = 31; // rough characters per wrapped callout line
const CALLOUTS_SHOWN = 4;

function calloutHeight(text: string): number {
  const plain = text.replace(/`/g, "");
  return 22 + Math.max(1, Math.ceil(plain.length / CALLOUT_CHARS)) * 26;
}

function EditorScene({
  step,
  tl,
  files,
  isNew,
}: {
  step: number;
  tl: EditTimeline;
  files: Map<string, FileStatus>;
  isNew: boolean;
}) {
  const frame = useCurrentFrame();
  const typing = frame < tl.typingEnd;

  // Sidebar: this slide's file is being written, then badged.
  const live = new Map(files);
  const prior = files.get(tl.path);
  live.set(
    tl.path,
    typing ? (isNew ? "writing-new" : "writing-edit") : isNew || prior === "created" ? "created" : "modified",
  );

  // Line currently being typed (fractional, for smooth scrolling).
  let cursorRow = tl.firstTyped;
  let activeLine = -1;
  tl.lines.forEach((l, i) => {
    if (l.existing || frame < l.start) return;
    const p = l.typeFrames === 0 ? 1 : Math.min(1, (frame - l.start) / l.typeFrames);
    cursorRow = i + p;
    activeLine = i;
  });
  // Appends open at the end of the file: glide there over the first second.
  const targetScroll = Math.max(0, cursorRow + 3 - ED_MAX_ROWS) * ED_ROW;
  const scroll = targetScroll * interpolate(frame, [0, 24], [0, 1], { extrapolateRight: "clamp" });
  const viewportH = ED_MAX_ROWS * ED_ROW;

  // The newest few callouts. Top-down, each sits at its line, pushed
  // down only as far as needed to clear the one above; then bottom-up,
  // anything that would fall off the bottom pushes the stack up.
  const shown = tl.callouts.filter((c) => frame >= c.appear).slice(-CALLOUTS_SHOWN);
  const placed = shown.map((c) => ({ c, y: ED_PAD_Y + c.line * ED_ROW - scroll - 6, h: calloutHeight(c.text) }));
  for (let i = 1; i < placed.length; i++) {
    placed[i].y = Math.max(placed[i].y, placed[i - 1].y + placed[i - 1].h + 12);
  }
  let floor = viewportH;
  for (let i = placed.length - 1; i >= 0; i--) {
    placed[i].y = Math.min(placed[i].y, floor - placed[i].h);
    floor = placed[i].y - 12;
  }
  const newest = shown[shown.length - 1];
  const codeRight = 1376 - CALLOUT_W; // editor width minus callout column

  return (
    <>
      <Sidebar step={step} phase="code" files={live} />
      <Main style={{ padding: CODE_PADDING }}>
        <CodeHeader step={step} sub={isNew ? `new file · ${tl.path}` : `editing · ${tl.path}`} />
        <div
          style={{
            flex: 1,
            background: ED.bg,
            border: `1px solid ${ED.border}`,
            borderRadius: 12,
            boxShadow: "0 30px 80px rgba(0,0,0,0.6)",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          }}
        >
          {/* title bar + single tab */}
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8, background: ED.chrome, borderBottom: `1px solid ${ED.border}`, padding: "0 16px", height: 46 }}>
            <div style={{ display: "flex", gap: 8, alignSelf: "center", marginRight: 18 }}>
              {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
                <span key={c} style={{ width: 12, height: 12, borderRadius: "50%", background: c }} />
              ))}
            </div>
            <div
              style={{
                fontFamily: FONT_MONO,
                fontSize: 19,
                color: COLORS.text,
                background: ED.bg,
                border: `1px solid ${ED.border}`,
                borderBottom: "none",
                borderRadius: "8px 8px 0 0",
                padding: "8px 18px",
                display: "flex",
                gap: 12,
                alignItems: "center",
              }}
            >
              {tl.path}
              <span style={{ color: typing ? COLORS.ident : COLORS.string, fontSize: 14 }}>{typing ? "●" : "✓ saved"}</span>
            </div>
          </div>

          <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
            {/* code */}
            <div style={{ position: "absolute", left: 0, top: 0, right: CALLOUT_W, bottom: 0, overflow: "hidden" }}>
              <div
                style={{
                  transform: `translateY(${-scroll}px)`,
                  padding: `${ED_PAD_Y}px 0`,
                  fontFamily: FONT_MONO,
                  fontSize: ED_FONT,
                  lineHeight: ED_LH,
                  whiteSpace: "pre",
                }}
              >
                {tl.lines.map((l, i) => {
                  if (!l.existing && frame < l.start) return null;
                  const n = l.existing || l.typeFrames === 0 ? l.text.length : Math.floor(((frame - l.start) / l.typeFrames) * l.text.length);
                  const isActive = i === activeLine && typing;
                  const isNewest = newest && newest.line === i;
                  return (
                    <div
                      key={i}
                      style={{
                        display: "flex",
                        height: ED_ROW,
                        background: isNewest ? ED.lineHi : isActive ? "rgba(255,255,255,0.04)" : "transparent",
                        opacity: l.existing ? 0.45 : 1,
                      }}
                    >
                      <span
                        style={{
                          width: ED_GUTTER,
                          flexShrink: 0,
                          textAlign: "right",
                          paddingRight: 14,
                          boxSizing: "border-box",
                          color: isActive ? COLORS.textDim : ED.gutter,
                          borderRight: `3px solid ${!l.existing && tl.append ? ED.added : "transparent"}`,
                        }}
                      >
                        {i + 1}
                      </span>
                      <span style={{ paddingLeft: ED_PAD_X }}>
                        {renderSegs(tokenize(l.text, tl.lang), n)}
                        {isActive ? <Cursor /> : null}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* callouts */}
            <div style={{ position: "absolute", top: 0, right: 0, bottom: 0, width: CALLOUT_W, borderLeft: `1px solid ${ED.border}`, background: "#0a0a0c" }}>
              {placed.map(({ c, y }) => {
                const isNewest = c === newest;
                const o = fadeIn(frame, c.appear, 10);
                return (
                  <div
                    key={c.n}
                    style={{
                      position: "absolute",
                      left: 14,
                      right: 16,
                      top: y + interpolate(o, [0, 1], [10, 0]),
                      opacity: o * (isNewest ? 1 : 0.55),
                      display: "flex",
                      gap: 12,
                      background: isNewest ? "#121a2e" : "#111216",
                      border: `1px solid ${isNewest ? COLORS.prompt : ED.border}`,
                      borderRadius: 10,
                      padding: "10px 14px",
                      fontFamily: FONT_BODY,
                      fontSize: 18,
                      lineHeight: 1.45,
                      color: COLORS.text,
                    }}
                  >
                    <span
                      style={{
                        flexShrink: 0,
                        width: 24,
                        height: 24,
                        borderRadius: "50%",
                        background: isNewest ? COLORS.prompt : COLORS.comment,
                        color: ED.bg,
                        fontSize: 13,
                        fontWeight: 700,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        marginTop: 1,
                      }}
                    >
                      {c.n}
                    </span>
                    <span>
                      <RichText text={c.text} />
                    </span>
                  </div>
                );
              })}
            </div>
            {/* connectors from each annotated line to its callout */}
            <svg style={{ position: "absolute", left: 0, top: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
              {placed.map(({ c, y }) => {
                const line = tl.lines[c.line];
                const lineY = ED_PAD_Y + c.line * ED_ROW - scroll + ED_ROW / 2;
                const endX = Math.min(codeRight - 26, ED_GUTTER + ED_PAD_X + line.text.length * ED_CHAR + 14);
                const isNewest = c === newest;
                const color = isNewest ? COLORS.prompt : COLORS.comment;
                const o = fadeIn(frame, c.appear, 10);
                return (
                  <g key={c.n} opacity={o * (isNewest ? 1 : 0.6)}>
                    <path
                      d={`M ${endX + 10} ${lineY} L ${codeRight - 8} ${lineY} L ${codeRight + 14} ${y + 22}`}
                      fill="none"
                      stroke={color}
                      strokeWidth={1.5}
                      strokeDasharray={isNewest ? "0" : "4 4"}
                    />
                    <circle cx={endX} cy={lineY} r={10} fill={color} />
                    <text x={endX} y={lineY + 4.5} textAnchor="middle" fontSize={12} fontWeight={700} fill={ED.bg} fontFamily={FONT_BODY}>
                      {c.n}
                    </text>
                  </g>
                );
              })}
            </svg>

          </div>
        </div>
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- explainer

function ExplainScene({ step }: { step: number }) {
  const frame = useCurrentFrame();
  const s = STEPS[step];
  const touched = filesTouched(step);
  const files = idleFiles(pathsBefore(step));
  for (const t of touched) files.set(t.path, t.isNew ? "todo-new" : "todo-edit");
  return (
    <>
      <Sidebar step={step} phase="explain" files={files} />
      <Main style={{ justifyContent: "center", paddingRight: 120 }}>
        <div
          style={{
            fontFamily: FONT_BODY,
            fontSize: 24,
            fontWeight: 700,
            letterSpacing: 3,
            color: COLORS.prompt,
            opacity: fadeIn(frame, 0),
          }}
        >
          STEP {step + 1} OF {STEPS.length}
        </div>
        <div
          style={{
            fontFamily: FONT_BODY,
            fontSize: 80,
            fontWeight: 800,
            letterSpacing: -2,
            color: COLORS.text,
            marginTop: 8,
            opacity: fadeIn(frame, 4),
            transform: `translateY(${interpolate(fadeIn(frame, 4), [0, 1], [14, 0])}px)`,
          }}
        >
          {s.title}
        </div>
        <div
          style={{
            fontFamily: FONT_BODY,
            fontSize: 32,
            lineHeight: 1.5,
            color: COLORS.textDim,
            marginTop: 26,
            maxWidth: 1200,
            opacity: fadeIn(frame, 16),
          }}
        >
          <RichText text={s.what} />
        </div>
        <ul style={{ listStyle: "none", padding: 0, margin: "40px 0 0", display: "flex", flexDirection: "column", gap: 18 }}>
          {s.bullets.map((b, i) => {
            const start = 40 + i * 14;
            return (
              <li
                key={i}
                style={{
                  display: "flex",
                  gap: 20,
                  fontFamily: FONT_BODY,
                  fontSize: 29,
                  lineHeight: 1.45,
                  color: COLORS.text,
                  opacity: fadeIn(frame, start),
                  transform: `translateX(${interpolate(fadeIn(frame, start), [0, 1], [-12, 0])}px)`,
                }}
              >
                <span style={{ color: COLORS.prompt, fontWeight: 700 }}>›</span>
                <span>
                  <RichText text={b} />
                </span>
              </li>
            );
          })}
        </ul>
        {touched.length > 0 ? (
          <div
            style={{
              display: "flex",
              gap: 14,
              marginTop: 48,
              alignItems: "center",
              flexWrap: "wrap",
              opacity: fadeIn(frame, 40 + s.bullets.length * 14),
            }}
          >
            <span style={{ fontFamily: FONT_BODY, fontSize: 20, color: COLORS.comment, letterSpacing: 2, fontWeight: 700 }}>
              FILES IN THIS STEP
            </span>
            {touched.map((t) => (
              <span
                key={t.path}
                style={{
                  fontFamily: FONT_MONO,
                  fontSize: 21,
                  color: COLORS.text,
                  background: COLORS.panel,
                  border: `1px solid ${t.isNew ? COLORS.string : COLORS.ident}`,
                  borderRadius: 8,
                  padding: "6px 14px",
                }}
              >
                {t.path}
                <span style={{ marginLeft: 10, fontFamily: FONT_BODY, fontSize: 15, fontWeight: 700, color: t.isNew ? COLORS.string : COLORS.ident }}>
                  {t.isNew ? "NEW" : "EDIT"}
                </span>
              </span>
            ))}
          </div>
        ) : null}
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- about rigx

function AboutScene() {
  const frame = useCurrentFrame();
  const cards: [string, React.ReactNode, string][] = [
    [
      "Hermetic",
      "Every build runs in a sandbox that sees only what you declared. Same inputs, same result, on any machine.",
      COLORS.prompt,
    ],
    [
      "Built on Nix. No Nix knowledge required.",
      <>
        You write a short <RichText text="`rigx.toml`" />. rigx generates and drives the Nix underneath, so you never
        have to read or write any.
      </>,
      COLORS.keyword,
    ],
    [
      "Dependencies from nixpkgs",
      <>
        Compilers, libraries and tools come from <span style={{ color: COLORS.func, fontFamily: FONT_MONO }}>nixpkgs.org</span>,
        a huge collection of open-source packages, pinned to one release so they never drift.
      </>,
      COLORS.string,
    ],
  ];
  return (
    <>
      <Sidebar step={-1} files={new Map()} />
      <Main style={{ justifyContent: "center", paddingRight: 110 }}>
        <div style={{ fontFamily: FONT_BODY, fontSize: 24, fontWeight: 700, letterSpacing: 3, color: COLORS.prompt, opacity: fadeIn(frame, 0) }}>
          A RIGX TUTORIAL
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 72, fontWeight: 800, letterSpacing: -2, color: COLORS.text, marginTop: 8, lineHeight: 1.1, opacity: fadeIn(frame, 6) }}>
          rigx is a hermetic build system built on Nix
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 20, marginTop: 48 }}>
          {cards.map(([title, body, color], i) => {
            const start = 30 + i * 40;
            return (
              <div
                key={title}
                style={{
                  background: COLORS.panel,
                  border: `1px solid ${COLORS.panelBorder}`,
                  borderLeft: `5px solid ${color}`,
                  borderRadius: 12,
                  padding: "20px 28px",
                  opacity: fadeIn(frame, start),
                  transform: `translateY(${interpolate(fadeIn(frame, start), [0, 1], [12, 0])}px)`,
                }}
              >
                <div style={{ fontFamily: FONT_BODY, fontSize: 32, fontWeight: 700, color: COLORS.text }}>{title}</div>
                <div style={{ fontFamily: FONT_BODY, fontSize: 26, lineHeight: 1.5, color: COLORS.textDim, marginTop: 6 }}>{body}</div>
              </div>
            );
          })}
        </div>
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- overview diagram

// Animated sketch of the final build graph: every target in rigx.toml,
// arrows from each dependency to what uses it. A highlight walks the
// graph in build order, the way `rigx build` + `rigx test` would.
type GNode = { id: string; kind: string; x: number; y: number; color: string };

const GRAPH_NODES: GNode[] = [
  { id: "greet", kind: "static_library · fmt", x: 375, y: 70, color: COLORS.keyword },
  { id: "hello", kind: "executable", x: 375, y: 250, color: COLORS.prompt },
  { id: "hello_arm64", kind: "executable · aarch64", x: 875, y: 250, color: COLORS.accent },
  { id: "hello_zip", kind: "run · zip", x: 125, y: 430, color: COLORS.func },
  { id: "test_hello", kind: "test", x: 375, y: 430, color: COLORS.string },
  { id: "hello_capsule", kind: "capsule", x: 625, y: 430, color: COLORS.func },
  { id: "test_arm64", kind: "test", x: 875, y: 430, color: COLORS.string },
  { id: "test_capsule", kind: "test · host", x: 625, y: 600, color: COLORS.string },
];

const GRAPH_EDGES: [string, string][] = [
  ["greet", "hello"],
  ["hello", "hello_zip"],
  ["hello", "test_hello"],
  ["hello", "hello_capsule"],
  ["hello_arm64", "test_arm64"],
  ["hello_capsule", "test_capsule"],
];

// Build order for the highlight.
const BUILD_ORDER = ["greet", "hello", "hello_zip", "test_hello", "hello_arm64", "test_arm64", "hello_capsule", "test_capsule"];
const STEP_FRAMES = 22;
const NODE_W = 236;
const NODE_H = 86;

function BuildGraph() {
  const frame = useCurrentFrame();
  const node = (id: string) => GRAPH_NODES.find((n) => n.id === id)!;
  const cycle = BUILD_ORDER.length * STEP_FRAMES + 50;
  const t = frame < 50 ? -1 : (frame - 50) % cycle;
  const builtCount = t < 0 ? 0 : Math.min(BUILD_ORDER.length, Math.floor(t / STEP_FRAMES) + 1);
  const isBuilt = (id: string) => BUILD_ORDER.indexOf(id) < builtCount;
  const active = t >= 0 && builtCount <= BUILD_ORDER.length && t < BUILD_ORDER.length * STEP_FRAMES ? BUILD_ORDER[builtCount - 1] : "";

  return (
    <svg viewBox="0 0 1000 680" style={{ width: "100%", height: "100%" }}>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={COLORS.comment} />
        </marker>
      </defs>
      {GRAPH_EDGES.map(([a, b], i) => {
        const from = node(a);
        const to = node(b);
        const lit = isBuilt(b);
        return (
          <line
            key={i}
            x1={from.x}
            y1={from.y + NODE_H / 2}
            x2={to.x}
            y2={to.y - NODE_H / 2 - 4}
            stroke={lit ? to.color : COLORS.panelBorder}
            strokeWidth={lit ? 2.5 : 2}
            markerEnd="url(#arrow)"
            opacity={fadeIn(frame, 16 + i * 4)}
          />
        );
      })}
      {GRAPH_NODES.map((n, i) => {
        const built = isBuilt(n.id);
        const on = active === n.id;
        const glow = on ? 0.6 + 0.4 * Math.sin(frame / 3) : 0;
        return (
          <g key={n.id} opacity={fadeIn(frame, 8 + i * 4)}>
            <rect
              x={n.x - NODE_W / 2}
              y={n.y - NODE_H / 2}
              width={NODE_W}
              height={NODE_H}
              rx={14}
              fill={COLORS.panel}
              stroke={built ? n.color : COLORS.panelBorder}
              strokeWidth={on ? 3 : 2}
              style={{ filter: on ? `drop-shadow(0 0 ${12 * glow}px ${n.color})` : undefined }}
            />
            <text x={n.x} y={n.y - 6} textAnchor="middle" fill={COLORS.text} fontSize={21} fontWeight={700} fontFamily={FONT_MONO}>
              {n.id}
            </text>
            <text x={n.x} y={n.y + 24} textAnchor="middle" fill={built ? n.color : COLORS.comment} fontSize={16} fontFamily={FONT_MONO}>
              {built ? `✓ ${n.kind}` : n.kind}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function OverviewScene() {
  const frame = useCurrentFrame();
  return (
    <>
      <Sidebar step={-1} files={new Map()} />
      <Main>
        <div style={{ fontFamily: FONT_BODY, fontSize: 24, fontWeight: 700, letterSpacing: 3, color: COLORS.prompt, opacity: fadeIn(frame, 0) }}>
          WHAT WE'RE GOING TO BUILD
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 64, fontWeight: 800, letterSpacing: -1.5, color: COLORS.text, marginTop: 6, opacity: fadeIn(frame, 4) }}>
          One C++ program, many targets
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 28, lineHeight: 1.5, color: COLORS.textDim, marginTop: 16, maxWidth: 1250, opacity: fadeIn(frame, 14) }}>
          A small library and program that use <RichText text="`fmt`" />. From that one program we'll get a zip to
          share, a sandboxed test, an ARM build checked by its own test, and a container checked by a test that runs it.
          All of it is declared in one <RichText text="`rigx.toml`" />.
        </div>
        <div style={{ marginTop: 26 }}>
          <Prereqs start={24} />
        </div>
        <div style={{ flex: 1, minHeight: 0, marginTop: 10, opacity: fadeIn(frame, 20) }}>
          <BuildGraph />
        </div>
      </Main>
    </>
  );
}

// The tutorial's whole host footprint, stated up front and at the end.
function Prereqs({ start, note = true }: { start: number; note?: boolean }) {
  const frame = useCurrentFrame();
  const chip = (label: string, i: number) => (
    <span
      key={label}
      style={{
        fontFamily: FONT_MONO,
        fontSize: 26,
        color: COLORS.text,
        background: COLORS.panel,
        border: `1px solid ${COLORS.string}`,
        borderRadius: 10,
        padding: "6px 18px",
        opacity: fadeIn(frame, start + 6 + i * 6),
      }}
    >
      <span style={{ color: COLORS.string, marginRight: 10 }}>✓</span>
      {label}
    </span>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <span style={{ fontFamily: FONT_BODY, fontSize: 20, fontWeight: 700, letterSpacing: 2.5, color: COLORS.comment, opacity: fadeIn(frame, start) }}>
          YOU NEED
        </span>
        {["a shell", "a text editor", "rigx"].map(chip)}
        <span style={{ fontFamily: FONT_BODY, fontSize: 26, color: COLORS.text, fontWeight: 600, marginLeft: 8, opacity: fadeIn(frame, start + 26) }}>
          Nothing else to install.
        </span>
      </div>
      {note ? (
        <div style={{ fontFamily: FONT_BODY, fontSize: 19, color: COLORS.comment, opacity: fadeIn(frame, start + 34) }}>
          The compiler, fmt, zip and the ARM cross-compiler all come through rigx. Running the capsule also needs docker or podman.
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- title + outro

function TitleScene() {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [0, 20, 100, 120], [0, 1, 1, 0], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill style={{ alignItems: "center", justifyContent: "center", flexDirection: "column", opacity: o }}>
      <div style={{ fontFamily: FONT_BODY, fontSize: 140, fontWeight: 800, color: COLORS.text, letterSpacing: -4 }}>rigx</div>
      <div style={{ fontFamily: FONT_BODY, fontSize: 40, color: COLORS.textDim, marginTop: 10 }}>
        One <span style={{ color: COLORS.func }}>C++ program</span>,{" "}
        <span style={{ color: COLORS.string }}>many targets</span>
      </div>
      <div style={{ fontFamily: FONT_MONO, fontSize: 24, color: COLORS.comment, marginTop: 30 }}>
        C++ · fmt · zip · tests · ARM · capsules
      </div>
      <div style={{ marginTop: 56 }}>
        <Prereqs start={30} note={false} />
      </div>
    </AbsoluteFill>
  );
}

function OutroScene() {
  const frame = useCurrentFrame();
  const recap = [
    ['kind = "static_library"', "a library with public headers, linked into the program"],
    ['deps.nixpkgs = ["fmt"]', "libraries and tools through rigx, nothing installed on the host"],
    ['kind = "run"', "a zip produced by a nixpkgs tool"],
    ['kind = "test"', "sandboxed and cached, or on the host with sandbox = false"],
    ['target = "aarch64-linux"', "an ARM build, cross-compiler included"],
    ['kind = "capsule"', "a minimal container around the program"],
  ];
  return (
    <>
      <Sidebar step={STEPS.length} files={idleFiles(pathsBefore(STEPS.length))} />
      <Main style={{ justifyContent: "center" }}>
        <div style={{ fontFamily: FONT_BODY, fontSize: 72, fontWeight: 800, color: COLORS.text, letterSpacing: -2, opacity: fadeIn(frame, 0) }}>
          That's the whole project.
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 30, color: COLORS.textDim, marginTop: 12, opacity: fadeIn(frame, 8) }}>
          One <RichText text="`rigx.toml`" />, eight targets, built from scratch:
        </div>
        <div style={{ marginTop: 40, display: "flex", flexDirection: "column", gap: 16 }}>
          {recap.map(([k, v], i) => (
            <div key={k} style={{ display: "flex", gap: 28, alignItems: "baseline", opacity: fadeIn(frame, 20 + i * 10) }}>
              <span style={{ fontFamily: FONT_MONO, fontSize: 27, color: COLORS.func, width: 470 }}>{k}</span>
              <span style={{ fontFamily: FONT_BODY, fontSize: 28, color: COLORS.text }}>{v}</span>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 56 }}>
          <Prereqs start={90} />
        </div>
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- going further

// Where to go next: the other two tutorials, which build on capsules.
function FurtherScene() {
  const frame = useCurrentFrame();
  const next: [string, string, string][] = [
    [
      "A matrix-multiplication cluster",
      "Pin clang 18, use Eigen, and drive three copies of one capsule from a Python testbed.",
      COLORS.keyword,
    ],
    [
      "A Fibonacci service with a C++ cache",
      "Connect a JavaScript capsule to a C++ one through a testbed link, then inject network faults.",
      COLORS.func,
    ],
  ];
  const more: [string, string][] = [
    ["Other languages", "Go, Rust, Zig, Nim and Python targets, same rigx.toml."],
    ["Variants", "Debug, release or cross builds of one target from one definition."],
    ["rigx remote", "Deploy a testbed to any Linux host over SSH."],
  ];
  return (
    <>
      <Sidebar step={STEPS.length} files={idleFiles(pathsBefore(STEPS.length))} />
      <Main style={{ justifyContent: "center" }}>
        <div style={{ fontFamily: FONT_BODY, fontSize: 24, fontWeight: 700, letterSpacing: 3, color: COLORS.prompt, opacity: fadeIn(frame, 0) }}>
          GOING FURTHER
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 60, fontWeight: 800, letterSpacing: -1.5, color: COLORS.text, marginTop: 6, opacity: fadeIn(frame, 4) }}>
          From one capsule to whole systems
        </div>
        <div style={{ fontFamily: FONT_BODY, fontSize: 27, lineHeight: 1.5, color: COLORS.textDim, marginTop: 14, maxWidth: 1300, opacity: fadeIn(frame, 12) }}>
          Capsules become interesting when several run together. Testbeds start them from Python, wire them up, and can
          make the network between them misbehave on demand. Two more tutorials show how.
        </div>
        <div style={{ display: "flex", gap: 28, marginTop: 40 }}>
          {next.map(([t, d, c], i) => (
            <div
              key={t}
              style={{
                flex: 1,
                background: COLORS.panel,
                border: `1px solid ${COLORS.panelBorder}`,
                borderTop: `5px solid ${c}`,
                borderRadius: 14,
                padding: "26px 30px",
                opacity: fadeIn(frame, 30 + i * 14),
                transform: `translateY(${interpolate(fadeIn(frame, 30 + i * 14), [0, 1], [12, 0])}px)`,
              }}
            >
              <div style={{ fontFamily: FONT_BODY, fontSize: 19, fontWeight: 700, letterSpacing: 2, color: c }}>NEXT TUTORIAL</div>
              <div style={{ fontFamily: FONT_BODY, fontSize: 34, fontWeight: 800, color: COLORS.text, marginTop: 8 }}>{t}</div>
              <div style={{ fontFamily: FONT_BODY, fontSize: 24, lineHeight: 1.5, color: COLORS.textDim, marginTop: 10 }}>{d}</div>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", gap: 28, marginTop: 28 }}>
          {more.map(([t, d], i) => (
            <div
              key={t}
              style={{
                flex: 1,
                background: COLORS.panel,
                border: `1px solid ${COLORS.panelBorder}`,
                borderLeft: `4px solid ${COLORS.accent}`,
                borderRadius: 10,
                padding: "14px 20px",
                opacity: fadeIn(frame, 70 + i * 12),
              }}
            >
              <div style={{ fontFamily: FONT_BODY, fontSize: 24, fontWeight: 700, color: COLORS.text }}>{t}</div>
              <div style={{ fontFamily: FONT_BODY, fontSize: 20, color: COLORS.textDim, marginTop: 4, lineHeight: 1.4 }}>{d}</div>
            </div>
          ))}
        </div>
      </Main>
    </>
  );
}

// ---------------------------------------------------------------- composition

function SceneView({ scene }: { scene: SceneSpec }) {
  switch (scene.kind) {
    case "title":
      return <TitleScene />;
    case "about":
      return <AboutScene />;
    case "overview":
      return <OverviewScene />;
    case "explain":
      return <ExplainScene step={scene.step} />;
    case "edit":
      return <EditorScene step={scene.step} tl={scene.tl} files={scene.files} isNew={scene.isNew} />;
    case "terminal":
      return <TerminalScene step={scene.step} tl={scene.tl} files={scene.files} />;
    case "further":
      return <FurtherScene />;
    case "outro":
      return <OutroScene />;
  }
}

export const HelloCppVideo: React.FC = () => (
  <AbsoluteFill style={{ background: COLORS.bg }}>
    {SCENES.map((scene, i) => (
      <Sequence key={i} from={scene.from} durationInFrames={scene.frames}>
        <AbsoluteFill>
          <Background />
          <SceneView scene={scene} />
        </AbsoluteFill>
      </Sequence>
    ))}
  </AbsoluteFill>
);
