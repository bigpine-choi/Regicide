#include "regicide/see.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

// Build the archived SEE source with its public symbol renamed to
// reference_static_exchange_evaluation and define REGICIDE_SEE_COMPARE here.
// No reference implementation or profiling clock enters the playing engine.
#ifdef REGICIDE_SEE_COMPARE
namespace regicide {
int reference_static_exchange_evaluation(const Board&, Move);
}
#endif

namespace {
struct Query {
    regicide::Board board;
    regicide::Move move;
};
using Evaluator = int (*)(const regicide::Board&, regicide::Move);
struct Sample {
    std::int64_t nanoseconds;
    std::int64_t score_sum;
};
Sample measure(const std::vector<Query>& queries, int repeats, Evaluator evaluate) {
    std::int64_t score_sum = 0;
    const auto started = std::chrono::steady_clock::now();
    for (int repeat = 0; repeat < repeats; ++repeat)
        for (const auto& query : queries) score_sum += evaluate(query.board, query.move);
    const auto elapsed = std::chrono::steady_clock::now() - started;
    return {std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count(), score_sum};
}
void print_sample(const char* name, const Sample& sample, std::uint64_t calls) {
    std::cout << '\"' << name << "\":{\"nanoseconds\":" << sample.nanoseconds
              << ",\"calls\":" << calls << ",\"score_sum\":" << sample.score_sum << '}';
}
} // namespace

// Input is FEN<TAB>UCI, one independently parsed legal query per line.
// Parsing, legal-move generation, exact-value comparison and board snapshots
// stay outside timing. Samples alternate measurement order to balance warmup.
int main(int argc, char** argv) {
    try {
        int repeats = 1000, samples = 7;
        bool emit_values = false;
        for (int i = 1; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--emit-values") emit_values = true;
            else if ((option == "--repeats" || option == "--samples") && i + 1 < argc) {
                const int count = std::stoi(argv[++i]);
                if (count < 1 || count > 1000000) throw std::invalid_argument("count outside 1..1000000");
                if (option == "--repeats") repeats = count;
                else samples = count;
            } else throw std::invalid_argument("usage: profile_see [--repeats N] [--samples N] [--emit-values]");
        }
        std::vector<Query> queries;
        std::string line;
        while (std::getline(std::cin, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (line.empty()) continue;
            const auto separator = line.find('\t');
            if (separator == std::string::npos) throw std::invalid_argument("expected FEN<TAB>UCI");
            regicide::Board board(line.substr(0, separator));
            const auto move = regicide::Move::from_uci(line.substr(separator + 1));
            const auto legal = board.legal_moves();
            if (std::find(legal.begin(), legal.end(), move) == legal.end())
                throw std::invalid_argument("illegal workload move");
            queries.push_back({std::move(board), move});
        }
        if (queries.empty() || queries.size() > 1000000) throw std::invalid_argument("expected 1..1000000 queries");
        std::int64_t expected_sum = 0;
        for (std::size_t i = 0; i < queries.size(); ++i) {
            const auto& query = queries[i];
            const auto saved = query.board;
            const int actual = regicide::static_exchange_evaluation(query.board, query.move);
#ifdef REGICIDE_SEE_COMPARE
            const int reference = regicide::reference_static_exchange_evaluation(query.board, query.move);
            if (actual != reference) throw std::runtime_error("SEE value mismatch at query " + std::to_string(i));
#endif
            if (!(query.board == saved)) throw std::runtime_error("SEE changed board/history");
            expected_sum += actual;
            if (emit_values) std::cout << i << '\t' << actual << '\n';
        }
        if (emit_values) return 0;
        const auto calls = static_cast<std::uint64_t>(queries.size()) * static_cast<unsigned>(repeats);
        std::cout << "{\"queries\":" << queries.size() << ",\"repeats\":" << repeats
                  << ",\"expected_score_sum\":" << expected_sum * repeats
                  << ",\"samples\":[";
        for (int sample = 0; sample < samples; ++sample) {
            Sample candidate{};
#ifdef REGICIDE_SEE_COMPARE
            Sample reference{};
            if (sample % 2 == 0) {
                reference = measure(queries, repeats, regicide::reference_static_exchange_evaluation);
                candidate = measure(queries, repeats, regicide::static_exchange_evaluation);
            } else {
                candidate = measure(queries, repeats, regicide::static_exchange_evaluation);
                reference = measure(queries, repeats, regicide::reference_static_exchange_evaluation);
            }
#else
            candidate = measure(queries, repeats, regicide::static_exchange_evaluation);
#endif
            if (candidate.score_sum != expected_sum * repeats) throw std::runtime_error("candidate checksum changed");
            if (sample) std::cout << ',';
            std::cout << "{\"index\":" << sample << ',';
#ifdef REGICIDE_SEE_COMPARE
            if (reference.score_sum != candidate.score_sum) throw std::runtime_error("reference checksum mismatch");
            std::cout << "\"reference_first\":" << (sample % 2 == 0 ? "true" : "false") << ',';
            print_sample("reference", reference, calls);
            std::cout << ',';
#endif
            print_sample("candidate", candidate, calls);
            std::cout << '}';
        }
        std::cout << "]}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
