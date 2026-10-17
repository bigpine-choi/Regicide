"""Local click-board host. Regicide alone chooses the opponent's moves.

python-chess supplies UCI transport, legality, adjudication and PGN formatting.
Bind only to loopback; JSON mutations require a per-process browser token.
"""

import argparse
import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import uuid
import webbrowser

import chess
import chess.engine
import chess.pgn

from play_game import source_version

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/human-play"


class RevisionError(ValueError):
    """A stale browser must refresh instead of applying its old action."""


class HumanGame:
    def __init__(self, binary, save_dir, engine_factory=None):
        self.binary = Path(binary).resolve(strict=True)
        self.save_dir = Path(save_dir).resolve()
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.binary_sha256 = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.source = source_version()
        self._factory = engine_factory or (
            lambda binary: chess.engine.SimpleEngine.popen_uci([str(binary), "uci"], timeout=10))
        self._lock = threading.RLock()
        self._engine = None
        self.revision = 0
        try:
            self._ensure_engine()
            self._reset("white", 1000)
        except Exception:
            self.close()
            raise

    def _ensure_engine(self):
        if self._engine is None:
            self._engine = self._factory(self.binary)
        self.engine_name = self._engine.id.get("name", "Regicide")

    def _reset(self, color, movetime_ms):
        self.board = chess.Board()
        self.human_color = chess.WHITE if color == "white" else chess.BLACK
        self.movetime_ms = movetime_ms
        self.error = None
        self._override = None
        self._game_token = object()
        self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.game_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
        self.revision += 1
        self._save()

    def _check_revision(self, revision):
        if type(revision) is not int or revision != self.revision:
            raise RevisionError("다른 작업이 먼저 반영됐습니다. 현재 체스판에서 다시 선택해 주세요.")

    def _outcome(self):
        if self._override:
            return self._override
        outcome = self.board.outcome(claim_draw=True)
        return (outcome.result(), outcome.termination.name.lower()) if outcome else ("*", None)

    def state(self):
        with self._lock:
            result, termination = self._outcome()
            human_turn = result == "*" and not self.error and self.board.turn == self.human_color
            engine_turn = result == "*" and not self.error and self.board.turn != self.human_color
            names = {"checkmate": "체크메이트", "stalemate": "스테일메이트",
                     "insufficient_material": "메이트 불가능한 기물 구성",
                     "threefold_repetition": "3회 반복 청구", "fivefold_repetition": "5회 반복",
                     "fifty_moves": "50수 청구", "seventyfive_moves": "75수 규칙",
                     "resignation": "기권"}
            if self.error:
                status = "엔진 응답에 문제가 생겼습니다. 기보는 보존됐습니다. 새 대국으로 다시 시작해 주세요."
            elif result == "1/2-1/2":
                status = "무승부 · " + names.get(termination, termination or "대국 종료")
            elif result != "*":
                human_won = (result == "1-0") == self.human_color
                status = ("당신이 이겼습니다" if human_won else "Regicide가 이겼습니다")
                status += " · " + names.get(termination, termination or "대국 종료")
            else:
                status = "당신 차례입니다" if human_turn else "Regicide 차례입니다"
                if self.board.is_check():
                    status += " · 체크"
            replay = self.board.root()
            moves = []
            for ply, move in enumerate(self.board.move_stack, 1):
                moves.append({"ply": ply, "san": replay.san(move), "uci": move.uci(),
                              "actor": "human" if replay.turn == self.human_color else "engine"})
                replay.push(move)
            minimum_undo = 2 if self.human_color else 3
            can_undo = len(self.board.move_stack) >= minimum_undo
            if engine_turn and self.board.move_stack:
                can_undo = True
            return {"revision": self.revision,
                    "board": {chess.square_name(square): piece.symbol()
                              for square, piece in self.board.piece_map().items()},
                    "turn": "white" if self.board.turn else "black",
                    "human_color": "white" if self.human_color else "black",
                    "human_to_move": bool(human_turn), "engine_to_move": bool(engine_turn),
                    "legal_moves": [move.uci() for move in self.board.legal_moves] if human_turn else [],
                    "moves": moves, "status": status, "result": result, "termination": termination,
                    "in_check": self.board.is_check(),
                    "last_move": self.board.peek().uci() if self.board.move_stack else None,
                    "can_undo": can_undo, "error": self.error,
                    "engine": {"name": self.engine_name, "movetime_ms": self.movetime_ms,
                               "source_commit": self.source["commit"],
                               "binary_sha256": self.binary_sha256}}

    def new_game(self, color, movetime_ms, revision):
        with self._lock:
            self._check_revision(revision)
            if color not in ("white", "black") or type(movetime_ms) is not int or not 100 <= movetime_ms <= 5000:
                raise ValueError("백 또는 흑, 100~5000ms의 생각 시간을 선택해 주세요.")
            self._ensure_engine()
            self._reset(color, movetime_ms)
            return self.state()

    def human_move(self, uci, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.state()["human_to_move"]:
                raise ValueError("지금은 당신 차례가 아닙니다.")
            if not isinstance(uci, str) or len(uci) not in (4, 5):
                raise ValueError("올바른 수를 선택해 주세요.")
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                raise ValueError("올바른 수를 선택해 주세요.") from None
            if move not in self.board.legal_moves:
                raise ValueError("그 칸으로 이동할 수 없습니다.")
            self.board.push(move)
            self.revision += 1
            self._save()
            return self.state()

    def engine_move(self, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.state()["engine_to_move"]:
                raise ValueError("지금은 Regicide 차례가 아닙니다.")
            try:
                reply = self._engine.play(self.board, chess.engine.Limit(time=self.movetime_ms / 1000),
                                          game=self._game_token, info=chess.engine.INFO_ALL)
                if reply.move is None or reply.move not in self.board.legal_moves:
                    raise RuntimeError("엔진이 합법 수를 반환하지 않았습니다.")
                self.board.push(reply.move)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                if self._engine is not None:
                    self._engine.close()
                    self._engine = None
            self.revision += 1
            self._save()
            return self.state()

    def undo(self, revision):
        with self._lock:
            self._check_revision(revision)
            if not self.state()["can_undo"]:
                raise ValueError("되돌릴 당신의 수가 없습니다.")
            if self.board.turn != self.human_color:
                self.board.pop()  # Human move submitted before the engine reply.
            else:
                self.board.pop()
                self.board.pop()
            self.error = None
            self._override = None
            if self._engine is None:
                self._ensure_engine()
            self.revision += 1
            self._save()
            return self.state()

    def resign(self, revision):
        with self._lock:
            self._check_revision(revision)
            if self._outcome()[0] != "*" or self.error:
                raise ValueError("이미 종료된 대국입니다.")
            self._override = ("0-1" if self.human_color else "1-0", "resignation")
            self.revision += 1
            self._save()
            return self.state()

    def pgn(self):
        with self._lock:
            game = chess.pgn.Game.from_board(self.board)
            result, termination = self._outcome()
            game.headers.update({"Event": "Regicide local human practice", "Site": "local",
                                 "Date": self.started_at[:10].replace("-", "."),
                                 "White": "Human" if self.human_color else self.engine_name,
                                 "Black": self.engine_name if self.human_color else "Human",
                                 "Result": result, "EngineMoveTimeMS": str(self.movetime_ms),
                                 "EngineSHA256": self.binary_sha256,
                                 "InterfaceSourceCommit": self.source["commit"] or "unknown",
                                 "DrawPolicy": "always claim available threefold/50-move draws",
                                 "Termination": "unterminated" if result == "*" else "normal",
                                 "TerminationReason": termination or ("engine_error" if self.error else "in_progress")})
            return str(game) + "\n\n"

    def _save(self):
        state = self.state()
        record = {"game_id": self.game_id, "started_at": self.started_at,
                  "source": self.source, "binary": str(self.binary),
                  "initial_fen": self.board.root().fen(en_passant="fen"),
                  "final_fen": self.board.fen(en_passant="fen"),
                  "uci_moves": [move.uci() for move in self.board.move_stack],
                  "state": state, "draw_policy": "always claim available threefold/50-move draws",
                  "purpose": "Human practice; undo permitted, not a strength measurement"}
        for extension, contents in (("pgn", self.pgn()),
                                    ("json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")):
            target = self.save_dir / f"{self.game_id}.{extension}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(contents, encoding="utf-8", newline="\n")
            temporary.replace(target)

    def close(self):
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.quit()
                except Exception:
                    self._engine.close()
                self._engine = None


class HumanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, game, port=0):
        super().__init__(("127.0.0.1", port), HumanHandler)
        self.game = game
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"


class HumanHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Local request bodies and tokens do not belong in logs.

    def _send(self, code, content, mime="application/json; charset=utf-8"):
        data = json.dumps(content, ensure_ascii=False).encode("utf-8") if isinstance(content, dict) else content
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if mime.startswith("application/x-chess-pgn"):
            self.send_header("Content-Disposition", 'attachment; filename="Regicide-game.pgn"')
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self, api=False):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self._send(403, {"error": "허용되지 않은 주소입니다."})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._send(403, {"error": "같은 대국 화면에서만 요청할 수 있습니다."})
            return False
        if api and not secrets.compare_digest(self.headers.get("X-Regicide-Token", ""), self.server.token):
            self._send(403, {"error": "대국 화면을 새로 열어 주세요."})
            return False
        return True

    def do_GET(self):
        if not self._allowed(self.path.startswith("/api/")):
            return
        if self.path == "/api/state":
            self._send(200, self.server.game.state())
        elif self.path == "/api/pgn":
            self._send(200, self.server.game.pgn(), "application/x-chess-pgn; charset=utf-8")
        elif self.path in ("/", "/index.html", "/style.css", "/app.js"):
            filename = "index.html" if self.path == "/" else self.path[1:]
            mime = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript"}[filename]
            contents = (ASSETS / filename).read_text(encoding="utf-8")
            if filename == "index.html":
                contents = contents.replace("__REGICIDE_TOKEN__", self.server.token)
            self._send(200, contents, mime + "; charset=utf-8")
        else:
            self._send(404, {"error": "페이지를 찾을 수 없습니다."})

    def do_POST(self):
        if not self._allowed(True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._send(415, {"error": "JSON 요청이 필요합니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("요청 크기가 올바르지 않습니다.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("올바른 요청이 필요합니다.")
            revision = data.get("revision")
            game = self.server.game
            actions = {"/api/new": lambda: game.new_game(data.get("color"), data.get("movetime_ms"), revision),
                       "/api/move": lambda: game.human_move(data.get("move"), revision),
                       "/api/engine": lambda: game.engine_move(revision),
                       "/api/undo": lambda: game.undo(revision),
                       "/api/resign": lambda: game.resign(revision)}
            if self.path == "/api/shutdown":
                game._check_revision(revision)
                self._send(200, {"closed": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif self.path in actions:
                self._send(200, actions[self.path]())
            else:
                self._send(404, {"error": "지원하지 않는 요청입니다."})
        except RevisionError as exc:
            self._send(409, {"error": str(exc), "state": self.server.game.state()})
        except (ValueError, UnicodeError) as exc:
            self._send(400, {"error": str(exc), "state": self.server.game.state()})
        except Exception as exc:
            print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._send(500, {"error": "대국 요청을 처리하지 못했습니다. 저장 위치와 실행 기록을 확인해 주세요.",
                             "state": self.server.game.state()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / ("build/regicide.exe" if sys.platform == "win32" else "build/regicide"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--save-dir", type=Path, default=ROOT / "build/human-games")
    parser.add_argument("--startup-file", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be in 0..65535")
    game = HumanGame(args.binary, args.save_dir)
    try:
        with HumanServer(game, args.port) as server:
            startup = {"url": server.origin, "pid": os.getpid(),
                       "engine_name": game.engine_name, "binary_sha256": game.binary_sha256}
            if args.startup_file:
                args.startup_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.startup_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(startup), encoding="utf-8")
                temporary.replace(args.startup_file)
            print(json.dumps(startup), flush=True)
            if not args.no_browser:
                webbrowser.open(server.origin)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        game.close()


if __name__ == "__main__":
    main()
