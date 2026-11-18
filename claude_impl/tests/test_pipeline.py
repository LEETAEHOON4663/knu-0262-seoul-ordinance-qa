"""⑧ 결합 경로 오프라인 검사(가짜 Solar). 실제 Solar 응답 품질·점수와 무관하다."""
import json
import unittest

from lawqa.contracts import UNKNOWN
from lawqa.llm import CallError, Ledger, SolarClient
from lawqa.pipeline import Pipeline
from lawqa import selfcheck

from helpers import FakeSolar, TIMEOUT, alias_for, generate_json, make_cfg, plan, review_json, select_json, tmpdir

Q = "공영주차장 주차요금은 얼마인가요?"
PARK_LAW = "서울특별시 주차장 설치 및 관리 조례"


def good_select(body):
    return select_json([("공영주차장 주차요금", [alias_for(body, "제6조 제1항"), alias_for(body, "별표 1")])])


def good_generate(body):
    return generate_json("공영주차장 주차요금은 별표 1의 급지별 요금표에 따릅니다.",
                         [alias_for(body, "제6조 제1항"), alias_for(body, "별표 1")])


def unsupported(body):
    return select_json([("특정 주차장 운영시간", [])])


def gen_abstain(body):
    return generate_json("확인 불가", [])


NO_ISSUES = review_json()
OK = {"lawqa_plan": plan([PARK_LAW]), "lawqa_select": good_select, "lawqa_generate": good_generate,
      "lawqa_review": NO_ISSUES}


def H(**over):
    h = dict(OK)
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def run(handlers, **cfg_kw):
    fake = FakeSolar(handlers)
    cfg = make_cfg(tmpdir(), **cfg_kw)
    pipe = Pipeline(cfg, transport=fake)
    status, sub, tr = pipe.answer(Q)
    return status, sub, tr, fake, pipe


class PipelinePaths(unittest.TestCase):
    def test_happy_path(self):
        status, sub, tr, fake, _ = run(H())
        self.assertEqual(status, "ANSWER")
        self.assertEqual(set(sub), {"answer", "citation", "source_url", "effective_date"})
        self.assertEqual(sub["citation"], [f"{PARK_LAW} 제6조 제1항", f"{PARK_LAW} 별표 1"])
        self.assertEqual(sub["effective_date"], "2026-10-01")
        self.assertEqual(fake.names(), ["lawqa_plan", "lawqa_select", "lawqa_generate", "lawqa_review"])
        self.assertTrue(tr["route"].startswith("answered:review=ok"))
        gen = json.loads([b for n, b in fake.calls if n == "lawqa_generate"][0]["messages"][1]["content"])
        sel = json.loads([b for n, b in fake.calls if n == "lawqa_select"][0]["messages"][1]["content"])
        self.assertLess(len(gen["evidence"]), len(sel["evidence"]))  # 답변은 선택 근거(+필수근거)만
        check = selfcheck.load_original_check()
        gold = [[f"{PARK_LAW} 제6조 제1항"], [f"{PARK_LAW} 별표 1"]]
        self.assertEqual(check.score_citation(sub["citation"], gold), 1.0)

    def test_generation_context_adds_required_evidence(self):
        def only_article(body):
            return select_json([("요금", [alias_for(body, "제6조 제1항")])])
        status, sub, tr, fake, _ = run(H(lawqa_select=only_article))
        gen = json.loads([b for n, b in fake.calls if n == "lawqa_generate"][0]["messages"][1]["content"])
        self.assertIn("주차장:별표1", [e["id"] for e in gen["evidence"]])  # 필수근거 복원

    def test_unsupported_core_abstains_without_generation(self):
        status, sub, tr, fake, _ = run(H(lawqa_select=unsupported, lawqa_generate=None, lawqa_review=None))
        self.assertEqual(status, "ABSTAIN")
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(fake.names(), ["lawqa_plan", "lawqa_select"])

    def test_partial_support_abstains(self):
        def partial(body):
            return select_json([("요금", [alias_for(body, "별표 1")]), ("운영시간", [])])
        status, sub, tr, fake, _ = run(H(lawqa_select=partial, lawqa_generate=None, lawqa_review=None))
        self.assertEqual(sub, UNKNOWN)

    def test_generator_abstention_and_hedge_are_canonical(self):
        for text in ("확인 불가", "확인 불가. 다만 관련 규정은 제6조입니다."):
            with self.subTest(text=text):
                status, sub, tr, fake, _ = run(H(lawqa_generate=lambda b, t=text: generate_json(t, [alias_for(b, "별표 1")]),
                                                 lawqa_review=None))
                self.assertEqual(sub, UNKNOWN)
                self.assertEqual(tr["route"], "generate_abstained")
                self.assertEqual(tr["recoveries"], [])

    def test_contract_repair_once_then_success(self):
        def bad(body):
            return generate_json("요금표에 따릅니다.", [])
        status, sub, tr, fake, _ = run(H(lawqa_generate=bad, **{"lawqa_generate#repair": good_generate}))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["recoveries"][0]["error"], "answer_without_evidence")
        self.assertTrue(tr["recoveries"][0]["recovered"])
        repair_body = [b for n, b in fake.calls if n == "lawqa_generate#repair"][0]
        self.assertIn("answer_without_evidence", repair_body["messages"][-1]["content"])
        first = [b for n, b in fake.calls if n == "lawqa_generate"][0]
        self.assertEqual(repair_body["messages"][1], first["messages"][1])

    def test_repair_failure_is_operational_error_not_abstention(self):
        status, sub, tr, fake, _ = run(H(lawqa_select="not json", **{"lawqa_select#repair": "still not json"}))
        self.assertEqual(status, "ERROR")
        self.assertIn("contract", sub["error"])
        self.assertNotEqual(sub, UNKNOWN)

    def test_repair_is_shared_once_per_request(self):
        status, sub, tr, fake, _ = run(H(lawqa_select="not json", lawqa_review='{"a_check":"","b_issues":[{"a_kind":"nope"}]}',
                                         **{"lawqa_select#repair": good_select}))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["review"]["status"], "unavailable")
        self.assertNotIn("lawqa_review#repair", fake.names())

    def test_review_issue_triggers_single_correction_without_rereview(self):
        def first(body):
            return generate_json("요금표에 따릅니다.", [alias_for(body, "제6조 제1항")])

        def issue(body):
            return review_json([("citation_missing", [alias_for(body, "별표 1")], "")])
        sel = lambda b: select_json([("요금", [alias_for(b, "제6조 제1항")])])  # noqa: E731
        status, sub, tr, fake, _ = run(H(lawqa_select=sel, lawqa_generate=first, lawqa_review=[issue, NO_ISSUES],
                                         lawqa_correct=good_generate), rereview=True)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(fake.names(), ["lawqa_plan", "lawqa_select", "lawqa_generate", "lawqa_review",
                                        "lawqa_correct", "lawqa_review"])
        self.assertEqual(tr["rereview"]["issues"], [])
        self.assertIn(f"{PARK_LAW} 별표 1", sub["citation"])
        corr = json.loads([b for n, b in fake.calls if n == "lawqa_correct"][0]["messages"][1]["content"])
        self.assertEqual(corr["review_issues"][0]["kind"], "citation_missing")
        self.assertEqual(set(corr["draft"]), {"a_answer", "b_evidence"})

    def test_rereview_substantive_issue_abstains(self):
        bad = review_json([("unanswered_core", [], "")])
        status, sub, tr, fake, _ = run(H(lawqa_review=[bad, bad], lawqa_correct=good_generate), rereview=True)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "rereview_rejected")

    def test_rereview_condition_issue_keeps_corrected_answer(self):
        cond = review_json([("missing_condition", [], "")])
        status, sub, tr, fake, _ = run(H(lawqa_review=[review_json([("unsupported_claim", [], "")]), cond],
                                         lawqa_correct=good_generate), rereview=True)
        self.assertEqual(status, "ANSWER")
        self.assertIn("rereview=ok", tr["route"])

    def test_unanchored_issue_is_dropped(self):
        status, sub, tr, fake, _ = run(H(lawqa_review=review_json([("unsupported_claim", [], "답변에 없는 문장")])))
        self.assertEqual(status, "ANSWER")
        self.assertNotIn("lawqa_correct", fake.names())
        self.assertEqual(len(tr["review"]["dropped"]), 1)

    def test_rereview_off_by_default_keeps_corrected_answer(self):
        bad = review_json([("unanswered_core", [], "")])
        status, sub, tr, fake, _ = run(H(lawqa_review=bad, lawqa_correct=good_generate))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(fake.names().count("lawqa_review"), 1)

    def test_correction_can_abstain_on_core_issue(self):
        status, sub, tr, fake, _ = run(H(
            lawqa_review=review_json([("unanswered_core", [], "")]),
            lawqa_correct=gen_abstain))
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "correction_unsupported_request")

    def test_correction_can_abstain_on_unsupported_claim(self):
        status, sub, tr, fake, _ = run(H(
            lawqa_review=review_json([("unsupported_claim", [], "")]),
            lawqa_correct=gen_abstain))
        self.assertEqual(sub, UNKNOWN)

    def test_correction_cannot_abstain_on_condition_issue(self):
        status, sub, tr, fake, _ = run(H(
            lawqa_review=review_json([("missing_condition", [], "")]),
            lawqa_correct=gen_abstain))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["correction"]["status"], "abstain_rejected_without_core_issue")

    def test_citation_missing_for_already_cited_is_dropped(self):
        def issue(body):
            return review_json([("citation_missing", [alias_for(body, "별표 1")], "")])
        status, sub, tr, fake, _ = run(H(lawqa_review=issue))
        self.assertEqual(status, "ANSWER")
        self.assertNotIn("lawqa_correct", fake.names())
        self.assertEqual(len(tr["review"]["dropped"]), 1)

    def test_correction_failure_keeps_draft_and_records(self):
        status, sub, tr, fake, _ = run(H(
            lawqa_review=review_json([("missing_condition", [], "")]),
            lawqa_correct=TIMEOUT))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["correction"]["status"], "unavailable")

    def test_plan_failure_falls_back_without_using_repair(self):
        status, sub, tr, fake, _ = run(H(lawqa_plan="garbage"))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["plan"]["fallback"], "original_question_only")
        self.assertNotIn("lawqa_plan#repair", fake.names())

    def test_timeout_is_error_and_never_resent(self):
        status, sub, tr, fake, pipe = run(H(lawqa_select=TIMEOUT))
        self.assertEqual(status, "ERROR")
        self.assertEqual(sub["http_status"], 504)
        self.assertEqual(fake.names().count("lawqa_select"), 1)
        self.assertGreater(pipe.client.ledger.snapshot()["uncertain_usd"], 0)

    def test_review_transport_error_keeps_draft(self):
        status, sub, tr, fake, _ = run(H(lawqa_review=TIMEOUT))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["review"]["status"], "unavailable")

    def test_truncated_output_goes_to_repair(self):
        status, sub, tr, fake, _ = run(H(lawqa_select="<<TRUNCATE>>", **{"lawqa_select#repair": good_select}))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["recoveries"][0]["error"], "output_truncated")

    def test_budget_cap_is_error(self):
        status, sub, tr, fake, _ = run(H(), budget_cap_usd=0.0)
        self.assertEqual(status, "ERROR")
        self.assertEqual(sub["error"], "budget_cap_reached")
        self.assertEqual(fake.calls, [])

    def test_offline_without_transport_is_error(self):
        pipe = Pipeline(make_cfg(tmpdir()))
        status, sub, tr = pipe.answer(Q)
        self.assertEqual(status, "ERROR")
        self.assertEqual(sub["error"], "offline_no_transport")

    def test_no_lexical_candidates_abstains_without_model(self):
        fake = FakeSolar({"lawqa_plan": plan(queries=["zzzz"])})
        pipe = Pipeline(make_cfg(tmpdir()), transport=fake)
        status, sub, tr = pipe.answer("zzzz qqqq")
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "no_lexical_candidates")

    def test_review_disabled(self):
        status, sub, tr, fake, _ = run(H(lawqa_review=None), review=False)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(fake.names(), ["lawqa_plan", "lawqa_select", "lawqa_generate"])

    def test_cache_replay_identical_requests(self):
        tmp = tmpdir()
        fake = FakeSolar({k: [v] for k, v in OK.items()})
        Pipeline(make_cfg(tmp), transport=fake).answer(Q)
        status, sub, tr = Pipeline(make_cfg(tmp, mode="replay")).answer(Q)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(all(c["cache"] for c in tr["calls"]))
        self.assertEqual(tr["usd"], 0)

    def test_inference_input_contains_only_question_and_evidence(self):
        status, sub, tr, fake, _ = run(H())
        for name, body in fake.calls:
            user = json.loads(body["messages"][1]["content"])
            self.assertEqual(user["question"], Q)
            self.assertFalse({"id", "gold", "score", "answer_key"} & set(user))


class CitationSupplement(unittest.TestCase):
    def test_answer_mentioning_annex_adds_citation_from_context(self):
        def only_article_gen(body):
            return generate_json("공영주차장 주차요금은 별표 1에 따릅니다.", [alias_for(body, "제6조 제1항")])

        def only_article_sel(body):
            return select_json([("요금", [alias_for(body, "제6조 제1항")])])
        status, sub, tr, fake, _ = run(H(lawqa_select=only_article_sel, lawqa_generate=only_article_gen))
        self.assertEqual(sub["citation"], [f"{PARK_LAW} 제6조 제1항", f"{PARK_LAW} 별표 1"])
        self.assertEqual(tr["citation_supplement"], ["unit:2171833:별표1"])

    def test_external_law_mention_not_supplemented(self):
        def ext(body):
            return generate_json("「주차장법」 제9조와 무관하게 조례 별표에 따릅니다.", [alias_for(body, "제6조 제1항")])
        status, sub, tr, fake, _ = run(H(lawqa_select=lambda b: select_json([("요금", [alias_for(b, "제6조 제1항")])]),
                                         lawqa_generate=ext))
        self.assertNotIn("citation_supplement", tr)


class LedgerAndLive(unittest.TestCase):
    def test_ledger(self):
        led = Ledger(f"{tmpdir()}/l.json", 1.0)
        r = led.reserve(0.6)
        with self.assertRaises(CallError):
            led.reserve(0.5)
        led.settle(r, 0.1)
        r2 = led.reserve(0.5)
        led.mark_uncertain(r2)
        r3 = led.reserve(0.3)
        led.release(r3)
        s = led.snapshot()
        self.assertAlmostEqual(s["settled_usd"], 0.1)
        self.assertAlmostEqual(s["uncertain_usd"], 0.5)
        self.assertEqual(s["reserved"], {})

    def test_live_requires_flag_cap_and_key(self):
        tmp = tmpdir()
        with self.assertRaises(CallError) as cm:
            SolarClient(make_cfg(tmp, mode="live"))
        self.assertIn("allow-live", cm.exception.code)
        with self.assertRaises(CallError) as cm:
            SolarClient(make_cfg(tmp, mode="live", budget_cap_usd=0.0), allow_live=True)
        self.assertEqual(cm.exception.code, "live_requires_positive_budget_cap")
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"UPSTAGE_API_KEY": ""}):
            with self.assertRaises(CallError) as cm:
                SolarClient(make_cfg(tmp, mode="live", api_key_file=f"{tmp}/none"), allow_live=True)
        self.assertEqual(cm.exception.code, "live_requires_api_key_file_or_UPSTAGE_API_KEY")
        with mock.patch.dict(os.environ, {"UPSTAGE_API_KEY": "test-key-not-real"}):  # 키 파일 대신 환경변수
            c = SolarClient(make_cfg(tmp, mode="live", api_key_file=f"{tmp}/none"), allow_live=True)
        self.assertIsNotNone(c.transport)


if __name__ == "__main__":
    unittest.main()


class EvidenceCap(unittest.TestCase):
    def test_too_many_evidence_ids_is_contract_error(self):
        from lawqa import contracts
        aliases = [f"주차장:제{i}조" for i in range(1, 30)]
        with self.assertRaises(contracts.ContractError) as cm:
            contracts.parse_select(select_json([("x", aliases[:13])]), aliases)
        self.assertEqual(cm.exception.code, "too_many_evidence_ids")


if __name__ == "__main__":
    unittest.main()
