"""PKG-v2 실행 경로.

질문 → [Pro4] 핵심 요청·검색어 → 명제 검색(BM25+dense) → [Pro4] 요청별 근거 카드 선택
     → 그래프로 필수 원문 복원 → [Pro4] 요청별 주장(문장+근거 id)과 상태
     → [Mini4] 검토 → 문제 있으면 [Pro4] 교정 1회 → 주장으로 답변 구성, 주장 근거로 인용 조립.
결과 상태: ANSWER / ABSTAIN(핵심 근거 부족) / ERROR(운영 오류 — 기권으로 바꾸지 않음).
"""
from __future__ import annotations

import copy
import json
import re
import time

from . import contracts, pkg_prompts as PP
from .contracts import ContractError, loads_strict, require
from .llm import CallError
from .pipeline import Pipeline
from .pkg_runtime import PropIndex
from .pkg_verify import AnswerVerifier

SUBSTANTIVE = ("unanswered_core", "unsupported_claim")


def parse_ids(text, allowed: set, code: str) -> list[str]:
    """쉼표 id 목록. 공백 차이는 허용, 제공되지 않은 id는 계약 오류(조용히 지우지 않음)."""
    require(isinstance(text, str), code + "_type")
    table = {re.sub(r"\s+", "", a): a for a in allowed}
    out = []
    for tok in re.split(r"[,，;\n]+", text):
        key = re.sub(r"\s+", "", tok).strip("\"'[]()")
        if not key:
            continue
        hit = table.get(key)
        require(hit is not None, code + "_unknown_id:" + key[:40])
        if hit not in out:
            out.append(hit)
    return out


_SENT = re.compile(r"[^.?!\n]+[.?!]?")
_ASKING = re.compile(r"\?|(?:나요|까요|가요|는지요|습니까|니까|할까|일까|되나|하나|있나|없나|인가|건가요|죠|지요|"
                     r"궁금해요|궁금합니다|알려\s*주세요|알고\s*싶어요|알고\s*싶습니다)\s*[.?!]?\s*$")
_BACKGROUND = re.compile(r"(?:모르겠어요|모르겠습니다|모르겠네요|부담돼요|부담됩니다|걱정돼요|걱정됩니다|불안했어요|"
                         r"힘들어요|했어요|됐어요|있어요|봤어요|했는데요|돼요)\s*[.!]?\s*$")


def background_flags(question: str, items: list[dict]) -> list[bool]:
    """F28·PKG8: 배경 진술 문장('~모르겠어요.')에서만 나온 요청을 표시한다(질문에 실제 물음 문장이 따로 있을 때만).
    PKG7은 이런 요청을 뺐지만 '~어떻게 되는지도 걱정돼요'처럼 답할 수 있는 물음까지 빠져(R3-REAL-A-07),
    PKG8부터는 빼지 않고 '답 전체를 막지 않는 요청'으로 둔다(근거가 없으면 부수 사항으로 처리)."""
    kept, dropped = filter_background_requests(question, items)
    keep_ids = {id(x) for x in kept}
    return [id(it) not in keep_ids for it in items]


def filter_background_requests(question: str, items: list[dict]):
    """배경 진술 문장에서만 나온 요청을 가른다. 인용 구절이 질문에 없으면 판단하지 않고 둔다."""
    sents = [m.group(0).strip() for m in _SENT.finditer(question) if m.group(0).strip()]

    def sentence_of(quote: str):
        """인용 구절을 담은 문장. 구절이 여러 문장에 걸치면(PKG10, R3-REAL-S-09) 이어진 문장을 합쳐 본다."""
        q = re.sub(r"\s+", "", quote)
        if not q:
            return None
        for width in (1, 2, 3):
            for i in range(len(sents) - width + 1):
                span = " ".join(sents[i:i + width])
                if q in re.sub(r"\s+", "", span):
                    return span
        return None
    kinds = []
    for it in items:
        s = sentence_of(str(it.get("a_quote", "")))
        kinds.append("unknown" if s is None else "ask" if _ASKING.search(s) else
                     "background" if _BACKGROUND.search(s) else "other")
    if "ask" not in kinds:
        return items, []
    kept = [it for it, k in zip(items, kinds) if k != "background"]
    dropped = [str(it.get("b_request", ""))[:120] for it, k in zip(items, kinds) if k == "background"]
    return (kept or items), (dropped if kept else [])


def request_wire(requests: list[str]) -> list[dict]:
    """계획 단계에서 고정한 요청 id(R1…)와 문구. 선택·생성·검토는 이 id로 1:1 대응해야 한다(F05)."""
    return [{"id": f"R{i}", "request": r} for i, r in enumerate(requests, 1)]


def check_request_ids(items: list, requests: list[str], code: str):
    """출력 요청 수가 고정 요청 수와 같고, 순서대로 같은 id(또는 같은 문구)인지 검사한다."""
    require(len(items) == len(requests), code + "_request_count")
    for i, (it, text) in enumerate(zip(items, requests), 1):
        v = str(it.get("a_request", "")).strip()
        require(v == f"R{i}" or v.startswith((f"R{i}:", f"R{i} ", f"R{i}.")) or v == text.strip(),
                code + f"_request_id:R{i}")


class PkgPipeline(Pipeline):
    def __init__(self, cfg, client=None, transport=None, allow_live=False):
        super().__init__(cfg, client=client, transport=transport, allow_live=allow_live)
        self.index = PropIndex(self.corpus, cfg.data_dir, use_dense=True)
        self.verify = AnswerVerifier(self)  # PKG12 답변 후 검증(외부 의존 확인·기권 복구, 기본 끔)

    # ------------------------------------------------------------------ 계약
    def _frame(self, question, deadline, tr, state):
        body = contracts.chat_body(self.cfg.plan_model, PP.FRAME_SYSTEM, {"question": question},
                                   PP.frame_schema(), "pkg_frame", self.cfg.plan_max_tokens)

        def parse(raw):
            v = loads_strict(raw)
            require(isinstance(v, dict) and set(v) == {"a_requests", "b_queries"}, "frame_fields")
            items = [r for r in v["a_requests"] if isinstance(r, dict) and str(r.get("b_request", "")).strip()]
            qs = [q.strip() for q in v["b_queries"] if isinstance(q, str) and q.strip()]
            require(items and qs, "frame_empty")
            items = items[:4]
            bg = background_flags(question, items)
            if any(bg):
                tr["frame_background_nonblocking"] = [str(r["b_request"])[:120] for r, b in zip(items, bg) if b]
            return {"requests": [str(r["b_request"]).strip() for r in items],
                    "quotes": [str(r.get("a_quote", "")).strip() for r in items],
                    "background": bg, "queries": list(dict.fromkeys(qs))[:4]}
        try:
            return self._structured("plan", body, parse, deadline, tr, state, None, allow_repair=False)
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            tr["frame_fallback"] = getattr(e, "code", str(e))
            return {"requests": [question], "queries": []}

    def _select(self, question, frame, cards, deadline, tr, state):
        # 카드에 보여 준 id(본문 카드에 딸린 단서 id 포함)는 모두 고를 수 있다.
        allowed = {c["id"] for c in cards} | {c["proviso"]["id"] for c in cards if "proviso" in c}
        n = len(frame["requests"])
        body = contracts.chat_body(self.cfg.answer_model, PP.SELECT_SYSTEM,
                                   {"question": question, "requests": request_wire(frame["requests"]), "cards": cards},
                                   PP.select_schema(n), "pkg_select", self.cfg.select_max_tokens)

        def parse(raw):
            v = loads_strict(raw)
            require(isinstance(v, dict) and set(v) == {"a_requests"}, "select_fields")
            reqs = v["a_requests"]
            require(isinstance(reqs, list), "select_count")
            for r in reqs:
                require(isinstance(r, dict) and set(r) == {"a_request", "b_ids"}, "select_item")
            check_request_ids(reqs, frame["requests"], "select")
            out = [{"id": f"R{i}", "request": text, "ids": parse_ids(r["b_ids"], allowed, "select")}
                    for i, (r, text) in enumerate(zip(reqs, frame["requests"]), 1)]
            if self.cfg.pkg_select_max_ids > 0:  # PKG8 상한은 정당한 넓은 선택(감면 20여 호)까지 막아 PKG9에서 기본 끔
                for r in out:
                    require(len(r["ids"]) <= self.cfg.pkg_select_max_ids, f"select_too_many_ids:{r['id']}")
            return out
        return self._structured("select", body, parse, deadline, tr, state, PP.REPAIR_TEMPLATE)

    def _generate_body(self, question, frame, sources, draft=None, issues=None):
        value = {"question": question, "requests": request_wire(frame["requests"]), "sources": sources}
        base = PP.generate_system(self.cfg.pkg_generate_external_rule)
        system, name = base, "pkg_generate"
        if draft is not None:
            value["draft"] = draft["wire"]
            value["review_issues"] = issues
            system, name = base + PP.CORRECT_ADDENDUM, "pkg_correct"
        return contracts.chat_body(self.cfg.answer_model, system, value, PP.generate_schema(len(frame["requests"])),
                                   name, self.cfg.answer_max_tokens)

    @staticmethod
    def parse_generate(raw, allowed: set, requests: list[str] | None = None, strict: bool = False,
                       background: list[bool] | None = None) -> dict:
        """요청별: 묻는 값(b_asked) → 원문 지위(c_basis) → 주장 → 상태.
        상태는 원문 지위와 맞아야 한다(F15): 원문이 묻는 값·판정을 주지 않는다고 스스로 판정한 요청
        (SILENT·DELEGATED·EXTERNAL)을 ANSWERED로 내면 핵심 근거 부족으로 본다."""
        v = loads_strict(raw)
        require(isinstance(v, dict) and set(v) == {"a_requests"}, "generate_fields")
        reqs = v["a_requests"]
        require(isinstance(reqs, list) and 1 <= len(reqs) <= 4, "generate_count")
        for r in reqs:
            require(isinstance(r, dict) and set(r) == {"a_request", "b_asked", "c_basis", "d_claims", "e_status"},
                    "generate_item")
        if requests is not None:
            check_request_ids(reqs, requests, "generate")
        texts = list(requests) if requests is not None else [str(r["a_request"]).strip() for r in reqs]
        parsed, missing = [], False
        for i, r in enumerate(reqs, 1):
            require(r["e_status"] in ("ANSWERED", "CORE_MISSING", "SIDE_MISSING"), "generate_status")
            require(r["c_basis"] in PP.BASIS, "generate_basis")
            status, basis = r["e_status"], r["c_basis"]
            # F27: 원문 지위와 상태는 양방향으로 맞아야 한다. 엄격 모드에서는 충돌을 계약 오류로 보내 1회 복구한다.
            conflict = (status == "ANSWERED" and basis in PP.BASIS_MISSING) or \
                (status != "ANSWERED" and basis not in PP.BASIS_MISSING)
            if strict and conflict:
                raise ContractError(f"basis_status_conflict:R{i}:{basis}/{status}")
            base = {"id": f"R{i}", "request": texts[i - 1], "asked": str(r["b_asked"]).strip()[:120], "basis": basis}
            if conflict:
                base["basis_status_conflict"] = f"{basis}/{status}"
            if status == "ANSWERED" and basis in PP.BASIS_MISSING:
                status = "CORE_MISSING"
                base["status_from_basis"] = True  # 모델이 붙인 ANSWERED와 원문 지위 판정이 충돌
            if status == "CORE_MISSING" and background and i <= len(background) and background[i - 1]:
                status = "SIDE_MISSING"  # PKG8: 배경 진술에서 나온 요청은 답 전체를 막지 않는다
                base["background_nonblocking"] = True
            if status in ("CORE_MISSING", "SIDE_MISSING"):
                # 근거 부족 요청의 설명 문장은 제출하지 않으므로 근거 id를 요구하지 않는다(기록만).
                parsed.append(dict(base, status=status, claims=[],
                                   discarded_text=[str(c.get("a_text", ""))[:200] for c in r["d_claims"]
                                                   if isinstance(c, dict)]))
                missing = missing or status == "CORE_MISSING"
                continue
            claims = []
            for j, c in enumerate(r["d_claims"], 1):
                require(isinstance(c, dict) and set(c) == {"a_text", "b_ids"}, "claim_fields")
                text = str(c["a_text"]).strip()
                require(text, "claim_text")
                ids = parse_ids(c["b_ids"], allowed, "claim")
                require(ids, "claim_without_ids")  # 답한 주장마다 근거가 있어야 한다
                claims.append({"no": f"R{i}C{j}", "text": text, "ids": ids})
            require(claims, "answered_without_claims")
            require(not claims[0]["text"].startswith(contracts.ABSTAIN_TEXT), "answered_but_abstain_text")
            parsed.append(dict(base, status=status, claims=claims))
        # 핵심 요청 근거 부족이면 기권. 부수 사항만 없으면 답한 요청으로 답한다(답한 요청이 하나는 있어야 함).
        if not missing and not any(r["status"] == "ANSWERED" for r in parsed):
            missing = True
        decision = "ABSTAIN" if missing else "ANSWER"
        return {"decision": decision, "requests": parsed, "wire": v,
                "answer": " ".join(c["text"] for r in parsed for c in r["claims"]) if not missing else "",
                "used": list(dict.fromkeys(i for r in parsed for c in r["claims"] for i in c["ids"]))}

    def _review(self, question, parsed, sources, allowed, deadline, tr, state):
        claims = [{"no": c["no"], "text": c["text"], "ids": ", ".join(c["ids"])}
                  for r in parsed["requests"] for c in r["claims"]]
        wire = [{"id": r["id"], "request": r["request"], "asked": r.get("asked", ""), "basis": r.get("basis", ""),
                 "status": r["status"],
                 "claims": [{"no": c["no"], "text": c["text"], "ids": ", ".join(c["ids"])} for c in r["claims"]]}
                for r in parsed["requests"]]
        n = len(parsed["requests"])
        body = contracts.chat_body(self.cfg.review_model, PP.REVIEW_SYSTEM,
                                   {"question": question, "requests": wire, "sources": sources},
                                   PP.review_schema(n), "pkg_review", self.cfg.review_max_tokens)
        claim_ids = {c["no"]: set(c["ids"].split(", ")) for c in claims}
        request_ids = {r["id"] for r in parsed["requests"]}

        def parse(raw):
            v = loads_strict(raw)
            require(isinstance(v, dict) and set(v) == {"a_requests", "b_issues"}, "review_fields")
            verdicts = v["a_requests"]
            require(isinstance(verdicts, list), "review_verdict_count")
            for it in verdicts:
                require(isinstance(it, dict) and set(it) == {"a_request", "b_check", "c_verdict"}, "verdict_fields")
                require(it["c_verdict"] in PP.VERDICTS, "verdict_kind")
            check_request_ids(verdicts, [r["request"] for r in parsed["requests"]], "review")
            out = []
            for it in v["b_issues"]:
                require(isinstance(it, dict) and set(it) == {"a_kind", "b_claim", "c_ids", "d_reason"}, "issue_fields")
                require(it["a_kind"] in PP.ISSUE_KINDS, "issue_kind")
                ids = parse_ids(it["c_ids"], allowed, "issue")
                out.append({"kind": it["a_kind"], "claim": it["b_claim"].strip(), "ids": ids,
                            "detail": it["d_reason"][:300]})
            # F15: 요청 판정이 '핵심 미응답'인데 조치 목록에 그 요청 지적이 없으면, 판정을 구조화된 지적으로 옮긴다.
            # (자유 메모를 정규식으로 읽지 않는다. 판정 필드만 쓴다.)
            for i, it in enumerate(verdicts, 1):
                rid = f"R{i}"
                # PKG11: 없는 주장 번호(R1C9 등)의 지적은 뒤에서 버려지므로 '이미 지적됨'으로 세지 않는다.
                flagged = any(o["kind"] == "unanswered_core" and
                              (o["claim"] == rid or (o["claim"].startswith(rid + "C") and o["claim"] in claim_ids))
                              for o in out)
                if it["c_verdict"] == "UNANSWERED_CORE" and not flagged:
                    out.append({"kind": "unanswered_core", "claim": rid, "ids": [], "detail": it["b_check"][:300],
                                "from_verdict": True})
            return {"issues": out, "verdicts": [{"id": f"R{i}", "verdict": it["c_verdict"], "check": it["b_check"][:300]}
                                                for i, it in enumerate(verdicts, 1)]}
        try:
            res = self._structured("review", body, parse, deadline, tr, state, PP.REPAIR_TEMPLATE)
            issues = res["issues"]
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            tr["review"] = {"status": "unavailable", "error": getattr(e, "code", str(e))}
            return None
        # 버리는 지적: 없는 주장 번호, 이미 그 주장의 근거에 있는 id의 '인용 누락'.
        kept, dropped = [], []
        for i in issues:
            on_request = i["kind"] == "unanswered_core" and i["claim"] in request_ids  # 요청 단위 핵심 미응답(F05)
            if not on_request and (i["claim"] not in claim_ids or
                                   (i["kind"] == "citation_missing" and set(i["ids"]) <= claim_ids[i["claim"]])):
                dropped.append(i)
            else:
                kept.append(i)
        tr["review"] = {"status": "ok", "issues": kept, "verdicts": res["verdicts"]}
        if dropped:
            tr["review"]["dropped"] = dropped
        return kept

    def external_list_dependencies(self, parsed: dict) -> list:
        """후보 B: 답한 요청의 주장이 외부 법령 목록(LIST) 명제를 근거로 쓰면, 그 결론이 목록 포함 여부에 기대는지
        교정자가 원문과 대조해 확인하도록 요청 단위 지적을 만든다. 판단과 기각은 교정자가 한다."""
        out = []
        for r in parsed["requests"]:
            if r["status"] != "ANSWERED":
                continue
            ids = [i for c in r["claims"] for i in c["ids"]
                   if ((self.index.by_id.get(i) or {}).get("attrs", {}).get("external") or {}).get("what") == "LIST"]
            ids = list(dict.fromkeys(ids))
            if not ids:
                continue
            spans = "; ".join(self.index.by_id[i]["attrs"]["external"].get("span") or i for i in ids)
            out.append({"kind": "unanswered_core", "claim": r["id"], "ids": ids,
                        "detail": (f"이 요청의 주장이 외부 법령 목록(«{spans}»)에 특정 행위·대상이 포함되는지에 기대는지 "
                                   "원문과 대조해 확인할 것. 그 목록은 이 조각에 열거되지 않는다. 결론이 목록 포함 여부와 "
                                   "무관하면 이 지적을 기각한다."),
                        "from_external_list_check": True})
        return out

    def scope_violations(self, parsed: dict) -> list:
        """F25(PKG9): 주장이 '제N항에 따른 X'로 적용 대상이 정해진 규정(applies_to)을 쓰면서, 그 대상 항은 근거에 없고
        같은 조의 다른 항은 근거에 있으면 범위 밖 적용 의심으로 본다(결정적 검사 → 교정에 지적으로 전달)."""
        out = []
        for r in parsed["requests"]:
            for c in r["claims"]:
                units = {(self.index.by_id.get(i) or {}).get("unit") for i in c["ids"]}
                for i in c["ids"]:
                    p = self.index.by_id.get(i)
                    if not p or not p["attrs"].get("applies_to"):
                        continue
                    m = re.match(r"(unit:\d+:)(제\d+조(?:의\d+)?)제(\d+)항", p["unit"])
                    if not m:
                        continue
                    prefix, art, own = m.group(1), m.group(2), m.group(3)
                    for scope in p["attrs"]["applies_to"]:
                        t = re.match(r"(제\d+조(?:의\d+)?)?제(\d+)항", scope)
                        if not t:
                            continue
                        target = f"{prefix}{t.group(1) or art}제{t.group(2)}항"
                        others = [u for u in units if u and u.startswith(prefix + art + "제")
                                  and u not in (target, p["unit"])]
                        if target not in units and others:
                            out.append({"kind": "contradiction", "claim": c["no"], "ids": [i],
                                        "detail": f"{i}의 효과는 '{scope}'에만 적용된다(원문). 이 주장은 그 대상이 아닌 "
                                                  f"{', '.join(u.split(':')[-1] for u in others)}에 적용한 것으로 보인다.",
                                        "from_scope_check": True})
        return out

    # PKG11.1: Pro4는 교정 기권 때 유형을 거의 SILENT로 적는다(생성 EXTERNAL 0.5%, 교정 1/54). 그래서 EXTERNAL만 받던
    # 예외는 저장 실행 전체에서 한 번도 작동하지 않았다. 유형 라벨 대신 그래프의 외부 참조 표시를 근거로 삼는다.
    EXTERNAL_ABSTAIN_BASES = ("EXTERNAL", "SILENT")

    def correction_external_ok(self, issues: list, corrected: dict, draft: dict) -> bool:
        """교정 기권 수용(외부 의존): 검토가 그 요청을 핵심 미응답으로 지적했고, 교정이 CORE_MISSING(EXTERNAL·SILENT)이며,
        초안이 그 요청에 쓴 명제에 그래프상 외부 참조(목록·정의 참조, 외부 법령 위임)가 있을 때."""
        flagged = {i["claim"].split("C")[0] if "C" in i["claim"][1:] else i["claim"]
                   for i in issues if i["kind"] == "unanswered_core"}
        drafted = {r["id"]: r for r in draft["requests"]}
        for r in corrected["requests"]:
            if r["status"] != "CORE_MISSING" or r.get("basis") not in self.EXTERNAL_ABSTAIN_BASES \
                    or r["id"] not in flagged:
                continue
            used = [i for c in drafted.get(r["id"], {}).get("claims", []) for i in c["ids"]]
            if any(self.verify.outside_reference(i) for i in used):
                return True
        return False

    @staticmethod
    def correction_abstain_ok(issues: list, corrected: dict) -> bool:
        flagged = set()
        for i in issues:
            if i["kind"] == "unanswered_core":
                flagged.add(i["claim"].split("C")[0] if "C" in i["claim"][1:] else i["claim"])
        return any(r["status"] == "CORE_MISSING" and r.get("basis") in PP.BASIS_MISSING and r["id"] in flagged
                   for r in corrected["requests"])

    # ------------------------------------------------------------------ 흐름
    def _run(self, question, deadline, tr, state):
        cfg = self.cfg
        frame = self._frame(question, deadline, tr, state)
        tr["frame"] = frame
        qvec, qvecs = None, []
        if self.index.vecs is not None:
            try:
                vecs = self.client.embed("solar-embedding-2-query", [question] + frame["queries"], deadline,
                                         cfg.stage_caps["embed"], tr["calls"])
                qvec, qvecs = vecs[0], vecs[1:]
            except CallError as e:
                if e.code == "budget_cap_reached":
                    raise
                tr["dense"] = {"fallback": "sparse_only", "error": e.code}
        ranked = self.index.rank(question, frame["queries"], qvec, qvecs, depth=cfg.pkg_candidates)
        if not ranked:
            tr["route"] = "no_lexical_candidates"
            return "ABSTAIN", dict(contracts.UNKNOWN)
        cards = self.index.candidate_cards(ranked)
        tr["retrieval"] = {"ranked": ranked[:20], "cards": [c["id"] for c in cards]}

        sel = self._select(question, frame, cards, deadline, tr, state)
        tr["selection"] = sel
        bg = frame.get("background") or [False] * len(sel)
        # PKG9: 배경 진술에서 나온 요청은 선택 단계에서도 답 전체를 막지 않는다(PKG8 B-09: 근거 0개 → 전체 기권).
        if any(not r["ids"] for r, b in zip(sel, bg) if not b) or not any(r["ids"] for r in sel):
            tr["route"] = "select_core_missing"
            return "ABSTAIN", dict(contracts.UNKNOWN)
        selected = list(dict.fromkeys(i for r in sel for i in r["ids"]))
        delivery = self.index.deliver(selected)
        sources = self.index.render(delivery, external_list_note=cfg.pkg_render_external_note,
                                    designation_note=cfg.pkg_render_designation_note,
                                    definition_note=cfg.pkg_render_definition_note,
                                    junyong_note=cfg.pkg_render_junyong_note,
                                    outside_note=cfg.pkg_render_outside_note)
        allowed = set(delivery.prop_ids)
        tr["delivery"] = {"units": delivery.units, "reasons": delivery.reasons,
                          "chars": sum(len(json.dumps(s, ensure_ascii=False)) for s in sources)}

        def parse(raw):
            # F27: 복구 기회가 남아 있으면 지위·상태 충돌을 계약 오류로 보내 다시 대조하게 하고,
            # 이미 썼거나 복구 응답도 충돌하면 기존 규칙(ANSWERED+원문 부재 → 근거 부족, 그 반대는 모델 상태)으로 받는다.
            strict = not state["repair_used"]
            out = self.parse_generate(raw, allowed, frame["requests"], strict=strict,
                                      background=frame.get("background"))
            if any(r.get("basis_status_conflict") for r in out["requests"]):
                tr.setdefault("basis_conflicts_accepted", []).extend(
                    r["id"] + ":" + r["basis_status_conflict"] for r in out["requests"] if r.get("basis_status_conflict"))
            return out
        draft = self._structured("generate", self._generate_body(question, frame, sources), parse,
                                 deadline, tr, state, PP.REPAIR_TEMPLATE)
        tr["draft"] = {k: draft[k] for k in ("decision", "requests", "used")}
        if draft["decision"] == "ABSTAIN":
            recovered = self.verify.recover(question, frame, draft, sources, allowed, deadline, tr, state) \
                if cfg.pkg_abstention_recovery else None  # PKG12 C(기본 끔)
            if recovered is None:
                tr["route"] = "generate_core_missing"
                return "ABSTAIN", dict(contracts.UNKNOWN)
            return self.submission(recovered, tr, "recovered:generate")

        final = draft
        if cfg.review and deadline - time.monotonic() >= cfg.min_seconds_for_review:
            issues = self._review(question, draft, sources, allowed, deadline, tr, state)
            scope = self.scope_violations(draft)  # PKG9(F25): 적용 대상이 정해진 규정의 범위 밖 적용 의심
            if scope:
                tr["scope_violations"] = scope
                issues = (issues or []) + scope
            if cfg.pkg_external_list_check:  # PKG11 후보 B(시험용, 기본 끔)
                xl = self.external_list_dependencies(draft)
                if xl:
                    tr["external_list_check"] = xl
                    issues = (issues or []) + xl
            if issues:
                if deadline - time.monotonic() < cfg.min_seconds_for_review:
                    tr["correction"] = {"status": "skipped_insufficient_time"}
                else:
                    try:
                        final = self._structured(
                            "correct", self._generate_body(question, frame, sources, draft, issues),
                            parse, deadline, tr, state, PP.REPAIR_TEMPLATE)
                        tr["correction"] = {"status": "ok", "decision": final["decision"],
                                            "requests": final["requests"]}
                        # F01: 교정 기권은 '검토가 그 요청의 핵심 미응답을 지적했고, 교정자가 원문과 대조해
                        # 묻는 값·판정이 원문에 없다(SILENT·DELEGATED·EXTERNAL)고 판정한 요청'이 있을 때만 받는다.
                        # 다른 종류의 지적(조건 누락 등)에서 나온 기권은 버리고 초안을 유지한다.
                        # PKG5 실측: 이 규칙으로 받은 교정 기권은 정답 2건 손실·함정 1건 획득이라 기본값은 끈다.
                        if final["decision"] == "ABSTAIN":
                            if cfg.pkg_accept_correction_abstain and self.correction_abstain_ok(issues, final):
                                tr["correction"]["status"] = "abstain_accepted"
                            elif cfg.pkg_accept_external_correction_abstain and \
                                    self.correction_external_ok(issues, final, draft):
                                # G-09(PKG8): 검토가 핵심 미응답을 지적하고, 교정자가 외부 법령 의존으로 판정했으며,
                                # 초안이 쓴 명제에 그래프상 외부 의존(목록·정의)이 있으면 교정 기권을 받는다.
                                tr["correction"]["status"] = "abstain_accepted_external"
                            else:
                                tr["correction"]["status"] = "abstain_rejected"
                                final = draft
                    except (ContractError, CallError) as e:
                        if isinstance(e, CallError) and e.code == "budget_cap_reached":
                            raise
                        tr["correction"] = {"status": "unavailable", "error": getattr(e, "code", str(e))}
                        final = draft
        elif cfg.review:
            tr["review"] = {"status": "skipped_insufficient_time"}
        if final["decision"] == "ABSTAIN":
            recovered = self.verify.recover(question, frame, final, sources, allowed, deadline, tr, state) \
                if cfg.pkg_abstention_recovery else None  # PKG12 C(기본 끔)
            if recovered is None:
                tr["route"] = "correction_core_missing"
                return "ABSTAIN", dict(contracts.UNKNOWN)
            return self.submission(recovered, tr, "recovered:correction")
        if cfg.pkg_external_dependency_check and self.verify.external_dependency(question, frame, final, sources,
                                                                                 deadline, tr):
            tr["route"] = "external_dependency_abstain"  # PKG12 A·B(기본 끔)
            return "ABSTAIN", dict(contracts.UNKNOWN)
        return self.submission(final, tr, f"answered:review={tr.get('review', {}).get('status', 'disabled')}"
                                          f":correction={tr.get('correction', {}).get('status', 'none')}")

    def submission(self, final: dict, tr: dict, route: str):
        """최종 답(parse_generate 형식) → 제출 객체. 인용·URL·시행일은 주장에 쓴 명제에서 코드가 조립한다."""
        units = self.index.citation_closure(final["used"])
        labels = list(dict.fromkeys(self.corpus.by_id[u].label for u in units))
        first = self.corpus.by_id[units[0]]
        tr["citation_units"] = units
        tr["route"] = route
        return "ANSWER", {"answer": final["answer"], "citation": labels,
                          "source_url": first.source_url, "effective_date": first.effective_date}
