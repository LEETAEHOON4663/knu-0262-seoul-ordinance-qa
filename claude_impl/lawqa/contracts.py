"""⑥ ID·JSON 계약: 요청 본문 구성과 엄격한 출력 파서.

파서는 형식·ID·필드 조합만 검사한다. 법률적 옳고 그름은 판정하지 않는다.
기권 여부는 답변 문장이 아니라 requests[].supported 판단에서 결정한다.
"""
from __future__ import annotations

import json
import re

from . import prompts

ABSTAIN_TEXT = "확인 불가"
MAX_EVIDENCE = 12  # 인용 부풀리기 방지(감점이 없다는 채점 한계를 이용하지 않음)
UNKNOWN = {"answer": ABSTAIN_TEXT, "citation": [], "source_url": "", "effective_date": ""}


class ContractError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(cond, code):
    if not cond:
        raise ContractError(code)


def loads_strict(raw: str):
    def pairs(items):
        out = {}
        for k, v in items:
            require(k not in out, "duplicate_json_key")
            out[k] = v
        return out
    require(isinstance(raw, str), "content_not_text")
    text = raw.strip()
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ContractError:
        raise
    except ValueError:
        raise ContractError("invalid_json") from None


def chat_body(model: str, system: str, user_value: dict, schema: dict, name: str,
              max_tokens: int) -> dict:
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_value, ensure_ascii=False,
                                                   separators=(",", ":"))},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "strict": True, "schema": schema}},
    }


# ------------------------------------------------------------------ PLAN
def build_plan(question: str, law_names: list[str], model: str, max_tokens: int) -> dict:
    return chat_body(model, prompts.PLAN_SYSTEM, {"question": question, "law_list": law_names},
                     prompts.plan_schema(law_names), "lawqa_plan", max_tokens)


def parse_plan(raw: str, law_names: list[str]) -> dict:
    v = loads_strict(raw)
    require(isinstance(v, dict) and set(v) == {"laws", "queries"}, "plan_fields")
    laws, queries = v["laws"], v["queries"]
    require(isinstance(laws, list) and len(laws) <= 2 and
            all(isinstance(x, str) and x in law_names for x in laws), "plan_laws")
    require(isinstance(queries, list) and 1 <= len(queries) <= 4 and
            all(isinstance(q, str) and q.strip() for q in queries), "plan_queries")
    return {"laws": list(dict.fromkeys(laws)),
            "queries": [q.strip()[:80] for q in dict.fromkeys(queries)]}


# ------------------------------------------------------------------ SELECT
def build_select(question: str, evidence: list[dict], model: str, max_tokens: int) -> dict:
    return chat_body(model, prompts.SELECT_SYSTEM, {"question": question, "evidence": evidence},
                     prompts.select_schema(), "lawqa_select", max_tokens)


def _evidence_codes(text, aliases, notes):
    """쉼표로 구분한 근거 ID 문자열 → 문맥 ID 목록.

    공백 차이는 무시한다. 조례 접두어가 빠졌으면 문맥에서 유일하게 맞을 때만 받아들인다.
    문맥에 없는 ID는 인용하지 않고 버리며 기록한다(존재하지 않는 근거는 절대 인용하지 않음).
    """
    require(isinstance(text, str), "evidence_type")
    table = {re.sub(r"\s+", "", a): a for a in aliases}
    out = []
    for tok in re.split(r"[,，;\n]+", text):
        key = re.sub(r"\s+", "", tok).strip("\"'[]()")
        if not key:
            continue
        hit = table.get(key)
        if hit is None and ":" not in key:
            cands = [a for k, a in table.items() if k.split(":", 1)[1] == key]
            hit = cands[0] if len(cands) == 1 else None
        if hit is None:
            notes.append("unknown_evidence_id_dropped:" + key[:40])
            continue
        if hit in out:
            notes.append("duplicate_evidence_codes_removed")
            continue
        out.append(hit)
    return out


def _id_list(ids, aliases, code):
    require(isinstance(ids, list) and all(isinstance(i, str) and i in aliases for i in ids)
            and len(ids) == len(set(ids)), code)
    return list(ids)


def parse_select(raw: str, aliases: list[str]) -> dict:
    """요구별 근거. 근거가 빈 요구가 있으면 핵심 미지원 → 기권(IA 선택기의 지원⇔근거 불변식)."""
    v = loads_strict(raw)
    require(isinstance(v, dict) and set(v) == {"a_requests"}, "select_fields")
    reqs = v["a_requests"]
    require(isinstance(reqs, list) and 1 <= len(reqs) <= 4, "requests_count")
    notes: list[str] = []
    out = []
    for r in reqs:
        require(isinstance(r, dict) and set(r) == {"a_request", "b_evidence"}, "request_fields")
        require(isinstance(r["a_request"], str) and r["a_request"].strip(), "request_text")
        ids = _evidence_codes(r["b_evidence"], aliases, notes)
        out.append({"request": r["a_request"].strip(), "evidence_ids": ids, "supported": bool(ids)})
    selected = list(dict.fromkeys(i for r in out for i in r["evidence_ids"]))
    require(len(selected) <= MAX_EVIDENCE, "too_many_evidence_ids")
    return {"decision": "SELECTED" if all(r["supported"] for r in out) else "ABSTAIN",
            "requests": out, "selected": selected, "notes": notes}


# ------------------------------------------------------------------ GENERATE / CORRECT
def build_generate(question: str, evidence: list[dict], model: str, max_tokens: int,
                   draft: dict | None = None, issues: list | None = None) -> dict:
    value = {"question": question, "evidence": evidence}
    system, name = prompts.GENERATE_SYSTEM, "lawqa_generate"
    if draft is not None:
        value["draft"] = {"a_answer": draft["answer"], "b_evidence": ",".join(draft["evidence_ids"])}
        value["review_issues"] = issues or []
        system, name = prompts.GENERATE_SYSTEM + prompts.CORRECT_ADDENDUM, "lawqa_correct"
    return chat_body(model, system, value, prompts.generate_schema(), name, max_tokens)


def parse_generate(raw: str, aliases: list[str], requests: list | None = None) -> dict:
    """반환: {"decision": "ANSWER"|"ABSTAIN", "answer", "evidence_ids", "requests", "notes"}."""
    v = loads_strict(raw)
    require(isinstance(v, dict) and set(v) == {"a_answer", "b_evidence"}, "generate_fields")
    answer = v["a_answer"]
    require(isinstance(answer, str) and answer.strip(), "answer_text")
    answer = answer.strip()
    notes: list[str] = []
    ids = _evidence_codes(v["b_evidence"], aliases, notes)
    require(len(ids) <= MAX_EVIDENCE, "too_many_evidence_ids")
    reqs = list(requests or [])
    if answer.startswith(ABSTAIN_TEXT):
        # 선택 근거를 보고도 결론을 '확인 불가'로 냈다. 설명이 붙어도 제출하지 않는다.
        if answer != ABSTAIN_TEXT:
            notes.append("hedged_abstention_text_discarded")
        return {"decision": "ABSTAIN", "answer": answer, "evidence_ids": [], "requests": reqs, "notes": notes}
    require(ids, "answer_without_evidence")
    decision = "ANSWER"
    if denies_core(answer):
        # 첫 결론이 '확인할 수 없다/규정이 없다'면 실질 답이 아니다(IA draft_ids 규칙 차용).
        decision = "ABSTAIN"
        notes.append("first_sentence_denies_core")
    return {"decision": decision, "answer": answer, "evidence_ids": ids, "requests": reqs, "notes": notes}


_DENY = (re.compile(r"(?:단정|확정|확인|판단)(?:하기|할 수)?\s*(?:어렵|없)"),
         re.compile(r"(?:자료|조례|원문).*(?:명시|규정|나와).*(?:않|없)"))


def denies_core(answer: str) -> bool:
    """답변 첫 문장이 핵심을 확인할 수 없다고 말하는지."""
    first = re.split(r"[.!?。\n]", answer, maxsplit=1)[0]
    return any(p.search(first) for p in _DENY)


def used_aliases(parsed: dict) -> list[str]:
    """최종 사용 근거: answer evidence_ids 순서 + 지원된 요구의 근거(중복 제거)."""
    out = list(parsed["evidence_ids"])
    for r in parsed["requests"]:
        if r["supported"]:
            out.extend(r["evidence_ids"])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ REVIEW
def build_review(question: str, evidence: list[dict], aliases: list[str], parsed: dict,
                 model: str, max_tokens: int) -> dict:
    value = {"question": question,
             "draft_answer": parsed["answer"],
             "cited_ids": used_aliases(parsed),
             "evidence": evidence}
    return chat_body(model, prompts.REVIEW_SYSTEM, value, prompts.review_schema(),
                     "lawqa_review", max_tokens)


def parse_review(raw: str, aliases: list[str], answer: str = "") -> list[dict]:
    """지적 목록. 답변 인용구가 실제 답변에 없으면 그 지적은 근거 없는 지적으로 버린다(기록)."""
    v = loads_strict(raw)
    require(isinstance(v, dict) and set(v) == {"a_check", "b_issues"}, "review_fields")
    issues = v["b_issues"]
    require(isinstance(issues, list) and len(issues) <= 4, "review_issues")
    out = []
    for it in issues:
        require(isinstance(it, dict) and set(it) == {"a_kind", "b_answer_quote", "c_evidence", "d_reason"},
                "issue_fields")
        require(it["a_kind"] in prompts.ISSUE_KINDS, "issue_kind")
        notes: list[str] = []
        ids = _evidence_codes(it["c_evidence"], aliases, notes)
        quote = it["b_answer_quote"].strip()
        anchored = (not quote) or (re.sub(r"\s+", "", quote) in re.sub(r"\s+", "", answer))
        out.append({"kind": it["a_kind"], "evidence_ids": ids, "answer_quote": quote,
                    "detail": it["d_reason"][:300], "anchored": anchored})
    return out


# ------------------------------------------------------------------ 최종 조립
def assemble(parsed: dict, context) -> dict:
    """⑥ 인용·URL·시행일을 동결 원문 메타데이터로 조립한다."""
    if parsed["decision"] != "ANSWER":
        return dict(UNKNOWN, citation=[])
    units = [context.unit(a) for a in used_aliases(parsed)]
    require(units, "assemble_without_units")
    citation = list(dict.fromkeys(u.label for u in units))
    first = units[0]
    return {"answer": parsed["answer"], "citation": citation,
            "source_url": first.source_url, "effective_date": first.effective_date}
