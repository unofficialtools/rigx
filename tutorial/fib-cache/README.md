# fib-cache

The finished project from the `rigx-fib-cache` video (`../src/fibcache/`).
It has two programs, each packaged as a capsule:

- a C++ key-value store built with a pinned clang 18;
- a JavaScript Fibonacci service that caches every result in the store.

A Python testbed starts both capsules and checks the first 100
Fibonacci numbers.

The files are generated from `../src/fibcache/content.ts` (`finalFiles()`
in `timeline.ts`), so they match what the video types out.

```sh
rigx build kvstore
rigx test test_kvstore                 # sandboxed: SET hello world, GET hello
rigx build fib                         # node --check, then installs fib.js
rigx run check                         # needs docker or podman
```

`rigx run check` builds both capsules and starts them. The service
reaches the store through a testbed link (`net.link("fib", "kv",
to_port=7000)`), which hands it the address as `KV_7000_ADDR`. The
script then asks for fib(0)..fib(99), checks every answer, and removes
the containers on exit:

```
OK: fib(0)..fib(99) all correct, fib(99) = 218922995834555169026
kv_server holds 100 cached values
```

The service runs with `network="host"`. Under docker's default bridge
networking, `127.0.0.1` inside a container is the container itself, so
a bridged capsule can't reach the testbed's link proxy on the host.

You need only a shell, a text editor, and rigx. clang, Node.js, socat
and Python all come from nixpkgs through rigx, and no git repository is
needed. Running the testbed also needs docker or podman.

Pinned to nixos-24.11 (clang 18.1.8, Node.js 20.19.1) through `flake.lock`.
