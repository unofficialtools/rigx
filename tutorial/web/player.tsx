// Browser entry for the hosted tutorials: plays a composition live with
// @remotion/player (no video file), with a chapter list that seeks to
// each step. The page picks the tutorial with <script data-video="…">.

import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Player, PlayerRef } from "@remotion/player";
import { HelloCppVideo } from "../src/hellocpp/Video";
import * as hellocpp from "../src/hellocpp/timeline";
import { STEPS as HELLOCPP_STEPS } from "../src/hellocpp/content";
import { ClusterVideo } from "../src/cluster/Video";
import * as cluster from "../src/cluster/timeline";
import { STEPS as CLUSTER_STEPS } from "../src/cluster/content";
import { FibCacheVideo } from "../src/fibcache/Video";
import * as fibcache from "../src/fibcache/timeline";
import { STEPS as FIBCACHE_STEPS } from "../src/fibcache/content";

type Scene = { kind: string; from: number; step?: number };

const VIDEOS = {
  "hello-cpp": { component: HelloCppVideo, scenes: hellocpp.SCENES, total: hellocpp.TOTAL_FRAMES, steps: HELLOCPP_STEPS },
  "matmul-cluster": { component: ClusterVideo, scenes: cluster.SCENES, total: cluster.TOTAL_FRAMES, steps: CLUSTER_STEPS },
  "fib-cache": { component: FibCacheVideo, scenes: fibcache.SCENES, total: fibcache.TOTAL_FRAMES, steps: FIBCACHE_STEPS },
};

const FPS = 30;

function chapters(scenes: Scene[], steps: { title: string }[]) {
  const out: { label: string; from: number }[] = [];
  for (const s of scenes) {
    if (s.kind === "about") out.push({ label: "What is rigx", from: s.from });
    if (s.kind === "overview") out.push({ label: "What we'll build", from: s.from });
    if (s.kind === "explain") out.push({ label: `${s.step! + 1}. ${steps[s.step!].title}`, from: s.from });
    if (s.kind === "further") out.push({ label: "Going further", from: s.from });
  }
  return out;
}

function clock(frame: number): string {
  const s = Math.floor(frame / FPS);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function App({ id }: { id: keyof typeof VIDEOS }) {
  const v = VIDEOS[id];
  const ref = useRef<PlayerRef>(null);
  const [frame, setFrame] = useState(0);
  const list = chapters(v.scenes as Scene[], v.steps);

  useEffect(() => {
    const p = ref.current;
    if (!p) return;
    const onFrame = (e: { detail: { frame: number } }) => setFrame(e.detail.frame);
    p.addEventListener("frameupdate", onFrame);
    return () => p.removeEventListener("frameupdate", onFrame);
  }, []);

  const current = list.reduce((i, c, j) => (c.from <= frame ? j : i), -1);

  return (
    <>
      <Player
        ref={ref}
        component={v.component}
        durationInFrames={v.total}
        fps={FPS}
        compositionWidth={1920}
        compositionHeight={1080}
        controls
        allowFullscreen
        clickToPlay
        spaceKeyToPlayOrPause
        acknowledgeRemotionLicense
        // Frame 0 is the title still fading in from black.
        initialFrame={50}
        style={{ width: "100%", aspectRatio: "16 / 9", borderRadius: 12, overflow: "hidden" }}
      />
      <ol className="chapters">
        {list.map((c, i) => (
          <li key={c.from}>
            <button
              className={i === current ? "on" : ""}
              onClick={() => {
                ref.current?.seekTo(c.from);
                ref.current?.play();
              }}
            >
              <span className="t">{clock(c.from)}</span> {c.label}
            </button>
          </li>
        ))}
      </ol>
    </>
  );
}

const script = document.currentScript as HTMLScriptElement;
const id = script.dataset.video as keyof typeof VIDEOS;
createRoot(document.getElementById("player")!).render(<App id={id} />);
