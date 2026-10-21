#pragma once
#include <iosfwd>

namespace regicide {
// The input thread remains responsive while one worker searches a position copy.
int run_uci(std::istream& input, std::ostream& output);
}
