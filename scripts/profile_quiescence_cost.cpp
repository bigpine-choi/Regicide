// Diagnostic-only hooks for an archived, instrumented copy of search.cpp.
// No pruning, reordering, evaluator or search-budget changes are made here.
#include "regicide/search.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <array>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <tuple>
#include <vector>

namespace {
using namespace regicide;
using State = std::tuple<std::uint64_t, std::uint64_t, int, int, int, int, int, bool>;
using WindowState = std::tuple<State, int, int>;
struct Visit {
    std::uint64_t count = 0;
    std::string fen, path;
};
std::map<State, Visit> states;
std::map<WindowState, std::uint64_t> windows;
std::map<std::uint64_t, std::uint64_t> position_only;
std::map<std::string, std::uint64_t> branches;
std::array<std::uint64_t, max_search_ply + 1> qplies{}, checked_plies{};
std::uint64_t checked_count = 0, defensive_count = 0, evaluations = 0;
std::string stop_reason = "none", last_path;
std::string path_text(const std::vector<Move>& path) {
    std::string text;
    for (Move move : path) { if (!text.empty()) text += ' '; text += move.uci(); }
    return text;
}
std::string quote(const std::string& text) {
    std::string out = "\"";
    for (char c : text) { if (c == '"' || c == '\\') out += '\\'; out += c; }
    return out + '"';
}
template<class Values> void histogram(const Values& values) {
    std::cout << '{'; bool first = true;
    for (std::size_t i = 0; i < values.size(); ++i) if (values[i]) {
        if (!first) std::cout << ','; first = false;
        std::cout << quote(std::to_string(i)) << ':' << values[i];
    }
    std::cout << '}';
}
}

namespace regicide {
void qcost_visit(const Board& board, int ply, int qply, int checks_left,
                 int threats_left, bool defensive_reply, int alpha, int beta,
                 const std::vector<Move>& path) {
    const bool checked = board.in_check();
    ++qplies.at(static_cast<std::size_t>(qply));
    if (checked) { ++checked_count; ++checked_plies.at(static_cast<std::size_t>(qply)); }
    if (defensive_reply) ++defensive_count;
    const State key{board.position_key(), board.repetition_context_key(),
                    board.position().halfmove, max_quiescence_depth - qply, ply,
                    checks_left, threats_left, defensive_reply};
    auto& item = states[key];
    if (++item.count == 1) { item.fen = board.fen(); item.path = path_text(path); }
    ++windows[{key, alpha, beta}];
    ++position_only[board.position_key()];
    ++branches[path.empty() ? "root" : path.front().uci()];
    last_path = path_text(path);
}
void qcost_stop(const char* reason) { stop_reason = reason; }
}

int main(int argc, char** argv) {
    try {
        using namespace regicide;
        if (argc != 2) throw std::invalid_argument("one model path required");
        const auto model = load_neural_model(argv[1]);
        std::string line;
        while (std::getline(std::cin, line)) {
            std::vector<std::string> columns;
            std::size_t start = 0;
            for (;;) {
                const auto tab = line.find('\t', start);
                columns.push_back(line.substr(start, tab == std::string::npos ? tab : tab - start));
                if (tab == std::string::npos) break;
                start = tab + 1;
            }
            if (columns.size() != 4) throw std::invalid_argument("id, FEN, history, threats flag required");
            states.clear(); windows.clear(); position_only.clear(); branches.clear();
            qplies.fill(0); checked_plies.fill(0); checked_count = defensive_count = evaluations = 0;
            stop_reason = "none"; last_path.clear();
            Board board(columns[1]); std::istringstream history(columns[2]); std::string move;
            while (history >> move) board.play(move);
            SearchOptions options; options.nnue = true; options.nnue_model = model;
            options.quiescence_threats = columns[3] == "1";
            options.static_evaluation_observer = [](const Board&, const std::vector<Move>&,
                                                    const StaticEvaluationObservation&) { ++evaluations; };
            SearchLimits limits; limits.depth = 64; limits.nodes = 10000;
            const auto result = search(board, limits, nullptr, {}, options);
            const auto repeats = [](const auto& map) {
                std::uint64_t result = 0;
                for (const auto& item : map) result += item.second > 0 ? item.second - 1 : 0;
                return result;
            };
            std::uint64_t state_repeats = 0;
            for (const auto& item : states) state_repeats += item.second.count - 1;
            std::vector<std::pair<State, Visit>> repeated(states.begin(), states.end());
            std::sort(repeated.begin(), repeated.end(), [](const auto& a, const auto& b) {
                return a.second.count > b.second.count;
            });
            std::cout << "{\"id\":" << quote(columns[0]) << ",\"fen\":" << quote(board.fen())
                      << ",\"threats\":" << (options.quiescence_threats ? "true" : "false")
                      << ",\"move\":" << quote(result.best_move ? result.best_move->uci() : "")
                      << ",\"depth\":" << result.depth << ",\"score\":" << result.score
                      << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes
                      << ",\"seldepth\":" << result.seldepth << ",\"stopped\":" << (result.stopped ? "true" : "false")
                      << ",\"stop_reason\":" << quote(stop_reason) << ",\"last_qpath\":" << quote(last_path)
                      << ",\"checked_qnodes\":" << checked_count << ",\"defensive_qnodes\":" << defensive_count
                      << ",\"static_evaluations\":" << evaluations
                      << ",\"qstate_distinct\":" << states.size() << ",\"qstate_repeat_visits\":" << state_repeats
                      << ",\"same_window_repeat_visits\":" << repeats(windows)
                      << ",\"position_only_repeat_visits\":" << repeats(position_only)
                      << ",\"qply_histogram\":"; histogram(qplies);
            std::cout << ",\"checked_qply_histogram\":"; histogram(checked_plies);
            std::cout << ",\"root_branch_qnodes\":{"; bool first = true;
            for (const auto& [branch, count] : branches) {
                if (!first) std::cout << ','; first = false; std::cout << quote(branch) << ':' << count;
            }
            std::cout << "},\"most_repeated_qstates\":[";
            for (std::size_t i = 0; i < std::min<std::size_t>(5, repeated.size()); ++i) {
                if (i) std::cout << ',';
                const auto& [key, item] = repeated[i];
                std::cout << "{\"count\":" << item.count << ",\"fen\":" << quote(item.fen)
                          << ",\"first_path\":" << quote(item.path)
                          << ",\"qplies_left\":" << std::get<3>(key) << ",\"ply\":" << std::get<4>(key)
                          << ",\"checks_left\":" << std::get<5>(key) << ",\"threats_left\":" << std::get<6>(key)
                          << ",\"defensive_reply\":" << (std::get<7>(key) ? "true" : "false") << '}';
            }
            std::cout << "]}" << std::endl;
        }
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
