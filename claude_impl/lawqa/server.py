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
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
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
            if self.path != "/ask":
                return self._send(404, {"error": "not_found"})
            try:
                return self._ask(received)
            except Exception as e:  # noqa: BLE001  예상 못 한 예외에도 응답은 낸다
                if not fallback:
                    raise
                try:
                    return self._fail(500, "unexpected:" + type(e).__name__, received=received)
                except Exception:  # noqa: BLE001  연결이 이미 끊긴 경우 등
                    return None

        def _ask(self, received):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._fail(400, "bad_content_length", received=received)
            if length <= 0 or length > MAX_BODY:
                return self._fail(400, "body_size", received=received)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
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
