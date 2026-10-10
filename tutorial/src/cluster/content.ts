// Content for the "matmul cluster" tutorial: hello world → clang 18 →
// Eigen → capsule → multi-capsule testbed driven from Python.
//
// Every file, command, and output below was run for real against
// rigx on nixos-24.11 (clang 18.1.8, Eigen 3.4.0); `../matmul-cluster/`
// is this content replayed into a project. Store paths in build output
// are shortened to `./output/<name>`.
//
// Each step is an explainer page (what we're about to do) followed by
// slides. An `edit` slide shows one file in an editor and types `text`
// into it, with a callout per `notes` entry; a `terminal` slide runs
// commands. Scene lengths are derived from this content.

export type Lang = "cpp" | "toml" | "py" | "txt";

export type Note = {
  // Substring identifying the line the callout points at: the first
  // typed line containing it.
  at: string;
  // `backticks` render as code.
  text: string;
};

export type TermItem =
  // Typed after a `$` prompt. `creates` lists directories (trailing
  // `/`) the command makes, so the sidebar file tree can show them.
  | { kind: "cmd"; text: string; creates?: string[] }
  // Printed instantly, one line every few frames, after the preceding cmd.
  | { kind: "out"; text: string }
  // A typed `# ...` shell comment.
  | { kind: "comment"; text: string }
  // Dead air, e.g. to let a result sit on screen.
  | { kind: "pause"; seconds: number };

export type Slide =
  | {
      kind: "edit";
      path: string;
      lang: Lang;
      // Typed at the end of the file. With `append`, the file's
      // current contents (from earlier slides) are shown first.
      text: string;
      append?: boolean;
      notes: Note[];
    }
  | { kind: "terminal"; items: TermItem[] };

export type Step = {
  title: string;
  // What this step achieves, not how. `backticks` render as code.
  what: string;
  bullets: string[];
  slides: Slide[];
};

export const STEPS: Step[] = [
  {
    title: "Start a project",
    what: "This tutorial assumes nothing on your machine except a shell, a text editor, and rigx. We start with an empty folder.",
    bullets: [
      "No compiler, no Eigen, no Python to install: rigx brings pinned versions of every tool we use",
      "Folders for the C++ code, the Python orchestrator, and sample data",
    ],
    slides: [
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx --version" },
          { kind: "out", text: "rigx 0.12.0" },
          { kind: "cmd", text: "mkdir matmul-cluster && cd matmul-cluster" },
          { kind: "cmd", text: "mkdir src testbeds data", creates: ["src/", "testbeds/", "data/"] },
        ],
      },
    ],
  },
  {
    title: "Hello, world",
    what: "Build and run the smallest possible C++ program, using whatever compiler rigx picks by default.",
    bullets: [
      "A one-file program that prints a greeting and the compiler that built it",
      "A project file that tells rigx about it",
      "Build it, run it, and see which compiler we got",
    ],
    slides: [
      {
        kind: "edit",
        path: "src/hello.cpp",
        lang: "cpp",
        text: `#include <iostream>

int main() {
    std::cout << "Hello, world! Built with " << __VERSION__ << "\\n";
}`,
        notes: [
          { at: "#include", text: "Standard streams, for printing." },
          { at: "__VERSION__", text: "Every compiler fills in `__VERSION__` with its own version, so the program reports what built it." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        text: `[project]
name    = "matmul-cluster"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"

[targets.hello]
kind    = "executable"
sources = ["src/hello.cpp"]`,
        notes: [
          { at: "[project]", text: "The project's name and version." },
          { at: "ref =", text: "Every compiler and library comes from nixpkgs (nixpkgs.org). This pins it to one release." },
          { at: "[targets.hello]", text: "A target is one thing rigx builds. This one is named `hello`." },
          { at: "kind", text: "Build a program, as opposed to a library, a test, or a container." },
          { at: "sources", text: "The `.cpp` extension tells rigx this is C++." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello" },
          { kind: "out", text: "[rigx] building hello" },
          { kind: "out", text: "  hello -> ./output/hello" },
          { kind: "out", text: "      run:   output/hello/bin/hello" },
          { kind: "cmd", text: "./output/hello/bin/hello" },
          { kind: "out", text: "Hello, world! Built with 13.3.0" },
        ],
      },
    ],
  },
  {
    title: "Pin a specific clang",
    what: "Make every build of the program use exactly clang 18, on any machine, without installing clang.",
    bullets: [
      "Switch the hello program from the default compiler to clang 18",
      "Rebuild and confirm which compiler made the binary",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `compiler = "clang18"`,
        notes: [
          {
            at: "compiler",
            text: "Use clang 18 from the pinned package set instead of the default gcc. Everyone gets the same clang build.",
          },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build hello" },
          { kind: "out", text: "[rigx] building hello" },
          { kind: "out", text: "  hello -> ./output/hello" },
          { kind: "cmd", text: "./output/hello/bin/hello" },
          { kind: "out", text: "Hello, world! Built with Clang 18.1.8" },
        ],
      },
    ],
  },
  {
    title: "Multiply matrices with Eigen",
    what: "Write the worker program: it reads two matrices, multiplies them with the Eigen library, and prints the product.",
    bullets: [
      "Built with the same clang 18. Eigen comes through rigx too, so there's nothing to install",
      "A small sample input to try it on",
      "Run it and check the answer by hand",
    ],
    slides: [
      {
        kind: "edit",
        path: "src/matmul.cpp",
        lang: "cpp",
        text: `// Reads "m k n", then A (m×k) and B (k×n) from stdin; prints C = A·B.
#include <eigen3/Eigen/Dense>
#include <iostream>

int main() {
    int m, k, n;
    if (!(std::cin >> m >> k >> n)) return 1;
    Eigen::MatrixXd A(m, k), B(k, n);
    for (int i = 0; i < m; ++i)
        for (int j = 0; j < k; ++j) std::cin >> A(i, j);
    for (int i = 0; i < k; ++i)
        for (int j = 0; j < n; ++j) std::cin >> B(i, j);

    Eigen::MatrixXd C = A * B;
    std::cout << m << " " << n << "\\n"
              << C.format(Eigen::IOFormat(Eigen::FullPrecision)) << "\\n";
}`,
        notes: [
          { at: "Eigen/Dense", text: "Eigen's dense matrices. nixpkgs installs the headers under `eigen3/`." },
          { at: "int m, k, n", text: "A is m×k and B is k×n, so C is m×n." },
          { at: "return 1", text: "Read the sizes first. Exit with an error on bad input." },
          { at: "MatrixXd A", text: "Matrices of doubles, sized at run time." },
          { at: "std::cin >> A", text: "Fill A row by row from stdin…" },
          { at: "std::cin >> B", text: "…then B, the same way." },
          { at: "= A * B", text: "The multiplication itself. Eigen picks a vectorized kernel." },
          { at: "std::cout << m", text: "Reply in the same format: the size of C, then its rows." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.matmul]
kind         = "executable"
sources      = ["src/matmul.cpp"]
compiler     = "clang18"
cxxflags     = ["-std=c++17", "-O2", "-march=x86-64-v3"]
deps.nixpkgs = ["eigen"]`,
        notes: [
          { at: "[targets.matmul]", text: "A second program: the matrix worker." },
          { at: "compiler", text: "The same pinned clang as hello." },
          { at: "cxxflags", text: "C++17, optimized, and allowed to use AVX2 (`x86-64-v3`)." },
          { at: "deps.nixpkgs", text: "Eigen from the pinned package set. rigx puts its headers on the include path." },
        ],
      },
      {
        kind: "edit",
        path: "data/small.txt",
        lang: "txt",
        text: `2 2 2
1 2
3 4
5 6
7 8`,
        notes: [
          { at: "2 2 2", text: "m k n: a 2×2 matrix times a 2×2 matrix." },
          { at: "1 2", text: "A, one row per line." },
          { at: "5 6", text: "B, one row per line." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build matmul" },
          { kind: "out", text: "[rigx] building matmul" },
          { kind: "out", text: "  matmul -> ./output/matmul" },
          { kind: "cmd", text: "./output/matmul/bin/matmul < data/small.txt" },
          { kind: "out", text: "2 2" },
          { kind: "out", text: "19 22" },
          { kind: "out", text: "43 50" },
        ],
      },
    ],
  },
  {
    title: "Lock it in with a test",
    what: "Add an automated check that the multiplication is right, so a later change can't break it unnoticed.",
    bullets: [
      "Runs the worker on the sample input and expects the known product",
      "Runs isolated from your machine",
      "Re-runs only when the worker or the test changes",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.test_matmul]
kind          = "test"
deps.internal = ["matmul"]
deps.nixpkgs  = ["gnugrep"]
script = """
\${matmul}/bin/matmul < data/small.txt | grep -qx '43 50'
"""`,
        notes: [
          { at: '"test"', text: "A check that `rigx test` runs inside a sandbox." },
          { at: "deps.internal", text: "Build `matmul` first." },
          { at: "deps.nixpkgs", text: "Tools the script may use. Nothing else is on its PATH." },
          { at: "grep -qx", text: "`${matmul}` is the built program. The test passes only if a line reads exactly `43 50`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx test test_matmul" },
          { kind: "out", text: "[rigx test] test_matmul (sandboxed)" },
          { kind: "out", text: "" },
          { kind: "out", text: "  PASS  test_matmul" },
          { kind: "out", text: "" },
          { kind: "out", text: "1 passed, 0 failed" },
        ],
      },
    ],
  },
  {
    title: "Package it as a capsule",
    what: "Turn the worker into a tiny container that answers multiplication requests over the network.",
    bullets: [
      "One container image that we can start as many times as we like",
      "Each network connection is one multiplication",
      "Small: just the worker and the libraries it needs, with no base OS",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.matmul_worker]
kind          = "capsule"
backend       = "lite"
deps.internal = ["matmul"]
deps.nixpkgs  = ["socat"]
entrypoint    = "socat TCP-LISTEN:7000,fork EXEC:\${matmul}/bin/matmul"
ports         = [7000]`,
        notes: [
          { at: '"capsule"', text: "Package a built program as a runnable container." },
          { at: "backend", text: "`lite`: a minimal image with just the program and the libraries it needs. No base OS." },
          { at: "deps.internal", text: "The worker program goes inside." },
          { at: "deps.nixpkgs", text: "`socat` is the container's only other tool." },
          { at: "entrypoint", text: "Listen on 7000. Each connection starts one `matmul`, with the socket as its stdin and stdout." },
          { at: "ports", text: "The port the container exposes." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build matmul_worker" },
          { kind: "out", text: "[rigx] building matmul_worker" },
          { kind: "out", text: "  matmul_worker -> ./output/matmul_worker" },
          { kind: "out", text: "      start: output/matmul_worker/bin/run-matmul_worker" },
          { kind: "out", text: "      shell: output/matmul_worker/bin/shell-matmul_worker" },
        ],
      },
    ],
  },
  {
    title: "Write the orchestrator",
    what: "Write the Python program that runs the cluster: it starts three workers, gives each a share of the work, and puts the answer back together.",
    bullets: [
      "Starts three copies of the worker container",
      "Gives each worker a different slice of the rows of A",
      "Stacks the partial results into C and checks them",
      "Python comes through rigx as well; it doesn't need to be installed",
    ],
    slides: [
      {
        kind: "edit",
        path: "testbeds/orchestrate.py",
        lang: "py",
        text: `import random, socket
from contextlib import ExitStack
from rigx.capsule import start
from rigx.testbed import Network

N, WORKERS = 6, ["w1", "w2", "w3"]

def remote_matmul(cap, A, B):
    """Send A and B to one worker capsule, read back A·B."""
    m, k, n = len(A), len(B), len(B[0])
    nums = " ".join(str(x) for row in A + B for x in row)
    with socket.create_connection(cap.host_endpoint(7000)) as s:
        s.sendall(f"{m} {k} {n}\\n{nums}\\n".encode())
        s.shutdown(socket.SHUT_WR)
        out = s.makefile().read().split()
    rows, cols = int(out[0]), int(out[1])
    vals = [float(v) for v in out[2:]]
    return [vals[i * cols:(i + 1) * cols] for i in range(rows)]

A = [[random.randint(-9, 9) for _ in range(N)] for _ in range(N)]
B = [[random.randint(-9, 9) for _ in range(N)] for _ in range(N)]

net = Network()
for w in WORKERS:
    net.declare(w, listens_on=[7000])

with net, ExitStack() as stack:
    starts = [start("matmul_worker", **net.bindings(w)) for w in WORKERS]
    caps = [stack.enter_context(s) for s in starts]
    for cap in caps:
        cap.wait_for_port(7000, timeout=30)

    step = N // len(caps)
    C = []
    for i, (name, cap) in enumerate(zip(WORKERS, caps)):
        rows = A[i * step:(i + 1) * step]
        C += remote_matmul(cap, rows, B)
        print(f"[{name}] rows {i*step}..{(i+1)*step-1} done")

    expected = [[sum(a * b for a, b in zip(r, c)) for c in zip(*B)]
                for r in A]
    assert C == expected
    print(f"OK: {N}x{N} product assembled from {len(caps)} capsules")`,
        notes: [
          { at: "import Network", text: "`start` runs a built capsule; `Network` connects capsules to host ports." },
          { at: "N, WORKERS", text: "6×6 matrices, split across three workers." },
          { at: "def remote_matmul", text: "Sends one multiplication to one worker and returns the result." },
          { at: "nums =", text: "The same text format `matmul` reads: the sizes, then A, then B." },
          { at: "create_connection", text: "Connect to the host port that maps to the worker's port 7000." },
          { at: "SHUT_WR", text: "Signal that the input is complete, then read the whole reply." },
          { at: "net = Network()", text: "Declare the layout before starting it: three workers, each listening on 7000." },
          { at: "with net", text: "Bring the testbed up." },
          { at: "starts = [", text: "The same capsule, three times: one per worker." },
          { at: "caps = [", text: "Start them all. `ExitStack` stops every container on the way out." },
          { at: "wait_for_port", text: "Wait until every worker accepts connections." },
          { at: "rows = A[", text: "Each worker gets two rows of A, plus all of B." },
          { at: "C += remote_matmul", text: "Partial results stack directly into C." },
          { at: "assert C", text: "Recompute the product in plain Python and compare." },
        ],
      },
    ],
  },
  {
    title: "Run the testbed",
    what: "Register the scenario with rigx and run the whole cluster end to end with one command.",
    bullets: [
      "The worker container is built before the scenario starts",
      "Three containers start, do their share, and are cleaned up afterwards",
      "The output shows which worker did what, and whether the product is correct",
      "Running containers is the one thing rigx can't provide: this step needs docker or podman on the machine",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.cluster]
kind          = "testbed"
deps.internal = ["matmul_worker"]
deps.nixpkgs  = ["python3"]
script        = "python3 testbeds/orchestrate.py"`,
        notes: [
          { at: '"testbed"', text: "A scenario that runs on the host. Start it with `rigx run`." },
          { at: "deps.internal", text: "Build the capsule before the script starts." },
          { at: "deps.nixpkgs", text: "Python for the orchestrator. rigx makes its own module importable." },
          { at: "script", text: "What the scenario runs." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx run cluster" },
          { kind: "out", text: "[rigx] running cluster" },
          { kind: "out", text: "[w1] rows 0..1 done" },
          { kind: "out", text: "[w2] rows 2..3 done" },
          { kind: "out", text: "[w3] rows 4..5 done" },
          { kind: "out", text: "OK: 6x6 product assembled from 3 capsules" },
        ],
      },
    ],
  },
];
