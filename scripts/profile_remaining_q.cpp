// Hook-only accounting for an archived production search; no policy changes.
#include "regicide/search.hpp"
#include "regicide/nnue.hpp"
#include <algorithm>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

namespace {
using namespace regicide;
using Counts = std::map<std::string, std::uint64_t>;
struct Edge { std::string category; int qply; };
std::map<std::string, Edge> edges;
Counts totals, subtrees, first_totals, first_subtrees;
std::map<std::string, Counts> branches, first_branches;
std::set<std::string> observed_fens;
std::uint64_t first_nodes = 0;
std::string quote(const std::string& text) {
    std::string out = "\"";
    for (char c : text) { if (c == '"' || c == '\\') out += '\\'; out += c; }
    return out + '"';
}
std::string path_text(const std::vector<Move>& path) {
    std::string text;
    for (Move move : path) { if (!text.empty()) text += ' '; text += move.uci(); }
    return text;
}
void counts(const Counts& values) {
    std::cout << '{'; bool first = true;
    for (const auto& [key, value] : values) {
        if (!first) std::cout << ','; first = false;
        std::cout << quote(key) << ':' << value;
    }
    std::cout << '}';
}
void branch_counts(const std::map<std::string, Counts>& values) {
    std::cout << '{'; bool first = true;
    for (const auto& [key, value] : values) {
        if (!first) std::cout << ','; first = false;
        std::cout << quote(key) << ':'; counts(value);
    }
    std::cout << '}';
}
void top_paths(const Counts& values) {
    std::vector<std::pair<std::string, std::uint64_t>> ordered(values.begin(), values.end());
    std::sort(ordered.begin(), ordered.end(), [](const auto& left, const auto& right) {
        return left.second != right.second ? left.second > right.second : left.first < right.first;
    });
    std::cout << '[';
    for (std::size_t i = 0; i < std::min<std::size_t>(40, ordered.size()); ++i) {
        if (i) std::cout << ',';
        std::cout << "{\"path\":" << quote(ordered[i].first) << ",\"qnode_descendants\":" << ordered[i].second;
        const auto edge = edges.find(ordered[i].first);
        if (edge != edges.end()) std::cout << ",\"entry_category\":" << quote(edge->second.category) << ",\"entry_qply\":" << edge->second.qply;
        std::cout << '}';
    }
    std::cout << ']';
}
}

namespace regicide {
void qremain_edge(const Board& board, Move move, int qply, bool checked, bool defensive,
                  bool threat, int checks, const std::vector<Move>& path) {
    const auto& pos = board.position();
    const char piece = pos.squares[move.from];
    const bool ep = (piece == 'P' || piece == 'p') && move.to == pos.en_passant;
    const bool quiet = pos.squares[move.to] == '.' && !ep && !move.promotion;
    const bool checking = board.gives_check_generated(move);
    std::string category = checked ? "evasion" : defensive ? "threat_defense" :
        threat ? "optional_threat" : quiet && checking && checks > 0 ? "optional_quiet_check" :
        checking ? "checking_capture" : "ordinary_capture";
    auto key = path_text(path);
    if (!key.empty()) key += ' ';
    key += move.uci();
    edges[key] = {category, qply};
    ++totals["edges_" + category];
    ++totals["edges_" + category + "_qply_" + std::to_string(qply)];
}
void qremain_event(const char* event, const std::vector<Move>& path) {
    ++totals[event];
    ++branches[path.empty() ? "root" : path.front().uci()][event];
}
void qremain_visit(const Board& board, int qply, const std::vector<Move>& path) {
    observed_fens.insert(board.fen());
    const std::string branch = path.empty() ? "root" : path.front().uci();
    auto& branch_stats = branches[branch];
    ++totals["qnodes"]; ++branch_stats["qnodes"];
    ++totals["qply_" + std::to_string(qply)];
    if (board.in_check()) { ++totals["checked_qnodes"]; ++branch_stats["checked_qnodes"]; }
    int quiet_check_qply = -1, threat_qply = -1;
    std::string key;
    bool checking_capture = false;
    for (Move move : path) {
        if (!key.empty()) key += ' ';
        key += move.uci();
        ++subtrees[key];
        const auto it = edges.find(key);
        if (it == edges.end()) continue;
        if (it->second.category == "optional_quiet_check" && quiet_check_qply < 0) quiet_check_qply = it->second.qply;
        if (it->second.category == "optional_threat" && threat_qply < 0) threat_qply = it->second.qply;
        if (it->second.category == "checking_capture") checking_capture = true;
    }
    const auto account = [&](const std::string& metric) { ++totals[metric]; ++branch_stats[metric]; };
    account(quiet_check_qply < 0 ? "without_optional_quiet_check" : "under_optional_quiet_check_qply_" + std::to_string(quiet_check_qply));
    account(threat_qply < 0 ? "without_optional_threat" : "under_optional_threat_qply_" + std::to_string(threat_qply));
    if (checking_capture) account("under_checking_capture");
    const auto incoming = edges.find(key);
    account(incoming == edges.end() ? "incoming_normal" : "incoming_" + incoming->second.category);
}
}

int main(int argc, char** argv) {
    try {
        using namespace regicide;
        if (argc != 2) throw std::invalid_argument("one model path required");
        const auto model = load_neural_model(argv[1]);
        std::string line;
        while (std::getline(std::cin, line)) {
            std::vector<std::string> columns; std::size_t start = 0;
            for (;;) {
                const auto tab = line.find('\t', start);
                columns.push_back(line.substr(start, tab == std::string::npos ? tab : tab - start));
                if (tab == std::string::npos) break;
                start = tab + 1;
            }
            if (columns.size() != 4) throw std::invalid_argument("id FEN history ordering required");
            edges.clear(); totals.clear(); branches.clear(); subtrees.clear();
            first_totals.clear(); first_branches.clear(); first_subtrees.clear(); observed_fens.clear(); first_nodes = 0;
            Board board(columns[1]); std::istringstream history(columns[2]); std::string move;
            while (history >> move) board.play(move);
            SearchOptions options; options.nnue = true; options.nnue_model = model;
            options.quiescence_threats = true; options.quiescence_cache = true;
            options.tactical_threat_ordering = columns[3] == "1";
            options.static_evaluation_observer = [](const Board& at, const std::vector<Move>&, const StaticEvaluationObservation&) {
                observed_fens.insert(at.fen());
            };
            SearchLimits limits; limits.depth = 64; limits.nodes = 10000;
            const auto callback = [](const SearchResult& result) {
                if (result.depth == 1) {
                    first_nodes = result.nodes; first_totals = totals; first_branches = branches; first_subtrees = subtrees;
                }
            };
            const auto result = search(board, limits, nullptr, callback, options);
            std::cout << "{\"id\":" << quote(columns[0]) << ",\"fen\":" << quote(board.fen())
                << ",\"ordering\":" << (options.tactical_threat_ordering ? "true" : "false")
                << ",\"move\":" << quote(result.best_move ? result.best_move->uci() : "")
                << ",\"depth\":" << result.depth << ",\"score\":" << result.score
                << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes
                << ",\"qcache_hits\":" << result.qcache_hits << ",\"qcache_cutoffs\":" << result.qcache_cutoffs
                << ",\"first_iteration_nodes\":" << first_nodes << ",\"first_iteration_totals\":"; counts(first_totals);
            std::cout << ",\"first_iteration_branches\":"; branch_counts(first_branches);
            std::cout << ",\"first_iteration_top_paths\":"; top_paths(first_subtrees);
            std::cout << ",\"totals\":"; counts(totals);
            std::cout << ",\"branches\":"; branch_counts(branches);
            std::cout << ",\"top_paths\":"; top_paths(subtrees);
            std::cout << ",\"observed_fens\":["; bool first = true;
            for (const auto& fen : observed_fens) { if (!first) std::cout << ','; first = false; std::cout << quote(fen); }
            std::cout << "]}" << std::endl;
        }
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
