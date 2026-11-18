"""오프라인 테스트용 가짜 Solar. 실제 모델 성능을 나타내지 않는다."""
import json
import tempfile
from pathlib import Path

from lawqa.config import Config
from lawqa.llm import CallError


def make_cfg(tmp: str, **kw) -> Config:
    base = dict(mode="offline", runtime_dir=tmp, budget_cap_usd=1.0, trace=False)
    base.update(kw)
    return Config(**base)


def tmpdir():
    return tempfile.mkdtemp(prefix="lawqa-test-")


def user_value(body):
    return json.loads(body["messages"][1]["content"])


def alias_for(body, label_suffix):
    """요청 본문 evidence에서 조문 위치가 label_suffix(예: '제6조 제1항', '별표 1')인 근거 id."""
    want = label_suffix.replace(" ", "")
    for e in user_value(body)["evidence"]:
        if e["id"].split(":", 1)[1] == want:
            return e["id"]
    raise AssertionError("label not in context: " + label_suffix)


class FakeSolar:
    """schema 이름별 처리기. 처리기는 body를 받아 content 문자열 또는 예외/특수값을 낸다."""

    def __init__(self, handlers):
        self.handlers = {k: (list(v) if isinstance(v, list) else v) for k, v in handlers.items()}
        self.calls = []

    def __call__(self, url, body, timeout):
        if url.endswith("/embeddings"):
            self.calls.append(("embed", body))
            return {"model": body["model"], "data": [{"index": i, "embedding": [1.0, 0.0]}
                                                    for i, _ in enumerate(body["input"])],
                    "usage": {"total_tokens": 10}}
        name = body["response_format"]["json_schema"]["name"]
        repair = body["messages"][-1]["role"] == "user" and len(body["messages"]) > 2
        key = name + ("#repair" if repair else "")
        self.calls.append((key, body))
        h = self.handlers.get(key, self.handlers.get(name))
        if isinstance(h, list):
            h = h.pop(0)
        if h is None:
            raise AssertionError("unexpected call " + key)
        out = h(body) if callable(h) else h
        if isinstance(out, Exception):
            raise out
        finish = "stop"
        if out == "<<TRUNCATE>>":
            out, finish = '{"requests":[', "length"
        return {"model": body["model"] + "-260806",
                "choices": [{"message": {"content": out}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}

    def names(self):
        return [c[0] for c in self.calls]


def plan(laws=(), queries=("주차요금",)):
    return json.dumps({"laws": list(laws), "queries": list(queries)}, ensure_ascii=False)


def select_json(requests):
    """requests: [(요구, [E코드...])] → 선택 wire."""
    return json.dumps({"a_requests": [{"a_request": r, "b_evidence": ",".join(ids)} for r, ids in requests]},
                      ensure_ascii=False)


def generate_json(answer, ids):
    return json.dumps({"a_answer": answer, "b_evidence": ",".join(ids)}, ensure_ascii=False)


def review_json(issues=()):
    """issues: [(kind, [근거 id], 답변 인용구)]"""
    return json.dumps({"a_check": "대조", "b_issues": [
        {"a_kind": k, "b_answer_quote": q, "c_evidence": ",".join(ids), "d_reason": "이유"} for k, ids, q in issues]},
        ensure_ascii=False)


TIMEOUT = CallError("provider_timeout", uncertain=True, status=504)
