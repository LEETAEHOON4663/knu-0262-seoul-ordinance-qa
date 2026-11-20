"""⑧ 전체 결합: 질문 → (계획) → 검색·필수근거 → 답변 → 검토 → (교정) → 조립.

결과 상태
  ANSWER  : 근거 있는 답변 4필드
  ABSTAIN : 정확한 확인 불가 객체(핵심 요구 미지원 판단)
  ERROR   : 운영 오류(통신·시간·계약 복구 실패·예산). 기권으로 바꾸지 않는다.

복구 규칙
  - 출력 계약 복구는 요청 전체에서 1회 공유.
  - 처리 불명(uncertain) 호출은 재전송하지 않는다.
  - 계획·임베딩 실패는 원질문 검색으로 진행(선택 단계). 기록을 남긴다.
  - 검토 실패·시간 부족은 검토 전 답변을 유지하고 그 사실을 기록한다.
  - 교정은 1회이며 재검토하지 않는다.
"""
from __future__ import annotations

import copy
import time

from . import contracts, prompts
from .contracts import ContractError
from .corpus import Corpus
from .graph import EvidenceGraph
from .llm import TRUNCATED, CallError, SolarClient
from .search import DenseIndex, NodeDenseIndex, Retriever, attach_focus

# 재검증에서 남으면 기권하는 근거 문제. 조건·인용 누락은 교정 대상이지 기권 사유가 아니다.
SUBSTANTIVE = ("unanswered_core", "unsupported_claim", "contradiction")


class Pipeline:
    def __init__(self, cfg, client: SolarClient | None = None, transport=None,
                 allow_live: bool = False):
        self.cfg = cfg
        self.corpus = Corpus(cfg.data_dir)
        self.graph = EvidenceGraph(self.corpus)
        dense = None
        if cfg.vectors_file and cfg.node_vectors_dir:
            raise ValueError("choose_one_dense_index")
        if cfg.vectors_file:
            dense = DenseIndex(self.corpus, cfg.vectors_file, cfg.vectors_sha256)
        elif cfg.node_vectors_dir:
            dense = NodeDenseIndex(self.corpus, cfg.node_vectors_dir, cfg.node_catalog_sha256,
                                   cfg.node_vectors_sha256)
            if dense.unmapped:
                raise ValueError("node_labels_unmapped")
        self.retriever = Retriever(self.corpus, self.graph, dense)
        self.client = client or SolarClient(cfg, transport=transport, allow_live=allow_live)

    # ------------------------------------------------------------------
    def answer(self, question: str, started: float | None = None) -> tuple[str, dict, dict]:
        """started: HTTP 수신 시각(monotonic). 대기 시간도 같은 마감에 포함한다."""
        started = time.monotonic() if started is None else started
        deadline = started + self.cfg.request_deadline_seconds
        tr = {"question": question, "calls": [], "recoveries": [], "route": None}
        state = {"repair_used": False}
        try:
            status, submission = self._run(question, deadline, tr, state)
        except CallError as e:
            status, submission = "ERROR", {"error": e.code, "http_status": e.status}
            tr["error"] = e.code
        except ContractError as e:
            status, submission = "ERROR", {"error": "contract_" + e.code, "http_status": 502}
            tr["error"] = "contract_" + e.code
        tr["status"] = status
        tr["seconds"] = round(time.monotonic() - started, 3)
        tr["usd"] = round(sum(c.get("usd", 0) for c in tr["calls"]), 8)
        return status, submission, tr

    # ------------------------------------------------------------------
    def _structured(self, stage, body, parser, deadline, tr, state, repair_template,
                    allow_repair=True):
        cap = self.cfg.stage_caps["correct" if stage == "correct" else stage]
        effort = getattr(self.cfg, "reasoning_effort", "")
        if effort and stage in getattr(self.cfg, "reasoning_stages", ()):
            # Solar 추론 설정(주최 측 안내 문서의 개선 항목). 추론 토큰도 완료 토큰에 들어가므로 상한을 늘린다.
            body = dict(body, reasoning_effort=effort, max_tokens=body["max_tokens"] + self.cfg.reasoning_extra_tokens)
        raw = self.client.chat(stage, body, deadline, cap, tr["calls"])
        try:
            if raw == TRUNCATED:
                raise ContractError("output_truncated")
            return parser(raw)
        except ContractError as e:
            rec = {"stage": stage, "error": e.code, "previous_output": raw[:2000]}
            tr["recoveries"].append(rec)
            if not allow_repair or state["repair_used"]:
                rec["recovered"] = False
                raise ContractError(stage + "_contract_repair_budget_used:" + e.code)
            state["repair_used"] = True
            repair = copy.deepcopy(body)
            if raw == TRUNCATED:  # 잘린 출력은 상한을 늘려 1회 재요청
                repair["max_tokens"] = min(body["max_tokens"] * 2, 3000)
            repair["messages"] = body["messages"] + [
                {"role": "assistant", "content": raw if raw != TRUNCATED else ""},
                {"role": "user", "content": repair_template.format(error=e.code)}]
            raw2 = self.client.chat(stage + "_repair", repair, deadline, cap, tr["calls"])
            try:
                if raw2 == TRUNCATED:
                    raise ContractError("output_truncated")
                out = parser(raw2)
            except ContractError as e2:
                rec["recovered"] = False
                if getattr(self.cfg, "pkg_contract_retry", False) and not state.get("fresh_retry_used"):
                    # 복구 대화가 같은 오류를 되풀이하면 대화 없이 새로 1회 호출한다(본문이 달라 캐시도 새로 탄다).
                    state["fresh_retry_used"] = True
                    fresh = dict(body, temperature=0.3)
                    raw3 = self.client.chat(stage + "_retry", fresh, deadline, cap, tr["calls"])
                    try:
                        if raw3 == TRUNCATED:
                            raise ContractError("output_truncated")
                        out = parser(raw3)
                        rec["recovered"] = "fresh_retry"
                        return out
                    except ContractError as e3:
                        raise ContractError(stage + "_contract_after_retry:" + e3.code)
                raise ContractError(stage + "_contract_after_repair:" + e2.code)
            rec["recovered"] = True
            return out

    def _supplement_citations(self, parsed, ctx, tr):
        """T1: 답변 문장에 명시한 같은 조례 조·항·별표가 문맥에 있었는데 인용에서 빠졌으면 보충.

        조례가 하나일 때만 해석한다(여러 조례면 '제6조'가 어느 조례인지 모호).
        항 없이 '제N조'만 쓴 경우는 조 단위 원문이 있을 때만 보충한다.
        """
        used = contracts.used_aliases(parsed)
        docs = {ctx.unit(a).doc_id for a in used}
        if len(docs) != 1:
            return parsed
        rev = {v: k for k, v in ctx.aliases.items()}
        added = []
        for uid, basis in self.graph.find_refs(parsed["answer"], docs.pop(), whole_article=False):
            alias = rev.get(uid)
            if alias and alias not in used and alias not in added:
                added.append(alias)
        if not added:
            return parsed
        tr["citation_supplement"] = [ctx.aliases[a] for a in added]
        out = dict(parsed)
        out["evidence_ids"] = list(parsed["evidence_ids"]) + added
        return out

    def _closure_aliases(self, ctx, selected: list[str]) -> list[str]:
        """선택 근거 + 그 필수근거(1단계) 중 문맥에 있는 것. 문맥 순서 유지."""
        keep = set()
        for a in selected:
            uid = ctx.aliases[a]
            keep.add(uid)
            keep.update(e.dst for e in self.graph.required(uid))
        return [a for a, uid in ctx.aliases.items() if uid in keep]

    def _review(self, question, evidence, ctx, parsed, deadline, tr, state, stage):
        """검증 1회. 반환: 남길 지적 목록, 검증 불가·시간 부족이면 None(기록)."""
        cfg = self.cfg
        if deadline - time.monotonic() < cfg.min_seconds_for_review:
            tr[stage] = {"status": "skipped_insufficient_time"}
            return None
        r_aliases = self._review_aliases(ctx, contracts.used_aliases(parsed))
        r_evidence = [e for e in evidence if e["id"] in r_aliases]
        try:
            issues = self._structured(
                stage, contracts.build_review(question, r_evidence, r_aliases, parsed,
                                              cfg.review_model, cfg.review_max_tokens),
                lambda raw: contracts.parse_review(raw, r_aliases, parsed["answer"]), deadline, tr,
                state, prompts.REVIEW_REPAIR_TEMPLATE)
        except (ContractError, CallError) as e:
            if isinstance(e, CallError) and e.code == "budget_cap_reached":
                raise
            tr[stage] = {"status": "unavailable", "error": getattr(e, "code", str(e))}
            return None
        cited = set(contracts.used_aliases(parsed))
        # 버리는 지적: 이미 인용한 근거의 '인용 누락', 실제 답변에 없는 문장을 인용한 지적.
        dropped = [i for i in issues if (i["kind"] == "citation_missing" and set(i["evidence_ids"]) <= cited)
                   or not i["anchored"]]
        kept = [i for i in issues if i not in dropped]
        tr[stage] = {"status": "ok", "issues": kept, "context": r_aliases}
        if dropped:
            tr[stage]["dropped"] = dropped
        return kept

    def _review_aliases(self, ctx, used: list[str]) -> list[str]:
        keep = set()
        for a in used:
            uid = ctx.aliases[a]
            keep.add(uid)
            keep.update(u.id for u in self.corpus.group_units(uid))
            keep.update(e.dst for e in self.graph.required(uid))
        return [a for a, uid in ctx.aliases.items() if uid in keep]

    def _run(self, question, deadline, tr, state):
        from . import prompts
        cfg = self.cfg
        laws = self.corpus.law_names

        # ③ 질의계획(선택 단계)
        plan = {"laws": [], "queries": []}
        if cfg.query_planning:
            try:
                plan = self._structured(
                    "plan", contracts.build_plan(question, laws, cfg.plan_model, cfg.plan_max_tokens),
                    lambda raw: contracts.parse_plan(raw, laws), deadline, tr, state,
                    None, allow_repair=False)
                tr["plan"] = plan
            except (ContractError, CallError) as e:
                if isinstance(e, CallError) and e.code in ("budget_cap_reached",):
                    raise
                code = getattr(e, "code", str(e))
                tr["plan"] = {"fallback": "original_question_only", "error": code}
                plan = {"laws": [], "queries": []}

        # ② 밀집 질의 임베딩(선택 단계)
        qvec, qvecs = None, []
        if self.retriever.dense is not None:
            try:
                vecs = self.client.embed("solar-embedding-2-query", [question] + plan["queries"],
                                         deadline, cfg.stage_caps["embed"], tr["calls"])
                qvec, qvecs = vecs[0], vecs[1:]
            except CallError as e:
                if e.code == "budget_cap_reached":
                    raise
                tr["dense"] = {"fallback": "sparse_only", "error": e.code}

        # ③ 검색 + 필수근거 문맥
        ranked = self.retriever.rank(question, plan["queries"], plan["laws"], qvec, qvecs,
                                     depth=cfg.retrieval_depth)
        if not ranked:
            # 질문에 조례 어휘가 전혀 없다. 모델 호출 없이 판단할 근거가 없으므로 기권.
            tr["route"] = "no_lexical_candidates"
            return "ABSTAIN", dict(contracts.UNKNOWN)
        ctx = self.retriever.build_context(ranked, cfg.context_budget_chars, cfg.expand_article_chars)
        attach_focus(ctx, self.corpus, self.retriever.dense, [qvec] + list(qvecs))
        evidence = ctx.payload()
        aliases = list(ctx.aliases)
        tr["retrieval"] = {"ranked": ranked[:15], "context_units": [u.id for u in ctx.units],
                           "aliases": ctx.aliases, **ctx.trace}

        # ③⑦ 근거 선택: 요구별 근거. 근거 없는 핵심 요구가 있으면 기권.
        sel = self._structured(
            "select", contracts.build_select(question, evidence, cfg.answer_model, cfg.select_max_tokens),
            lambda raw: contracts.parse_select(raw, aliases), deadline, tr, state, prompts.REPAIR_TEMPLATE)
        tr["selection"] = sel
        if sel["decision"] == "ABSTAIN":
            tr["route"] = "select_unsupported_request"
            return "ABSTAIN", dict(contracts.UNKNOWN)

        # ⑤ 답변: 선택 근거 + 그 필수근거만 전달(선택 누락을 그래프가 보완).
        g_aliases = self._closure_aliases(ctx, sel["selected"])
        g_evidence = [e for e in evidence if e["id"] in g_aliases]
        tr["generate_context"] = g_aliases

        def parse(raw):
            return contracts.parse_generate(raw, g_aliases, sel["requests"])
        draft = self._structured(
            "generate", contracts.build_generate(question, g_evidence, cfg.answer_model,
                                                 cfg.answer_max_tokens),
            parse, deadline, tr, state, prompts.REPAIR_TEMPLATE)
        tr["draft"] = draft
        if draft["decision"] == "ABSTAIN":
            tr["route"] = "generate_abstained"
            return "ABSTAIN", dict(contracts.UNKNOWN)

        final = draft
        # ④ 검증 → (문제 있으면) 교정 1회. 선택: 재검증에서도 근거 문제가 남으면 기권(cfg.rereview).
        # 449 실측(V7)에서 재검증 기권은 함정 9건을 잡고 근거 있는 정답 18건을 잃어 기본은 끈다.
        if cfg.review:
            issues = self._review(question, evidence, ctx, draft, deadline, tr, state, "review")
            if issues:
                if deadline - time.monotonic() < cfg.min_seconds_for_review:
                    tr["correction"] = {"status": "skipped_insufficient_time"}
                else:
                    # 교정자는 검토자가 본 원문까지 함께 본다(검토가 지적한 근거 id를 쓸 수 있게).
                    # PKG-v2 교차검토에서 확인된 V8 공통 버그 수정: 이전에는 선택 근거만 전달했다.
                    c_set = set(g_aliases) | set(tr["review"].get("context", []))
                    c_aliases = [a for a in ctx.aliases if a in c_set]
                    c_evidence = [e for e in evidence if e["id"] in c_set]

                    def parse_c(raw):
                        return contracts.parse_generate(raw, c_aliases, sel["requests"])
                    try:
                        final = self._structured(
                            "correct", contracts.build_generate(
                                question, c_evidence, cfg.answer_model,
                                cfg.answer_max_tokens, draft=draft, issues=issues),
                            parse_c, deadline, tr, state, prompts.REPAIR_TEMPLATE)
                        tr["correction"] = {"status": "ok", "parsed": final}
                        # 교정이 기권하려면 검증이 근거 문제를 지적했어야 한다(조건·인용 지적은 고칠 대상).
                        if final["decision"] == "ABSTAIN" and not any(
                                i["kind"] in SUBSTANTIVE for i in issues):
                            tr["correction"]["status"] = "abstain_rejected_without_core_issue"
                            final = draft
                    except (ContractError, CallError) as e:
                        if isinstance(e, CallError) and e.code == "budget_cap_reached":
                            raise
                        tr["correction"] = {"status": "unavailable", "error": getattr(e, "code", str(e))}
                        final = draft
                    if cfg.rereview and final["decision"] == "ANSWER" and tr["correction"].get("status") == "ok":
                        again = self._review(question, evidence, ctx, final, deadline, tr, state, "rereview")
                        if again and any(i["kind"] in SUBSTANTIVE for i in again):
                            tr["route"] = "rereview_rejected"
                            return "ABSTAIN", dict(contracts.UNKNOWN)
        if final["decision"] == "ABSTAIN":
            tr["route"] = "correction_unsupported_request"
            return "ABSTAIN", dict(contracts.UNKNOWN)
        final = self._supplement_citations(final, ctx, tr)
        review = tr.get("review", {}).get("status", "disabled")
        correction = tr.get("correction", {}).get("status", "none")
        rereview = tr.get("rereview", {}).get("status", "none")
        tr["route"] = f"answered:review={review}:correction={correction}:rereview={rereview}"
        return "ANSWER", contracts.assemble(final, ctx)
