import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import type { Line } from "./content";

export const COLORS = {
  bg: "#0c0e14",
  panel: "#161922",
  panelBorder: "#262936",
  text: "#e3e6f3",
  textDim: "#a9b1d6",
  comment: "#565f89",
  prompt: "#7aa2f7",
  string: "#9ece6a",
  keyword: "#bb9af7",
  func: "#7dcfff",
  ident: "#e0af68",
  accent: "#f7768e",
  out: "#cfd8e8",
};

const FONT_BODY =
  '"Inter", "SF Pro Display", system-ui, -apple-system, sans-serif';
const FONT_MONO =
  '"JetBrains Mono", "Fira Code", "Menlo", monospace';

export function SceneTitle({ text }: { text: string }) {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 18], [0, 1], {
    extrapolateRight: "clamp",
  });
  const dy = interpolate(frame, [0, 18], [-12, 0], {
    extrapolateRight: "clamp",
  });
  return (
    <div
      style={{
        position: "absolute",
        top: 60,
        left: 80,
        right: 80,
        fontSize: 60,
        fontWeight: 700,
        letterSpacing: -0.5,
        color: COLORS.text,
        fontFamily: FONT_BODY,
        opacity,
        transform: `translateY(${dy}px)`,
      }}
    >
      {text}
    </div>
  );
}

export function SceneCaption({ text }: { text: string }) {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [12, 30], [0, 1], {
    extrapolateRight: "clamp",
  });
  return (
    <div
      style={{
        position: "absolute",
        top: 150,
        left: 80,
        right: 80,
        fontSize: 28,
        color: COLORS.textDim,
        fontFamily: FONT_BODY,
        fontWeight: 400,
        opacity,
      }}
    >
      {text}
    </div>
  );
}

// Reveals a string char-by-char as the frame advances. Returns the
// portion currently visible plus a bool indicating whether typing is
// still in progress (so we can show a blinking cursor).
function typedSlice(
  text: string,
  frame: number,
  startFrame: number,
  charsPerFrame: number,
): { visible: string; typing: boolean } {
  const elapsed = Math.max(0, frame - startFrame);
  const n = Math.min(text.length, Math.floor(elapsed * charsPerFrame));
  const typing = elapsed > 0 && n < text.length;
  return { visible: text.slice(0, n), typing };
}

function Cursor() {
  const frame = useCurrentFrame();
  // Blink at ~2 Hz (every 15 frames at 30fps).
  const on = Math.floor(frame / 15) % 2 === 0;
  return (
    <span
      style={{
        display: "inline-block",
        width: "0.55em",
        height: "1.1em",
        background: on ? COLORS.text : "transparent",
        verticalAlign: "text-bottom",
        marginLeft: 2,
      }}
    />
  );
}

// Font + line-height of the rendered terminal text. Used so the
// terminal can scroll the same way the file panel does once the
// typed-out content overflows the viewport.
const TERM_FONT_PX = 26;
const TERM_LINE_HEIGHT = 1.55;
const TERM_LINE_PX = TERM_FONT_PX * TERM_LINE_HEIGHT; // 40.3

export function TerminalPanel({
  lines,
  startFrame = 30,
  style,
  // Cap on how many terminal rows fit before scrolling kicks in. The
  // default suits the standard SceneRenderer layout; tighten it for
  // the split-panel half-height case.
  maxVisibleLines = 14,
}: {
  lines: Line[];
  startFrame?: number;
  style?: React.CSSProperties;
  maxVisibleLines?: number;
}) {
  const frame = useCurrentFrame();
  const charsPerFrame = 1.6;
  const interLineGap = 6; // frames between lines

  // Pre-compute when each line begins typing.
  let cursor = startFrame;
  const lineStarts: number[] = [];
  for (const line of lines) {
    lineStarts.push(cursor);
    cursor += Math.ceil(line.text.length / charsPerFrame) + interLineGap;
  }

  // Smoothly slide the inner block up once the typed content has
  // produced more rows than the viewport can hold. We treat each
  // already-revealed line as a "full" row and the currently-typing
  // line as a fractional row (based on how far through its typing we
  // are) so the scroll keeps pace with the cursor instead of
  // jumping a full line at the moment the next line starts.
  let revealedRows = 0;
  for (let i = 0; i < lines.length; i++) {
    const start = lineStarts[i];
    if (frame < start) break;
    const charsTyped = Math.min(
      lines[i].text.length,
      (frame - start) * charsPerFrame,
    );
    const lineProgress =
      lines[i].text.length === 0
        ? 1
        : Math.min(1, charsTyped / lines[i].text.length);
    revealedRows += lineProgress;
  }
  const overflow = Math.max(0, revealedRows - maxVisibleLines);
  const translateY = -overflow * TERM_LINE_PX;

  return (
    <div
      style={{
        background: COLORS.panel,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 14,
        fontFamily: FONT_MONO,
        boxShadow: "0 30px 80px rgba(0,0,0,0.4)",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        ...style,
      }}
    >
      <div style={{ padding: "20px 24px 4px 24px", flexShrink: 0 }}>
        <TerminalChrome />
      </div>
      <div
        style={{
          flex: 1,
          overflow: "hidden",
          position: "relative",
        }}
      >
        <div
          style={{
            padding: "8px 32px 26px 32px",
            fontSize: TERM_FONT_PX,
            color: COLORS.text,
            lineHeight: TERM_LINE_HEIGHT,
            whiteSpace: "pre",
            transform: `translateY(${translateY}px)`,
            willChange: "transform",
          }}
        >
          {lines.map((line, i) => {
            const start = lineStarts[i];
            if (frame < start) return <div key={i}>&nbsp;</div>;
            const { visible, typing } = typedSlice(
              line.text,
              frame,
              start,
              charsPerFrame,
            );
            const isCmd = line.kind === "cmd";
            const isComment = line.kind === "comment";
            const color = isComment
              ? COLORS.comment
              : isCmd
                ? COLORS.text
                : COLORS.out;
            const prefix = isCmd ? (
              <span style={{ color: COLORS.prompt, marginRight: 10 }}>$</span>
            ) : null;
            return (
              <div key={i} style={{ color }}>
                {prefix}
                <span>{visible}</span>
                {typing ? <Cursor /> : null}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function TerminalChrome() {
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        marginBottom: 18,
        marginLeft: -6,
      }}
    >
      <Dot color="#ff5f57" />
      <Dot color="#febc2e" />
      <Dot color="#28c840" />
    </div>
  );
}

function Dot({ color }: { color: string }) {
  return (
    <span
      style={{
        width: 14,
        height: 14,
        borderRadius: "50%",
        background: color,
        display: "inline-block",
      }}
    />
  );
}

// Light syntax shading without pulling in a real lexer — just colors a
// few token classes inline. Good enough for the screenshots-as-video
// shape. Comments and strings get full-line/word treatment; keywords
// and idents are highlighted by token regex.
function colorizeLine(text: string, lang: "cpp" | "toml" | "py"): React.ReactNode[] {
  // Comment-line short-circuit.
  if (lang === "py" && text.trimStart().startsWith("#")) {
    return [<span style={{ color: COLORS.comment }}>{text}</span>];
  }
  if (lang === "cpp" && text.trimStart().startsWith("//")) {
    return [<span style={{ color: COLORS.comment }}>{text}</span>];
  }
  if (lang === "toml" && text.trimStart().startsWith("#")) {
    return [<span style={{ color: COLORS.comment }}>{text}</span>];
  }
  // Section header for TOML.
  if (lang === "toml" && /^\s*\[/.test(text)) {
    return [<span style={{ color: COLORS.func, fontWeight: 600 }}>{text}</span>];
  }
  // String highlighting (all langs): match double-quoted runs.
  const parts: React.ReactNode[] = [];
  const re = /"([^"\\]|\\.)*"/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) {
      parts.push(
        nonStringChunk(text.slice(last, m.index), lang),
      );
    }
    parts.push(<span style={{ color: COLORS.string }}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parts.push(nonStringChunk(text.slice(last), lang));
  }
  return parts;
}

function nonStringChunk(s: string, lang: "cpp" | "toml" | "py"): React.ReactNode {
  // Highlight some keywords per language. Cheap regex pass.
  const kw: Record<string, string[]> = {
    cpp: [
      "include",
      "pragma",
      "namespace",
      "return",
      "int",
      "char",
      "void",
      "const",
      "if",
      "else",
      "std",
      "string",
      "auto",
    ],
    toml: [],
    py: [
      "from",
      "import",
      "with",
      "as",
      "def",
      "return",
      "assert",
      "for",
      "in",
      "True",
      "False",
      "None",
      "if",
      "else",
    ],
  };
  const list = kw[lang];
  if (list.length === 0) return s;
  const re = new RegExp(`\\b(${list.join("|")})\\b`, "g");
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    out.push(<span style={{ color: COLORS.keyword }}>{m[0]}</span>);
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

function detectLang(path: string): "cpp" | "toml" | "py" {
  if (path.includes(".toml")) return "toml";
  if (path.includes(".py")) return "py";
  return "cpp";
}

// Font + line-height of the rendered code. Constants so the scroll
// math below can convert "lines overflowed" into pixel offsets.
const CODE_FONT_PX = 22;
const CODE_LINE_HEIGHT = 1.6;
const CODE_LINE_PX = CODE_FONT_PX * CODE_LINE_HEIGHT; // 35.2

export function FilePanel({
  path,
  content,
  startFrame = 30,
  style,
  // Cap on how many lines fit in the viewport before scrolling kicks
  // in. Default suits the standard SceneRenderer layout (file panel
  // ~760px tall after the header bar and padding); pass a smaller
  // number if the panel is shorter.
  maxVisibleLines = 18,
}: {
  path: string;
  content: string;
  startFrame?: number;
  style?: React.CSSProperties;
  maxVisibleLines?: number;
}) {
  const frame = useCurrentFrame();
  const lines = content.split("\n");
  const linesPerFrame = 0.6; // ~18 lines/sec
  const elapsed = Math.max(0, frame - startFrame);
  // `visibleFloat` advances every frame; floor() drives the per-line
  // opacity step, the fractional part drives the smooth upward scroll
  // once we exceed the viewport.
  const visibleFloat = Math.min(lines.length, elapsed * linesPerFrame);
  const visibleCount = Math.min(
    lines.length,
    Math.floor(visibleFloat) + 1,
  );
  // Once the revealed content overflows the viewport, slide the inner
  // block up so the latest line stays at the bottom of the view.
  const overflow = Math.max(0, visibleFloat - maxVisibleLines);
  const translateY = -overflow * CODE_LINE_PX;
  const lang = detectLang(path);

  return (
    <div
      style={{
        background: COLORS.panel,
        border: `1px solid ${COLORS.panelBorder}`,
        borderRadius: 14,
        boxShadow: "0 30px 80px rgba(0,0,0,0.4)",
        overflow: "hidden",
        fontFamily: FONT_MONO,
        display: "flex",
        flexDirection: "column",
        ...style,
      }}
    >
      <div
        style={{
          background: "#1d2030",
          padding: "12px 22px",
          fontSize: 22,
          color: COLORS.textDim,
          borderBottom: `1px solid ${COLORS.panelBorder}`,
          fontFamily: FONT_BODY,
          flexShrink: 0,
        }}
      >
        {path}
      </div>
      {/* Viewport: clips overflow so the inner block can translate
          freely without bleeding past the panel borders. */}
      <div
        style={{
          flex: 1,
          overflow: "hidden",
          position: "relative",
        }}
      >
        <div
          style={{
            padding: "20px 28px",
            fontSize: CODE_FONT_PX,
            color: COLORS.text,
            lineHeight: CODE_LINE_HEIGHT,
            whiteSpace: "pre",
            transform: `translateY(${translateY}px)`,
            willChange: "transform",
          }}
        >
          {lines.map((line, i) => {
            const opacity = i < visibleCount ? 1 : 0;
            return (
              <div key={i} style={{ opacity }}>
                {line.length === 0 ? " " : colorizeLine(line, lang)}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
