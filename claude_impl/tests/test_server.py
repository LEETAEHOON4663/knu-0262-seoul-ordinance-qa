"""HTTP 계약 오프라인 검사(가짜 Solar)."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from lawqa.contracts import UNKNOWN
from lawqa.pipeline import Pipeline
from lawqa.server import make_handler

from helpers import FakeSolar, TIMEOUT, make_cfg, plan, tmpdir
from test_pipeline import OK, good_generate, good_select, unsupported

NO_ISSUES = '{"a_check":"","b_issues":[]}'


class Server(unittest.TestCase):
    def start(self, handlers):
        cfg = make_cfg(tmpdir(), trace=True)
        pipe = Pipeline(cfg, transport=FakeSolar(handlers))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe, cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", cfg

    def post(self, url, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url + "/ask", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_answer_has_exactly_four_fields(self):
        url, cfg = self.start(dict(OK))
        status, body = self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?", "id": "A01"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"answer", "citation", "source_url", "effective_date"})
        self.assertRegex(body["effective_date"], r"^\d{4}-\d{2}-\d{2}$")
        traces = list((cfg.runtime_dir and __import__("pathlib").Path(cfg.runtime_dir) / "traces").glob("*.jsonl"))
        line = traces[0].read_text(encoding="utf-8")
        self.assertNotIn("A01", line)  # 문항 ID는 추론·기록에 들어가지 않음

    def test_abstention_object_exact(self):
        url, _ = self.start({"lawqa_plan": plan(), "lawqa_select": unsupported})
        status, body = self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?"})
        self.assertEqual((status, body), (200, UNKNOWN))

    def test_operational_error_is_not_abstention(self):
        url, _ = self.start({"lawqa_plan": plan(), "lawqa_select": TIMEOUT})
        status, body = self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?"})
        self.assertEqual(status, 504)
        self.assertEqual(set(body), {"error"})

    def test_bad_inputs(self):
        url, _ = self.start({})
        self.assertEqual(self.post(url, {"q": "x"})[0], 400)
        self.assertEqual(self.post(url, {"question": "   "})[0], 400)
        self.assertEqual(self.post(url, None, raw=b"{bad")[0], 400)
        self.assertEqual(self.post(url, {"question": "가" * 2001})[0], 400)

    def test_health(self):
        url, _ = self.start({})
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["mode"], "offline")


class SubmissionMode(unittest.TestCase):
    """submission_abstain_on_error=True: POST /ask는 어떤 경우에도 200 + 4필드."""

    def start(self, handlers, **kw):
        cfg = make_cfg(tmpdir(), trace=True, submission_abstain_on_error=True, **kw)
        pipe = Pipeline(cfg, transport=FakeSolar(handlers))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe, cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", cfg, pipe

    post = Server.post

    def health(self, url):
        with urllib.request.urlopen(url + "/health", timeout=5) as r:
            return json.loads(r.read())["verification"]

    def test_answer_unchanged(self):
        url, _, _ = self.start(dict(OK))
        status, body = self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?"})
        self.assertEqual(status, 200)
        self.assertEqual(list(body), ["answer", "citation", "source_url", "effective_date"])
        self.assertTrue(body["citation"])
        self.assertEqual(self.health(url)["fallback_abstain"], 0)

    def test_operational_error_becomes_abstention(self):
        url, cfg, _ = self.start({"lawqa_plan": plan(), "lawqa_select": TIMEOUT})
        status, body = self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?"})
        self.assertEqual((status, body), (200, UNKNOWN))
        h = self.health(url)
        self.assertEqual((h["error"], h["fallback_abstain"]), (1, 1))
        line = next(__import__("pathlib").Path(cfg.runtime_dir, "traces").glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertIn('"fallback": "provider_timeout"', line)

    def test_bad_inputs_become_abstention(self):
        url, _, _ = self.start({})
        for payload, raw in (({"q": "x"}, None), ({"question": "   "}, None), (None, b"{bad"),
                             ({"question": "가" * 2001}, None)):
            self.assertEqual(self.post(url, payload, raw=raw), (200, UNKNOWN))
        self.assertEqual(self.health(url)["fallback_abstain"], 4)

    def test_unexpected_exception_becomes_abstention(self):
        url, _, pipe = self.start({})

        def boom(*a, **k):
            raise KeyError("x")
        pipe.answer = boom
        self.assertEqual(self.post(url, {"question": "공영주차장 주차요금은 얼마인가요?"}), (200, UNKNOWN))
        self.assertEqual(self.health(url)["fallback_abstain"], 1)

    def test_busy_becomes_abstention(self):
        # 동시 1건, 마감 31초 → 대기 한도 약 1초. 첫 요청이 3초 걸리는 동안 둘째 요청은 대기 초과.
        import time as _t

        def slow_plan(body):
            _t.sleep(3)
            return plan()
        url, _, _ = self.start({"lawqa_plan": slow_plan, "lawqa_select": unsupported},
                               max_concurrent_requests=1, request_deadline_seconds=31.0)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("first", self.post(url, {"question": "주차요금은?"})))
        t.start()
        _t.sleep(0.5)
        second = self.post(url, {"question": "자전거 주차요금은?"})
        t.join()
        self.assertEqual(second, (200, UNKNOWN))
        self.assertEqual(out["first"], (200, UNKNOWN))  # 첫 요청은 정상 기권(근거 부족)
        self.assertEqual(self.health(url)["fallback_abstain"], 1)

    def test_other_paths_still_404(self):
        url, _, _ = self.start({})
        req = urllib.request.Request(url + "/nope", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 404)


class Connection(unittest.TestCase):
    """같은 연결(keep-alive)로 이어지는 요청이 앞 요청의 남은 본문에 깨지지 않는지(2026-11-19 공개 주소에서 발견)."""

    def start(self, submission):
        cfg = make_cfg(tmpdir(), trace=False, submission_abstain_on_error=submission)
        pipe = Pipeline(cfg, transport=FakeSolar(dict(OK)))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pipe, cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    Q = json.dumps({"question": "공영주차장 주차요금은 얼마인가요?"}, ensure_ascii=False).encode()

    def req(self, conn, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        chunked = h.get("Transfer-Encoding") == "chunked"
        if chunked:
            del h["Transfer-Encoding"]          # http.client가 직접 조각 형식으로 보낸다
        conn.request(method, path, body=body, headers=h, encode_chunked=chunked)
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type"), r.read()

    def test_wrong_path_does_not_poison_next_request(self):
        import http.client
        port = self.start(submission=False)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.assertEqual(self.req(c, "POST", "/nope", self.Q)[0], 404)
        status, ctype, data = self.req(c, "POST", "/ask", self.Q)
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(list(json.loads(data)), ["answer", "citation", "source_url", "effective_date"])

    def test_chunked_body_is_read(self):
        import http.client
        port = self.start(submission=True)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        body = iter([self.Q[:7], self.Q[7:]])
        status, _, data = self.req(c, "POST", "/ask", body, {"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["citation"])          # 질문을 읽고 답했다(기권 대체가 아님)
        status, _, data = self.req(c, "POST", "/ask", self.Q)  # 같은 연결의 다음 요청도 정상
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["citation"])

    def test_path_variants(self):
        import http.client
        port = self.start(submission=True)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        for path in ("/ask/", "/ask?x=1", "/"):
            status, _, data = self.req(c, "POST", path, self.Q)
            self.assertEqual(status, 200, path)
            self.assertTrue(json.loads(data)["citation"], path)
        port2 = self.start(submission=False)                   # 기본 모드에서 루트는 404 그대로
        c2 = http.client.HTTPConnection("127.0.0.1", port2, timeout=30)
        self.assertEqual(self.req(c2, "POST", "/", self.Q)[0], 404)

    def test_oversized_body_then_normal_request(self):
        import http.client
        port = self.start(submission=True)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        big = json.dumps({"question": "가" * 20000}, ensure_ascii=False).encode()
        status, _, data = self.req(c, "POST", "/ask", big)
        self.assertEqual((status, json.loads(data)), (200, UNKNOWN))
        status, _, data = self.req(c, "POST", "/ask", self.Q)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["citation"])

    def test_malformed_request_line_gets_json(self):
        import socket
        port = self.start(submission=True)
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        # 터널에서 실제로 본 모양: 앞 요청의 남은 본문이 다음 요청 줄 앞에 붙은 것
        s.sendall('{"question":"x"}POST /ask HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\n'.encode())
        raw = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw += chunk
        s.close()
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"application/json", head)
        self.assertIn("error", json.loads(body))


class DotEnvKey(unittest.TestCase):
    def test_reads_only_upstage_key(self):
        from pathlib import Path
        from lawqa.llm import key_from_dotenv
        d = Path(tmpdir())
        (d / ".env").write_text("OTHER=1\n# c\nexport UPSTAGE_API_KEY='up_test_value'\n", encoding="utf-8")
        self.assertEqual(key_from_dotenv([d / "missing.env", d / ".env"]), "up_test_value")
        self.assertEqual(key_from_dotenv([d / "missing.env"]), "")


if __name__ == "__main__":
    unittest.main()
