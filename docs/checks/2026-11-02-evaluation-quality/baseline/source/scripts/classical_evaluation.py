"""Exact integer Python mirror of Regicide's own classical static evaluator.

This is a training baseline, not an independent chess engine or rule oracle.
Formulas follow src/evaluation.cpp, including its phase blend and truncation
towards zero. No neural-model import, search, board mutation or external weights.
Static scores do not replace rule outcomes or draw-claim decisions.
"""

import chess

FULL_PHASE = 24
MATERIAL = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
            chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
PHASE = {chess.PAWN: 0, chess.KNIGHT: 1, chess.BISHOP: 1,
         chess.ROOK: 2, chess.QUEEN: 4, chess.KING: 0}
PASSED_MIDDLEGAME = (0, 0, 5, 10, 20, 35, 60, 0)
PASSED_ENDGAME = (0, 0, 10, 20, 40, 70, 120, 0)


def trunc_div(numerator, denominator):
    """C++ signed integer division without a floating-point intermediate."""
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise ValueError("trunc_div requires integer numerator and positive integer denominator")
    return numerator // denominator if numerator >= 0 else -((-numerator) // denominator)


def _home_minors(board, color):
    base = 0 if color else 56
    return sum(board.piece_at(base + file) == chess.Piece(kind, color)
               for file, kind in ((1, chess.KNIGHT), (6, chess.KNIGHT),
                                  (2, chess.BISHOP), (5, chess.BISHOP)))


def _position_bonus(kind, file, rank):
    file_center = min(file, 7 - file)
    center = file_center + min(rank, 7 - rank)
    if kind == chess.PAWN:
        advanced = max(0, rank - 3)
        return (4 * rank + 3 * file_center + (3 * file_center if 2 <= rank <= 4 else 0),
                8 * rank + 2 * file_center + 4 * advanced * advanced, 0)
    if kind == chess.KNIGHT:
        return 6 * center + (12 if rank > 0 else 0), 4 * center, 0
    if kind == chess.BISHOP:
        return 4 * center + (10 if rank > 0 else 0), 4 * center, 0
    if kind == chess.ROOK:
        return 2 * file_center + (18 if rank == 6 else 0), 2 * file_center + (20 if rank == 6 else 0), 3 if 0 < rank < 5 else 0
    if kind == chess.QUEEN:
        return 2 * center, 3 * center, 4 if rank > 0 else 0
    if kind == chess.KING:
        return -7 * center - 5 * rank + (25 if rank == 0 and (file <= 2 or file >= 6) else 0), 8 * center, 0
    raise ValueError("unknown piece kind")


def _shield_penalty(pawns, king_square, color, attacking_phase):
    if not attacking_phase or king_square is None:
        return 0
    king_file = chess.square_file(king_square)
    rank = chess.square_rank(king_square)
    king_rank = rank if color else 7 - rank
    penalty = 0
    for file in range(max(0, king_file - 1), min(7, king_file + 1) + 1):
        ahead = pawns[file] >> (king_rank + 1)
        if ahead & 1:
            continue
        penalty += 4 if ahead & 2 else 8 if ahead & 4 else 12
    return penalty * min(attacking_phase, FULL_PHASE // 2) // (FULL_PHASE // 2)


def _pawn_score(board, own, enemy, color):
    middlegame = endgame = 0
    for file, mask in enumerate(own):
        if not mask:
            continue
        count, front = mask.bit_count(), mask.bit_length() - 1
        isolated = (file == 0 or not own[file - 1]) and (file == 7 or not own[file + 1])
        middlegame -= (count - 1) * 8 + (count * 10 if isolated else 0)
        endgame -= (count - 1) * 12 + (count * 12 if isolated else 0)
        if not PASSED_ENDGAME[front]:
            continue
        opposing = enemy[file]
        if file > 0:
            opposing |= enemy[file - 1]
        if file < 7:
            opposing |= enemy[file + 1]
        if opposing & ((1 << (7 - front)) - 1):
            continue
        next_rank = (front + 1) * 8 if color else 56 - (front + 1) * 8
        blocked = int(board.piece_at(next_rank + file) is not None)
        middlegame += PASSED_MIDDLEGAME[front] >> blocked
        endgame += PASSED_ENDGAME[front] >> blocked
    return middlegame, endgame


def classical_cp_stm(board, king_pawn_shield=True, pawn_structure=True):
    """Return the unchanged classical cp score from the side-to-move view."""
    if not isinstance(board, chess.Board) or not board.is_valid() or board.chess960:
        raise ValueError("classical evaluation requires a valid standard chess board")
    if type(king_pawn_shield) is not bool or type(pawn_structure) is not bool:
        raise ValueError("evaluation switches must be booleans")
    homes = {color: _home_minors(board, color) for color in (chess.WHITE, chess.BLACK)}
    pawn_masks = {chess.WHITE: [0] * 8, chess.BLACK: [0] * 8}
    side_phase = {chess.WHITE: 0, chess.BLACK: 0}
    material = middlegame = endgame = phase = 0
    for square, piece in board.piece_map().items():
        color, kind = piece.color, piece.piece_type
        sign = 1 if color else -1
        file = chess.square_file(square)
        rank = chess.square_rank(square) if color else 7 - chess.square_rank(square)
        mg, eg, undeveloped = _position_bonus(kind, file, rank)
        material += sign * MATERIAL[kind]
        middlegame += sign * (mg - undeveloped * homes[color])
        endgame += sign * eg
        phase += PHASE[kind]
        side_phase[color] += PHASE[kind]
        if kind == chess.PAWN:
            pawn_masks[color][file] |= 1 << rank
    if king_pawn_shield:
        middlegame += (_shield_penalty(pawn_masks[chess.BLACK], board.king(chess.BLACK), chess.BLACK, side_phase[chess.WHITE]) -
                      _shield_penalty(pawn_masks[chess.WHITE], board.king(chess.WHITE), chess.WHITE, side_phase[chess.BLACK]))
    if pawn_structure:
        white_mg, white_eg = _pawn_score(board, pawn_masks[chess.WHITE], pawn_masks[chess.BLACK], chess.WHITE)
        black_mg, black_eg = _pawn_score(board, pawn_masks[chess.BLACK], pawn_masks[chess.WHITE], chess.BLACK)
        middlegame += white_mg - black_mg
        endgame += white_eg - black_eg
    phase = min(phase, FULL_PHASE)
    score = material + trunc_div(middlegame * phase + endgame * (FULL_PHASE - phase), FULL_PHASE)
    return score if board.turn else -score
