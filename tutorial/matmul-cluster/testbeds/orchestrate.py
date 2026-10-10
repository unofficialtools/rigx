import random, socket
from contextlib import ExitStack
from rigx.capsule import start
from rigx.testbed import Network

N, WORKERS = 6, ["w1", "w2", "w3"]

def remote_matmul(cap, A, B):
    """Send A and B to one worker capsule, read back A·B."""
    m, k, n = len(A), len(B), len(B[0])
    nums = " ".join(str(x) for row in A + B for x in row)
    with socket.create_connection(cap.host_endpoint(7000)) as s:
        s.sendall(f"{m} {k} {n}\n{nums}\n".encode())
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
    print(f"OK: {N}x{N} product assembled from {len(caps)} capsules")
