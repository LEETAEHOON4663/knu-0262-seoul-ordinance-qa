"""⑧ Solar 호출 경계: 마감 시각, 예산 선예약, 성공 응답 캐시, 불명확 요청 재전송 금지.

모드
  offline : 네트워크 transport가 없다. 테스트용 fake transport만 주입 가능.
  replay  : 이 구현의 캐시에 있는 동일 요청만 재생한다. 없으면 오류.
  live    : 설정 mode=live + 실행 시 --allow-live + 키 파일 + 예산 한도>0 일 때만.

예산 원장은 이 구현 전용 로컬 파일이다. 공유 원장(서버)을 대신하지 않는다.
따라서 live 실행 전 한도를 공유 차단선 잔여액 이하로 정해야 한다(README 참고).
"""
from __future__ import annotations

try:  # POSIX 파일 잠금. Windows에서는 msvcrt로 대신한다.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt
import hashlib
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

BASE = "https://api.upstage.ai/v1"
CHAT_URL = BASE + "/chat/completions"
EMBED_URL = BASE + "/embeddings"
TRUNCATED = "\u0000TRUNCATED"  # finish_reason=length 표시(계약 오류로 처리)


class CallError(RuntimeError):
    """운영 오류. uncertain=True면 서버가 처리했을 수 있으므로 재전송하지 않는다."""

    def __init__(self, code: str, uncertain: bool = False, status: int = 503):
        self.code, self.uncertain, self.status = code, uncertain, status
        super().__init__(code)


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


# ------------------------------------------------------------------ 예산 원장
class Ledger:
    def __init__(self, path: Path, cap_usd: float):
        self.path = Path(path)
        self.cap = float(cap_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()  # 같은 프로세스 안 스레드 간(파일 잠금과 별도)

    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(".lock")
        with self._thread_lock, open(lock, "a+") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            else:  # pragma: no cover - Windows
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else \
                    {"settled_usd": 0.0, "uncertain_usd": 0.0, "reserved": {}, "calls": 0}
                yield state
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1))
                os.replace(tmp, self.path)
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

    def snapshot(self) -> dict:
        with self._locked() as s:
            return json.loads(json.dumps(s))

    def reserve(self, amount: float) -> str:
        with self._locked() as s:
            committed = s["settled_usd"] + s["uncertain_usd"] + sum(s["reserved"].values())
            if committed + amount > self.cap + 1e-12:
                raise CallError("budget_cap_reached", status=503)
            rid = uuid.uuid4().hex
            s["reserved"][rid] = amount
            return rid

    def settle(self, rid: str, actual: float):
        with self._locked() as s:
            s["reserved"].pop(rid, None)
            s["settled_usd"] += actual
            s["calls"] += 1

    def release(self, rid: str):
        """요청이 확실히 처리되지 않았을 때(전송 전 실패, 429 등)만 쓴다."""
        with self._locked() as s:
            s["reserved"].pop(rid, None)

    def mark_uncertain(self, rid: str):
        with self._locked() as s:
            amount = s["reserved"].pop(rid, 0.0)
            s["uncertain_usd"] += amount
            s["calls"] += 1


def key_from_dotenv(paths=None) -> str:
    """.env의 UPSTAGE_API_KEY= 줄만 읽는다(다른 변수는 무시, 환경변수를 바꾸지 않음)."""
    impl = Path(__file__).resolve().parent.parent
    for p in paths or (impl / ".env", impl.parent / ".env"):
        p = Path(p)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line.startswith("UPSTAGE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


# ------------------------------------------------------------------ transports
def http_transport(api_key: str):
    def send(url: str, body: dict, timeout: float) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise CallError("rate_limited", uncertain=False, status=503) from None
            # 4xx는 처리 거절로 본다. 5xx는 처리 여부 불명.
            raise CallError(f"http_{e.code}", uncertain=e.code >= 500, status=502) from None
        except (socket.timeout, TimeoutError):
            raise CallError("provider_timeout", uncertain=True, status=504) from None
        except urllib.error.URLError as e:
            uncertain = isinstance(e.reason, (socket.timeout, TimeoutError))
            raise CallError("provider_unreachable", uncertain=uncertain, status=502) from None
        try:
            return json.loads(payload)
        except ValueError:
            raise CallError("provider_body_not_json", uncertain=True, status=502) from None
    return send


# ------------------------------------------------------------------ client
class SolarClient:
    def __init__(self, cfg, transport=None, allow_live: bool = False):
        self.cfg = cfg
        self.cache_dir = Path(cfg.runtime_dir) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # F07: 같은 요청(같은 key)이 동시에 들어오면 한 실행만 유료 호출하고 나머지는 그 캐시를 쓴다.
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_guard = threading.Lock()
        self._uncertain_at: dict[str, float] = {}  # 처리 불명으로 끝난 시각(같은 프로세스 안)
        self.ledger = Ledger(Path(cfg.runtime_dir) / "ledger.json", cfg.budget_cap_usd)
        self.mode = cfg.mode
        if transport is not None:
            self.transport = transport  # 테스트 주입(fake)
        elif cfg.mode == "live":
            if not allow_live:
                raise CallError("live_not_authorized_use_--allow-live", status=503)
            if cfg.budget_cap_usd <= 0:
                raise CallError("live_requires_positive_budget_cap", status=503)
            key_file = Path(cfg.api_key_file or "")
            if key_file.is_file():
                key = key_file.read_text(encoding="utf-8").strip()
            else:  # 키 파일이 없으면 환경변수 UPSTAGE_API_KEY, 그다음 .env(claude_impl/ 또는 패키지 루트)를 쓴다.
                key = os.environ.get("UPSTAGE_API_KEY", "").strip() or key_from_dotenv()
            if not key:
                raise CallError("live_requires_api_key_file_or_UPSTAGE_API_KEY", status=503)
            self.transport = http_transport(key)
        else:
            self.transport = None

    # 비용 추정: 한국어 1글자를 1토큰으로 보는 보수적 상한(실제보다 크게 잡힘).
    def _estimate(self, kind: str, body: dict) -> float:
        rates = self.cfg.rates
        if kind == "embed":
            chars = sum(len(t) for t in body["input"])
            return (chars + 50) * rates["embedding"][0] / 1e6
        chars = sum(len(m["content"]) for m in body["messages"]) + \
            len(json.dumps(body["response_format"], ensure_ascii=False))
        rin, rout = rates[body["model"]]
        return ((chars + 200) * rin + body["max_tokens"] * rout) / 1e6

    def _actual(self, kind: str, body: dict, resp: dict, estimate: float) -> float:
        usage = resp.get("usage") or {}
        rates = self.cfg.rates
        try:
            if kind == "embed":
                return usage["total_tokens"] * rates["embedding"][0] / 1e6
            rin, rout = rates[body["model"]]
            return (usage["prompt_tokens"] * rin + usage["completion_tokens"] * rout) / 1e6
        except (KeyError, TypeError):
            return estimate  # 사용량이 없으면 추정치를 정산액으로 남긴다.

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def call(self, kind: str, body: dict, deadline: float, stage_cap: float, trace: list) -> dict:
        url = EMBED_URL if kind == "embed" else CHAT_URL
        key = digest({"url": url, "body": body})
        lock = self._lock_for(key)
        stage = kind if kind == "embed" else body.get("_stage", "chat")
        t0 = time.monotonic()
        waited = not lock.acquire(blocking=False)
        if waited and not lock.acquire(timeout=max(0.0, deadline - time.monotonic() - 1.0)):
            trace.append({"stage": stage, "key": key[:16], "cache": False, "error": "same_request_in_flight"})
            raise CallError("same_request_in_flight", status=504)
        try:
            # 기다리는 동안 앞선 동일 요청이 처리 불명으로 끝났으면 곧바로 다시 보내지 않는다.
            if waited and self._uncertain_at.get(key, -1.0) >= t0:
                trace.append({"stage": stage, "key": key[:16], "cache": False, "error": "same_request_uncertain"})
                raise CallError("same_request_uncertain", status=504)
            return self._call_locked(kind, url, key, body, deadline, stage_cap, trace)
        finally:
            lock.release()

    def _call_locked(self, kind: str, url: str, key: str, body: dict, deadline: float, stage_cap: float,
                     trace: list) -> dict:
        cache_file = self.cache_dir / f"{key}.json"
        rec = {"stage": kind if kind == "embed" else body.get("_stage", "chat"),
               "key": key[:16], "cache": False}
        if cache_file.exists():
            rec["cache"] = True
            trace.append(rec)
            return json.loads(cache_file.read_text())
        if self.transport is None:
            trace.append(dict(rec, error="no_transport_" + self.mode))
            raise CallError("replay_cache_miss" if self.mode == "replay" else "offline_no_transport")
        remaining = deadline - time.monotonic()
        timeout = min(stage_cap, remaining - 1.0)
        if timeout < 2.0:
            trace.append(dict(rec, error="deadline"))
            raise CallError("deadline_before_call", status=504)
        wire = {k: v for k, v in body.items() if not k.startswith("_")}
        est = self._estimate(kind, wire)
        rid = self.ledger.reserve(est)
        started = time.monotonic()
        try:
            resp = self.transport(url, wire, timeout)
        except CallError as e:
            (self.ledger.mark_uncertain if e.uncertain else self.ledger.release)(rid)
            if e.uncertain:
                self._uncertain_at[key] = time.monotonic()
            trace.append(dict(rec, error=e.code, uncertain=e.uncertain,
                              seconds=round(time.monotonic() - started, 2)))
            raise
        except Exception:  # noqa: BLE001 - transport 버그도 처리 불명으로 기록
            self.ledger.mark_uncertain(rid)
            self._uncertain_at[key] = time.monotonic()
            trace.append(dict(rec, error="transport_exception", uncertain=True))
            raise CallError("transport_exception", uncertain=True)
        cost = self._actual(kind, wire, resp, est)
        self.ledger.settle(rid, cost)
        rec.update(seconds=round(time.monotonic() - started, 2), usd=round(cost, 8),
                   model=resp.get("model"), usage=resp.get("usage"))
        trace.append(rec)
        tmp = cache_file.with_name(f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp")  # 쓰기마다 독립 임시 파일
        tmp.write_text(json.dumps(resp, ensure_ascii=False))
        os.replace(tmp, cache_file)
        return resp

    # -------------------------------------------------------------- helpers
    def chat(self, stage: str, body: dict, deadline: float, stage_cap: float, trace: list) -> str:
        resp = self.call("chat", dict(body, _stage=stage), deadline, stage_cap, trace)
        try:
            choice = resp["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
            model = resp.get("model", "")
        except (KeyError, IndexError, TypeError):
            raise CallError("chat_response_shape", status=502) from None
        if not str(model).startswith(body["model"]):
            raise CallError("returned_model_mismatch", status=502)
        if finish == "length":
            # 잘린 출력은 계약 오류로 다룬다(복구 1회 대상).
            return TRUNCATED
        if not isinstance(content, str):
            raise CallError("chat_content_type", status=502)
        return content

    def embed(self, model: str, texts: list[str], deadline: float, stage_cap: float,
              trace: list) -> list[list[float]]:
        resp = self.call("embed", {"model": model, "input": texts}, deadline, stage_cap, trace)
        try:
            rows = sorted(resp["data"], key=lambda r: r["index"])
            vecs = [r["embedding"] for r in rows]
        except (KeyError, TypeError):
            raise CallError("embedding_response_shape", status=502) from None
        if len(vecs) != len(texts) or any(not all(math.isfinite(x) for x in v) for v in vecs):
            raise CallError("embedding_response_shape", status=502)
        return vecs
