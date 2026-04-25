#include <stdio.h>

int main(int argc, char **argv) {
    const char *who = (argc > 1) ? argv[1] : "world";
    printf("Hello, %s! (from C)\n", who);
    return 0;
}
