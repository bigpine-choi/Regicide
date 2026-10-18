#include "regicide/board.hpp"
#include "regicide/search.hpp"
#include "regicide/neural.hpp"
#include "regicide/uci.hpp"

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>

using regicide::Board;
namespace {
void print_moves(const Board& board) {
    auto moves = board.legal_moves();
    std::sort(moves.begin(), moves.end(), [](auto a, auto b) { return a.uci() < b.uci(); });
    bool first = true;
    for (auto move : moves) {
        if (!first) std::cout << ' ';
        std::cout << move.uci();
        first = false;
    }
}
void batch() {
    // Input: six-field FEN [| space-separated coordinate moves]. One result per line.
    std::string line;
    while (std::getline(std::cin, line)) {
        try {
            const auto split = line.find('|');
            Board board(line.substr(0, split));
            if (split != std::string::npos) {
                std::istringstream moves(line.substr(split + 1));
                std::string move;
                while (moves >> move) board.play(move);
            }
            const auto saved = board;
            const auto moves = board.legal_moves();
            for (auto move : moves) { board.play(move); board.undo(); }
            if (board != saved) throw std::logic_error("state restoration failed");
            const auto claims = board.claims_now();
            std::cout << board.fen() << '\t';
            print_moves(board);
            std::cout << '\t' << regicide::name(board.outcome()) << '\t' << board.repetition_count()
                      << '\t' << claims.threefold << '\t' << claims.fifty_moves << '\n';
        } catch (const std::exception& e) {
            std::cout << "ERROR\t" << e.what() << '\n';
        }
    }
}
int depth_arg(std::string_view text) {
    int value = -1;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size())
        throw std::invalid_argument("invalid depth");
    return value;
}
}
int main(int argc, char** argv) {
    try {
        const std::string command = argc > 1 ? argv[1] : "moves";
        if (command == "--help" || command == "help") {
            std::cout << "Regicide 0.6.5\n"
                         "regicide [moves|inspect] [--fen \"six-field FEN\"] [--play e2e4 e7e5 ...]\n"
                         "regicide [perft|divide] DEPTH [--fen \"FEN\"] [--play ...]\n"
                         "regicide search [DEPTH=4] [--nodes N] [--movetime MS] [--fen \"FEN\"] [--play ...]\n"
                         "  optional search flags: --no-position --no-quiescence --no-tt --hash MiB --no-fast-moves\n"
                         "    --no-quiet-ordering --no-quiescence-checks --no-see --no-pawn-shield --no-pawn-structure --no-lmr\n"
                         "    --eval-file MODEL (experimental neural evaluation; default remains classical)\n"
                         "regicide eval --eval-file MODEL [--fen \"FEN\"] [--play ...] (static cp only)\n"
                         "    optional eval flags: --no-pawn-shield --no-pawn-structure\n"
                         "regicide uci  (UCI engine protocol)\n"
                         "regicide batch  (stdin: FEN|moves; stdout: tab-separated state)\n";
            return 0;
        }
        if (command == "batch") {
            if (argc != 2) throw std::invalid_argument("batch takes no arguments");
            batch(); return 0;
        }
        if (command == "uci") {
            if (argc != 2) throw std::invalid_argument("uci takes no arguments");
            return regicide::run_uci(std::cin, std::cout);
        }
        if (command != "moves" && command != "inspect" && command != "perft" && command != "divide" && command != "search" && command != "eval")
            throw std::invalid_argument("unknown command; see --help");
        int arg = 2, depth = 0;
        if (command == "perft" || command == "divide") {
            if (arg >= argc) throw std::invalid_argument("missing depth");
            depth = depth_arg(argv[arg++]);
        }
        regicide::SearchLimits limits;
        regicide::SearchOptions search_options;
        if (command == "search" && arg < argc && std::string_view(argv[arg]).substr(0, 2) != "--")
            limits.depth = depth_arg(argv[arg++]);
        std::string fen(Board::start_fen);
        std::vector<std::string> moves;
        while (arg < argc) {
            const std::string option = argv[arg++];
            if (option == "--fen" && arg < argc) fen = argv[arg++];
            else if ((command == "search" || command == "eval") && option == "--eval-file" && arg < argc)
                search_options.neural = regicide::NeuralEvaluator::load(argv[arg++]);
            else if (command == "search" && option == "--no-position") search_options.positional = false;
            else if (command == "search" && option == "--no-quiescence") search_options.quiescence = false;
            else if (command == "search" && option == "--no-tt") search_options.transposition = false;
            else if (command == "search" && option == "--no-fast-moves") search_options.fast_moves = false;
            else if (command == "search" && option == "--no-quiet-ordering") search_options.quiet_ordering = false;
            else if (command == "search" && option == "--no-quiescence-checks") search_options.quiescence_checks = false;
            else if (command == "search" && option == "--no-see") search_options.see_ordering = false;
            else if ((command == "search" || command == "eval") && option == "--no-pawn-shield") search_options.king_pawn_shield = false;
            else if ((command == "search" || command == "eval") && option == "--no-pawn-structure") search_options.pawn_structure = false;
            else if (command == "search" && option == "--no-lmr") search_options.lmr = false;
            else if (command == "search" && option == "--hash" && arg < argc) {
                const int amount = depth_arg(argv[arg++]);
                if (amount < 1 || amount > 256) throw std::invalid_argument("hash must be 1..256 MiB");
                search_options.hash_mb = amount;
            }
            else if (option == "--play" && arg < argc) {
                while (arg < argc && std::string_view(argv[arg]).substr(0, 2) != "--") moves.emplace_back(argv[arg++]);
            } else if (command == "search" && option == "--nodes" && arg < argc) {
                const std::string_view value(argv[arg++]);
                const auto result = std::from_chars(value.data(), value.data() + value.size(), limits.nodes);
                if (result.ec != std::errc{} || result.ptr != value.data() + value.size() || !limits.nodes)
                    throw std::invalid_argument("nodes must be a positive integer");
            } else if (command == "search" && option == "--movetime" && arg < argc) {
                const int time = depth_arg(argv[arg++]);
                if (time < 1) throw std::invalid_argument("movetime must be positive milliseconds");
                limits.movetime = std::chrono::milliseconds(time);
            } else throw std::invalid_argument("invalid option or missing value");
        }
        Board board(fen);
        for (const auto& move : moves) board.play(move);
        if (command == "eval") {
            if (!search_options.neural) throw std::invalid_argument("eval requires --eval-file MODEL");
            std::cout << "score_cp: " << search_options.neural->evaluate(
                board, search_options.king_pawn_shield, search_options.pawn_structure) << '\n';
        } else if (command == "search") {
            const auto result = regicide::search(board, limits, nullptr, {}, search_options);
            std::cout << "bestmove: " << (result.best_move ? result.best_move->uci() : "0000")
                      << '\n';
            if (std::abs(result.score) >= regicide::mate_score - regicide::max_search_ply) {
                const int mate = (regicide::mate_score - std::abs(result.score) + 1) / 2;
                std::cout << "score_mate: " << (result.score < 0 ? -mate : mate);
            } else std::cout << "score_cp: " << result.score;
            std::cout << "\ndepth: " << result.depth
                      << "\nnodes: " << result.nodes << "\ntime_ms: " << result.elapsed.count()
                      << "\nqnodes: " << result.qnodes << "\nseldepth: " << result.seldepth
                      << "\ntt_probes: " << result.tt_probes << "\ntt_hits: " << result.tt_hits
                      << "\ntt_cutoffs: " << result.tt_cutoffs << "\ntt_stores: " << result.tt_stores
                      << "\ntt_allocations: " << result.tt_allocations << "\ntt_reuses: " << result.tt_reuses
                      << "\ntt_clears: " << result.tt_clears << "\ntt_bytes: " << result.tt_bytes
                      << "\nsee_calls: " << result.see_calls
                      << "\nlmr_reductions: " << result.lmr_reductions
                      << "\nlmr_researches: " << result.lmr_researches
                      << "\nstopped: " << result.stopped << "\npv:";
            for (auto move : result.pv) std::cout << ' ' << move.uci();
            std::cout << '\n';
        } else if (command == "perft") std::cout << "nodes: " << board.perft(depth) << '\n';
        else if (command == "divide") {
            std::uint64_t total = 0;
            for (auto [move, nodes] : board.divide(depth)) {
                std::cout << move.uci() << ": " << nodes << '\n'; total += nodes;
            }
            std::cout << "nodes: " << total << '\n';
        } else {
            std::cout << "fen: " << board.fen() << "\nlegal_moves: " << board.legal_moves().size() << '\n';
            print_moves(board);
            std::cout << '\n';
            const auto saved = board;
            for (auto move : board.legal_moves()) { board.play(move); board.undo(); }
            if (saved != board) throw std::logic_error("state restoration failed");
            std::cout << "make_undo: OK\n";
            if (command == "inspect") {
                const auto claims = board.claims_now();
                std::cout << "check: " << board.in_check() << "\noutcome: " << regicide::name(board.outcome())
                          << "\nrepetitions: " << board.repetition_count()
                          << "\nclaim_threefold_now: " << claims.threefold
                          << "\nclaim_fifty_moves_now: " << claims.fifty_moves << '\n';
            }
        }
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << '\n';
        return 1;
    }
}
