import socket, time, urllib.request
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
        s.sendall(b"SIZE\n")
        return int(s.makefile().readline())

with net, start("kv_server", **net.bindings("kv")) as kv, \
          start("fib_server", network="host",
                env=net.bindings("fib")["env"]):
    kv.wait_for_port(7000, timeout=30)

    a, b = 0, 1
    for n in range(100):
        got = fib_service(n)
        assert got == a, f"fib({n}) = {got}, expected {a}"
        a, b = b, a + b
    print(f"OK: fib(0)..fib(99) all correct, fib(99) = {got}")
    print(f"kv_server holds {kv_size(kv)} cached values")
