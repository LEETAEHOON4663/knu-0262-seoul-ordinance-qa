"""POST /ask HTTP 서버(표준 라이브러리).

응답(기본)
  200 + 4필드        : 답변 또는 정확한 확인 불가 객체
  400                : 잘못된 입력
  503/502/504 + error: 운영 오류(기권으로 바꾸지 않음)
응답(제출 모드, submission_abstain_on_error=true)
  POST /ask는 항상 200 + 4필드. 운영 오류·대기 초과·잘못된 입력·예상 못 한 예외는 정확한 기권 객체로 답하고,
  원인은 trace(fallback 필드)와 /health(fallback_abstain)에 남긴다.
입력에서 question만 사용한다(문항 ID 등 다른 필드는 추론에 넣지 않는다).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .contracts import UNKNOWN

MAX_BODY = 16384
DRAIN_LIMIT = 1 << 20   # 이보다 큰 본문은 끝까지 읽지 않고 연결을 닫는다


def _route(path):
    """경로만 남긴다: 쿼리·조각을 떼고 끝의 '/'를 없앤다('/ask/', '/ask?x=1' → '/ask', '' → '/')."""
    p = (path or "/").split("?", 1)[0].split("#", 1)[0]
    return p.rstrip("/") or "/"


def make_handler(pipeline, cfg):
    gate = threading.BoundedSemaphore(cfg.max_concurrent_requests)
    trace_lock = threading.Lock()
    trace_dir = Path(cfg.runtime_dir) / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    # O1: 겉의 HTTP 성공과 내부 검토 완료를 따로 센다. 검토 없이 제출한 답변은 '검증 완료'로 세지 않는다.
    stats = {"requests": 0, "answer": 0, "abstain": 0, "error": 0,
             "review_completed": 0, "review_unavailable": 0, "review_skipped": 0,
             "answers_submitted_without_completed_review": 0}
    fallback = bool(getattr(cfg, "submission_abstain_on_error", False))
    if fallback:
        stats["fallback_abstain"] = 0
    stats_lock = threading.Lock()

    def note_fallback(reason, question=None, received=None):
        with stats_lock:
            stats["fallback_abstain"] += 1
        rec = {"question": question, "status": "FALLBACK_ABSTAIN", "fallback": reason}
        if received is not None:
            rec["http_seconds"] = round(time.monotonic() - received, 3)
        try:
            write_trace(rec)
        except Exception:  # noqa: BLE001  기록 실패가 응답을 막지 않게
            pass

    def count(status, tr):
        rv = (tr.get("review") or {}).get("status")
        with stats_lock:
            stats["requests"] += 1
            stats[status.lower()] = stats.get(status.lower(), 0) + 1
            if rv == "ok":
                stats["review_completed"] += 1
            elif rv == "unavailable":
                stats["review_unavailable"] += 1
            elif rv:
                stats["review_skipped"] += 1
            if status == "ANSWER" and rv != "ok":
                stats["answers_submitted_without_completed_review"] += 1

    def write_trace(rec):
        if not cfg.trace:
            return
        with trace_lock:
            path = trace_dir / f"{datetime.now():%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 기본 stderr 로그 억제
            pass

        def _send(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def send_error(self, code, message=None, explain=None):
            """HTTP 해석 오류(요청 줄 깨짐, 지원하지 않는 메서드 등)도 HTML이 아니라 JSON으로 답하고 연결을 닫는다."""
            self.close_connection = True
            try:
                self._send(code, {"error": "bad_http_request" if code < 500 else "unsupported"})
            except Exception:  # noqa: BLE001  연결이 이미 끊긴 경우
                pass

        def _read_body(self):
            """요청 본문을 끝까지 읽는다(Content-Length 또는 chunked). (bytes|None, 오류 코드|None).

            응답 전에 본문을 반드시 비운다. 남겨 두면 같은 연결(keep-alive, 터널의 연결 공유)로 오는 다음 요청이
            남은 본문과 섞여 깨진다(2026-11-19 공개 주소에서 실제로 확인). 끝까지 읽을 수 없으면 연결을 닫는다."""
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                data = bytearray()
                try:
                    while True:
                        line = self.rfile.readline(1024)
                        if not line:
                            self.close_connection = True
                            return None, "body_incomplete"
                        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                                pass                      # 트레일러 줄을 건너뛴다
                            break
                        if len(data) + size > DRAIN_LIMIT:
                            self.close_connection = True
                            return None, "body_size"
                        data += self.rfile.read(size)
                        self.rfile.readline(8)           # 조각 뒤의 CRLF
                except (ValueError, OSError):
                    self.close_connection = True
                    return None, "bad_chunked"
                return (None, "body_size") if len(data) > MAX_BODY else (bytes(data), None)
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                return b"", None
            try:
                length = int(raw_len)
            except ValueError:
                self.close_connection = True
                return None, "bad_content_length"
            if length < 0 or length > DRAIN_LIMIT:
                self.close_connection = True
                return None, "body_size" if length > 0 else "bad_content_length"
            data = self.rfile.read(length) if length else b""
            if len(data) < length:
                self.close_connection = True
                return None, "body_incomplete"
            return (None, "body_size") if length > MAX_BODY else (data, None)

        def do_GET(self):
            try:
                self._read_body()                 # GET에 본문이 붙어 와도 연결에 남기지 않는다
            except Exception:  # noqa: BLE001
                self.close_connection = True
            if _route(self.path) == "/health":
                with stats_lock:
                    snap = dict(stats)
                self._send(200, {"status": "ok", "mode": cfg.mode, "verification": snap})
            else:
                self._send(404, {"error": "not_found"})

        def _fail(self, status, error, question=None, received=None):
            """운영 오류 응답. 제출 모드에서는 정확한 기권 객체(200)로 바꾼다."""
            if fallback:
                note_fallback(error, question, received)
                return self._send(200, dict(UNKNOWN, citation=[]))
            return self._send(status, {"error": error})

        def do_POST(self):
            received = time.monotonic()
            try:
                raw, err = self._read_body()      # 어떤 응답이든 보내기 전에 본문부터 비운다
            except Exception:  # noqa: BLE001
                self.close_connection = True
                raw, err = None, "body_read_error"
            path = _route(self.path)
            # '/ask/', '/ask?…'도 /ask로 받는다. 제출 모드에서는 루트('/')도 /ask로 받는다(주소를 루트로 적은 경우 대비).
            if path != "/ask" and not (fallback and path == "/"):
                return self._send(404, {"error": "not_found"})
            try:
                return self._ask(received, raw, err)
            except Exception as e:  # noqa: BLE001  예상 못 한 예외에도 응답은 낸다
                if not fallback:
                    raise
                try:
                    return self._fail(500, "unexpected:" + type(e).__name__, received=received)
                except Exception:  # noqa: BLE001  연결이 이미 끊긴 경우 등
                    return None

        def _ask(self, received, raw, err):
            if err:
                return self._fail(400, err, received=received)
            if not raw:
                return self._fail(400, "body_size", received=received)
            try:
                body = json.loads(raw.decode("utf-8-sig"))   # 앞에 BOM이 붙은 본문도 받는다
            except (UnicodeDecodeError, ValueError):
                return self._fail(400, "invalid_json", received=received)
            q = body.get("question") if isinstance(body, dict) else None
            if not isinstance(q, str) or not q.strip() or len(q) > cfg.max_question_chars:
                return self._fail(400, "invalid_question", received=received)
            # 대기 시간도 90초 안에 포함: 남은 시간이 30초 미만이 되면 처리하지 않는다.
            wait = cfg.request_deadline_seconds - 30 - (time.monotonic() - received)
            if not gate.acquire(timeout=max(wait, 0.1)):
                return self._fail(503, "server_busy", q.strip(), received)
            try:
                status, sub, tr = pipeline.answer(q.strip(), started=received)
            finally:
                gate.release()
            tr["http_seconds"] = round(time.monotonic() - received, 3)
            if status == "ERROR" and fallback:
                tr["fallback"] = sub["error"]
            write_trace(tr)
            count(status, tr)
            if status == "ERROR":
                if fallback:  # 원인은 위 trace의 error·fallback에 이미 남김
                    with stats_lock:
                        stats["fallback_abstain"] += 1
                    return self._send(200, dict(UNKNOWN, citation=[]))
                return self._send(sub.get("http_status", 503), {"error": sub["error"]})
            return self._send(200, sub)

    return Handler


def serve(pipeline, cfg):
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(pipeline, cfg))
    httpd.daemon_threads = True
    print(f"lawqa serving on http://{cfg.host}:{cfg.port}/ask mode={cfg.mode}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
