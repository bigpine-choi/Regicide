#include "regicide/search.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

// Development-only, six fixed searches on already exposed positions. These
// observations never constitute a fresh gate, a training target, or a match.
int main(int argc, char** argv) {
    try {
        using namespace regicide;
        if (argc != 2) throw std::invalid_argument("usage: probe_tactical_search MODEL");
        const auto model = load_neural_model(argv[1]);
        Board root;
        for (const char* uci : {"d2d4", "d7d5", "c2c4", "d5c4", "e2e4", "e7e5", "g1f3",
                                "e5d4", "f1c4", "b8c6", "e1g1", "g8f6", "e4e5", "a7a5",
                                "a2a3", "a5a4", "a1a2"}) root.play(uci);
        std::vector<Board> boards{root};
        root.play("c8f5"); boards.push_back(root);
        root.play("e5f6"); root.play("f5b1"); boards.push_back(root);
        std::cout << "{\"maximum_native_searches\":6,\"maximum_nodes_per_search\":10000,"
                     "\"teacher_queries\":0,\"training_runs\":0,\"fresh_gate_access\":false,\"cases\":[";
        bool first_case = true;
        for (std::size_t index = 0; index < boards.size(); ++index) {
            for (bool enabled : {false, true}) {
                SearchOptions options;
                options.nnue = true;
                options.nnue_model = model;
                options.quiescence_threats = enabled;
                std::string first_ng5, first_exf6;
                int ng5_iteration = 0, exf6_iteration = 0;
                options.static_evaluation_observer = [&](const Board&, const std::vector<Move>& path,
                                                        const StaticEvaluationObservation& observation) {
                    const auto witness = [&](const char* move, std::string& first, int& iteration) {
                        if (!first.empty() || std::find(path.begin(), path.end(), Move::from_uci(move)) == path.end())
                            return;
                        std::ostringstream text;
                        for (Move step : path) { if (text.tellp() > 0) text << ' '; text << step.uci(); }
                        first = text.str(); iteration = observation.iteration_depth;
                    };
                    witness("f3g5", first_ng5, ng5_iteration);
                    witness("e5f6", first_exf6, exf6_iteration);
                };
                const auto result = search(boards[index], {.depth = 64, .nodes = 10000}, nullptr, {}, options);
                if (!first_case) std::cout << ',';
                first_case = false;
                std::cout << "{\"case\":" << index << ",\"enabled\":" << (enabled ? "true" : "false")
                          << ",\"move\":\"" << result.best_move->uci() << "\",\"score_cp\":" << result.score
                          << ",\"completed_depth\":" << result.depth << ",\"nodes\":" << result.nodes
                          << ",\"qnodes\":" << result.qnodes << ",\"seldepth\":" << result.seldepth
                          << ",\"elapsed_ms\":" << result.elapsed.count()
                          << ",\"threat_candidates\":" << result.quiet_threat_candidates
                          << ",\"threat_extensions\":" << result.quiet_threat_extensions
                          << ",\"defensive_qnodes\":" << result.defensive_qnodes
                          << ",\"first_observed_ng5_path\":\"" << first_ng5
                          << "\",\"first_observed_ng5_iteration\":" << ng5_iteration
                          << ",\"first_observed_exf6_path\":\"" << first_exf6
                          << "\",\"first_observed_exf6_iteration\":" << exf6_iteration
                          << ",\"pv\":[";
                bool first_move = true;
                for (Move move : result.pv) {
                    if (!first_move) std::cout << ',';
                    first_move = false;
                    std::cout << '"' << move.uci() << '"';
                }
                std::cout << "]}";
            }
        }
        std::cout << "]}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
