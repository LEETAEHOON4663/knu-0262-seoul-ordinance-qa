"""PKG12 답변 후 검증: 외부 의존 확인(A·B)과 기권 복구(C).

97.64% 제안의 복구 단계 논리만 참고해 새로 썼다(코드·지시문은 쓰지 않음). 원칙은 셋이다.
- 게이트는 그래프 속성·질문 낱말 같은 결정적 규칙으로 정하고, 문항 ID·특정 문항 낱말을 쓰지 않는다.
- 판단을 바꾸는 결론은 서로 다른 입력을 본 두 호출이 일치하고, 인용·근거 id를 코드가 확인할 때만 받는다.
- 오류·시간 부족이면 아무것도 바꾸지 않는다(원래 답·기권 유지).
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict

from . import contracts, pkg_prompts as PP
from .contracts import ContractError, loads_strict, require
from .llm import TRUNCATED, CallError
from .pkg_runtime import outside_reference

_DEF = re.compile(r'\s*\d+(?:의\d+)?\.\s*"([^"]+)"')
_PARTICLE = re.compile(r"(?:으로|에서|까지|이라|이나|은|는|이|가|을|를|도|만|의|에|로|라|나|와|과)$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _law(pid: str) -> str:
    return pid.split(":", 1)[0]


class AnswerVerifier:
    def __init__(self, pipe):
        self.pipe = pipe
        self.index, self.cfg = pipe.index, pipe.cfg
        self.defined = defaultdict(set)   # 조례별 정의된 용어
        self.external_terms = []          # (명제 id, 용어, 조례): 정의가 외부 법령에 기대는 용어
        for p in self.index.by_id.values():
            m = _DEF.match(p.get("text", ""))
            if not m:
                continue
            self.defined[_law(p["id"])].add(m.group(1))
            if self.outside_reference(p["id"]):
                self.external_terms.append((p["id"], m.group(1), _law(p["id"])))

    @property
    def client(self):
        return self.pipe.client

    # ------------------------------------------------------------------ 게이트(결정적)
    def outside_reference(self, pid: str) -> str:
        """그래프 속성상 이 명제가 내용을 외부 법령에 맡겼으면 원문 구간 설명(pkg_runtime.outside_reference)."""
        return outside_reference((self.index.by_id.get(pid) or {}).get("attrs", {}))

    def imported_terms(self, question: str, final: dict) -> list:
        """B: 질문에 외부 정의 용어의 하위 종류(그 용어로 끝나는 더 긴 낱말, 그 조례가 따로 정의하지 않음)가 있고
        답이 같은 조례를 근거로 쓰면 그 정의 명제 id."""
        laws = {_law(i) for r in final["requests"] if r["status"] == "ANSWERED" for c in r["claims"] for i in c["ids"]}
        stems = {_PARTICLE.sub("", w) for w in re.findall(r"[가-힣A-Za-z]+", question)}
        out = []
        for pid, term, law in self.external_terms:
            if law in laws and any(s.endswith(term) and len(s) > len(term) and s not in self.defined[law]
                                   for s in stems):
                out.append(pid)
        return out

    def external_targets(self, question: str, final: dict) -> list:
        """(요청, 근거 명제, 게이트 명제) 목록. A: 주장이 쓴 외부 참조 명제. B(플래그): 하위 종류 용어의 외부 정의."""
        extra = self.imported_terms(question, final) if self.cfg.pkg_extdep_imported_terms else []
        out = []
        for r in final["requests"]:
            if r["status"] != "ANSWERED":
                continue
            used = list(dict.fromkeys([i for c in r["claims"] for i in c["ids"]] + extra))
            gated = [i for i in used if self.outside_reference(i)]
            if gated:
                out.append((r, used, gated))
        return out

    @staticmethod
    def request_quote(question: str, frame: dict, rid: str) -> str:
        """요청의 사용자 원문 구절: 질문 분해 인용이 질문과 글자 그대로(공백 무시) 같으면 그것, 아니면 질문 전체."""
        quotes = frame.get("quotes") or []
        k = int(rid[1:]) - 1 if re.fullmatch(r"R\d+", rid or "") else -1
        q = quotes[k] if 0 <= k < len(quotes) else ""
        return q if _norm(q) and _norm(q) in _norm(question) else question

    def _part_texts(self, sources: list) -> dict:
        out = {}
        for s in sources:
            for p in s["parts"]:
                if p.get("id"):
                    out[p["id"]] = (s["unit"], p["text"])
        return out

    def _with_outside_marks(self, sources: list, extra_ids: list) -> list:
        """반대 확인용 원문: 외부 참조 표시를 붙이고, 전달되지 않은 정의 명제(B)는 조각으로 덧붙인다."""
        shown = [dict(s, parts=[dict(p, refers_outside=self.outside_reference(p["id"]))
                                if p.get("id") and self.outside_reference(p["id"]) else p for p in s["parts"]])
                 for s in sources]
        have = set(self._part_texts(sources))
        for pid in extra_ids:
            if pid in have:
                continue
            p = self.index.by_id[pid]
            shown.append({"unit": self.pipe.corpus.by_id[p["unit"]].label, "why": "용어 정의",
                          "parts": [{"id": pid, "text": p["text"], "refers_outside": self.outside_reference(pid)}]})
        return shown

    # ------------------------------------------------------------------ A·B: 외부 의존 확인
    def external_dependency(self, question: str, frame: dict, final: dict, sources: list, deadline: float,
                            tr: dict, key: str = "external_dependency") -> bool:
        """점검(답변을 봄) → 코드 검증 → 반대 확인(답변을 보지 않음). 반대 확인까지 '핵심이고 조례 밖 내용 필요'면
        True(답 전체 기권). 호출·계약 오류나 시간 부족이면 False(답 유지)."""
        targets = self.external_targets(question, final)
        rec = tr[key] = {"status": "not_applicable"}
        if not targets:
            return False
        if deadline - time.monotonic() < self.cfg.min_seconds_for_review:
            rec["status"] = "skipped_insufficient_time"
            return False
        texts = self._part_texts(sources)
        keys, evidence, gated_keys, extra = {}, [], {}, []
        for r, used, gated in targets:
            gated_keys[r["id"]] = set()
            for pid in used:
                if pid not in keys:
                    keys[pid] = f"E{len(keys) + 1}"
                    unit, text = texts.get(pid) or (self.pipe.corpus.by_id[self.index.by_id[pid]["unit"]].label,
                                                    self.index.by_id[pid]["text"])
                    if pid not in texts:
                        extra.append(pid)
                    evidence.append({"key": keys[pid], "unit": unit, "text": text,
                                     "refers_outside": self.outside_reference(pid)})
                if pid in gated:
                    gated_keys[r["id"]].add(keys[pid])
        rec["gated"] = sorted({g for _, _, gs in targets for g in gs})
        quote = {r["id"]: self.request_quote(question, frame, r["id"]) for r, _, _ in targets}
        wire = [{"id": r["id"], "request": r["request"], "quote": quote[r["id"]]} for r, _, _ in targets]
        body = contracts.chat_body(self.cfg.review_model, PP.EXTDEP_INSPECT_SYSTEM,
                                   {"question": question, "requests": wire, "answer": final["answer"],
                                    "evidence": evidence},
                                   PP.extdep_inspect_schema(len(targets)), "pkg_extdep_inspect", 900)
        cap = self.cfg.stage_caps["review"]
        try:
            v = loads_strict(self.client.chat("extdep_inspect", body, deadline, cap, tr["calls"]))
            require(isinstance(v, dict) and set(v) == {"a_checks"} and isinstance(v["a_checks"], list)
                    and len(v["a_checks"]) == len(targets), "extdep_inspect_fields")
            rec.update(status="inspected", checks=v["a_checks"], counters=[])
            candidates = []
            for it, (r, _, _) in zip(v["a_checks"], targets):
                require(isinstance(it, dict) and set(it) == {"a_request", "b_answer_quote", "c_source", "d_needed",
                                                             "e_verdict"}, "extdep_inspect_item_fields")
                require(it["e_verdict"] in PP.EXTDEP_VERDICTS, "extdep_inspect_item")
                if (it["e_verdict"] == "DEPENDS_ON_OUTSIDE" and it["a_request"] == r["id"]
                        and _norm(it["b_answer_quote"]) and _norm(it["b_answer_quote"]) in _norm(final["answer"])
                        and it["c_source"] in gated_keys[r["id"]] and _norm(it["d_needed"])):
                    candidates.append(r)
            if not candidates:
                rec["status"] = "kept_no_verified_dependency"
                return False
            shown = self._with_outside_marks(sources, extra)
            for r in candidates:
                cbody = contracts.chat_body(self.cfg.review_model, PP.EXTDEP_COUNTER_SYSTEM,
                                            {"question": question,
                                             "outcome": {"question_quote": quote[r["id"]], "request": r["request"]},
                                             "sources": shown},
                                            PP.extdep_counter_schema(), "pkg_extdep_counter", 500)
                c = loads_strict(self.client.chat("extdep_counter", cbody, deadline, cap, tr["calls"]))
                require(isinstance(c, dict) and set(c) == {"a_core", "b_source", "c_missing", "d_basis"},
                        "extdep_counter_fields")
                rec["counters"].append({"request": r["id"], **c})
                if c["a_core"] == "CORE" and c["d_basis"] == "OUTSIDE" and _norm(c["c_missing"]):
                    rec.update(status="abstain_external_dependency", request=r["id"])
                    return True
            rec["status"] = "kept_counter_disagreed"
            return False
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            rec.update(status="unavailable", error=getattr(e, "code", str(e)))
            return False

    # ------------------------------------------------------------------ C: 기권 복구
    GUIDED_BASES = ("DELEGATED", "EXTERNAL")

    def governance(self, question: str, parsed: dict, sources: list, allowed: set, deadline: float, tr: dict,
                   tag: str) -> dict:
        """새 채점 지침: 안내 답(DELEGATED·EXTERNAL)과 핵심 근거 부족 요청만 다시 판정한다.
        - 안내 답인데 판정기가 NOT_GOVERNS → 그 요청은 근거 부족(CORE_MISSING)으로 바꾼다.
        - 근거 부족인데 판정기가 GOVERNS이고 인용 구절이 그 조각 원문에 글자 그대로 있으면 → 안내 답으로 바꾼다.
        오류·시간 부족이면 아무것도 바꾸지 않는다."""
        rec = tr.setdefault("governance", {})[tag] = {"status": "not_applicable"}
        targets = [r for r in parsed["requests"]
                   if (r["status"] == "ANSWERED" and r.get("basis") in self.GUIDED_BASES) or r["status"] == "CORE_MISSING"]
        if not targets or not sources:
            return parsed
        if deadline - time.monotonic() < self.cfg.min_seconds_for_review:
            rec["status"] = "skipped_insufficient_time"
            return parsed
        texts = self._part_texts(sources)
        wire = [{"id": r["id"], "request": r["request"], "asked": r.get("asked", ""), "status": r["status"],
                 "answer": " ".join(c["text"] for c in r["claims"])} for r in targets]
        body = contracts.chat_body(self.cfg.answer_model, PP.GOVERN_SYSTEM,
                                   {"question": question, "requests": wire, "sources": sources},
                                   PP.govern_schema(len(targets)), "pkg_govern", 1200)
        try:
            raw = self.client.chat("govern", body, deadline, self.cfg.stage_caps["review"], tr["calls"])
            if raw == TRUNCATED:
                raise ContractError("output_truncated")
            v = loads_strict(raw)
            require(isinstance(v, dict) and set(v) == {"a_checks"} and isinstance(v["a_checks"], list)
                    and len(v["a_checks"]) == len(targets), "govern_fields")
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            rec["status"] = "unavailable:" + getattr(e, "code", str(e))
            return parsed
        changes = {}
        for it, r in zip(v["a_checks"], targets):
            if not (isinstance(it, dict) and str(it.get("a_request", "")).strip() == r["id"]
                    and it.get("e_verdict") in PP.GOVERN_VERDICTS):
                continue
            src = str(it.get("c_source", "")).strip()
            quote_ok = (src in allowed and src in texts and _norm(it.get("d_quote")) != ""
                        and _norm(it.get("d_quote")) in _norm(texts[src][1]))
            ans = str(it.get("f_answer", "")).strip()
            if r["status"] == "ANSWERED" and it["e_verdict"] == "NOT_GOVERNS":
                changes[r["id"]] = dict(r, status="CORE_MISSING", claims=[], governance="dropped",
                                        discarded_text=[c["text"][:200] for c in r["claims"]])
            elif r["status"] == "CORE_MISSING" and it["e_verdict"] == "GOVERNS" and quote_ok and ans \
                    and not ans.startswith(contracts.ABSTAIN_TEXT):
                changes[r["id"]] = dict(r, status="ANSWERED", basis="DELEGATED", governance="rescued",
                                        claims=[{"no": f"{r['id']}C1", "text": ans, "ids": [src]}])
        rec.update(status="ok", checks=v["a_checks"], changed={k: x["governance"] for k, x in changes.items()})
        if not changes:
            return parsed
        reqs = [changes.get(r["id"], r) for r in parsed["requests"]]
        return self.pipe.finalize(reqs, parsed["wire"], partial=self.cfg.pkg_partial_answers)

    def recover(self, question: str, frame: dict, abstained: dict, sources: list, allowed: set, deadline: float,
                tr: dict, state: dict):
        """핵심 근거 부족으로 기권한 답(abstained)을 다섯 단계를 모두 통과할 때만 답으로 되돌린다.
        되돌린 최종 답(parse_generate 형식) 또는 None(기권 유지)."""
        rec = tr["recovery"] = {"status": "not_applicable"}
        missing = [r for r in abstained["requests"] if r["status"] == "CORE_MISSING"]
        if not missing or not sources:
            return None
        if deadline - time.monotonic() < 2 * self.cfg.min_seconds_for_review:
            rec["status"] = "skipped_insufficient_time"
            return None
        texts = self._part_texts(sources)
        cap = self.cfg.stage_caps["review"]
        try:
            # 1. 위치 확인(초안을 보지 않음)
            wire = [{"id": r["id"], "request": r["request"], "quote": self.request_quote(question, frame, r["id"])}
                    for r in missing]
            body = contracts.chat_body(self.cfg.review_model, PP.LOCATE_SYSTEM,
                                       {"question": question, "requests": wire, "sources": sources},
                                       PP.locate_schema(len(missing)), "pkg_locate", 900)
            v = loads_strict(self.client.chat("locate", body, deadline, cap, tr["calls"]))
            require(isinstance(v, dict) and set(v) == {"a_checks"} and isinstance(v["a_checks"], list)
                    and len(v["a_checks"]) == len(missing), "locate_fields")
            rec.update(status="located", checks=v["a_checks"])
            located = []
            # 2. 코드 검증: 모든 핵심 미응답 요청이 DECIDES이고 구절이 그 조각과 글자 그대로 같아야 한다.
            for it, r in zip(v["a_checks"], missing):
                require(isinstance(it, dict) and set(it) == {"a_request", "b_source", "c_span", "d_outcome",
                                                             "e_status"}, "locate_item_fields")
                require(it["e_status"] in PP.LOCATE_STATUS, "locate_status")
                src = it["b_source"].strip()
                if not (it["e_status"] == "DECIDES" and it["a_request"] == r["id"] and src in allowed
                        and src in texts and _norm(it["c_span"]) and _norm(it["c_span"]) in _norm(texts[src][1])
                        and _norm(it["d_outcome"])):
                    rec["status"] = "kept_not_located"
                    return None
                located.append({"request": r["id"], "source": src, "span": it["c_span"]})
            # 3. 재생성: 확인된 조각을 근거로 써서 ANSWER가 나와야 한다.
            gbody = self.pipe._generate_body(question, frame, sources)
            value = loads_strict(gbody["messages"][1]["content"])
            value["located"] = located
            gbody["messages"][0]["content"] += PP.RECOVERY_ADDENDUM
            gbody["messages"][1]["content"] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            gbody["response_format"]["json_schema"]["name"] = "pkg_recover"
            raw = self.client.chat("recover", gbody, deadline, self.cfg.stage_caps["generate"], tr["calls"])
            if raw == TRUNCATED:
                raise ContractError("output_truncated")
            cand = self.pipe.parse_generate(raw, allowed, frame["requests"], strict=False,
                                            background=frame.get("background"))
            if cand["decision"] != "ANSWER":
                rec["status"] = "kept_regenerate_abstained"
                return None
            by = {r["id"]: r for r in cand["requests"]}
            for loc in located:
                r = by.get(loc["request"])
                if not r or r["status"] != "ANSWERED" or loc["source"] not in {i for c in r["claims"] for i in c["ids"]}:
                    rec["status"] = "kept_regenerate_inconsistent"
                    return None
            # 4. 검토: 지적이 하나도 없어야 한다.
            prev = tr.pop("review", None)  # 원래 검토 기록은 보존하고 복구 검토는 따로 남긴다
            try:
                issues = self.pipe._review(question, cand, sources, allowed, deadline, tr, state)
            finally:
                rec["review"] = tr.pop("review", None)
                if prev is not None:
                    tr["review"] = prev
            review = rec["review"] or {}
            if review.get("status") != "ok":
                rec["status"] = "kept_review_unavailable"
                return None
            verdict = {x.get("id"): x.get("verdict") for x in review.get("verdicts", []) if isinstance(x, dict)}
            if issues or any(verdict.get(loc["request"]) != "ANSWERED" for loc in located):
                rec["status"] = "kept_review_flagged"
                return None
            # 5. 외부 의존 확인(A·B)이 기권으로 판정하지 않아야 한다.
            if self.cfg.pkg_external_dependency_check and \
                    self.external_dependency(question, frame, cand, sources, deadline, tr, key="recovery_extdep"):
                rec["status"] = "kept_external_dependency"
                return None
            rec.update(status="recovered", located=located)
            return cand
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            rec.update(status="unavailable", error=getattr(e, "code", str(e)))
            return None
