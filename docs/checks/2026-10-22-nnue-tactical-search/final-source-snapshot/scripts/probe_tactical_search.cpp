#include "regicide/search.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <iostream>
#include <set>
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
        std::vector<std::vector<Move>> histories(1);
        for (const char* move : {"d2d4", "d7d5", "c2c4", "d5c4", "e2e4", "e7e5", "g1f3",
                                 "e5d4", "f1c4", "b8c6", "e1g1", "g8f6", "e4e5", "a7a5",
                                 "a2a3", "a5a4", "a1a2"}) histories[0].push_back(Move::from_uci(move));
        root.play("c8f5"); boards.push_back(root);
        histories.push_back(histories[0]); histories.back().push_back(Move::from_uci("c8f5"));
        root.play("e5f6"); root.play("f5b1"); boards.push_back(root);
        histories.push_back(histories[1]);
        histories.back().push_back(Move::from_uci("e5f6")); histories.back().push_back(Move::from_uci("f5b1"));
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
                std::string first_critical_ng5;
                int ng5_iteration = 0, exf6_iteration = 0;
                int critical_ng5_iteration = 0;
                std::vector<Move> critical;
                if (index == 0) critical.push_back(Move::from_uci("c8f5"));
                if (index <= 1) {
                    critical.push_back(Move::from_uci("e5f6"));
                    critical.push_back(Move::from_uci("f5b1"));
                }
                critical.push_back(Move::from_uci("f3g5"));
                std::set<std::string> observed_fens;
                options.static_evaluation_observer = [&](const Board& observed, const std::vector<Move>& path,
                                                        const StaticEvaluationObservation& observation) {
                    observed_fens.insert(observed.fen());
                    const auto witness = [&](const char* move, std::string& first, int& iteration) {
                        if (!first.empty() || std::find(path.begin(), path.end(), Move::from_uci(move)) == path.end())
                            return;
                        std::ostringstream text;
                        for (Move step : path) { if (text.tellp() > 0) text << ' '; text << step.uci(); }
                        first = text.str(); iteration = observation.iteration_depth;
                    };
                    witness("f3g5", first_ng5, ng5_iteration);
                    witness("e5f6", first_exf6, exf6_iteration);
                    if (first_critical_ng5.empty() && path.size() >= critical.size() &&
                        std::equal(critical.begin(), critical.end(), path.begin())) {
                        std::ostringstream text;
                        for (Move step : path) { if (text.tellp() > 0) text << ' '; text << step.uci(); }
                        first_critical_ng5 = text.str();
                        critical_ng5_iteration = observation.iteration_depth;
                    }
                };
                const auto result = search(boards[index], {.depth = 64, .nodes = 10000}, nullptr, {}, options);
                if (!first_case) std::cout << ',';
                first_case = false;
                std::cout << "{\"case\":" << index << ",\"enabled\":" << (enabled ? "true" : "false")
                          << ",\"initial_fen\":\"" << Board::start_fen << "\",\"fen\":\"" << boards[index].fen()
                          << "\",\"history\":[";
                for (std::size_t step = 0; step < histories[index].size(); ++step) {
                    if (step) std::cout << ',';
                    std::cout << '"' << histories[index][step].uci() << '"';
                }
                std::cout << ']'
                          << ",\"move\":\"" << result.best_move->uci() << "\",\"score_cp\":" << result.score
                          << ",\"completed_depth\":" << result.depth << ",\"nodes\":" << result.nodes
                          << ",\"qnodes\":" << result.qnodes << ",\"seldepth\":" << result.seldepth
                          << ",\"elapsed_ms\":" << result.elapsed.count()
                          << ",\"threat_candidates\":" << result.quiet_threat_candidates
                          << ",\"threat_extensions\":" << result.quiet_threat_extensions
                          << ",\"defensive_qnodes\":" << result.defensive_qnodes
                          << ",\"qsee_pruned\":" << result.qsee_pruned
                          << ",\"first_observed_ng5_path\":\"" << first_ng5
                          << "\",\"first_observed_ng5_iteration\":" << ng5_iteration
                          << ",\"first_observed_exf6_path\":\"" << first_exf6
                          << "\",\"first_observed_exf6_iteration\":" << exf6_iteration
                          << ",\"first_critical_ng5_path\":\"" << first_critical_ng5
                          << "\",\"first_critical_ng5_iteration\":" << critical_ng5_iteration
                          << ",\"pv\":[";
                bool first_move = true;
                for (Move move : result.pv) {
                    if (!first_move) std::cout << ',';
                    first_move = false;
                    std::cout << '"' << move.uci() << '"';
                }
                std::cout << "],\"observed_static_positions\":[";
                bool first_fen = true;
                for (const auto& fen : observed_fens) {
                    if (!first_fen) std::cout << ',';
                    first_fen = false;
                    std::cout << "{\"fen\":\"" << fen << "\"}";
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
