#include "regicide/transposition.hpp"

#include <iostream>

int main(int argc, char** argv) {
    const regicide::TranspositionTable table(16);
    const auto entry = sizeof(regicide::TranspositionEntry);
    const bool valid = entry == 56 && table.capacity() == 299593 &&
                       table.allocated_bytes() == 16777208;
    std::cout << "{\"role\":\"" << (argc > 1 ? argv[1] : "unknown")
              << "\",\"entry_bytes\":" << entry
              << ",\"capacity\":" << table.capacity()
              << ",\"allocated_bytes\":" << table.allocated_bytes()
              << ",\"valid\":" << (valid ? "true" : "false") << "}\n";
    return valid ? 0 : 1;
}
