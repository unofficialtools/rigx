# hello-cpp

The finished project from the `rigx-cpp-tutorial` video (`../src/hellocpp/`).
It has a small C++ library and a program that use fmt. From that one
program it builds:

- a zip to share;
- a sandboxed test;
- an ARM (aarch64) cross-build, checked by its own test;
- a capsule, checked by a host-side test that runs it.

The files are generated from `../src/hellocpp/content.ts` (`finalFiles()`
in `timeline.ts`), so they match what the video types out.

```sh
rigx build hello && ./output/hello/bin/hello rigx   # Hello, rigx!
rigx build hello_zip                                # output/hello_zip/hello.zip
rigx build hello_arm64                              # cross-compiled, fmt included
rigx build hello_capsule
./output/hello_capsule/bin/run-hello_capsule        # Hello, capsule!  (needs docker or podman)
rigx test                                           # test_hello, test_arm64, test_capsule
```

The first ARM build compiles an aarch64 fmt from source (a few minutes),
because the binary cache doesn't have one. Later builds are cached.

You need only a shell, a text editor, and rigx. The compiler, fmt, zip,
file and the ARM cross-compiler all come from nixpkgs through rigx, and
no git repository is needed. Running the capsule also needs docker or
podman.

Pinned to nixos-24.11 (gcc 13.3.0, fmt 10.2.1) through `flake.lock`.
Needs rigx 0.13.0 or later. Earlier versions linked the x86_64 fmt into
the ARM build, and didn't substitute `${hello_capsule}` in host-side
tests.
