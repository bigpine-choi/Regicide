#include "live_choice_support.hpp"

#include <charconv>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

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
void witness_json(std::ostream& out, const research::EvaluationWitness& item,
                  const std::vector<Move>& root_history) {
    auto history = root_history;
    history.insert(history.end(), item.path.begin(), item.path.end());
    const auto& observation = item.observation;
    const int white = item.turn == Color::white ? observation.raw_score : -observation.raw_score;
    out << "{\"fen\":" << quote(item.fen) << ",\"history\":";
    moves_json(out, history);
    out << ",\"path_from_root\":";
    moves_json(out, item.path);
    out << ",\"evaluation_index\":" << observation.evaluation_index
        << ",\"iteration_depth\":" << observation.iteration_depth << ",\"ply\":" << observation.ply
        << ",\"qply\":" << observation.qply << ",\"phase\":"
        << quote(observation.qply < 0 ? "normal_horizon" : "quiescence_stand_pat")
        << ",\"raw_score_cp\":" << observation.raw_score << ",\"score_cp\":" << observation.score
        << ",\"white_score_cp\":" << white << ",\"turn\":"
        << quote(item.turn == Color::white ? "white" : "black")
        << ",\"history_size\":" << item.history_size << ",\"repetition_count\":" << item.repetition_count
        << ",\"in_check\":" << (item.in_check ? "true" : "false")
        << ",\"terminal\":" << (item.outcome == Outcome::ongoing ? "false" : "true")
        << ",\"current_draw_claim\":" << (observation.current_draw_claim ? "true" : "false")
        << ",\"draw_claim_adjusted\":" << (observation.draw_claim_adjusted ? "true" : "false") << '}';
}
void result_json(std::ostream& out, const SearchResult& result, Color root_turn) {
    out << "{\"depth\":" << result.depth << ",\"score_cp\":" << result.score
        << ",\"white_score_cp\":" << (root_turn == Color::white ? result.score : -result.score)
        << ",\"bestmove\":" << (result.best_move ? quote(result.best_move->uci()) : "null") << ",\"pv\":";
    moves_json(out, result.pv);
    out << ",\"nodes\":" << result.nodes << ",\"qnodes\":" << result.qnodes
        << ",\"tt_probes\":" << result.tt_probes << ",\"tt_hits\":" << result.tt_hits
        << ",\"tt_cutoffs\":" << result.tt_cutoffs << ",\"pvs_probes\":" << result.pvs_probes
        << ",\"lmr_reductions\":" << result.lmr_reductions
        << ",\"stopped\":" << (result.stopped ? "true" : "false") << '}';
}
} // namespace

int main(int argc, char** argv) {
    try {
        using namespace regicide;
        std::string model_path;
        std::uint64_t nodes = 10000;
        int depth = 64;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (flag == "--help") {
                std::cout << "regicide_live_choice_data --model PATH [--depth 64] [--nodes 10000]\n"
                             "stdin: id<TAB>initial_fen<TAB>complete root history<TAB>forced root move or -\n";
                return 0;
            }
            if (++i == argc) throw std::invalid_argument("missing option value");
            if (flag == "--model") model_path = argv[i];
            else if (flag == "--nodes") nodes = number(argv[i]);
            else if (flag == "--depth") {
                const auto value = number(argv[i]);
                if (value < 1 || value > max_search_depth) throw std::invalid_argument("depth must be 1..64");
                depth = static_cast<int>(value);
            } else throw std::invalid_argument("unknown option: " + flag);
        }
        if (model_path.empty() || nodes == 0 || nodes > 10000)
            throw std::invalid_argument("model and node limit 1..10000 required");
        const auto model = load_neural_model(model_path);
        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::vector<std::string> columns;
            std::size_t start = 0;
            for (;;) {
                const auto tab = line.find('\t', start);
                columns.push_back(line.substr(start, tab == std::string::npos ? tab : tab - start));
                if (tab == std::string::npos) break;
                start = tab + 1;
            }
            if (columns.size() != 4 || columns[0].empty())
                throw std::invalid_argument("input requires id, initial_fen, history and forced_root_move columns");
            Board root(columns[1]);
            std::istringstream history_input(columns[2]);
            std::vector<Move> history;
            std::string move_text;
            while (history_input >> move_text) {
                const auto move = Move::from_uci(move_text);
                root.play(move);
                history.push_back(move);
            }
            const auto forced = columns[3] == "-" ? std::optional<Move>{} :
                                std::optional<Move>{Move::from_uci(columns[3])};
            const Board saved = root;
            std::vector<research::EvaluationWitness> observations;
            observations.reserve(static_cast<std::size_t>(nodes));
            SearchOptions options;
            options.nnue = true; options.nnue_model = model;
            if (forced) options.root_moves = std::vector<Move>{*forced};
            options.static_evaluation_observer = [&](const Board& board, const std::vector<Move>& path,
                                                     const StaticEvaluationObservation& observation) {
                if (observations.size() >= nodes) throw std::logic_error("static observations exceed node cap");
                observations.push_back(research::witness(board, path, observation));
            };
            SearchLimits limits; limits.depth = depth; limits.nodes = nodes;
            SearchResult completed;
            std::size_t completed_observations = 0;
            const auto result = search(root, limits, nullptr, [&](const SearchResult& iteration) {
                completed = iteration;
                completed_observations = observations.size();
            }, options);
            if (root != saved) throw std::logic_error("collection changed root/history");
            const auto support = research::live_support_leaf(root, depth, options, result, completed,
                                                           completed_observations, observations, *model);
            std::cout << "{\"schema_version\":1,\"id\":" << quote(columns[0])
                << ",\"initial_fen\":" << quote(columns[1]) << ",\"root_history\":";
            moves_json(std::cout, history);
            std::cout << ",\"root_fen\":" << quote(root.fen()) << ",\"forced_root_move\":" << (forced ? quote(forced->uci()) : "null")
                << ",\"model_path\":" << quote(model_path) << ",\"nodes_limit\":" << nodes
                << ",\"depth_limit\":" << depth
                << ",\"options\":{\"nnue\":true,\"nnue_incremental\":true,\"quiescence\":true,"
                   "\"transposition\":true,\"pvs\":true,\"lmr\":true,\"fast_moves\":true,"
                   "\"quiet_ordering\":true,\"quiescence_checks\":true,\"see_ordering\":true,"
                   "\"positional\":true,\"king_pawn_shield\":true,\"pawn_structure\":true,\"hash_mb\":16,"
                   "\"full_window_forced_root\":" << (forced ? "true" : "false") << "},\"search\":";
            result_json(std::cout, result, root.position().turn);
            std::cout << ",\"last_completed_iteration\":";
            result_json(std::cout, completed, root.position().turn);
            std::cout << ",\"completed_observations_count\":" << completed_observations
                << ",\"support_semantics\":\"observed completed-prefix PV value match; not a complete causal search trace\""
                << ",\"accepted\":"
                << (support.accepted ? "true" : "false") << ",\"reason\":" << quote(support.reason)
                << ",\"support_leaf\":";
            if (support.accepted) witness_json(std::cout, observations[support.witness_index], history);
            else std::cout << "null";
            // All actual invocations remain available internally for exact
            // final-PV matching. Persist only the verified support witness;
            // dumping every occurrence would inflate research receipts without
            // adding a second proof of its root-backed score or full history.
            std::cout << ",\"observations_count\":" << observations.size()
                << ",\"support_observation_index\":";
            if (support.accepted) std::cout << support.witness_index;
            else std::cout << "null";
            std::cout << ",\"support_observation_index_base\":0}\n" << std::flush;
        }
        if (std::cin.bad()) throw std::runtime_error("input read failure");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "live choice collection failed: " << error.what() << '\n';
        return 1;
    }
}
