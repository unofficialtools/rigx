import React from "react";
import { Composition } from "remotion";
import { TOTAL_FRAMES, Video } from "./Video";
import { ClusterVideo } from "./cluster/Video";
import { TOTAL_FRAMES as CLUSTER_FRAMES } from "./cluster/timeline";
import { FibCacheVideo } from "./fibcache/Video";
import { TOTAL_FRAMES as FIBCACHE_FRAMES } from "./fibcache/timeline";

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="rigx-cpp-tutorial"
        component={Video}
        durationInFrames={TOTAL_FRAMES}
        fps={30}
        width={1920}
        height={1080}
      />
      <Composition
        id="rigx-matmul-cluster"
        component={ClusterVideo}
        durationInFrames={CLUSTER_FRAMES}
        fps={30}
        width={1920}
        height={1080}
      />
      <Composition
        id="rigx-fib-cache"
        component={FibCacheVideo}
        durationInFrames={FIBCACHE_FRAMES}
        fps={30}
        width={1920}
        height={1080}
      />
    </>
  );
};
