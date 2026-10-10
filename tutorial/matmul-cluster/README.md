# matmul-cluster

The finished project from the `rigx-matmul-cluster` video
(`../src/cluster/`). It goes from a hello world to a C++ Eigen
matrix-multiply worker built with a pinned clang 18, packaged as a
capsule, and started three times by a Python testbed that splits the
work across them.

The files are generated from `../src/cluster/content.ts` (`finalFiles()`
in `timeline.ts`), so they match what the video types out.

```sh
rigx build hello && ./output/hello/bin/hello        # Hello, world! Built with Clang 18.1.8
rigx build matmul
./output/matmul/bin/matmul < data/small.txt
rigx test test_matmul                               # sandboxed
rigx run cluster                                    # needs docker or podman
```

`rigx run cluster` builds the `matmul_worker` capsule, starts three
copies, sends each a slice of A plus all of B over TCP port 7000, checks
the assembled product, and removes the containers on exit:

```
[w1] rows 0..1 done
[w2] rows 2..3 done
[w3] rows 4..5 done
OK: 6x6 product assembled from 3 capsules
```

You need only a shell, a text editor, and rigx. clang, Eigen, socat and
Python all come from nixpkgs through rigx, and no git repository is
needed. Running the cluster also needs docker or podman.

Pinned to nixos-24.11 (clang 18.1.8, Eigen 3.4.0) through `flake.lock`.
