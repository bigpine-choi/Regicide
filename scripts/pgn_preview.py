"""Validate one local PGN text and build an ephemeral, read-only replay.

No file paths, game stores, active sessions, engines or hint workers are used.
The original text is returned unchanged, including comments and variations.
"""
from __future__ import annotations

import hashlib
import io
import re

import chess
import chess.pgn

from opening_presentation import OpeningGuideCache

MAX_PGN_BYTES = 1024 * 1024
MAX_PGN_PLIES = 512
MAX_PGN_TOTAL_MOVES = 10_000
MAX_VARIATION_DEPTH = 128
# JSON may encode a Unicode character as six ASCII bytes. This transport cap
# is separate from the decoded PGN's strict UTF-8 byte cap.
MAX_PGN_REQUEST_BYTES = 6 * MAX_PGN_BYTES + 4096
_TAG = re.compile(r'\[([A-Za-z0-9][A-Za-z0-9_+#=:-]*)\s+"((?:[^"\\\r\n]|\\["\\])*)"\s*\]')
_TOKEN = re.compile(
    r'(?P<number>[0-9]+\.(?:\.\.)?)|(?P<result>1/2-1/2|1-0|0-1|\*)'
    r'|(?P<nag>\$[0-9]+)|(?P<annotation>[?!]{1,2})|(?P<ep>e\.p\.)'
    r'|(?P<null>--|Z0|0000|@@@@)'
    r'|(?P<move>(?:O-O(?:-O)?|0-0(?:-0)?|[NBKRQ]?[a-h]?[1-8]?[x-]?[a-h][1-8](?:=?[NBRQnbrq])?)[+#]{0,2})')
_RESULTS = {'*', '1-0', '0-1', '1/2-1/2'}


class PGNPreviewError(ValueError):
    """User-readable PGN validation failure."""


def _scan(text):
    """Reject skipped garbage and same-line second games before tolerant parsing."""
    pos, headers, line_moves = 0, {}, [0]
    moved, main_result, total_moves, variations = False, None, 0, False
    while pos < len(text):
        char = text[pos]
        if char.isspace():
            pos += 1
            continue
        if char == '{':
            end = text.find('}', pos + 1)
            if end < 0:
                raise PGNPreviewError('PGN 주석의 닫는 괄호가 없습니다.')
            pos = end + 1
            continue
        if char == ';' or (char == '%' and (pos == 0 or text[pos - 1] in '\r\n')):
            end = text.find('\n', pos)
            pos = len(text) if end < 0 else end + 1
            continue
        if char == '[':
            if moved or main_result is not None:
                raise PGNPreviewError('한 번에 하나의 PGN 대국만 열 수 있습니다.')
            match = _TAG.match(text, pos)
            if not match:
                raise PGNPreviewError('PGN 헤더 형식이 올바르지 않습니다.')
            line_start = text.rfind('\n', 0, pos) + 1
            line_end = text.find('\n', match.end())
            if text[line_start:pos].strip() or text[match.end():line_end if line_end >= 0 else len(text)].strip():
                raise PGNPreviewError('PGN 헤더는 각각 한 줄에 작성해야 합니다.')
            name, value = match.groups()
            if name in headers:
                raise PGNPreviewError('같은 PGN 헤더가 두 번 들어 있습니다.')
            headers[name] = re.sub(r'\\(["\\])', r'\1', value)
            pos = match.end()
            continue
        if main_result is not None:
            raise PGNPreviewError('대국 종료 표기 뒤에 다른 수순이 있습니다. 한 대국만 열어 주세요.')
        if char == '(':
            if not line_moves[-1] or len(line_moves) >= MAX_VARIATION_DEPTH:
                raise PGNPreviewError('PGN 대안 수순의 위치나 깊이가 올바르지 않습니다.')
            line_moves.append(0)
            variations = True
            pos += 1
            continue
        if char == ')':
            if len(line_moves) == 1 or not line_moves[-1]:
                raise PGNPreviewError('PGN 대안 수순의 괄호가 올바르지 않습니다.')
            line_moves.pop()
            pos += 1
            continue
        match = _TOKEN.match(text, pos)
        if not match:
            raise PGNPreviewError('PGN에 해석할 수 없는 수나 문자가 있습니다.')
        kind = match.lastgroup
        if kind == 'null':
            raise PGNPreviewError('널 수가 포함된 PGN은 재생할 수 없습니다.')
        if kind == 'move':
            moved = True
            line_moves[-1] += 1
            total_moves += 1
            if line_moves[0] > MAX_PGN_PLIES:
                raise PGNPreviewError(f'이 미리보기는 주 수순 {MAX_PGN_PLIES}반수(256수)까지 지원합니다.')
            if total_moves > MAX_PGN_TOTAL_MOVES:
                raise PGNPreviewError('PGN의 대안 수순을 포함한 자료량이 너무 큽니다.')
        elif kind == 'result':
            if len(line_moves) != 1:
                raise PGNPreviewError('대안 수순 안의 대국 종료 표기는 지원하지 않습니다.')
            main_result = match.group()
        elif kind == 'nag' and len(match.group()) > 8:
            raise PGNPreviewError('PGN 주석 번호가 너무 큽니다.')
        pos = match.end()
    if len(line_moves) != 1:
        raise PGNPreviewError('PGN 대안 수순의 닫는 괄호가 없습니다.')
    if not moved or not line_moves[0]:
        raise PGNPreviewError('재생할 수 있는 착수가 들어 있는 PGN 파일을 선택해 주세요.')
    if 'Result' in headers and headers['Result'] not in _RESULTS:
        raise PGNPreviewError('PGN 대국 결과 표기가 올바르지 않습니다.')
    if main_result is not None and 'Result' in headers and headers['Result'] != main_result:
        raise PGNPreviewError('PGN 헤더와 본문의 대국 결과가 다릅니다.')
    return headers, main_result, variations


class _StrictGameBuilder(chess.pgn.GameBuilder):
    def end_headers(self):
        headers = self.game.headers
        if headers.get('Variant', 'Standard').lower() not in ('standard', 'chess', 'normal', 'classical', 'from position'):
            raise PGNPreviewError('표준 체스 PGN만 열 수 있습니다. 변형 체스와 Chess960은 지원하지 않습니다.')
        if headers.get('SetUp', '0') not in ('0', '1') or (headers.get('SetUp') == '1' and 'FEN' not in headers):
            raise PGNPreviewError('PGN의 초기 배치 SetUp/FEN 표기가 올바르지 않습니다.')
        try:
            board = headers.board()
        except (ValueError, TypeError, KeyError) as error:
            raise PGNPreviewError('PGN의 초기 체스판을 읽을 수 없습니다.') from error
        if type(board) is not chess.Board or board.chess960 or not board.is_valid():
            raise PGNPreviewError('유효한 표준 체스 초기 배치가 필요합니다.')

    def visit_move(self, board, move):
        if not move:
            raise PGNPreviewError('널 수가 포함된 PGN은 재생할 수 없습니다.')
        if board.is_game_over(claim_draw=False) or move not in board.legal_moves:
            raise PGNPreviewError('불법 수나 자동 종료 이후의 수가 포함된 PGN입니다.')
        super().visit_move(board, move)

    def handle_error(self, error):
        raise PGNPreviewError('합법적으로 해석할 수 없는 PGN 수순입니다.') from error


def preview_pgn(pgn):
    """Return the archive replay shape, without writing or altering any session."""
    if not isinstance(pgn, str):
        raise PGNPreviewError('PGN 본문이 필요합니다.')
    try:
        raw = pgn.encode('utf-8')
    except UnicodeError as error:
        raise PGNPreviewError('PGN은 올바른 UTF-8 텍스트여야 합니다.') from error
    if not raw or len(raw) > MAX_PGN_BYTES:
        raise PGNPreviewError('PGN 파일은 1MiB 이하의 텍스트여야 합니다.')
    if any(ord(char) < 32 and char not in '\t\r\n' for char in pgn):
        raise PGNPreviewError('PGN에 지원하지 않는 제어 문자가 있습니다.')
    text = pgn[1:] if pgn.startswith('\ufeff') else pgn
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    headers, marker, variations = _scan(text)
    try:
        stream = io.StringIO(text)
        game = chess.pgn.read_game(stream, Visitor=_StrictGameBuilder)
        if game is None or game.errors or chess.pgn.read_game(stream, Visitor=_StrictGameBuilder) is not None:
            raise PGNPreviewError('하나의 정상적인 PGN 대국만 열 수 있습니다.')
        root = game.board()
        mainline = list(game.mainline_moves())
    except PGNPreviewError:
        raise
    except (ValueError, TypeError, AssertionError, IndexError) as error:
        raise PGNPreviewError('PGN 수순을 읽을 수 없습니다.') from error
    if not mainline or len(mainline) > MAX_PGN_PLIES:
        raise PGNPreviewError(f'착수가 있는 {MAX_PGN_PLIES}반수 이하의 PGN만 열 수 있습니다.')
    result = game.headers.get('Result', '*')
    status = {'1-0': '백 승리', '0-1': '흑 승리', '1/2-1/2': '무승부', '*': '진행 기록'}[result]
    summary = {
        'game_id': 'pgn-preview-' + hashlib.sha256(raw).hexdigest()[:16],
        'title': 'PGN 파일', 'source_kind': 'pgn', 'human_color': 'white',
        'engine_name': 'PGN 기록', 'movetime_ms': None, 'ply_count': len(mainline),
        'white': game.headers.get('White', '?'), 'black': game.headers.get('Black', '?'),
        'result': result, 'termination': None, 'status': status,
        'record_status': 'completed' if result != '*' else 'unfinished',
        'started_at': '', 'updated_at': '', 'ended_at': None,
        'handicap_moves': 0, 'current': False, 'initial_fen': root.fen(en_passant='fen'),
    }
    board, moves, frames, opening = root.copy(stack=True), [], [], OpeningGuideCache()
    nodes = list(game.mainline())
    for ply in range(len(mainline) + 1):
        final = ply == len(mainline)
        frames.append({
            'ply': ply, 'fen': board.fen(en_passant='fen'),
            'board': {chess.square_name(square): piece.symbol() for square, piece in board.piece_map().items()},
            'turn': 'white' if board.turn else 'black', 'human_color': 'white',
            'in_check': board.is_check(), 'last_move': board.peek().uci() if board.move_stack else None,
            'result': result if final else '*', 'termination': None,
            'status': status if final else 'PGN 기보 · ' + ('백' if board.turn else '흑') + ' 차례',
            'human_to_move': False, 'engine_to_move': False, 'legal_moves': [],
            'can_undo': False, 'can_handicap': False, 'handicap_mode': False,
            'opening_guide': opening.get(board), 'error': None,
        })
        if not final:
            move = mainline[ply]
            entry = {'ply': ply + 1, 'uci': move.uci(), 'san': board.san(move),
                     'actor': 'human' if board.turn else 'engine'}
            if nodes[ply].comment.strip() == 'User-forced Regicide move (handicap)':
                entry.update(forced=True, comment=nodes[ply].comment)
                summary['handicap_moves'] += 1
            moves.append(entry)
            board.push(move)
    warnings = []
    if variations:
        warnings.append('대안 수순과 주석은 원문에 보존하며 화면은 주 수순만 재생합니다.')
    if marker is None:
        warnings.append('본문에 종료 표기가 없어 헤더의 결과 또는 진행 기록으로 표시합니다.')
    return {'summary': summary, 'moves': moves, 'frames': frames, 'pgn': pgn, 'warnings': warnings}
