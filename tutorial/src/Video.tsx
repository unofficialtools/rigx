import React from "react";
import { AbsoluteFill, Sequence, useCurrentFrame, interpolate } from "remotion";
import { SCENES } from "./content";
import {
  COLORS,
  FilePanel,
  SceneCaption,
  SceneTitle,
  TerminalPanel,
} from "./components";

const FPS = 30;

// Convert each scene's `seconds` into a frame count.
function sceneFrames(scene: (typeof SCENES)[number]): number {
  return Math.round(scene.seconds * FPS);
}

// Intro is one sustained "card" — long enough for a viewer to scan
// the bulleted feature list below the wordmark. Fade in, hold, fade
// out; the keyframes inside <Intro/> match this length.
const INTRO_FRAMES = 360; // 12s @ 30fps
const OUTRO_FRAMES = 60;

export const TOTAL_FRAMES =
  SCENES.reduce((sum, s) => sum + sceneFrames(s), 0) +
  INTRO_FRAMES +
  OUTRO_FRAMES;

function Background() {
  return (
    <AbsoluteFill
      style={{
        background:
          `radial-gradient(circle at 20% 0%, #1a1f33 0%, ${COLORS.bg} 60%)`,
      }}
    />
  );
}

function Watermark() {
  return (
    <div
      style={{
        position: "absolute",
        bottom: 36,
        right: 56,
        fontSize: 22,
        color: COLORS.comment,
        fontFamily: '"Inter", system-ui',
        letterSpacing: 2,
      }}
    >
      rigx · build a C++ project from scratch
    </div>
  );
}

// One bullet in the intro card: a small accent dot, a bold lead-in,
// and the rest of the line in the dimmer body color. Bullets are
// staggered so the eye is led down the list rather than dumped on it
// all at once.
function IntroBullet({
  index,
  lead,
  rest,
}: {
  index: number;
  lead: string;
  rest: React.ReactNode;
}) {
  const frame = useCurrentFrame();
  // First bullet at frame 60 (after wordmark + tagline land); each
  // subsequent bullet 12 frames later. Total stagger ~2.4s for 6
  // bullets — tight enough to feel intentional, slow enough to track.
  const start = 60 + index * 12;
  const opacity = interpolate(frame, [start, start + 18], [0, 1], {
    extrapolateRight: "clamp",
  });
  const dx = interpolate(frame, [start, start + 18], [-10, 0], {
    extrapolateRight: "clamp",
  });
  return (
    <li
      style={{
        display: "flex",
        gap: 22,
        alignItems: "baseline",
        opacity,
        transform: `translateX(${dx}px)`,
      }}
    >
      <span
        style={{
          color: COLORS.prompt,
          fontWeight: 700,
          fontFamily: '"JetBrains Mono", monospace',
        }}
      >
        ›
      </span>
      <span style={{ color: COLORS.textDim }}>
        <strong style={{ color: COLORS.text, fontWeight: 600 }}>
          {lead}
        </strong>{" "}
        {rest}
      </span>
    </li>
  );
}

function Intro() {
  const frame = useCurrentFrame();
  const opacity = interpolate(
    frame,
    [0, 30, INTRO_FRAMES - 30, INTRO_FRAMES],
    [0, 1, 1, 0],
    { extrapolateRight: "clamp" },
  );
  const dy = interpolate(frame, [0, 30], [12, 0]);
  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        opacity,
        transform: `translateY(${dy}px)`,
        padding: "60px 240px",
      }}
    >
      <div
        style={{
          fontSize: 120,
          fontWeight: 800,
          color: COLORS.text,
          fontFamily: '"Inter", system-ui',
          letterSpacing: -3,
          lineHeight: 1,
        }}
      >
        rigx
      </div>
      <div
        style={{
          marginTop: 18,
          fontSize: 30,
          color: COLORS.textDim,
          fontFamily: '"Inter", system-ui',
          fontStyle: "italic",
        }}
      >
        A declarative, Nix-powered build system. Like Bazel — for humans.
      </div>

      <ul
        style={{
          marginTop: 48,
          listStyle: "none",
          padding: 0,
          fontSize: 28,
          lineHeight: 1.55,
          fontFamily: '"Inter", system-ui',
          maxWidth: 1280,
          width: "100%",
          textAlign: "left",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        <IntroBullet
          index={0}
          lead="One TOML file."
          rest="No scripting, no DSL, no makefile macros."
        />
        <IntroBullet
          index={1}
          lead="Multi-language."
          rest="C, C++, Go, Rust, Zig, Nim, Python — toolchain auto-pulled per language from a pinned nixpkgs."
        />
        <IntroBullet
          index={2}
          lead="Sandboxed and reproducible."
          rest="Builds and tests run hermetic; outputs are content-addressed and cached locally and across machines."
        />
        <IntroBullet
          index={3}
          lead="Cross-compile with one knob."
          rest={
            <>
              <code style={{ fontFamily: '"JetBrains Mono", monospace', color: COLORS.func }}>
                target = "aarch64-linux"
              </code>{" "}
              and rigx routes c/cxx/go/zig/nim through the right cross-toolchain.
            </>
          }
        />
        <IntroBullet
          index={4}
          lead="Capsules."
          rest="Package any built artifact as a kilobyte container, a NixOS-systemd container, or a full qemu VM."
        />
        <IntroBullet
          index={5}
          lead="Testbeds."
          rest="Wire multiple capsules together and inject latency, drops, corruption, or partitions — assertions in plain Python."
        />
      </ul>

      <div
        style={{
          marginTop: 40,
          fontSize: 28,
          color: COLORS.text,
          fontFamily: '"Inter", system-ui',
          fontWeight: 500,
        }}
      >
        Let's start a simple C++ project …
      </div>
    </AbsoluteFill>
  );
}

function Outro() {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill
      style={{
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        opacity,
      }}
    >
      <div
        style={{
          fontSize: 56,
          fontWeight: 700,
          color: COLORS.text,
          fontFamily: '"Inter", system-ui',
        }}
      >
        That's the whole loop.
      </div>
      <div
        style={{
          marginTop: 18,
          fontSize: 28,
          color: COLORS.textDim,
          fontFamily: '"Inter", system-ui',
        }}
      >
        Sources, build, test, cross-compile, capsule, testbed — one rigx.toml.
      </div>
    </AbsoluteFill>
  );
}

function SceneFrame({ children }: { children: React.ReactNode }) {
  return (
    <AbsoluteFill>
      <Background />
      <Watermark />
      {children}
    </AbsoluteFill>
  );
}

function SceneRenderer({ scene }: { scene: (typeof SCENES)[number] }) {
  const titleStart = 30;
  if (scene.kind === "terminal") {
    return (
      <SceneFrame>
        <SceneTitle text={scene.title} />
        <SceneCaption text={scene.caption} />
        <div
          style={{
            position: "absolute",
            top: 240,
            left: 80,
            right: 80,
            bottom: 80,
          }}
        >
          <TerminalPanel
            lines={scene.lines}
            startFrame={titleStart}
            style={{ height: "100%", boxSizing: "border-box" }}
          />
        </div>
      </SceneFrame>
    );
  }
  if (scene.kind === "file") {
    return (
      <SceneFrame>
        <SceneTitle text={scene.title} />
        <SceneCaption text={scene.caption} />
        <div
          style={{
            position: "absolute",
            top: 240,
            left: 80,
            right: 80,
            bottom: 80,
          }}
        >
          <FilePanel
            path={scene.path}
            content={scene.content}
            startFrame={titleStart}
            style={{ height: "100%", boxSizing: "border-box" }}
          />
        </div>
      </SceneFrame>
    );
  }
  // split: file on the left, terminal on the right.
  return (
    <SceneFrame>
      <SceneTitle text={scene.title} />
      <SceneCaption text={scene.caption} />
      <div
        style={{
          position: "absolute",
          top: 240,
          left: 80,
          right: 80,
          bottom: 80,
          display: "flex",
          gap: 32,
        }}
      >
        <div style={{ flex: 1.1 }}>
          <FilePanel
            path={scene.file.path}
            content={scene.file.content}
            startFrame={titleStart}
            style={{ height: "100%", boxSizing: "border-box" }}
          />
        </div>
        <div style={{ flex: 0.9 }}>
          <TerminalPanel
            lines={scene.terminal}
            startFrame={titleStart + 60}
            style={{ height: "100%", boxSizing: "border-box" }}
          />
        </div>
      </div>
    </SceneFrame>
  );
}

export const Video: React.FC = () => {
  let cursor = 0;
  const sequences: React.ReactElement[] = [];
  sequences.push(
    <Sequence key="intro" from={cursor} durationInFrames={INTRO_FRAMES}>
      <Intro />
    </Sequence>,
  );
  cursor += INTRO_FRAMES;

  for (let i = 0; i < SCENES.length; i++) {
    const scene = SCENES[i];
    const dur = sceneFrames(scene);
    sequences.push(
      <Sequence
        key={`scene-${i}`}
        from={cursor}
        durationInFrames={dur}
      >
        <SceneRenderer scene={scene} />
      </Sequence>,
    );
    cursor += dur;
  }

  sequences.push(
    <Sequence key="outro" from={cursor} durationInFrames={OUTRO_FRAMES}>
      <SceneFrame>
        <Outro />
      </SceneFrame>
    </Sequence>,
  );

  return <AbsoluteFill style={{ background: COLORS.bg }}>{sequences}</AbsoluteFill>;
};
