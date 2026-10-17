#include "regicide/board.hpp"

#include <algorithm>
#include <charconv>
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
            std::cout << "Regicide rules core\n"
                         "regicide [moves|inspect] [--fen \"six-field FEN\"] [--play e2e4 e7e5 ...]\n"
                         "regicide [perft|divide] DEPTH [--fen \"FEN\"] [--play ...]\n"
                         "regicide batch  (stdin: FEN|moves; stdout: tab-separated state)\n";
            return 0;
        }
        if (command == "batch") {
            if (argc != 2) throw std::invalid_argument("batch takes no arguments");
            batch(); return 0;
        }
        if (command != "moves" && command != "inspect" && command != "perft" && command != "divide")
            throw std::invalid_argument("unknown command; see --help");
        int arg = 2, depth = 0;
        if (command == "perft" || command == "divide") {
            if (arg >= argc) throw std::invalid_argument("missing depth");
            depth = depth_arg(argv[arg++]);
        }
        std::string fen(Board::start_fen);
        std::vector<std::string> moves;
        while (arg < argc) {
            const std::string option = argv[arg++];
            if (option == "--fen" && arg < argc) fen = argv[arg++];
            else if (option == "--play" && arg < argc) {
                while (arg < argc && std::string_view(argv[arg]).substr(0, 2) != "--") moves.emplace_back(argv[arg++]);
            } else throw std::invalid_argument("invalid option or missing value");
        }
        Board board(fen);
        for (const auto& move : moves) board.play(move);
        if (command == "perft") std::cout << "nodes: " << board.perft(depth) << '\n';
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
