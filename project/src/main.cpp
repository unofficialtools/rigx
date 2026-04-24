#include "greet.h"

#include <iostream>
#include <string>

int main(int argc, char** argv) {
    std::string who = argc > 1 ? argv[1] : "world";
    std::cout << greet::hello(who) << std::endl;
    return 0;
}
