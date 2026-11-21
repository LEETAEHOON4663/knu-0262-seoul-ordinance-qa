"""새 채점 지침(2026-11-19) 대응 플래그 시험: 인용 개수 규칙(pkg_citation_rule), 위임·준용 안내 답(pkg_delegation_answers)."""
import json
import unittest
from pathlib import Path

from lawqa import pkg_prompts as PP
from lawqa.contracts import UNKNOWN
from lawqa.corpus import Corpus
from lawqa.pkg_pipeline import PkgPipeline
from lawqa.pkg_runtime import PropIndex

import test_pkg as T
from test_pkg import GW, LEAD, ITEM, gen, review, run, select

H0 = T.Flow.H  # 클래스를 직접 가져오면 그 시험이 이 모듈에서도 다시 돈다

X = PropIndex(Corpus(), Path(__file__).resolve().parent.parent / "data", use_dense=False)


def _closure_role(e):
    return "REVIEWED" if e["role"].startswith("REVIEWED:") else e["role"]


class CitationRule(unittest.TestCase):
    def test_claim_units_are_never_cut(self):
        used = ["주차장:제3조제2항", "주차장:제6조제1항", "주차장:제13조제3항", "주차장:제16조제2항"]
        claim_units = list(dict.fromkeys(X.by_id[p]["unit"] for p in used))
        got = X.citation_closure_ranked(used, 3)
        self.assertEqual(got, claim_units)  # 주장 단위가 이미 3개 이상이면 연결 단위를 붙이지 않는다
        self.assertGreater(len(got), 3)

    def test_closure_fills_to_total_by_priority(self):
        # 자전거 제9조 제1항 단서: 표(별표) 연결 + 검토 연결. 표가 먼저 붙어야 한다.
        used = ["자전거:제9조제1항단서"]
        got = X.citation_closure_ranked(used, 3)
        self.assertEqual(got[0], X.by_id[used[0]]["unit"])
        self.assertLessEqual(len(got), 3)
        table_targets = [e for e in X.out[used[0]] if e["role"] == "TABLE"]
        tu = X.by_id[table_targets[0]["to"]]["unit"] if table_targets[0]["to_kind"] == "prop" else table_targets[0]["to"]
        self.assertEqual(got[1], tu)
        self.assertTrue(set(got) <= set(X.citation_closure(used)))  # 기존 연결의 부분집합

    def test_reference_comes_last(self):
        used = ["주차장:제18조제1항"]  # 예외 1 + 참조 5
        full = X.citation_closure(used)
        got = X.citation_closure_ranked(used, 3)
        self.assertEqual(len(got), min(3, len(full)))
        exc = [e for e in X.out[used[0]] if e["role"] == "EXCEPTION"][0]
        eu = X.by_id[exc["to"]]["unit"] if exc["to_kind"] == "prop" else exc["to"]
        if eu != got[0]:
            self.assertEqual(got[1], eu)

    def test_flag_changes_submission_citations(self):
        h = dict(H0)
        _, off, _, _ = run(dict(h))
        _, on, tr, _ = run(dict(h), pkg_citation_rule=True)
        self.assertEqual(on["citation"][0], off["citation"][0])
        self.assertLessEqual(len(on["citation"]), max(3, 2))
        self.assertTrue(set(on["citation"]) <= set(off["citation"]))


class DelegationPrompts(unittest.TestCase):
    def test_off_is_unchanged(self):
        self.assertIs(PP.select_system(False), PP.SELECT_SYSTEM)
        self.assertIs(PP.review_system(False), PP.REVIEW_SYSTEM)
        self.assertEqual(PP.generate_system(False, False), PP.GENERATE_SYSTEM)
        self.assertEqual(PP.basis_missing(False), PP.BASIS_MISSING)

    def test_on_rewrites_abstention_rules(self):
        s, g, r = PP.select_system(True), PP.generate_system(False, True), PP.review_system(True)
        self.assertIn("그 주제를 다루는 카드가 하나도 없을 때뿐", s)
        self.assertNotIn("특정 장소가 실제로 지정·운영되는지 같은 현장 사실.", s)
        self.assertIn("c_basis가 DIRECT·APPLIED·DELEGATED·EXTERNAL이면 ANSWERED", g)
        self.assertIn("안내 답", g)
        self.assertIn("안내한 답도 ANSWERED", r)
        self.assertEqual(PP.basis_missing(True), ("SILENT",))
        # 외부 법령 경계 규칙과 함께 켜도 둘 다 들어간다
        both = PP.generate_system(True, True)
        self.assertIn("안내 답", both)
        self.assertNotEqual(both, g)


class DelegationParse(unittest.TestCase):
    allowed = {"a:1", "a:2"}

    def test_delegated_answer_is_answer_when_on(self):
        w = GW(("R1", [("조례에는 구체 금액이 없고 시장이 정합니다.", "a:1")], "ANSWERED", "DELEGATED"))
        off = PkgPipeline.parse_generate(w, self.allowed, ["요청1"])
        on = PkgPipeline.parse_generate(w, self.allowed, ["요청1"], missing_bases=PP.basis_missing(True))
        self.assertEqual(off["decision"], "ABSTAIN")
        self.assertEqual((on["decision"], on["used"]), ("ANSWER", ["a:1"]))

    def test_silent_still_abstains_when_on(self):
        w = GW(("R1", [("조례에 없습니다.", "a:1")], "ANSWERED", "SILENT"))
        on = PkgPipeline.parse_generate(w, self.allowed, ["요청1"], missing_bases=PP.basis_missing(True))
        self.assertEqual(on["decision"], "ABSTAIN")
        self.assertEqual(on["requests"][0]["status"], "CORE_MISSING")

    def test_external_answer_when_on(self):
        w = GW(("R1", [("구체 목록은 조례가 아니라 그 법률이 정합니다.", "a:2")], "ANSWERED", "EXTERNAL"))
        on = PkgPipeline.parse_generate(w, self.allowed, ["요청1"], missing_bases=PP.basis_missing(True))
        self.assertEqual(on["decision"], "ANSWER")


class DelegationFlow(unittest.TestCase):
    TEXT = "조례에는 구체 감면 절차가 없고, 시장은 다둥이 행복카드 소지자의 주차요금을 감면할 수 있습니다."

    def test_prompts_sent_follow_flag(self):
        _, _, _, fake = run(dict(H0), pkg_delegation_answers=True)
        sys_of = {n: b["messages"][0]["content"] for n, b in fake.calls if n != "embed"}
        self.assertEqual(sys_of["pkg_select"], PP.select_system(True))
        self.assertTrue(sys_of["pkg_generate"].startswith(PP.generate_system(False, True)[:200]))
        self.assertIn("안내 답", sys_of["pkg_generate"])
        self.assertEqual(sys_of["pkg_review"], PP.review_system(True))

    def test_delegated_draft_answers_on_and_abstains_off(self):
        h = dict(H0, pkg_generate=gen(f"{ITEM}, {LEAD}", "ANSWERED", text=self.TEXT, basis="DELEGATED"))
        s_off, sub_off, _, _ = run(dict(h))
        s_on, sub_on, tr, _ = run(dict(h), pkg_delegation_answers=True)
        self.assertEqual(sub_off, UNKNOWN)
        self.assertEqual(s_on, "ANSWER")
        self.assertTrue(sub_on["answer"].startswith("조례에는"))
        self.assertTrue(sub_on["citation"])

    def test_external_correction_abstain_not_accepted_when_on(self):
        # 검토가 핵심 미응답을 지적하고 교정이 SILENT로 기권해도, 켜면 외부 의존 기권 수용을 쓰지 않는다(초안 유지).
        h = dict(H0, pkg_review=review([("unanswered_core", "R1", "")], verdict="UNANSWERED_CORE"),
                 pkg_correct=gen("", "CORE_MISSING", basis="SILENT"))
        s_on, sub_on, tr, _ = run(dict(h), pkg_delegation_answers=True)
        self.assertEqual(s_on, "ANSWER")
        self.assertEqual(tr["correction"]["status"], "abstain_rejected")

    def test_select_empty_still_abstains_when_on(self):
        status, sub, tr, _ = run(dict(H0, pkg_select=select(""), pkg_generate=None, pkg_review=None),
                                 pkg_delegation_answers=True)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "select_core_missing")


if __name__ == "__main__":
    unittest.main()


def GW2(*items):
    """요청 여러 개: (id, claims, status, basis)"""
    out = []
    for rid, claims, status, basis in items:
        out.append({"a_request": rid, "b_asked": f"{rid} 묻는 값", "c_basis": basis,
                    "d_claims": [{"a_text": t, "b_ids": i} for t, i in claims], "e_status": status})
    return json.dumps({"a_requests": out}, ensure_ascii=False)


class PartialAnswers(unittest.TestCase):
    allowed = {"a:1", "a:2"}
    W = GW2(("R1", [("조례에는 구체 내용이 없고 시장이 따로 정합니다.", "a:1")], "ANSWERED", "DELEGATED"),
            ("R2", [], "CORE_MISSING", "SILENT"))

    def test_off_abstains_whole(self):
        p = PkgPipeline.parse_generate(self.W, self.allowed, ["요청1", "요청2"], missing_bases=PP.basis_missing(True))
        self.assertEqual(p["decision"], "ABSTAIN")

    def test_on_answers_answered_part_and_notes_missing(self):
        p = PkgPipeline.parse_generate(self.W, self.allowed, ["요청1", "요청2"], missing_bases=PP.basis_missing(True),
                                       partial=True)
        self.assertEqual((p["decision"], p["used"]), ("ANSWER", ["a:1"]))
        self.assertIn("R2 묻는 값", p["answer"])
        self.assertTrue(p["answer"].startswith("조례에는"))

    def test_on_all_missing_still_abstains(self):
        w = GW2(("R1", [], "CORE_MISSING", "SILENT"), ("R2", [], "CORE_MISSING", "SILENT"))
        p = PkgPipeline.parse_generate(w, self.allowed, ["요청1", "요청2"], missing_bases=PP.basis_missing(True),
                                       partial=True)
        self.assertEqual(p["decision"], "ABSTAIN")


class SelectFallback(unittest.TestCase):
    def test_empty_selection_goes_to_generator_with_top_cards(self):
        h = dict(H0, pkg_select=select(""))
        status, sub, tr, fake = run(h, pkg_delegation_answers=True, pkg_select_empty_fallback=5)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(len(tr["select_fallback"]), 5)
        self.assertIn("pkg_generate", fake.names())

    def test_empty_selection_and_silent_generation_abstains(self):
        h = dict(H0, pkg_select=select(""), pkg_generate=gen("", "CORE_MISSING", basis="SILENT"), pkg_review=None)
        status, sub, tr, fake = run(h, pkg_delegation_answers=True, pkg_select_empty_fallback=5)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "generate_core_missing")

    def test_fallback_off_keeps_select_abstain(self):
        status, sub, tr, _ = run(dict(H0, pkg_select=select(""), pkg_generate=None, pkg_review=None),
                                 pkg_delegation_answers=True, pkg_partial_answers=True)
        self.assertEqual(tr["route"], "select_core_missing")


class TopicRulePrompts(unittest.TestCase):
    def test_topic_only_with_delegation(self):
        self.assertEqual(PP.select_system(False, True), PP.SELECT_SYSTEM)
        self.assertEqual(PP.generate_system(False, False, True), PP.GENERATE_SYSTEM)
        self.assertIn("서울도서관", PP.select_system(True, True))
        self.assertIn("위임 문구가 없어도", PP.generate_system(False, True, True))
        self.assertNotIn("위임 문구가 없어도", PP.generate_system(False, True, False))


def govern(verdict, src="", quote="", answer="", rid="R1"):
    return lambda body: json.dumps({"a_checks": [{"a_request": rid, "b_object": "대상", "c_source": src, "d_quote": quote,
                                                  "e_verdict": verdict, "f_answer": answer}]}, ensure_ascii=False)


class GovernanceJudge(unittest.TestCase):
    FLAGS = dict(pkg_delegation_answers=True, pkg_partial_answers=True, pkg_governance_judge=True)
    GUIDED = "조례에는 구체 절차가 없고, 시장은 다둥이 행복카드 소지자의 주차요금을 감면할 수 있습니다."

    def test_rescues_silent_when_quote_is_in_source(self):
        h = dict(H0, pkg_generate=gen("", "CORE_MISSING", basis="SILENT"),
                 pkg_govern=govern("GOVERNS", ITEM, "다둥이", "조례에는 구체 내용이 없고 시장이 감면할 수 있습니다."))
        q = X.by_id[ITEM]["text"]
        self.assertIn("다둥이", q)
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["governance"]["draft"]["changed"], {"R1": "rescued"})
        self.assertTrue(sub["citation"])

    def test_forged_quote_is_not_rescued(self):
        h = dict(H0, pkg_generate=gen("", "CORE_MISSING", basis="SILENT"), pkg_review=None,
                 pkg_govern=govern("GOVERNS", ITEM, "원문에 없는 구절입니다", "조례에는 구체 내용이 없습니다."))
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(sub, UNKNOWN)

    def test_drops_guided_answer_not_governing(self):
        h = dict(H0, pkg_generate=gen(f"{ITEM}, {LEAD}", "ANSWERED", text=self.GUIDED, basis="DELEGATED"),
                 pkg_review=None, pkg_govern=govern("NOT_GOVERNS"))
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["governance"]["draft"]["changed"], {"R1": "dropped"})

    def test_direct_answers_are_not_judged(self):
        _, _, tr, fake = run(dict(H0), **self.FLAGS)
        self.assertNotIn("pkg_govern", fake.names())
        self.assertEqual(tr["governance"]["draft"]["status"], "not_applicable")

    def test_judge_failure_keeps_draft(self):
        h = dict(H0, pkg_generate=gen(f"{ITEM}, {LEAD}", "ANSWERED", text=self.GUIDED, basis="DELEGATED"),
                 pkg_govern=lambda body: "not json")
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(tr["governance"]["draft"]["status"].startswith("unavailable"))

    def test_off_never_calls_judge(self):
        h = dict(H0, pkg_generate=gen("", "CORE_MISSING", basis="SILENT"), pkg_review=None)
        _, sub, _, fake = run(h, pkg_delegation_answers=True, pkg_partial_answers=True)
        self.assertNotIn("pkg_govern", fake.names())
        self.assertEqual(sub, UNKNOWN)


def sel_scope(ids, scope="MATCH", rid="R1"):
    return lambda body: json.dumps({"a_requests": [{"a_request": rid, "b_object": "대상", "c_aspect": "금액·수치",
                                                    "d_ids": ids, "e_scope": scope}]}, ensure_ascii=False)


class ScopeSelect(unittest.TestCase):
    FLAGS = dict(pkg_delegation_answers=True, pkg_partial_answers=True, pkg_scope_select=True, pkg_citation_rule=True)
    GUIDED = "조례에는 구체 절차가 없고, 시장은 다둥이 행복카드 소지자의 주차요금을 감면할 수 있습니다."

    def test_scope_tags_loaded_and_on_cards(self):
        self.assertGreater(len(X.scope), 1000)
        cards = X.candidate_cards([(ITEM, 1.0)], with_scope=True)
        self.assertTrue(all("scope" in c for c in cards))
        self.assertNotIn("scope", X.candidate_cards([(ITEM, 1.0)])[0])

    def test_match_with_direct_answer(self):
        h = dict(H0, pkg_select_scope=sel_scope(f"{LEAD}, {ITEM}"))
        status, sub, tr, fake = run(h, **self.FLAGS)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["scope"][0]["scope"], "MATCH")
        gen_in = json.loads([b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][1]["content"])
        self.assertEqual(gen_in["requests"][0]["scope"], "MATCH")

    def test_no_match_abstains_without_generate(self):
        h = dict(H0, pkg_select_scope=sel_scope("", "NO_OBJECT"), pkg_generate=None, pkg_review=None)
        status, sub, tr, fake = run(h, **self.FLAGS)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "select_scope_none")
        self.assertNotIn("pkg_generate", fake.names())

    def test_match_without_ids_is_not_match(self):
        h = dict(H0, pkg_select_scope=sel_scope("", "MATCH"), pkg_generate=None, pkg_review=None)
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(tr["scope"][0]["scope"], "NO_OBJECT")
        self.assertEqual(sub, UNKNOWN)

    def test_match_but_generator_silent_is_rescued_with_quote(self):
        h = dict(H0, pkg_select_scope=sel_scope(f"{LEAD}, {ITEM}"),
                 pkg_generate=gen("", "CORE_MISSING", basis="SILENT"))
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["scope_applied"]["draft"], {"R1": "rescued"})
        self.assertIn("다둥이", sub["answer"])
        self.assertIn("서울특별시 주차장 설치 및 관리 조례 제7조 제1항", sub["citation"])

    def test_off_uses_old_select(self):
        _, _, tr, fake = run(dict(H0), pkg_delegation_answers=True)
        self.assertIn("pkg_select", fake.names())
        self.assertNotIn("scope", tr)


class ScopeApply(unittest.TestCase):
    """apply_scope: MATCH 아닌 요청의 안내 답은 버리고 직접 답은 둔다."""

    def setUp(self):
        from helpers import make_cfg, tmpdir
        self.pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_scope_select=True,
                                         pkg_partial_answers=True), transport=None)

    def parsed(self, basis, status="ANSWERED"):
        return {"requests": [{"id": "R1", "request": "q", "asked": "a", "basis": basis, "status": status,
                              "claims": [{"no": "R1C1", "text": "t", "ids": [ITEM]}] if status == "ANSWERED" else []}],
                "wire": {}, "decision": "ANSWER", "answer": "t", "used": [ITEM]}

    def test_drop_guided_when_not_match(self):
        out = self.pipe.apply_scope(self.parsed("DELEGATED"), [{"id": "R1", "scope": "OTHER_ASPECT", "ids": []}],
                                    {ITEM}, {}, "draft")
        self.assertEqual(out["decision"], "ABSTAIN")

    def test_keep_direct_when_not_match(self):
        p = self.parsed("DIRECT")
        out = self.pipe.apply_scope(p, [{"id": "R1", "scope": "NO_OBJECT", "ids": []}], {ITEM}, {}, "draft")
        self.assertIs(out, p)


class ScopeAdvisory(unittest.TestCase):
    FLAGS = dict(pkg_delegation_answers=True, pkg_partial_answers=True, pkg_scope_select=True, pkg_scope_advisory=True)

    def test_not_match_still_generates_and_keeps_direct(self):
        h = dict(H0, pkg_select_scope=sel_scope(f"{LEAD}, {ITEM}", "OTHER_ASPECT"))
        status, sub, tr, fake = run(h, **self.FLAGS)
        self.assertEqual(status, "ANSWER")  # 생성기의 직접 답(DIRECT)은 판정과 상관없이 둔다
        self.assertIn("pkg_generate", fake.names())
        sys_gen = [b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][0]["content"]
        self.assertIn("원문이 그 값·판정을 직접 정할 때만", sys_gen)

    def test_not_match_guided_is_dropped(self):
        h = dict(H0, pkg_select_scope=sel_scope(f"{LEAD}, {ITEM}", "NO_OBJECT"),
                 pkg_generate=gen(f"{ITEM}", "ANSWERED", text="조례에는 구체 내용이 없습니다.", basis="DELEGATED"),
                 pkg_review=None)
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["scope_applied"]["draft"], {"R1": "dropped:NO_OBJECT"})

    def test_no_ids_abstains(self):
        h = dict(H0, pkg_select_scope=sel_scope("", "NO_OBJECT"), pkg_generate=None, pkg_review=None)
        status, sub, tr, _ = run(h, **self.FLAGS)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "select_core_missing")


class BoundaryExamples(unittest.TestCase):
    def test_examples_added_only_when_on(self):
        _, _, _, fake = run(dict(H0), pkg_delegation_answers=True, pkg_boundary_examples=True)
        sys_gen = [b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][0]["content"]
        self.assertIn("판정 예시", sys_gen)
        _, _, _, fake = run(dict(H0), pkg_delegation_answers=True)
        sys_gen = [b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][0]["content"]
        self.assertNotIn("판정 예시", sys_gen)


class CitationFill(unittest.TestCase):
    """pkg_citation_fill: 인용이 N개 미만이면 선택기가 고른 근거 조항으로 채운다."""
    OTHER = "주차장:제6조제1항"

    def test_fill_adds_selected_units_up_to_n(self):
        h = dict(H0, pkg_select=select(f"{LEAD}, {ITEM}, {self.OTHER}"), pkg_generate=gen(ITEM))
        _, base, _, _ = run(dict(h))
        _, sub, tr, _ = run(dict(h), pkg_citation_rule=True, pkg_citation_fill=3)
        self.assertIn("서울특별시 주차장 설치 및 관리 조례 제6조 제1항", sub["citation"])
        self.assertLessEqual(len(sub["citation"]), 3)
        self.assertEqual(sub["citation"][0], base["citation"][0])  # 주장 근거가 맨 앞
        self.assertEqual(sub["source_url"], base["source_url"])

    def test_fill_off_is_unchanged(self):
        h = dict(H0, pkg_select=select(f"{LEAD}, {ITEM}, {self.OTHER}"), pkg_generate=gen(ITEM))
        _, sub, tr, _ = run(dict(h), pkg_citation_rule=True)
        self.assertNotIn("서울특별시 주차장 설치 및 관리 조례 제6조 제1항", sub["citation"])
        self.assertNotIn("citation_filled", tr)

    def test_abstention_is_not_filled(self):
        h = dict(H0, pkg_generate=gen("", "CORE_MISSING"), pkg_review=None)
        _, sub, _, _ = run(h, pkg_citation_fill=3)
        self.assertEqual(sub, UNKNOWN)


class ContractRetry(unittest.TestCase):
    def test_fresh_retry_after_failed_repair(self):
        h = dict(H0, pkg_select=["not json", select(ITEM)])
        h["pkg_select#repair"] = "still not json"
        status, sub, tr, fake = run(h, pkg_contract_retry=True)
        self.assertEqual(status, "ANSWER")
        retry = [b for n, b in fake.calls if n == "pkg_select"][1]
        self.assertEqual(retry["temperature"], 0.3)
        self.assertEqual(tr["recoveries"][0]["recovered"], "fresh_retry")

    def test_off_keeps_error(self):
        h = dict(H0, pkg_select=["not json", select(ITEM)])
        h["pkg_select#repair"] = "still not json"
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ERROR")

    def test_retry_failure_is_error(self):
        h = dict(H0, pkg_select=["not json", "nope"])
        h["pkg_select#repair"] = "still not json"
        status, sub, tr, fake = run(h, pkg_contract_retry=True)
        self.assertEqual(status, "ERROR")
        self.assertIn("after_retry", sub["error"])


class CitationCap(unittest.TestCase):
    def test_cap_keeps_first_n(self):
        import lawqa.pkg_pipeline as PPL
        ids = ["주차장:제3조제2항", "주차장:제6조제1항", "주차장:제13조제3항", "주차장:제16조제2항", ITEM]
        many = ", ".join(ids)
        h = dict(H0, pkg_select=select(many), pkg_generate=gen(many))
        orig = PPL.PropIndex.rank
        try:
            PPL.PropIndex.rank = lambda self, *a, **k: [(i, 1.0) for i in ids]
            _, base, _, _ = run(dict(h), pkg_citation_rule=True)
            _, sub, tr, _ = run(dict(h), pkg_citation_rule=True, pkg_citation_cap=4)
        finally:
            PPL.PropIndex.rank = orig
        self.assertGreater(len(base["citation"]), 4)
        self.assertEqual(sub["citation"], base["citation"][:4])
        self.assertEqual(sub["source_url"], base["source_url"])
        self.assertEqual(tr["citation_capped"], base["citation"][4:])


class AbstainResample(unittest.TestCase):
    """pkg_abstain_resample: 기권 초안이면 생성기를 높은 온도로 N번 더 뽑아 k개 이상 답하면 답한다."""
    SILENT = staticmethod(gen("", "CORE_MISSING", basis="SILENT"))  # 클래스 속성 함수가 메서드로 묶이지 않게

    def test_off_abstains(self):
        status, sub, tr, fake = run(dict(H0, pkg_generate=self.SILENT, pkg_review=None), pkg_delegation_answers=True)
        self.assertEqual(sub, UNKNOWN)
        self.assertNotIn("resample", tr)

    def test_rescued_when_k_answers(self):
        h = dict(H0, pkg_generate=[self.SILENT, self.SILENT, gen(ITEM), gen(ITEM)])
        status, sub, tr, fake = run(h, pkg_delegation_answers=True, pkg_abstain_resample=3, pkg_abstain_resample_k=2)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["resample"]["answers"], 2)
        temps = [b.get("temperature") for n, b in fake.calls if n == "pkg_generate"]
        self.assertEqual(temps.count(0.7), 3)
        self.assertIn("pkg_review", fake.names())  # 살린 초안도 검토를 거친다

    def test_not_rescued_below_k(self):
        h = dict(H0, pkg_generate=[self.SILENT, self.SILENT, self.SILENT, gen(ITEM)], pkg_review=None)
        status, sub, tr, fake = run(h, pkg_delegation_answers=True, pkg_abstain_resample=3, pkg_abstain_resample_k=2)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["resample"]["answers"], 1)

    def test_answer_draft_not_resampled(self):
        status, sub, tr, fake = run(dict(H0), pkg_abstain_resample=3)
        self.assertNotIn("resample", tr)
        self.assertEqual([n for n in fake.names() if n == "pkg_generate"], ["pkg_generate"])


class RateLimitRetry(unittest.TestCase):
    """rate_limit_retries: 429면 쉬었다가 같은 요청을 다시 보낸다(기본 0 = PKG11.1 동작)."""
    def test_retry_recovers(self):
        from lawqa.llm import CallError
        import lawqa.llm as L
        orig = L.time.sleep
        L.time.sleep = lambda s: None
        try:
            h = dict(H0, pkg_select=[CallError("rate_limited", uncertain=False, status=503), select(ITEM)])
            status, sub, tr, fake = run(h, rate_limit_retries=2, rate_limit_backoff=0.01)
        finally:
            L.time.sleep = orig
        self.assertEqual(status, "ANSWER")
        sel_calls = [c for c in tr["calls"] if c.get("stage") == "select"]
        self.assertEqual(sel_calls[0]["error"], "rate_limited")
        self.assertEqual(sel_calls[-1].get("rate_limit_retries"), 1)

    def test_off_fails(self):
        from lawqa.llm import CallError
        h = dict(H0, pkg_select=[CallError("rate_limited", uncertain=False, status=503), select(ITEM)])
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ERROR")
        self.assertEqual(sub["error"], "rate_limited")


class LenientSubitemIds(unittest.TestCase):
    def test_subitem_mapped_to_item_when_on(self):
        import lawqa.pkg_pipeline as PPL
        allowed = {"도시공원:제10조제1항제3호", "도시공원:제12조"}
        orig = PPL.LENIENT_SUBITEM_IDS
        try:
            PPL.LENIENT_SUBITEM_IDS = True
            self.assertEqual(PPL.parse_ids("도시공원:제10조제1항제3호가목, 도시공원:제12조", allowed, "claim"),
                             ["도시공원:제10조제1항제3호", "도시공원:제12조"])
            with self.assertRaises(Exception):  # 상위 호도 제공되지 않았으면 그대로 오류
                PPL.parse_ids("도시공원:제11조제1항제2호나목", allowed, "claim")
            PPL.LENIENT_SUBITEM_IDS = False
            with self.assertRaises(Exception):
                PPL.parse_ids("도시공원:제10조제1항제3호가목", allowed, "claim")
        finally:
            PPL.LENIENT_SUBITEM_IDS = orig

    def test_other_lenient_forms(self):
        import lawqa.pkg_pipeline as PPL
        allowed = {"주차장:제5조제1항제4호", "주차장:제2조제16호", "도시공원:제2조제16호", "자전거:제3조"}
        orig = PPL.LENIENT_SUBITEM_IDS
        try:
            PPL.LENIENT_SUBITEM_IDS = True
            self.assertEqual(PPL.parse_ids("주차장:제5조제1항제4호나", allowed, "claim"), ["주차장:제5조제1항제4호"])
            self.assertEqual(PPL.parse_ids("제3조", allowed, "claim"), ["자전거:제3조"])  # 하나에만 맞음
            with self.assertRaises(Exception):
                PPL.parse_ids("제2조제16호", allowed, "claim")  # 두 조례에 다 있어 모호
            self.assertEqual(PPL.parse_ids("해당없음, 자전거:제3조", allowed, "issue"), ["자전거:제3조"])
            with self.assertRaises(Exception):
                PPL.parse_ids("해당없음", allowed, "claim")  # 주장 근거에는 설명문을 받지 않는다
        finally:
            PPL.LENIENT_SUBITEM_IDS = orig

    def test_flag_set_from_config(self):
        import lawqa.pkg_pipeline as PPL
        run(dict(H0), pkg_lenient_subitem_ids=True)
        self.assertTrue(PPL.LENIENT_SUBITEM_IDS)
        run(dict(H0))
        self.assertFalse(PPL.LENIENT_SUBITEM_IDS)


class HumanizeIds(unittest.TestCase):
    def test_conversions(self):
        from lawqa.pkg_pipeline import humanize_ids
        self.assertEqual(humanize_ids("할인한다(주차장:제7조제1항제9호)."), "할인한다(주차장 조례 제7조 제1항 제9호).")
        self.assertEqual(humanize_ids("(도시공원:별표2#행25)"), "(도시공원 조례 별표 2)")
        self.assertEqual(humanize_ids("(주차장:별표1#비고11)"), "(주차장 조례 별표 1 비고 11)")
        self.assertEqual(humanize_ids("(도시공원:제5조제1항단서)"), "(도시공원 조례 제5조 제1항 단서)")
        self.assertEqual(humanize_ids("청년:제10조의2제3항"), "청년 기본 조례 제10조의2 제3항")
        self.assertEqual(humanize_ids("근거 없는 문장."), "근거 없는 문장.")

    def test_flag(self):
        txt = "네, 시장은 감면할 수 있습니다(주차장:제7조제1항제7호)."
        h = dict(H0, pkg_generate=gen(ITEM, text=txt))
        _, off, _, _ = run(dict(h))
        _, on, _, _ = run(dict(h), pkg_humanize_ids=True)
        self.assertIn("주차장:제7조제1항제7호", off["answer"])
        self.assertIn("(주차장 조례 제7조 제1항 제7호)", on["answer"])
        self.assertEqual(off["citation"], on["citation"])


class PartialNoteTopic(unittest.TestCase):
    def test_strip_redundant_prefix(self):
        from lawqa.pkg_pipeline import _missing_topic as m
        self.assertEqual(m("서울특별시 조례상 과태료 부과 여부"), "과태료 부과 여부")
        self.assertEqual(m("서울특별시 주차장 설치 및 관리 조례상 경차 할인"), "경차 할인")
        self.assertEqual(m("112 신고 가능 여부"), "112 신고 가능 여부")
        self.assertEqual(m("서울시 청년 기본 조례에서 정한 이사비"), "서울시 청년 기본 조례에서 정한 이사비")
        self.assertEqual(m("조례상"), "조례상")  # 지우면 빈 문자열이 되는 경우는 원문 유지


class RetryAfterBudgetUsed(unittest.TestCase):
    """앞 단계가 복구를 쓴 뒤 다음 단계 형식 오류도 새 호출 재시도를 받는다(pkg_contract_retry)."""
    def test_select_repaired_then_generate_retried(self):
        h = dict(H0, pkg_select=select("주차장:제99조"), pkg_generate=["not json", gen(f"{ITEM}, {LEAD}")])
        h["pkg_select#repair"] = select(ITEM)
        status, sub, tr, fake = run(h, pkg_contract_retry=True)
        self.assertEqual(status, "ANSWER")
        self.assertEqual([r.get("recovered") for r in tr["recoveries"]], [True, "fresh_retry"])

    def test_off_keeps_old_error(self):
        h = dict(H0, pkg_select=select("주차장:제99조"), pkg_generate=["not json", gen(f"{ITEM}, {LEAD}")])
        h["pkg_select#repair"] = select(ITEM)
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ERROR")
        self.assertIn("repair_budget_used", sub["error"])


class CodexFindings(unittest.TestCase):
    """Codex 점검(2026-11-21): d_claims null로 요청 전체가 죽던 문제, 근거 id가 붙은 미응답 지적 삭제."""
    NULL = json.dumps({"a_requests": [{"a_request": "R1", "b_asked": "x", "c_basis": "DIRECT", "d_claims": None,
                                       "e_status": "ANSWERED"}]})

    def test_null_claims_goes_to_repair(self):
        h = dict(H0, pkg_generate=lambda b: self.NULL)
        h["pkg_generate#repair"] = gen(f"{ITEM}, {LEAD}")
        status, sub, tr, fake = run(h, pkg_contract_retry=True)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["recoveries"][0]["error"], "generate_claims_type")

    def test_one_bad_vote_does_not_kill_resample(self):
        silent = gen("", "CORE_MISSING", basis="SILENT")
        h = dict(H0, pkg_generate=[silent, gen(ITEM), gen(ITEM), gen(ITEM), lambda b: self.NULL, gen(ITEM)])
        status, sub, tr, fake = run(h, pkg_delegation_answers=True, pkg_abstain_resample=5, pkg_abstain_resample_k=3)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["resample"]["answers"], 4)
        self.assertTrue(any(str(d).startswith("ERROR:") for d in tr["resample"]["decisions"]))

    def test_guard_converts_type_error(self):
        from lawqa.pipeline import Pipeline
        from lawqa.contracts import ContractError
        g = Pipeline._guarded(lambda raw: {}["x"])
        with self.assertRaises(ContractError):
            g("{}")

    def _two_req_run(self, issue_ids):
        def frame2(body):
            return json.dumps({"a_requests": [{"a_quote": "할인을 받을 수 있나요", "b_request": "할인 가능 여부"},
                                              {"a_quote": "신청은 어디서", "b_request": "신청 장소"}],
                               "b_queries": ["다둥이 주차요금 감면"]}, ensure_ascii=False)
        sel2 = lambda body: json.dumps({"a_requests": [{"a_request": "R1", "b_ids": ITEM}, {"a_request": "R2", "b_ids": ITEM}]})
        g2 = lambda body: GW2(("R1", [("네, 감면할 수 있습니다.", ITEM)], "ANSWERED", "DIRECT"),
                              ("R2", [], "CORE_MISSING", "SILENT"))
        rv = lambda body: json.dumps({"a_requests": [{"a_request": "R1", "b_check": "c", "c_verdict": "ANSWERED"},
                                                     {"a_request": "R2", "b_check": "c", "c_verdict": "ANSWERED"}],
                                      "b_issues": [{"a_kind": "unanswered_core", "b_claim": "R2", "c_ids": issue_ids,
                                                    "d_reason": "r"}]}, ensure_ascii=False)
        h = {"pkg_frame": frame2, "pkg_select": sel2, "pkg_generate": g2, "pkg_review": rv,
             "pkg_correct": lambda body: GW2(("R1", [("네, 감면할 수 있습니다.", ITEM)], "ANSWERED", "DIRECT"),
                                             ("R2", [("신청은 시장이 정한 곳에서 합니다.", ITEM)], "ANSWERED", "DIRECT"))}
        return run(h, pkg_delegation_answers=True, pkg_partial_answers=True)

    def test_evidenced_unanswered_issue_kept(self):
        status, sub, tr, fake = self._two_req_run(ITEM)
        self.assertIn("pkg_correct", fake.names())
        self.assertNotIn("review_issues_on_missing_dropped", tr)

    def test_unevidenced_unanswered_issue_dropped(self):
        status, sub, tr, fake = self._two_req_run("")
        self.assertNotIn("pkg_correct", fake.names())
        self.assertEqual(tr["review_issues_on_missing_dropped"], 1)
