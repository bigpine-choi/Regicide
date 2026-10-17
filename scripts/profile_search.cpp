#include "regicide/search.hpp"
#include <iostream>
#include <string>
#include <vector>

// Profiling workload shared by v0.3 and v0.4: 4 positions x 30 x 5,000 nodes.
// See docs/experiments/2026-10-17-profile-transposition-plan.md for build flags.
int main() {
    std::vector<regicide::Board> boards;
    boards.emplace_back();
    regicide::Board opening;
    for (const char* move : {"e2e4","e7e5","g1f3","b8c6","f1b5","a7a6","b5a4","g8f6","e1g1","f8e7"}) opening.play(move);
    boards.push_back(opening);
    boards.emplace_back("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1");
    boards.emplace_back("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1");
    unsigned long long total = 0;
    for (int repeat=0;repeat<30;++repeat) for (std::size_t i=0;i<boards.size();++i) {
        const auto r=regicide::search(boards[i], {.depth=64,.nodes=5000});
        total+=r.nodes;
        std::cout << repeat << ' ' << i << ' ' << r.depth << ' ' << r.nodes << ' ' << r.elapsed.count() << '\n';
    }
    std::cout << "total_nodes " << total << '\n';
}
