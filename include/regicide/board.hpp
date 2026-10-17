#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace regicide {

enum class Color { white, black };
constexpr Color opposite(Color color) {
    return color == Color::white ? Color::black : Color::white;
}

// a1 = 0, b1 = 1, ..., h8 = 63. Promotion uses q/r/b/n, or 0.
struct Move {
    int from;
    int to;
    char promotion = 0;
    std::string uci() const;
    static Move from_uci(std::string_view text);
    bool operator==(const Move&) const = default;
};

struct Position {
    std::array<char, 64> squares{}; // Empty = '.', white = uppercase.
    Color turn = Color::white;
    unsigned castling = 0; // K=1, Q=2, k=4, q=8.
    int en_passant = -1;
    int halfmove = 0;
    int fullmove = 1;
    bool operator==(const Position&) const = default;
};

enum class Outcome {
    ongoing, checkmate, stalemate, known_dead_material,
    fivefold_repetition, seventy_five_moves
};
std::string_view name(Outcome outcome);

struct DrawClaims {
    bool threefold = false;
    bool fifty_moves = false;
    bool operator==(const DrawClaims&) const = default;
};

class Board {
public:
    static constexpr std::string_view start_fen =
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
    explicit Board(std::string_view fen = start_fen);
    std::string fen() const;
    const Position& position() const { return position_; }
    std::vector<Move> legal_moves() const;
    bool in_check() const;

    // Validates legality; failure leaves every part of the board unchanged.
    // Position analysis may continue beyond a draw; a game driver must honor outcome().
    void play(Move move);
    void play(std::string_view uci) { play(Move::from_uci(uci)); }
    // Search-only fast path: move must come from legal_moves() on this exact
    // board state. Debug builds assert that precondition; external input uses play().
    void play_generated(Move move);
    void undo();
    std::size_t history_size() const { return history_.size(); }
    bool operator==(const Board&) const = default;

    // Imported FEN starts a new history: earlier repetitions are unknown.
    int repetition_count() const;
    // True if any position has already appeared twice since an irreversible
    // move. False proves that no next move can support a threefold claim.
    bool has_repetition_candidate() const;
    // Deterministic Zobrist identity: pieces, turn, castling, and legal EP only.
    // Hashes accelerate search; draw rules still compare exact position data.
    std::uint64_t position_key() const;
    // Count-multiset digest of positions since pawn/capture/castling-right loss.
    // Halfmove/fullmove counters are excluded; search must key halfmove separately.
    std::uint64_t repetition_context_key() const;
    Outcome outcome() const;
    DrawClaims claims_now() const;
    DrawClaims claims_after(Move intended_move) const;
    // Deliberately recognizes only proven material subsets of dead positions.
    bool has_known_dead_material() const;

    // Counts legal paths; draw adjudication does not truncate perft.
    std::uint64_t perft(int depth) const;
    std::vector<std::pair<Move, std::uint64_t>> divide(int depth) const;

private:
    struct RepetitionState {
        // Squares + side + castling + legal EP (encoded as square+1, or zero).
        std::array<char, 67> identity{};
        std::uint64_t key = 0;
        std::uint64_t context = 0;
        std::size_t reversible_begin = 0;
        int occurrences = 1;
        bool has_candidate = false;
        bool operator==(const RepetitionState&) const = default;
    };
    void apply_generated(Move move);
    Position position_;
    std::vector<Position> history_;
    std::vector<RepetitionState> repetition_states_;
};

} // namespace regicide
