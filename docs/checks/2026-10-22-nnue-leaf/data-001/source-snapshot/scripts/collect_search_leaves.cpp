#include "regicide/search.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <charconv>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

// Deliberately separate from the game/UCI executable. Input is UTF-8 TSV:
// id<TAB>initial six-field FEN<TAB>space-separated complete root history.
// One JSON receipt per input line. Sampling counts evaluator occurrences,
// including unfinished iterations; it does not pretend a TT-shortened PV ends
// at a static evaluation. Downstream data preparation must deduplicate features.
namespace {
using namespace regicide;
std::string quote(std::string_view value) {
    std::string out = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { out += '\\'; out += static_cast<char>(c); }
        else if (c < 32) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += static_cast<char>(c);
    }
    return out + '"';
}
std::uint64_t number(std::string_view text) {
    std::uint64_t result = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size())
        throw std::invalid_argument("invalid unsigned argument");
    return result;
}
void moves_json(std::ostream& out, const std::vector<Move>& moves) {
    out << '[';
    for (std::size_t i = 0; i < moves.size(); ++i) {
        if (i) out << ',';
        out << quote(moves[i].uci());
    }
    out << ']';
}
struct SplitMix64 {
    std::uint64_t state;
    std::uint64_t next() {
        std::uint64_t z = (state += UINT64_C(0x9e3779b97f4a7c15));
        z = (z ^ (z >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)) * UINT64_C(0x94d049bb133111eb);
        return z ^ (z >> 31);
    }
    std::uint64_t below(std::uint64_t bound) {
        const auto threshold = (UINT64_C(0) - bound) % bound;
        std::uint64_t value;
        do { value = next(); } while (value < threshold);
        return value % bound;
    }
};
struct Sample {
    std::string fen;
    std::vector<Move> path;
    StaticEvaluationObservation observation;
    Color turn;
};
} // namespace

int main(int argc, char** argv) {
    try {
        std::string model_path;
        std::uint64_t nodes = 10000, capacity = 4, seed = 2026102204;
        int depth = max_search_depth;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (flag == "--help") {
                std::cout << "regicide_leaf_data --model PATH [--nodes 10000] [--samples 4] "
                             "[--seed 2026102204] [--depth 64]\n"
                             "stdin: id<TAB>initial_fen<TAB>space-separated complete root moves\n";
                return 0;
            }
            if (++i == argc) throw std::invalid_argument("missing option value");
            if (flag == "--model") model_path = argv[i];
            else if (flag == "--nodes") nodes = number(argv[i]);
            else if (flag == "--samples") capacity = number(argv[i]);
            else if (flag == "--seed") seed = number(argv[i]);
            else if (flag == "--depth") {
                const auto value = number(argv[i]);
                if (value < 1 || value > max_search_depth) throw std::invalid_argument("depth must be 1..64");
                depth = static_cast<int>(value);
            } else throw std::invalid_argument("unknown option: " + flag);
        }
        if (model_path.empty() || nodes == 0 || capacity == 0 || capacity > 64)
            throw std::invalid_argument("model, positive node limit and 1..64 samples required");
        const auto model = load_neural_model(model_path);
        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            const auto first = line.find('\t');
            const auto second = first == std::string::npos ? first : line.find('\t', first + 1);
            if (first == std::string::npos || second == std::string::npos || first == 0 ||
                line.find('\t', second + 1) != std::string::npos)
                throw std::invalid_argument("input requires exactly id, initial_fen and history columns");
            const std::string id = line.substr(0, first);
            const std::string initial_fen = line.substr(first + 1, second - first - 1);
            Board root(initial_fen);
            std::istringstream history_input(line.substr(second + 1));
            std::vector<Move> root_history;
            std::string move_text;
            while (history_input >> move_text) {
                const auto move = Move::from_uci(move_text);
                root.play(move);
                root_history.push_back(move);
            }
            const Board saved = root;
            std::uint64_t total = 0, eligible = 0, excluded_checked = 0, excluded_claim = 0;
            SplitMix64 random{seed};
            std::vector<Sample> samples;
            samples.reserve(static_cast<std::size_t>(capacity));
            SearchOptions options;
            options.nnue = true;
            options.nnue_model = model;
            options.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                     const StaticEvaluationObservation& observation) {
                ++total;
                if (board.in_check()) { ++excluded_checked; return; }
                if (observation.current_draw_claim || observation.draw_claim_adjusted) {
                    ++excluded_claim;
                    return;
                }
                ++eligible;
                const auto index = eligible <= capacity ? eligible - 1 : random.below(eligible);
                if (index >= capacity) return;
                Sample sample{board.fen(), path, observation, board.position().turn};
                if (eligible <= capacity) samples.push_back(std::move(sample));
                else samples[static_cast<std::size_t>(index)] = std::move(sample);
            };
            SearchLimits limits;
            limits.depth = depth;
            limits.nodes = nodes;
            const auto result = search(root, limits, nullptr, {}, options);
            if (root != saved) throw std::logic_error("collection changed root/history");
            std::sort(samples.begin(), samples.end(), [](const Sample& a, const Sample& b) {
                return a.observation.evaluation_index < b.observation.evaluation_index;
            });
            const auto unfinished = std::count_if(samples.begin(), samples.end(), [&](const Sample& sample) {
                return sample.observation.iteration_depth > result.depth;
            });
            std::cout << "{\"schema_version\":1,\"id\":" << quote(id)
                << ",\"initial_fen\":" << quote(initial_fen) << ",\"root_history\":";
            moves_json(std::cout, root_history);
            std::cout << ",\"root_fen\":" << quote(root.fen()) << ",\"model_path\":" << quote(model_path)
                << ",\"nodes_limit\":" << nodes << ",\"depth_limit\":" << depth
                << ",\"sampling\":{\"algorithm\":\"splitmix64-reservoir-occurrences\",\"seed\":" << seed
                << ",\"capacity\":" << capacity << ",\"eligible_static_evaluations\":" << eligible
                << ",\"total_static_evaluations\":" << total << ",\"excluded_checked\":" << excluded_checked
                << ",\"excluded_draw_claim\":" << excluded_claim
                << ",\"samples_from_unfinished_iteration\":" << unfinished << "},\"search\":{\"depth\":" << result.depth
                << ",\"score_cp\":" << result.score << ",\"bestmove\":"
                << (result.best_move ? quote(result.best_move->uci()) : "null") << ",\"pv\":";
            moves_json(std::cout, result.pv);
            std::cout << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes
                << ",\"stopped\":" << (result.stopped ? "true" : "false") << "},\"samples\":[";
            for (std::size_t i = 0; i < samples.size(); ++i) {
                if (i) std::cout << ',';
                const auto& sample = samples[i];
                const auto& observation = sample.observation;
                auto history = root_history;
                history.insert(history.end(), sample.path.begin(), sample.path.end());
                std::cout << "{\"fen\":" << quote(sample.fen) << ",\"history\":";
                moves_json(std::cout, history);
                std::cout << ",\"path_from_root\":";
                moves_json(std::cout, sample.path);
                std::cout << ",\"evaluation_index\":" << observation.evaluation_index
                    << ",\"iteration_depth\":" << observation.iteration_depth << ",\"ply\":" << observation.ply
                    << ",\"qply\":" << observation.qply << ",\"phase\":"
                    << quote(observation.qply < 0 ? "normal_horizon" : "quiescence_stand_pat")
                    << ",\"raw_score_cp\":" << observation.raw_score << ",\"score_cp\":" << observation.score
                    << ",\"turn\":" << quote(sample.turn == Color::white ? "white" : "black")
                    << ",\"in_check\":false,\"current_draw_claim\":false,\"draw_claim_adjusted\":false}";
            }
            std::cout << "]}\n" << std::flush;
        }
        if (std::cin.bad()) throw std::runtime_error("input read failure");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "leaf collection failed: " << error.what() << '\n';
        return 1;
    }
}
