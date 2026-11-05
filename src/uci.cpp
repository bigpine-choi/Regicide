#include "regicide/uci.hpp"
#include "regicide/search.hpp"
#include "regicide/neural.hpp"
#include "regicide/nnue.hpp"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace regicide {
namespace {
std::vector<std::string> words(const std::string& line) {
    std::istringstream stream(line);
    std::vector<std::string> result;
    std::string word;
    while (stream >> word) result.push_back(word);
    return result;
}
std::uint64_t number(const std::string& text, std::uint64_t maximum) {
    std::uint64_t value = 0;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || value > maximum)
        throw std::invalid_argument("invalid numeric limit");
    return value;
}
struct Go {
    SearchLimits limits;
    bool infinite = false;
    std::optional<std::vector<Move>> root_moves;
};
Go parse_go(const std::vector<std::string>& tokens, Color turn, int overhead) {
    Go go;
    go.limits.depth = 64;
    bool bounded = false, explicit_depth = false;
    std::optional<std::int64_t> movetime, white, black;
    std::int64_t winc = 0, binc = 0, moves_to_go = 30;
    for (std::size_t i = 1; i < tokens.size(); ++i) {
        const auto& key = tokens[i];
        if (key == "infinite") { go.infinite = true; continue; }
        if (key == "searchmoves") {
            if (go.root_moves) throw std::invalid_argument("duplicate searchmoves parameter");
            go.root_moves.emplace();
            while (i + 1 < tokens.size()) {
                const auto& next = tokens[i + 1];
                if (next == "depth" || next == "nodes" || next == "movetime" ||
                    next == "wtime" || next == "btime" || next == "winc" ||
                    next == "binc" || next == "movestogo" || next == "infinite" ||
                    next == "ponder" || next == "mate" || next == "searchmoves") break;
                go.root_moves->push_back(Move::from_uci(tokens[++i]));
            }
            if (go.root_moves->empty()) throw std::invalid_argument("searchmoves requires legal moves");
            continue;
        }
        if (key == "ponder" || key == "mate")
            throw std::invalid_argument("unsupported go mode: " + key);
        if (key != "depth" && key != "nodes" && key != "movetime" && key != "wtime" &&
            key != "btime" && key != "winc" && key != "binc" && key != "movestogo")
            throw std::invalid_argument("unknown go parameter: " + key);
        if (++i == tokens.size()) throw std::invalid_argument("missing go value");
        const auto value = number(tokens[i], key == "nodes" ? UINT64_MAX : 1000000000000ULL);
        if (key == "depth") {
            if (value < 1 || value > 64) throw std::invalid_argument("depth must be 1..64");
            go.limits.depth = static_cast<int>(value); explicit_depth = true;
        } else if (key == "nodes") {
            if (!value) throw std::invalid_argument("nodes must be positive");
            go.limits.nodes = value; bounded = true;
        } else if (key == "movetime") { movetime = static_cast<std::int64_t>(value); bounded = true; }
        else if (key == "wtime") white = static_cast<std::int64_t>(value);
        else if (key == "btime") black = static_cast<std::int64_t>(value);
        else if (key == "winc") winc = static_cast<std::int64_t>(value);
        else if (key == "binc") binc = static_cast<std::int64_t>(value);
        else {
            if (value < 1 || value > 1000000) throw std::invalid_argument("movestogo must be 1..1000000");
            moves_to_go = static_cast<std::int64_t>(value);
        }
    }
    if (!go.infinite) {
        if (movetime) {
            go.limits.movetime = std::chrono::milliseconds(std::max<std::int64_t>(1, *movetime - overhead));
        } else if (white || black) {
            const auto clock = turn == Color::white ? white : black;
            if (!clock) throw std::invalid_argument("missing clock for side to move");
            const auto usable = std::max<std::int64_t>(1, *clock - overhead);
            const auto increment = turn == Color::white ? winc : binc;
            const auto allowance = *clock / moves_to_go + increment * 3 / 4;
            go.limits.movetime = std::chrono::milliseconds(std::clamp<std::int64_t>(allowance, 1, usable));
            bounded = true;
        }
        if (!bounded && !explicit_depth) go.limits.depth = 4;
    }
    return go;
}
Board parse_position(const std::vector<std::string>& tokens) {
    if (tokens.size() < 2) throw std::invalid_argument("missing position");
    std::size_t i = 2;
    std::string fen(Board::start_fen);
    if (tokens[1] == "fen") {
        if (tokens.size() < 8) throw std::invalid_argument("position fen requires six fields");
        fen.clear();
        for (; i < 8; ++i) { if (!fen.empty()) fen += ' '; fen += tokens[i]; }
    } else if (tokens[1] != "startpos") throw std::invalid_argument("expected startpos or fen");
    Board candidate(fen);
    if (i < tokens.size()) {
        if (tokens[i++] != "moves") throw std::invalid_argument("expected moves");
        for (; i < tokens.size(); ++i) candidate.play(tokens[i]);
    }
    return candidate;
}

class Session {
public:
    explicit Session(std::ostream& output) : output_(output) {}
    ~Session() { stop(); }
    void stop() {
        {
            // Pair the predicate update with the same mutex used by wait().
            // Otherwise a notify between the predicate check and wait can be lost.
            std::lock_guard lock(wait_mutex_);
            stop_.store(true, std::memory_order_relaxed);
        }
        wake_.notify_all();
        if (worker_.joinable()) worker_.join();
    }
    void send(const std::string& text) {
        std::lock_guard lock(output_mutex_);
        output_ << text << '\n' << std::flush;
    }
    void handle(const std::vector<std::string>& tokens, const std::string& raw_line) {
        if (tokens.empty()) return;
        const auto& command = tokens[0];
        if (command == "isready") { send("readyok"); return; }
        if (command == "uci") {
            send("id name Regicide 0.7.0-dev");
            send("id author Regicide project");
            send("option name Move Overhead type spin default 20 min 0 max 5000");
            send("option name UsePositionalEvaluation type check default true");
            send("option name UseQuiescence type check default true");
            send("option name UseTranspositionTable type check default true");
            send("option name Hash type spin default 16 min 1 max 256");
            send("option name Clear Hash type button");
            send("option name UseFastMoves type check default true");
            send("option name UseQuietMoveOrdering type check default true");
            send("option name UseQuiescenceChecks type check default true");
            send("option name UseQuiescenceThreats type check default false");
            send("option name UseQuiescenceThreatCache type check default false");
            send("option name UseTacticalThreatOrdering type check default false");
            send("option name UseQuiescenceReplyBudget type check default false");
            send("option name UseQuiescenceQueenDefense type check default false");
            send("option name UseQuiescenceForcing type check default false");
            send("option name PVS type check default true");
            send("option name UseSEE type check default true");
            send("option name UsePawnShield type check default true");
            send("option name UsePawnStructure type check default true");
            send("option name UsePieceActivity type check default false");
            send("option name UseLMR type check default true");
            send("option name EvalFile type string default <empty>");
            send("option name UseNeuralEvaluation type check default false");
            send("option name UseNNUEEvaluation type check default false");
            send("option name NeuralModelFile type string default <builtin>");
            send("option name UseIncrementalNeuralEvaluation type check default true");
            send("info string integer NNUE is experimental; default remains classical");
            send("uciok");
        } else if (command == "stop") stop();
        else if (command == "ucinewgame") { stop(); context_.clear(); board_ = Board(); }
        else if (command == "position") { stop(); board_ = parse_position(tokens); }
        else if (command == "setoption") {
            // Options are changed only after a current search has been joined.
            stop();
            set_option(tokens, raw_line);
        } else if (command == "go") {
            stop();
            try {
                const Go go = parse_go(tokens, board_.position().turn, overhead_);
                start(go);
            } catch (const std::exception& e) {
                send(std::string("info string ") + e.what()); send("bestmove 0000");
            }
        } else if (command != "debug" && command != "register" && command != "ponderhit")
            send("info string unknown command: " + command);
    }
private:
    void set_option(const std::vector<std::string>& tokens, const std::string& raw_line) {
        if (tokens.size() < 3 || tokens[1] != "name") return;
        const auto value = std::find(tokens.begin() + 2, tokens.end(), "value");
        std::string option;
        for (auto i = tokens.begin() + 2; i != value; ++i) {
            if (!option.empty()) option += ' ';
            option += *i;
        }
        if (option == "Clear Hash") {
            if (value != tokens.end()) throw std::invalid_argument("Clear Hash is a button without a value");
            context_.clear(); return;
        }
        if (option == "NeuralModelFile") {
            std::istringstream raw(raw_line);
            std::string token, path;
            for (int i = 0; i < 4; ++i) raw >> token;
            std::getline(raw, path);
            const auto first = path.find_first_not_of(" \t");
            path = first == std::string::npos ? std::string{} : path.substr(first);
            if (!path.empty() && path.back() == '\r') path.pop_back();
            std::shared_ptr<const NeuralModel> replacement;
            // The original NNUE protocol treats an empty string as builtin.
            if (!path.empty() && path != "<builtin>") replacement = load_neural_model(path);
            options_.nnue_model = std::move(replacement);
            context_.clear();
            const auto& selected = options_.nnue_model ? *options_.nnue_model : builtin_neural_model();
            send("info string neural model checksum " + std::to_string(neural_model_checksum(selected)));
            return;
        }
        if (option == "UseNNUEEvaluation" || option == "UseIncrementalNeuralEvaluation") {
            if (value == tokens.end() || tokens.end() - value != 2 ||
                (*(value + 1) != "true" && *(value + 1) != "false"))
                throw std::invalid_argument(option + " requires true or false");
            const bool enabled = *(value + 1) == "true";
            if (option == "UseNNUEEvaluation") {
                if (enabled && neural_enabled_)
                    throw std::invalid_argument("disable UseNeuralEvaluation before enabling NNUE");
                options_.nnue = enabled;
            } else options_.nnue_incremental = enabled;
            context_.clear();
            return;
        }
        if (option == "EvalFile") {
            if (value == tokens.end() || value + 1 == tokens.end())
                throw std::invalid_argument("EvalFile requires a model path");
            // UCI string values can contain consecutive spaces. Reading the
            // untouched value preserves filenames that token joining changes.
            std::istringstream raw(raw_line);
            std::string token, path;
            for (int i = 0; i < 4; ++i) raw >> token;
            std::getline(raw, path);
            const auto first = path.find_first_not_of(" \t");
            path = first == std::string::npos ? std::string{} : path.substr(first);
            if (!path.empty() && path.back() == '\r') path.pop_back();
            if (path.empty()) throw std::invalid_argument("EvalFile requires a model path");
            if (path == "<empty>") {
                loaded_model_.reset();
                options_.neural.reset();
                neural_enabled_ = false;
            } else {
                // Validate completely before replacing any live option.
                auto replacement = NeuralEvaluator::load(path);
                loaded_model_ = std::move(replacement);
                if (neural_enabled_) options_.neural = loaded_model_;
            }
            return;
        }
        if (option == "UseNeuralEvaluation") {
            if (value == tokens.end() || tokens.end() - value != 2 ||
                (*(value + 1) != "true" && *(value + 1) != "false"))
                throw std::invalid_argument("UseNeuralEvaluation requires true or false");
            const bool enabled = *(value + 1) == "true";
            if (enabled && !loaded_model_)
                throw std::invalid_argument("load a valid EvalFile before enabling neural evaluation");
            if (enabled && options_.nnue)
                throw std::invalid_argument("disable UseNNUEEvaluation before enabling residual neural evaluation");
            neural_enabled_ = enabled;
            options_.neural = enabled ? loaded_model_ : nullptr;
            return;
        }
        if (option != "Move Overhead" && option != "UsePositionalEvaluation" && option != "UseQuiescence" &&
            option != "UseTranspositionTable" && option != "UseFastMoves" && option != "Hash" &&
            option != "UseQuietMoveOrdering" && option != "UseQuiescenceChecks" && option != "PVS" &&
            option != "UseSEE" && option != "UsePawnShield" && option != "UsePawnStructure" &&
            option != "UsePieceActivity" && option != "UseLMR" && option != "UseQuiescenceThreats" &&
            option != "UseQuiescenceThreatCache" && option != "UseTacticalThreatOrdering" &&
            option != "UseQuiescenceReplyBudget" && option != "UseQuiescenceQueenDefense" &&
            option != "UseQuiescenceForcing") return;
        if (value == tokens.end() || tokens.end() - value != 2)
            throw std::invalid_argument("missing or extra option value");
        if (option == "Move Overhead") { overhead_ = static_cast<int>(number(*(value + 1), 5000)); return; }
        if (option == "Hash") {
            const auto amount = number(*(value + 1), 256);
            if (!amount) throw std::invalid_argument("Hash must be 1..256 MiB");
            options_.hash_mb = static_cast<int>(amount); return;
        }
        const auto& text = *(value + 1);
        if (text != "true" && text != "false") throw std::invalid_argument("check option requires true or false");
        if (option == "UsePositionalEvaluation") options_.positional = text == "true";
        else if (option == "UseQuiescence") options_.quiescence = text == "true";
        else if (option == "UseTranspositionTable") options_.transposition = text == "true";
        else if (option == "UseFastMoves") options_.fast_moves = text == "true";
        else if (option == "UseQuietMoveOrdering") options_.quiet_ordering = text == "true";
        else if (option == "UseQuiescenceChecks") options_.quiescence_checks = text == "true";
        else if (option == "UseQuiescenceThreats") options_.quiescence_threats = text == "true";
        else if (option == "UseQuiescenceThreatCache") options_.quiescence_cache = text == "true";
        else if (option == "UseTacticalThreatOrdering") options_.tactical_threat_ordering = text == "true";
        else if (option == "UseQuiescenceReplyBudget") options_.quiescence_horizon_budget = text == "true";
        else if (option == "UseQuiescenceQueenDefense") options_.quiescence_queen_defense = text == "true";
        else if (option == "UseQuiescenceForcing") options_.quiescence_forcing = text == "true";
        else if (option == "PVS") options_.pvs = text == "true";
        else if (option == "UseSEE") options_.see_ordering = text == "true";
        else if (option == "UsePawnShield") options_.king_pawn_shield = text == "true";
        else if (option == "UsePawnStructure") options_.pawn_structure = text == "true";
        else if (option == "UsePieceActivity") options_.piece_activity = text == "true";
        else options_.lmr = text == "true";
    }
    void progress(const SearchResult& result) {
        std::ostringstream line;
        line << "info depth " << result.depth << " seldepth " << result.seldepth << " score ";
        if (std::abs(result.score) >= mate_score - max_search_ply) {
            const int distance = mate_score - std::abs(result.score);
            const int moves = (distance + 1) / 2;
            line << "mate " << (result.score < 0 ? -moves : moves);
        } else line << "cp " << result.score;
        line << " nodes " << result.nodes << " time " << result.elapsed.count();
        if (!result.pv.empty()) {
            line << " pv";
            for (auto move : result.pv) line << ' ' << move.uci();
        }
        send(line.str());
        // Keep TT as the final string for clients retaining only one string.
        send("info string pvs probes " + std::to_string(result.pvs_probes) +
             " researches " + std::to_string(result.pvs_researches));
        send("info string see calls " + std::to_string(result.see_calls));
        send("info string lmr reductions " + std::to_string(result.lmr_reductions) +
             " researches " + std::to_string(result.lmr_researches));
        send("info string qthreat candidates " + std::to_string(result.quiet_threat_candidates) +
             " extensions " + std::to_string(result.quiet_threat_extensions) +
             " defenses " + std::to_string(result.defensive_qnodes) +
             " pruned " + std::to_string(result.qsee_pruned));
        send("info string qcache probes " + std::to_string(result.qcache_probes) +
             " hits " + std::to_string(result.qcache_hits) +
             " cutoffs " + std::to_string(result.qcache_cutoffs) +
             " stores " + std::to_string(result.qcache_stores) +
             " bytes " + std::to_string(result.qcache_bytes));
        send("info string rootthreat candidates " + std::to_string(result.root_threat_candidates) +
             " searches " + std::to_string(result.root_threat_searches));
        send("info string queen-defense qnodes " + std::to_string(result.queen_defense_qnodes));
        send("info string forcing checks " + std::to_string(result.forcing_checks));
        send("info string tt probes " + std::to_string(result.tt_probes) + " hits " + std::to_string(result.tt_hits) +
             " cutoffs " + std::to_string(result.tt_cutoffs) + " stores " + std::to_string(result.tt_stores) +
             " allocations " + std::to_string(result.tt_allocations) + " reuses " + std::to_string(result.tt_reuses) +
             " clears " + std::to_string(result.tt_clears) + " bytes " + std::to_string(result.tt_bytes));
    }
    void start(Go go) {
        stop_.store(false, std::memory_order_relaxed);
        const Board snapshot = board_;
        auto options = options_;
        options.root_moves = std::move(go.root_moves);
        worker_ = std::thread([this, snapshot, go, options] {
            try {
                auto result = search(snapshot, go.limits, &stop_, [this](const auto& r) { progress(r); }, options, &context_);
                if (go.infinite) {
                    std::unique_lock lock(wait_mutex_);
                    wake_.wait(lock, [this] { return stop_.load(std::memory_order_relaxed); });
                }
                // Includes a legal fallback when no iteration completed within a budget.
                progress(result);
                send("bestmove " + (result.best_move ? result.best_move->uci() : std::string("0000")));
            } catch (const std::exception& e) {
                send(std::string("info string search failed: ") + e.what()); send("bestmove 0000");
            }
        });
    }
    Board board_;
    std::ostream& output_;
    std::mutex output_mutex_, wait_mutex_;
    std::condition_variable wake_;
    std::atomic_bool stop_{false};
    std::thread worker_;
    int overhead_ = 20;
    SearchOptions options_;
    std::shared_ptr<const NeuralEvaluator> loaded_model_;
    bool neural_enabled_ = false;
    SearchContext context_; // Accessed only after joining any prior search worker.
};
} // namespace

int run_uci(std::istream& input, std::ostream& output) {
    Session session(output);
    std::string line;
    while (std::getline(input, line)) {
        const auto tokens = words(line);
        if (!tokens.empty() && tokens[0] == "quit") break;
        try { session.handle(tokens, line); }
        catch (const std::exception& e) { session.send(std::string("info string ") + e.what()); }
    }
    session.stop();
    return 0;
}
} // namespace regicide
