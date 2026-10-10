#include <mutex>
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
        while ((end = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, end);
            pending.erase(0, end + 1);
            std::string reply = handle(line) + "\n";
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
}
