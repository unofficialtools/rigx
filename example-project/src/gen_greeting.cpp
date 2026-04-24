#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    std::string name = "world";
    std::string out = "greeting.txt";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--name" && i + 1 < argc) {
            name = argv[++i];
        } else if (a == "--out" && i + 1 < argc) {
            out = argv[++i];
        } else {
            std::cerr << "unknown arg: " << a << "\n";
            return 1;
        }
    }
    std::ofstream f(out);
    if (!f) {
        std::cerr << "cannot open " << out << "\n";
        return 1;
    }
    f << "Hello, " << name << "!\n";
    f << "(generated at build time by gen_greeting)\n";
    return 0;
}
