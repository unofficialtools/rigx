const http = require("http");
const net = require("net");

const [KV_HOST, KV_PORT] = process.env.KV_7000_ADDR.split(":");

function kv(command) {
  return new Promise((resolve, reject) => {
    const sock = net.connect(KV_PORT, KV_HOST);
    sock.write(command + "\n");
    let reply = "";
    sock.on("data", (chunk) => {
      reply += chunk;
      if (reply.endsWith("\n")) {
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
  res.end(String(await fib(n)) + "\n");
}).listen(process.env.PORT);
