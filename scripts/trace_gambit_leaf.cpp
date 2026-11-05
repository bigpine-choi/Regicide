#include "regicide/search.hpp"

#include <algorithm>
#include <charconv>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

// Research observer only. A matching evaluator invocation is not proof that
// its value determined the root result; scouts, TT returns and draw handling
// can prevent that attribution. The core evaluator/search are unchanged.
namespace {
using namespace regicide;
std::string quote(std::string_view text) {
    std::string out = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') { out += '\\'; out += static_cast<char>(c); }
        else if (c < 32) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += static_cast<char>(c);
    }
    return out + '"';
}
std::uint64_t number(std::string_view text) {
    std::uint64_t value = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size())
        throw std::invalid_argument("invalid unsigned argument");
    return value;
}
std::vector<Move> play_line(Board& board, const std::string& text) {
    std::istringstream input(text);
    std::vector<Move> moves;
    std::string word;
    while (input >> word) {
        if (board.outcome() != Outcome::ongoing)
            throw std::invalid_argument("history/path continues past terminal position");
        const auto move = Move::from_uci(word);
        board.play(move);
        moves.push_back(move);
    }
    return moves;
}
void moves_json(const std::vector<Move>& moves) {
    std::cout << '[';
    for (std::size_t i = 0; i < moves.size(); ++i) {
        if (i) std::cout << ',';
        std::cout << quote(moves[i].uci());
    }
    std::cout << ']';
}
struct Match {
    std::string fen;
    StaticEvaluationObservation observation;
    Color turn;
    bool in_check;
};
} // namespace

int main(int argc, char** argv) {
    try {
        int depth = 6;
        std::uint64_t nodes = 5000000;
        bool forcing = false;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (flag == "--help") {
                std::cout << "regicide_trace_gambit_leaf [--depth 6] [--nodes 5000000] [--forcing]\n"
                    "stdin: initial_fen<TAB>full root history<TAB>root UCI move or -<TAB>target PV UCI sequence\n";
                return 0;
            }
            if (flag == "--forcing") { forcing = true; continue; }
            if (++i == argc) throw std::invalid_argument("missing option value");
            if (flag == "--nodes") nodes = number(argv[i]);
            else if (flag == "--depth") {
                const auto value = number(argv[i]);
                if (value < 1 || value > max_search_depth)
                    throw std::invalid_argument("depth must be 1..64");
                depth = static_cast<int>(value);
            } else throw std::invalid_argument("unknown option");
        }
        if (!nodes) throw std::invalid_argument("nodes must be positive");
        std::string line, extra;
        if (!std::getline(std::cin, line)) throw std::invalid_argument("one input row required");
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (std::getline(std::cin, extra)) throw std::invalid_argument("exactly one input row required");
        std::vector<std::string> fields;
        std::size_t start = 0;
        for (;;) {
            const auto next = line.find('\t', start);
            fields.push_back(line.substr(start, next == std::string::npos ? next : next - start));
            if (next == std::string::npos) break;
            start = next + 1;
        }
        if (fields.size() != 4 || line.find('\0') != std::string::npos ||
            line.find_first_of("\r\v\f") != std::string::npos)
            throw std::invalid_argument("exactly four TSV fields without control characters required");
        Board root(fields[0]);
        const auto history = play_line(root, fields[1]);
        if (root.outcome() != Outcome::ongoing) throw std::invalid_argument("nonterminal root required");
        const Board saved = root;
        Board endpoint = root;
        const auto target = play_line(endpoint, fields[3]);
        if (target.empty() || target.size() > static_cast<std::size_t>(max_search_ply))
            throw std::invalid_argument("nonempty bounded target path required");
        SearchOptions options;
        options.piece_activity = true;
        options.quiescence_forcing = forcing;
        if (fields[2] != "-") {
            const auto move = Move::from_uci(fields[2]);
            const auto legal = root.legal_moves();
            if (std::find(legal.begin(), legal.end(), move) == legal.end() || target.front() != move)
                throw std::invalid_argument("illegal root move or target/root mismatch");
            options.root_moves = std::vector<Move>{move};
        }
        std::vector<Match> matches;
        std::uint64_t matched = 0, observed = 0;
        options.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                const StaticEvaluationObservation& observation) {
            ++observed;
            if (observation.iteration_depth != depth || path != target) return;
            ++matched;
            if (matches.size() < 256)
                matches.push_back({board.fen(), observation, board.position().turn, board.in_check()});
        };
        SearchLimits limits;
        limits.depth = depth;
        limits.nodes = nodes;
        const auto result = search(root, limits, nullptr, {}, options);
        if (root != saved || result.nodes > nodes)
            throw std::logic_error("root/history changed or node cap exceeded");
        const bool completed = result.depth == depth && !result.stopped;
        std::cout << "{\"schema\":\"regicide.gambit-leaf-trace.v1\",\"root_fen\":" << quote(root.fen())
            << ",\"initial_fen\":" << quote(fields[0]) << ",\"history_uci\":";
        moves_json(history);
        std::cout << ",\"target_pv_uci\":";
        moves_json(target);
        std::cout << ",\"target_fen\":" << quote(endpoint.fen())
            << ",\"requested_depth\":" << depth << ",\"node_cap\":" << nodes
            << ",\"piece_activity\":true,\"lmr\":true,\"forcing\":" << (forcing ? "true" : "false")
            << ",\"root_constraint\":"
            << (fields[2] == "-" ? "null" : quote(fields[2]))
            << ",\"position_unchanged\":true,\"requested_iteration_completed\":" << (completed ? "true" : "false")
            << ",\"match_status\":" << quote(matched ? "observed_static_evaluation" : "unavailable")
            << ",\"causal_root_support_claim\":false,\"total_evaluator_calls\":" << observed
            << ",\"match_count\":" << matched << ",\"matches_truncated\":" << (matched > matches.size() ? "true" : "false")
            << ",\"search\":{\"depth\":" << result.depth << ",\"score_raw_root\":" << result.score
            << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes
            << ",\"stopped\":" << (result.stopped ? "true" : "false") << ",\"bestmove\":"
            << (result.best_move ? quote(result.best_move->uci()) : "null") << ",\"pv_uci\":";
        moves_json(result.pv);
        std::cout << "},\"matches\":[";
        for (std::size_t i = 0; i < matches.size(); ++i) {
            if (i) std::cout << ',';
            const auto& item = matches[i];
            const auto& o = item.observation;
            std::cout << "{\"fen\":" << quote(item.fen) << ",\"turn\":"
                << quote(item.turn == Color::white ? "white" : "black")
                << ",\"in_check\":" << (item.in_check ? "true" : "false")
                << ",\"iteration_depth\":" << o.iteration_depth << ",\"iteration_completed\":" << (completed ? "true" : "false")
                << ",\"raw_score\":" << o.raw_score << ",\"score\":" << o.score
                << ",\"ply\":" << o.ply << ",\"qply\":" << o.qply << ",\"evaluation_index\":" << o.evaluation_index
                << ",\"current_draw_claim\":" << (o.current_draw_claim ? "true" : "false")
                << ",\"draw_claim_adjusted\":" << (o.draw_claim_adjusted ? "true" : "false") << '}';
        }
        std::cout << "]}\n";
        return std::cout ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << "error: " << error.what() << '\n';
        return 1;
    }
}
