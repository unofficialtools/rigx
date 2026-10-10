#include "greet.h"
#include <iostream>

int main(int argc, char** argv) {
    std::cout << greet(argc > 1 ? argv[1] : "world") << "\n";
}
