#include "greet.h"

#include <fmt/core.h>

namespace greet {

std::string hello(const std::string& name) {
    return fmt::format("Hello, {}!", name);
}

}  // namespace greet
