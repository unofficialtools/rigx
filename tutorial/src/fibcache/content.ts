// Content for the "fib cache" tutorial: a C++ key-value store built
// with clang 18, a JavaScript Fibonacci service that caches its results
// in it, a capsule for each, and a Python testbed that checks the first
// 100 Fibonacci numbers.
//
// Every file, command, and output below was run for real against
// rigx on nixos-24.11 (clang 18.1.8, Node.js 20.19.1); `../fib-cache/` is
// this content replayed into a project. Store paths in build output
// are shortened to `./output/<name>`.
//
// Each step is an explainer page (what we're about to do) followed by
// slides. An `edit` slide shows one file in an editor and types `text`
// into it, with a callout per `notes` entry; a `terminal` slide runs
// commands. Scene lengths are derived from this content.

export type Lang = "cpp" | "js" | "toml" | "py" | "txt";

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
      "No compiler, no Node.js, no Python to install: rigx brings pinned versions of every tool we use",
      "Folders for the C++ and JavaScript code, the Python testbed, and test data",
    ],
    slides: [
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx --version" },
          { kind: "out", text: "rigx 0.13.0" },
          { kind: "cmd", text: "mkdir fib-cache && cd fib-cache" },
          { kind: "cmd", text: "mkdir src testbeds data", creates: ["src/", "testbeds/", "data/"] },
        ],
      },
    ],
  },
  {
    title: "Write the key-value store",
    what: "Write a small key-value store in C++: it keeps values in memory and answers requests to save and fetch them over the network.",
    bullets: [
      "Built with exactly clang 18, on any machine, without installing clang",
      "Serves many clients at once",
      "Build it",
    ],
    slides: [
      {
        kind: "edit",
        path: "src/kvstore.cpp",
        lang: "cpp",
        text: `#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <netinet/in.h>
#include <unistd.h>

std::unordered_map<std::string, std::string> store;
std::mutex store_mutex;

std::string handle(const std::string& line) {
    std::istringstream words(line);
    std::string cmd, key, value;
    words >> cmd >> key >> value;
    std::lock_guard<std::mutex> lock(store_mutex);
    if (cmd == "SET") {
        store[key] = value;
        return "OK";
    }
    if (cmd == "GET") {
        auto it = store.find(key);
        return it == store.end() ? "MISS" : it->second;
    }
    if (cmd == "SIZE") return std::to_string(store.size());
    return "ERROR";
}

void serve(int client) {
    std::string pending;
    char chunk[4096];
    ssize_t n;
    while ((n = read(client, chunk, sizeof chunk)) > 0) {
        pending.append(chunk, n);
        size_t end;
        while ((end = pending.find('\\n')) != std::string::npos) {
            std::string line = pending.substr(0, end);
            pending.erase(0, end + 1);
            std::string reply = handle(line) + "\\n";
            write(client, reply.data(), reply.size());
        }
    }
    close(client);
}

int main() {
    int server = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(7000);
    bind(server, (sockaddr*)&addr, sizeof addr);
    listen(server, 64);
    while (true) {
        int client = accept(server, nullptr, nullptr);
        std::thread(serve, client).detach();
    }
}`,
        notes: [
          { at: "#include <mutex>", text: "Only the standard library and POSIX sockets. Nothing to install." },
          { at: "unordered_map<", text: "The store itself: a hash map from string keys to string values, held in memory." },
          { at: "std::mutex", text: "One lock, because every client gets its own thread." },
          { at: "std::string handle", text: "Answers one request: `SET key value`, `GET key`, or `SIZE`." },
          { at: "lock_guard", text: "Hold the lock while touching the map." },
          { at: '"MISS"', text: "A key we've never seen replies `MISS`." },
          { at: "void serve", text: "Talks to one client until it hangs up." },
          { at: "pending.find", text: "Each request ends with a newline. Answer every complete line." },
          { at: "htons(7000)", text: "Listen on TCP port 7000." },
          { at: "std::thread", text: "Accept clients forever, each on its own thread." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        text: `[project]
name    = "fib-cache"
version = "0.1.0"

[nixpkgs]
ref = "nixos-24.11"

[targets.kvstore]
kind     = "executable"
sources  = ["src/kvstore.cpp"]
compiler = "clang18"
cxxflags = ["-std=c++17", "-O2"]`,
        notes: [
          { at: "[project]", text: "The project's name and version." },
          { at: "ref =", text: "Every compiler and library comes from nixpkgs (nixpkgs.org). This pins it to one release." },
          { at: "[targets.kvstore]", text: "A target is one thing rigx builds. This one is named `kvstore`." },
          { at: "kind", text: "Build a program, as opposed to a library, a test, or a container." },
          { at: "sources", text: "The `.cpp` extension tells rigx this is C++." },
          {
            at: "compiler",
            text: "Use clang 18 from the pinned package set instead of the default gcc. Everyone gets the same clang build.",
          },
          { at: "cxxflags", text: "C++17, optimized." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build kvstore" },
          { kind: "out", text: "[rigx] building kvstore" },
          { kind: "out", text: "  kvstore -> ./output/kvstore" },
          { kind: "out", text: "      run:   output/kvstore/bin/kvstore" },
        ],
      },
    ],
  },
  {
    title: "Lock it in with a test",
    what: "Add an automated check that the store remembers what it's told, so a later change can't break it unnoticed.",
    bullets: [
      "Starts the store, saves a value, and reads it back",
      "Runs isolated from your machine, network included",
      "Re-runs only when the store or the test changes",
    ],
    slides: [
      {
        kind: "edit",
        path: "data/session.txt",
        lang: "txt",
        text: `SET hello world
GET hello`,
        notes: [
          { at: "SET hello", text: "Save the value `world` under the key `hello`…" },
          { at: "GET hello", text: "…then ask for it back. The store should reply `OK`, then `world`." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.test_kvstore]
kind          = "test"
deps.internal = ["kvstore"]
deps.nixpkgs  = ["socat", "gnugrep"]
script = """
\${kvstore}/bin/kvstore &
socat - TCP:127.0.0.1:7000,retry=50 < data/session.txt | grep -qx world
"""`,
        notes: [
          { at: '"test"', text: "A check that `rigx test` runs inside a sandbox." },
          { at: "deps.internal", text: "Build `kvstore` first." },
          { at: "deps.nixpkgs", text: "`socat` to talk to the store, `grep` to check the reply. Nothing else is on its PATH." },
          { at: "kvstore &", text: "`${kvstore}` is the built program. Start it in the background." },
          { at: "socat -", text: "Send the session to port 7000. The test passes only if a reply reads exactly `world`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx test test_kvstore" },
          { kind: "out", text: "[rigx test] test_kvstore (sandboxed)" },
          { kind: "out", text: "" },
          { kind: "out", text: "  PASS  test_kvstore" },
          { kind: "out", text: "" },
          { kind: "out", text: "1 passed, 0 failed" },
        ],
      },
    ],
  },
  {
    title: "Write the Fibonacci service",
    what: "Write a web service in JavaScript that computes Fibonacci numbers and remembers every result in the key-value store, so nothing is computed twice.",
    bullets: [
      "Exact answers, even past the limits of 64-bit integers",
      "Node.js comes through rigx: nothing to install",
      "Checked for syntax errors when it's built",
    ],
    slides: [
      {
        kind: "edit",
        path: "src/fib.js",
        lang: "js",
        text: `const http = require("http");
const net = require("net");

const [KV_HOST, KV_PORT] = process.env.KV_7000_ADDR.split(":");

function kv(command) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(KV_PORT, KV_HOST);
    sock.write(command + "\\n");
    let reply = "";
    sock.on("data", (chunk) => {
      reply += chunk;
      if (reply.endsWith("\\n")) {
        sock.end();
        resolve(reply.trim());
      }
    });
    sock.on("error", reject);
  });
}

async function fib(n) {
  const cached = await kv("GET " + n);
  if (cached !== "MISS") return BigInt(cached);
  let value = BigInt(n);
  if (n >= 2) value = (await fib(n - 1)) + (await fib(n - 2));
  await kv("SET " + n + " " + value);
  return value;
}

http.createServer(async (req, res) => {
  const n = Number(req.url.slice(1));
  res.end(String(await fib(n)) + "\\n");
}).listen(process.env.PORT);`,
        notes: [
          { at: 'require("http")', text: "Node's built-in web server and TCP sockets. No npm packages." },
          { at: "KV_7000_ADDR", text: "Where to find the key-value store. The testbed fills this in." },
          { at: "function kv", text: "Sends one command to the store and returns its one-line reply." },
          { at: "sock.write", text: "A command is one line of text, like `GET 42`." },
          { at: "endsWith", text: "A newline ends the reply: hang up and return it." },
          { at: "async function fib", text: "Fibonacci, remembering every result." },
          { at: "cached !==", text: "Already in the store? Use it." },
          { at: "let value", text: "`BigInt` keeps answers exact past 64 bits. fib(0) is 0 and fib(1) is 1." },
          { at: "if (n >= 2)", text: "Otherwise add the two before it. After the first time, those come from the cache." },
          { at: '"SET "', text: "Save the result for next time." },
          { at: "createServer", text: "A request for `/42` gets fib(42) back." },
          { at: ".listen", text: "On the port given in `PORT`." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.fib]
kind           = "custom"
sources        = ["src/fib.js"]
deps.nixpkgs   = ["nodejs"]
build_script   = "node --check src/fib.js"
install_script = "install -D src/fib.js $out/fib.js"`,
        notes: [
          { at: '"custom"', text: "JavaScript isn't compiled, so we spell out the build steps ourselves." },
          { at: "deps.nixpkgs", text: "Node.js from the pinned package set." },
          { at: "build_script", text: "Fail the build if `fib.js` has a syntax error." },
          { at: "install_script", text: "The output is just the script: `output/fib/fib.js`." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build fib" },
          { kind: "out", text: "[rigx] building fib" },
          { kind: "out", text: "  fib -> ./output/fib" },
        ],
      },
    ],
  },
  {
    title: "Package each program as a capsule",
    what: "Turn each program into its own small container, ready to be started and wired together.",
    bullets: [
      "One capsule for the C++ store, one for the JavaScript service",
      "Each holds just its program and what it needs to run, with no base OS",
      "Built from the programs we already have",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.kv_server]
kind          = "capsule"
backend       = "lite"
deps.internal = ["kvstore"]
entrypoint    = "\${kvstore}/bin/kvstore"
ports         = [7000]`,
        notes: [
          { at: '"capsule"', text: "Package a built program as a runnable container." },
          { at: "backend", text: "`lite`: a minimal image with just the program and the libraries it needs. No base OS." },
          { at: "deps.internal", text: "The C++ store goes inside." },
          { at: "entrypoint", text: "What the container runs." },
          { at: "ports", text: "The port the container exposes." },
        ],
      },
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.fib_server]
kind          = "capsule"
backend       = "lite"
deps.internal = ["fib"]
deps.nixpkgs  = ["nodejs"]
entrypoint    = "node \${fib}/fib.js"
env           = { PORT = "8000" }`,
        notes: [
          { at: "[targets.fib_server]", text: "A second capsule, for the JavaScript service." },
          { at: "deps.internal", text: "The packaged script goes inside." },
          { at: "deps.nixpkgs", text: "Node.js to run it: the only tool in the container." },
          { at: "entrypoint", text: "Start the service." },
          { at: "env", text: "The port the service listens on." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx build kv_server fib_server" },
          { kind: "out", text: "[rigx] building kv_server" },
          { kind: "out", text: "[rigx] building fib_server" },
          { kind: "out", text: "  kv_server -> ./output/kv_server" },
          { kind: "out", text: "      start: output/kv_server/bin/run-kv_server" },
          { kind: "out", text: "      shell: output/kv_server/bin/shell-kv_server" },
          { kind: "out", text: "  fib_server -> ./output/fib_server" },
          { kind: "out", text: "      start: output/fib_server/bin/run-fib_server" },
          { kind: "out", text: "      shell: output/fib_server/bin/shell-fib_server" },
        ],
      },
    ],
  },
  {
    title: "Write the orchestrator",
    what: "Write the Python program that runs the system: it starts both capsules, connects the service to its cache, and checks the first 100 Fibonacci numbers.",
    bullets: [
      "Connects the service to the store through the testbed",
      "Asks the service for fib(0) to fib(99) and checks every answer",
      "Confirms that every result ended up in the cache",
      "Python comes through rigx as well; it doesn't need to be installed",
    ],
    slides: [
      {
        kind: "edit",
        path: "testbeds/check.py",
        lang: "py",
        text: `import socket, time, urllib.request
from rigx.capsule import start
from rigx.testbed import Network

net = Network()
net.declare("kv", listens_on=[7000])
net.declare("fib")
net.link("fib", "kv", to_port=7000)

def fib_service(n):
    """Ask the JavaScript capsule for fib(n)."""
    url = f"http://127.0.0.1:8000/{n}"
    for _ in range(100):
        try:
            return int(urllib.request.urlopen(url).read())
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(url)

def kv_size(cap):
    """How many values the C++ capsule holds."""
    with socket.create_connection(cap.host_endpoint(7000)) as s:
        s.sendall(b"SIZE\\n")
        return int(s.makefile().readline())

with net, start("kv_server", **net.bindings("kv")) as kv, \\
          start("fib_server", network="host",
                env=net.bindings("fib")["env"]):
    kv.wait_for_port(7000, timeout=30)

    a, b = 0, 1
    for n in range(100):
        got = fib_service(n)
        assert got == a, f"fib({n}) = {got}, expected {a}"
        a, b = b, a + b
    print(f"OK: fib(0)..fib(99) all correct, fib(99) = {got}")
    print(f"kv_server holds {kv_size(kv)} cached values")`,
        notes: [
          { at: "import Network", text: "`start` runs a built capsule; `Network` wires capsules together." },
          { at: 'declare("kv"', text: "Declare the layout before starting it. The store listens on 7000…" },
          { at: 'declare("fib")', text: "…and the service talks to it." },
          { at: "net.link", text: "Route fib → kv:7000 through the testbed. The service gets the address as `KV_7000_ADDR`." },
          { at: "def fib_service", text: "One web request to the JavaScript service." },
          { at: "except OSError", text: "Node may still be starting: retry for up to 20 seconds." },
          { at: "def kv_size", text: "Ask the C++ store directly how many values it holds." },
          { at: "with net", text: "Bring up the testbed and the store capsule." },
          { at: 'network="host"', text: "The service shares the host's network, so it can reach the testbed's link and serve on port 8000." },
          { at: "env=net", text: "Hand it `KV_7000_ADDR`." },
          { at: "wait_for_port", text: "Wait until the store accepts connections." },
          { at: "a, b = 0, 1", text: "Python works out the right answers alongside…" },
          { at: "assert got", text: "…and all of the first 100 must match." },
          { at: "kv_size(kv)", text: "Every value the service computed is now cached in C++." },
        ],
      },
    ],
  },
  {
    title: "Run the testbed",
    what: "Register the scenario with rigx and run the whole system end to end with one command.",
    bullets: [
      "Both capsules are built before the scenario starts",
      "Two containers start, talk to each other, and are cleaned up afterwards",
      "The output shows whether all 100 answers are right",
      "Running containers is the one thing rigx can't provide: this step needs docker or podman on the machine",
    ],
    slides: [
      {
        kind: "edit",
        path: "rigx.toml",
        lang: "toml",
        append: true,
        text: `
[targets.check]
kind          = "testbed"
deps.internal = ["kv_server", "fib_server"]
deps.nixpkgs  = ["python3"]
script        = "python3 testbeds/check.py"`,
        notes: [
          { at: '"testbed"', text: "A scenario that runs on the host. Start it with `rigx run`." },
          { at: "deps.internal", text: "Build both capsules before the script starts." },
          { at: "deps.nixpkgs", text: "Python for the orchestrator. rigx makes its own module importable." },
          { at: "script", text: "What the scenario runs." },
        ],
      },
      {
        kind: "terminal",
        items: [
          { kind: "cmd", text: "rigx run check" },
          { kind: "out", text: "[rigx] running check" },
          { kind: "out", text: "OK: fib(0)..fib(99) all correct, fib(99) = 218922995834555169026" },
          { kind: "out", text: "kv_server holds 100 cached values" },
        ],
      },
    ],
  },
];
