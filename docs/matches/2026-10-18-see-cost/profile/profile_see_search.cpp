#include "regicide/search.hpp"
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
namespace regicide { extern std::uint64_t measured_see_calls; extern std::int64_t measured_see_nanoseconds; }
int main() {
    std::vector<regicide::Board> boards;
    std::string fen;
    while(std::getline(std::cin,fen)) {
        if(!fen.empty() && fen.back()=='\r') fen.pop_back();
        if(!fen.empty()) boards.emplace_back(fen);
    }
    if(boards.empty()) return 1;
    std::int64_t empty_intervals = 0;
    const auto clock_start = std::chrono::steady_clock::now();
    for(int i=0;i<1000000;++i) {
        const auto a=std::chrono::steady_clock::now();
        empty_intervals += std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-a).count();
    }
    const auto clock_wall = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-clock_start).count();
    std::cout << "{\"clock_pair_iterations\":1000000,\"clock_pair_wall_ns\":" << clock_wall << ",\"empty_interval_ns\":" << empty_intervals << ",\"positions\":" << boards.size() << ",\"rows\":[";
    regicide::SearchContext context;
    bool first=true;
    for(int repeat=0;repeat<3;++repeat) for(std::size_t i=0;i<boards.size();++i) {
        regicide::measured_see_calls=0;
        regicide::measured_see_nanoseconds=0;
        const auto start=std::chrono::steady_clock::now();
        const auto result=regicide::search(boards[i],{.depth=64,.nodes=50000},nullptr,{}, {}, &context);
        const auto wall=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();
        if(regicide::measured_see_calls!=result.see_calls) throw std::runtime_error("wrapper/search count mismatch");
        if(!first)std::cout << ',';
        first=false;
        std::cout << "{\"repeat\":" << repeat << ",\"position\":" << i << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes << ",\"pvs_probes\":" << result.pvs_probes << ",\"pvs_researches\":" << result.pvs_researches << ",\"depth\":" << result.depth << ",\"see_calls\":" << result.see_calls << ",\"see_instrumented_ns\":" << regicide::measured_see_nanoseconds << ",\"search_instrumented_ns\":" << wall << '}';
    }
    std::cout << "]}\n";
}
