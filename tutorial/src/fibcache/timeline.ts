// Turns the content in content.ts into frame-accurate timelines:
// when each terminal row or editor line starts typing, when each
// callout appears, and how long every scene lasts. Rendering lives in
// Video.tsx.

import { Lang, STEPS, Slide, Step, TermItem } from "./content";

export const FPS = 30;

// Typing speeds in characters per frame.
const CMD_CPF = 1.3;
const EDIT_CPF = 2.6;
const CMD_GAP = 8; // after a command with no output
const EXEC_DELAY = 20; // between a command and its first output line
const OUT_GAP = 4; // between output lines
const LINE_GAP = 3; // the "Enter" between typed editor lines
const EDIT_START = 30; // header fades in, editor scrolls to the end
const HOLD = 75; // let the final state sit on screen

function wordCount(s: string): number {
  return s.split(/\s+/).filter(Boolean).length;
}

// ---------------------------------------------------------------- terminal

export type RowKind = "cmd" | "out" | "comment";

export type Row = {
  kind: RowKind;
  text: string;
  start: number;
  // Frames spent typing (0 = appears instantly).
  typeFrames: number;
};

export type TerminalTimeline = {
  rows: Row[];
  dirs: { path: string; start: number }[];
  total: number;
};

export function terminalTimeline(items: TermItem[]): TerminalTimeline {
  const rows: Row[] = [];
  const dirs: { path: string; start: number }[] = [];
  let t = 20;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const next = items[i + 1];
    if (item.kind === "pause") {
      t += Math.round(item.seconds * FPS);
    } else if (item.kind === "out") {
      rows.push({ kind: "out", text: item.text, start: t, typeFrames: 0 });
      t += next?.kind === "out" ? OUT_GAP : CMD_GAP + 6;
    } else {
      const typeFrames = Math.ceil(item.text.length / CMD_CPF);
      rows.push({ kind: item.kind, text: item.text, start: t, typeFrames });
      t += typeFrames;
      if (item.kind === "cmd") {
        for (const d of item.creates ?? []) dirs.push({ path: d, start: t });
      }
      t += next?.kind === "out" ? EXEC_DELAY : CMD_GAP;
    }
  }
  return { rows, dirs, total: t + HOLD };
}

// ---------------------------------------------------------------- editor

export type EditLine = {
  text: string;
  // Lines already in the file before this slide: shown, not typed.
  existing: boolean;
  start: number;
  typeFrames: number;
};

export type Callout = {
  // 1-based, in order of appearance.
  n: number;
  text: string;
  // Index into `lines`.
  line: number;
  appear: number;
};

export type EditTimeline = {
  path: string;
  lang: Lang;
  append: boolean;
  lines: EditLine[];
  callouts: Callout[];
  firstTyped: number;
  typingEnd: number;
  total: number;
};

export function editTimeline(slide: Extract<Slide, { kind: "edit" }>, before: string): EditTimeline {
  const lines: EditLine[] = [];
  if (slide.append && before) {
    for (const text of before.split("\n")) lines.push({ text, existing: true, start: 0, typeFrames: 0 });
  }
  const firstTyped = lines.length;
  const typed = slide.text.split("\n");
  // Which typed line each note points at.
  const noteAt = new Map<number, string>();
  for (const note of slide.notes) {
    const i = typed.findIndex((l, j) => l.includes(note.at) && !noteAt.has(j));
    if (i < 0) throw new Error(`note ${JSON.stringify(note.at)} matches no line in ${slide.path}`);
    noteAt.set(i, note.text);
  }
  const callouts: Callout[] = [];
  let t = EDIT_START;
  typed.forEach((text, i) => {
    const typeFrames = Math.ceil(text.length / EDIT_CPF);
    lines.push({ text, existing: false, start: t, typeFrames });
    t += typeFrames + LINE_GAP;
    const note = noteAt.get(i);
    if (note !== undefined) {
      callouts.push({ n: callouts.length + 1, text: note, line: firstTyped + i, appear: t });
      // Pause typing so the callout can be read.
      t += 24 + wordCount(note) * 6;
    }
  });
  return {
    path: slide.path,
    lang: slide.lang,
    append: !!slide.append,
    lines,
    callouts,
    firstTyped,
    typingEnd: t,
    total: t + HOLD,
  };
}

// ---------------------------------------------------------------- scenes

// Reading time for an explainer page, proportional to its text.
export function explainerFrames(step: Step): number {
  const words = wordCount(step.what) + step.bullets.reduce((n, b) => n + wordCount(b), 0);
  const seconds = Math.min(14, Math.max(7, 3 + words / 4.5));
  return Math.round(seconds * FPS);
}

export const TITLE_FRAMES = 4 * FPS;
export const ABOUT_FRAMES = 15 * FPS;
export const OVERVIEW_FRAMES = 18 * FPS;
export const FURTHER_FRAMES = 16 * FPS;
export const OUTRO_FRAMES = 9 * FPS;

export type FileStatus =
  | "idle"
  | "writing-new"
  | "writing-edit"
  | "created"
  | "modified"
  | "todo-new"
  | "todo-edit";

export type SceneSpec =
  | { kind: "title"; from: number; frames: number }
  | { kind: "about"; from: number; frames: number }
  | { kind: "overview"; from: number; frames: number }
  | { kind: "explain"; from: number; frames: number; step: number }
  | {
      kind: "edit";
      from: number;
      frames: number;
      step: number;
      tl: EditTimeline;
      // Sidebar state when the slide starts; the slide's own file is
      // overlaid on top while it's being typed.
      files: Map<string, FileStatus>;
      isNew: boolean;
    }
  | {
      kind: "terminal";
      from: number;
      frames: number;
      step: number;
      tl: TerminalTimeline;
      files: Map<string, FileStatus>;
    }
  | { kind: "further"; from: number; frames: number }
  | { kind: "outro"; from: number; frames: number };

// Every path (files and `dir/` entries) that exists once steps
// [0, step) have finished.
export function pathsBefore(step: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < step; i++) {
    for (const slide of STEPS[i].slides) {
      if (slide.kind === "edit") out.push(slide.path);
      else for (const item of slide.items) if (item.kind === "cmd") out.push(...(item.creates ?? []));
    }
  }
  return [...new Set(out)];
}

// Files a step will write, and whether each is new or an edit.
export function filesTouched(step: number): { path: string; isNew: boolean }[] {
  const before = new Set(pathsBefore(step));
  const seen = new Map<string, boolean>();
  for (const slide of STEPS[step].slides) {
    if (slide.kind !== "edit" || seen.has(slide.path)) continue;
    seen.set(slide.path, !before.has(slide.path));
  }
  return [...seen].map(([path, isNew]) => ({ path, isNew }));
}

function buildScenes(): SceneSpec[] {
  const scenes: SceneSpec[] = [];
  // Current contents of every file, for showing appends in context.
  const contents = new Map<string, string>();
  let from = 0;
  const push = (s: SceneSpec) => {
    scenes.push(s);
    from += s.frames;
  };
  push({ kind: "title", from, frames: TITLE_FRAMES });
  push({ kind: "about", from, frames: ABOUT_FRAMES });
  push({ kind: "overview", from, frames: OVERVIEW_FRAMES });
  STEPS.forEach((step, i) => {
    push({ kind: "explain", from, frames: explainerFrames(step), step: i });
    const files = new Map<string, FileStatus>(pathsBefore(i).map((p) => [p, "idle"]));
    for (const slide of step.slides) {
      if (slide.kind === "edit") {
        const before = contents.get(slide.path) ?? "";
        const tl = editTimeline(slide, before);
        const isNew = !contents.has(slide.path);
        push({ kind: "edit", from, frames: tl.total, step: i, tl, files: new Map(files), isNew });
        contents.set(slide.path, slide.append && before ? `${before}\n${slide.text}` : slide.text);
        // Once written in this step, a file keeps its badge for the
        // step's remaining slides. "New" wins over a later edit.
        files.set(slide.path, isNew || files.get(slide.path) === "created" ? "created" : "modified");
      } else {
        const tl = terminalTimeline(slide.items);
        push({ kind: "terminal", from, frames: tl.total, step: i, tl, files: new Map(files) });
        for (const d of tl.dirs) files.set(d.path, "idle");
      }
    }
  });
  push({ kind: "further", from, frames: FURTHER_FRAMES });
  push({ kind: "outro", from, frames: OUTRO_FRAMES });
  return scenes;
}

export const SCENES = buildScenes();
export const TOTAL_FRAMES = SCENES.reduce((n, s) => n + s.frames, 0);

// Final contents of every file the video writes, in the order the
// video writes them. Used to regenerate ../fib-cache/.
export function finalFiles(): Map<string, string> {
  const out = new Map<string, string>();
  for (const step of STEPS) {
    for (const slide of step.slides) {
      if (slide.kind !== "edit") continue;
      const before = out.get(slide.path);
      out.set(slide.path, slide.append && before ? `${before}\n${slide.text}` : slide.text);
    }
  }
  return out;
}
