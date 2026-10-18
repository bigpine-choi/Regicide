#include "regicide/evaluation.hpp"
#include "reference_evaluation_v054.hpp"
#include "reference_evaluation_v056.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

// Build this driver, the frozen reference, and the candidate as separate
// translation units WITHOUT LTO. This prevents hoisting repeated pure-looking
// evaluation calls. Board construction, equivalence checks, and IO are untimed.
namespace {
using Clock = std::chrono::steady_clock;
using Evaluation = int (*)(const regicide::Board&, bool);
constexpr std::uint64_t warmup_calls = 10000;
constexpr std::size_t block_count = 9;
constexpr std::array<std::array<std::size_t, 4>, 4> orders{{
    {{0, 1, 2, 3}}, {{3, 2, 1, 0}}, {{1, 0, 3, 2}}, {{2, 3, 0, 1}}
}};
constexpr std::array<const char*, 4> order_names{{"ABCD", "DCBA", "BADC", "CDAB"}};

struct Variant {
    const char* name;
    Evaluation evaluate;
    bool shield;
};
// Keep the original v0.5.4/v0.5.5 benchmark meaningful after the public
// evaluator gains another positional feature. The old/new comparison must
// explicitly disable pawn structure on the current implementation.
int current_legacy(const regicide::Board& board, bool shield) {
    return regicide::evaluate_position(board, shield, false);
}

int current_structure(const regicide::Board& board, bool shield) {
    return regicide::evaluate_position(board, shield, true);
}

int old_structure(const regicide::Board& board, bool shield) {
    return regicide::evaluate_position_v056(board, shield, true);
}

const std::array<Variant, 4> legacy_variants{{
    {"old_off", regicide::evaluate_position_v054, false},
    {"old_on", regicide::evaluate_position_v054, true},
    {"new_off", current_legacy, false},
    {"new_on", current_legacy, true}
}};
const std::array<Variant, 4> structure_variants{{
    {"structure_off_shield_off", current_legacy, false},
    {"structure_off_shield_on", current_legacy, true},
    {"structure_on_shield_off", current_structure, false},
    {"structure_on_shield_on", current_structure, true}
}};
const std::array<Variant, 4> optimization_variants{{
    {"old_v056_shield_off", old_structure, false},
    {"old_v056_shield_on", old_structure, true},
    {"new_shield_off", current_structure, false},
    {"new_shield_on", current_structure, true}
}};
constexpr std::array<const char*, 4> legacy_ratio_names{{
    "new_on_over_old_on", "new_off_over_old_off",
    "old_on_over_old_off", "new_on_over_new_off"
}};
constexpr std::array<const char*, 4> structure_ratio_names{{
    "structure_on_shield_on_over_structure_off_shield_on",
    "structure_on_shield_off_over_structure_off_shield_off",
    "structure_off_shield_on_over_structure_off_shield_off",
    "structure_on_shield_on_over_structure_on_shield_off"
}};
constexpr std::array<const char*, 4> optimization_ratio_names{{
    "new_shield_on_over_old_v056_shield_on",
    "new_shield_off_over_old_v056_shield_off",
    "old_v056_shield_on_over_old_v056_shield_off",
    "new_shield_on_over_new_shield_off"
}};

struct Options {
    std::string fen_file;
    std::string output;
    bool verify_only = false;
    bool compare_pawn_structure = false;
    bool compare_pawn_optimization = false;
    std::uint64_t calls = 0;
    double target_ms = 150.0;
};

struct Sample {
    std::size_t block;
    std::size_t order;
    std::size_t variant;
    std::int64_t elapsed_ns;
    std::int64_t checksum;
    double ns_per_call;
};

Options parse_options(int argc, char** argv) {
    Options result;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        if (argument == "--verify-only") {
            result.verify_only = true;
            continue;
        }
        if (argument == "--compare-pawn-structure") {
            result.compare_pawn_structure = true;
            continue;
        }
        if (argument == "--compare-pawn-optimization") {
            result.compare_pawn_optimization = true;
            continue;
        }
        if (i + 1 == argc) throw std::invalid_argument("Missing value for " + argument);
        const std::string value = argv[++i];
        if (argument == "--fen-file") result.fen_file = value;
        else if (argument == "--output") result.output = value;
        else if (argument == "--calls") {
            std::size_t parsed = 0;
            result.calls = std::stoull(value, &parsed);
            if (parsed != value.size() || value.front() == '-' ||
                result.calls == 0 || result.calls > 1000000000ULL) {
                throw std::invalid_argument("--calls must be 1..1000000000");
            }
        } else if (argument == "--target-ms") {
            std::size_t parsed = 0;
            result.target_ms = std::stod(value, &parsed);
            if (parsed != value.size() || !std::isfinite(result.target_ms) ||
                result.target_ms < 10.0 || result.target_ms > 1000.0) {
                throw std::invalid_argument("--target-ms must be 10..1000");
            }
        } else throw std::invalid_argument("Unknown option " + argument);
    }
    if (result.compare_pawn_structure && result.compare_pawn_optimization)
        throw std::invalid_argument("Choose only one comparison mode");
    if (result.fen_file.empty()) throw std::invalid_argument("--fen-file is required");
    return result;
}

std::vector<regicide::Board> load_boards(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("Cannot open FEN file " + path);
    std::vector<regicide::Board> result;
    std::string line;
    std::size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        const auto first = line.find_first_not_of(" \t\r");
        if (first == std::string::npos || line[first] == '#') continue;
        const auto last = line.find_last_not_of(" \t\r");
        try {
            result.emplace_back(line.substr(first, last - first + 1));
        } catch (const std::exception& error) {
            throw std::runtime_error("FEN line " + std::to_string(line_number) + ": " + error.what());
        }
    }
    if (result.empty()) throw std::runtime_error("FEN corpus is empty");
    return result;
}

// All variants visit precisely the same boards in the same order, with equal
// call counts. A signed checksum is returned and emitted, not discarded.
Sample measure(const Variant& variant, const std::vector<regicide::Board>& boards,
               std::uint64_t calls) {
    std::int64_t checksum = 0;
    std::size_t index = 0;
    const auto start = Clock::now();
    for (std::uint64_t call = 0; call < calls; ++call) {
        checksum += variant.evaluate(boards[index], variant.shield);
        if (++index == boards.size()) index = 0;
    }
    const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start).count();
    return {0, 0, 0, elapsed, checksum, static_cast<double>(elapsed) / static_cast<double>(calls)};
}

double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const std::size_t middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2.0;
}

void output_json(const std::string& data, const std::string& path) {
    if (!path.empty()) {
        std::ofstream output(path, std::ios::binary);
        if (!output) throw std::runtime_error("Cannot write output file " + path);
        output << data;
        if (!output) throw std::runtime_error("Failed writing output file " + path);
    }
    std::cout << data;
}

int run(const Options& options) {
    if (!options.output.empty() && std::filesystem::exists(options.output))
        throw std::runtime_error("Output already exists; choose a new output path");
    const auto boards = load_boards(options.fen_file);
    const auto& variants = options.compare_pawn_optimization ? optimization_variants :
                           options.compare_pawn_structure ? structure_variants : legacy_variants;
    const auto& ratio_names = options.compare_pawn_optimization ? optimization_ratio_names :
                              options.compare_pawn_structure ? structure_ratio_names : legacy_ratio_names;
    std::ostringstream mismatches;
    std::size_t mismatch_count = 0;
    for (std::size_t i = 0; i < boards.size(); ++i) {
        for (const bool shield : {false, true}) {
            const int before = regicide::evaluate_position_v054(boards[i], shield);
            const int after = current_legacy(boards[i], shield);
            if (before != after) {
                if (mismatch_count++) mismatches << ',';
                mismatches << "{\"position\":" << i << ",\"shield\":" << (shield ? "true" : "false")
                           << ",\"old\":" << before << ",\"new\":" << after << '}';
            }
        }
    }
    std::ostringstream optimization_mismatches;
    std::size_t optimization_mismatch_count = 0;
    if (options.compare_pawn_optimization) {
        // Untimed per-position checks, including structure OFF, detect value
        // changes that a summed checksum could accidentally conceal.
        for (std::size_t i = 0; i < boards.size(); ++i) {
            for (const bool shield : {false, true}) for (const bool structure : {false, true}) {
                const int before = regicide::evaluate_position_v056(boards[i], shield, structure);
                const int after = regicide::evaluate_position(boards[i], shield, structure);
                if (before != after) {
                    if (optimization_mismatch_count++) optimization_mismatches << ',';
                    optimization_mismatches << "{\"position\":" << i << ",\"shield\":"
                        << (shield ? "true" : "false") << ",\"structure\":"
                        << (structure ? "true" : "false") << ",\"old\":" << before
                        << ",\"new\":" << after << '}';
                }
            }
        }
    }
    std::ostringstream output;
    output << std::setprecision(12);
    output << "{\n  \"schema\":\"regicide-evaluation-profile-v1\",\n"
           << "  \"mode\":\"" << (options.compare_pawn_optimization ? "pawn_optimization_" :
                                     options.compare_pawn_structure ? "pawn_structure_" : "")
           << (options.verify_only ? "verify" : "profile") << "\",\n"
           << "  \"positions\":" << boards.size() << ",\n"
           << "  \"equivalence\":{\"scope\":\"structure_off_vs_frozen_v054_both_shield_settings\",\"comparisons\":" << boards.size() * 2
           << ",\"mismatch_count\":" << mismatch_count << ",\"mismatches\":[" << mismatches.str() << "]}";
    if (options.compare_pawn_optimization) {
        output << ",\n  \"optimization_equivalence\":{\"scope\":\"all_four_option_combinations_vs_frozen_v056\",\"comparisons\":"
               << boards.size() * 4 << ",\"mismatch_count\":" << optimization_mismatch_count
               << ",\"mismatches\":[" << optimization_mismatches.str() << "]}";
    }
    if (options.verify_only || mismatch_count != 0 || optimization_mismatch_count != 0) {
        output << "\n}\n";
        output_json(output.str(), options.output);
        return mismatch_count == 0 && optimization_mismatch_count == 0 ? 0 : 1;
    }

    std::array<std::int64_t, 4> warmup_checksums{};
    for (std::size_t i = 0; i < variants.size(); ++i) {
        warmup_checksums[i] = measure(variants[i], boards, warmup_calls).checksum;
    }
    std::uint64_t calls = options.calls;
    Sample calibration{};
    std::uint64_t calibration_calls = 0;
    if (!calls) {
        // One baseline-only calibration chooses the count; all variants then
        // receive this same count. Calibration never feeds any reported ratio.
        const auto board_count = static_cast<std::uint64_t>(boards.size());
        calibration_calls = ((100000ULL + board_count - 1) / board_count) * board_count;
        calibration = measure(variants[1], boards, calibration_calls);
        if (calibration.elapsed_ns <= 0) throw std::runtime_error("Clock did not advance");
        const double estimated = std::ceil(static_cast<double>(calibration_calls) * options.target_ms *
                                           1000000.0 / static_cast<double>(calibration.elapsed_ns));
        const double bounded = std::clamp(estimated, static_cast<double>(board_count), 999000000.0);
        calls = ((static_cast<std::uint64_t>(bounded) + board_count - 1) / board_count) * board_count;
    }
    output << ",\n  \"warmup_calls_per_variant\":" << warmup_calls << ",\n"
           << "  \"warmup_checksums\":[";
    for (std::size_t i = 0; i < variants.size(); ++i) {
        if (i) output << ',';
        output << warmup_checksums[i];
    }
    output << "],\n  \"calls_per_sample\":" << calls << ",\n"
           << "  \"calibration\":{\"variant\":\"" << variants[1].name << "\",\"calls\":" << calibration_calls
           << ",\"elapsed_ns\":" << calibration.elapsed_ns << ",\"checksum\":" << calibration.checksum
           << ",\"target_ms\":" << options.target_ms << "},\n"
           << "  \"blocks\":" << block_count << ",\n"
           << "  \"orders_per_block\":[\"ABCD\",\"DCBA\",\"BADC\",\"CDAB\"],\n"
           << "  \"variant_order\":[";
    for (std::size_t i = 0; i < variants.size(); ++i) {
        if (i) output << ',';
        output << '"' << variants[i].name << '"';
    }
    output << "],\n  \"samples\":[\n";

    std::array<std::vector<double>, 4> block_values;
    bool first_sample = true;
    bool checksums_match = true;
    std::array<std::int64_t, 4> expected_checksums{};
    for (std::size_t block = 0; block < block_count; ++block) {
        std::array<double, 4> sums{};
        std::array<std::int64_t, 4> checksums{};
        for (std::size_t round = 0; round < orders.size(); ++round) {
            // Rotate the four balanced orders between blocks as well.
            const std::size_t order = (block + round) % orders.size();
            for (const auto variant : orders[order]) {
                auto sample = measure(variants[variant], boards, calls);
                sample.block = block;
                sample.order = order;
                sample.variant = variant;
                sums[variant] += sample.ns_per_call;
                if (round && checksums[variant] != sample.checksum) checksums_match = false;
                if ((block || round) && expected_checksums[variant] != sample.checksum)
                    checksums_match = false;
                expected_checksums[variant] = sample.checksum;
                checksums[variant] = sample.checksum;
                if (!first_sample) output << ",\n";
                first_sample = false;
                output << "    {\"block\":" << sample.block << ",\"order\":\"" << order_names[sample.order]
                       << "\",\"variant\":\"" << variants[sample.variant].name << "\",\"elapsed_ns\":"
                       << sample.elapsed_ns << ",\"ns_per_call\":" << sample.ns_per_call
                       << ",\"checksum\":" << sample.checksum << '}';
            }
        }
        // Structure ON intentionally changes values; require stable checksums
        // for each variant, never equality between ON and OFF. The original
        // reference/candidate modes still demand exact cross-variant equality
        // for each shield setting, including the structure-ON optimization.
        if (!options.compare_pawn_structure) {
            checksums_match = checksums_match && checksums[0] == checksums[2] && checksums[1] == checksums[3];
        }
        for (std::size_t i = 0; i < variants.size(); ++i) block_values[i].push_back(sums[i] / 4.0);
    }
    output << "\n  ],\n  \"" << (options.compare_pawn_structure ? "timed_checksums_stable" : "timed_checksums_match")
           << "\":" << (checksums_match ? "true" : "false")
           << ",\n  \"cross_variant_checksum_equality_required\":"
           << (options.compare_pawn_structure ? "false" : "true")
           << ",\n  \"block_means_ns_per_call\":[\n";
    std::array<std::vector<double>, 4> ratios;
    for (std::size_t block = 0; block < block_count; ++block) {
        if (block) output << ",\n";
        const std::array<double, 4> block_ratios{{
            block_values[3][block] / block_values[1][block],
            block_values[2][block] / block_values[0][block],
            block_values[1][block] / block_values[0][block],
            block_values[3][block] / block_values[2][block]
        }};
        output << "    {\"block\":" << block;
        for (std::size_t i = 0; i < variants.size(); ++i) {
            output << ",\"" << variants[i].name << "\":" << block_values[i][block];
        }
        for (std::size_t i = 0; i < ratios.size(); ++i) {
            ratios[i].push_back(block_ratios[i]);
            output << ",\"" << ratio_names[i] << "\":" << block_ratios[i];
        }
        output << '}';
    }
    output << "\n  ],\n  \"medians\":{\"ns_per_call\":{";
    for (std::size_t i = 0; i < variants.size(); ++i) {
        if (i) output << ',';
        output << '"' << variants[i].name << "\":" << median(block_values[i]);
    }
    output << "},\"paired_ratios\":{";
    for (std::size_t i = 0; i < ratios.size(); ++i) {
        if (i) output << ',';
        output << '"' << ratio_names[i] << "\":" << median(ratios[i]);
    }
    output << "}}\n}\n";
    output_json(output.str(), options.output);
    return checksums_match ? 0 : 1;
}
} // namespace

int main(int argc, char** argv) {
    try {
        return run(parse_options(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << "profile_evaluation: " << error.what() << '\n';
        return 2;
    }
}
