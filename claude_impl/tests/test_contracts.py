"""⑥ ID/JSON 계약과 ⑦ 기권 결정 오프라인 검사."""
import json
import unittest

from lawqa import contracts
from lawqa.contracts import ContractError, UNKNOWN

A = ["주차장:제6조제1항", "주차장:별표1", "주차장:제7조"]
E1, E2, E3 = A


def sel(reqs):
    return json.dumps({"a_requests": [{"a_request": r, "b_evidence": ev} for r, ev in reqs]}, ensure_ascii=False)


def gen(answer, ev):
    return json.dumps({"a_answer": answer, "b_evidence": ev}, ensure_ascii=False)


class SelectParse(unittest.TestCase):
    def test_selected(self):
        p = contracts.parse_select(sel([("요금", f"{E2}, {E1}"), ("기준", f"{E3},{E2}")]), A)
        self.assertEqual(p["decision"], "SELECTED")
        self.assertEqual(p["selected"], [E2, E1, E3])

    def test_empty_evidence_is_core_unsupported(self):
        p = contracts.parse_select(sel([("요금", E1), ("운영시간", "")]), A)
        self.assertEqual(p["decision"], "ABSTAIN")

    def test_id_variants_resolved(self):
        p = contracts.parse_select(sel([("x", "주차장: 제6조 제1항, 별표1")]), A)
        self.assertEqual(p["selected"], [E1, E2])  # 공백 무시, 접두어 없는 유일 위치 허용

    def test_unknown_ids_dropped_never_cited(self):
        p = contracts.parse_select(sel([("x", "주차장:제99조, E3")]), A)
        self.assertEqual(p["decision"], "ABSTAIN")
        self.assertTrue(any(n.startswith("unknown_evidence_id_dropped") for n in p["notes"]))

    def test_duplicate_codes_tolerated_and_recorded(self):
        p = contracts.parse_select(sel([("x", f"{E1},{E1},{E2}")]), A)
        self.assertEqual(p["selected"], [E1, E2])
        self.assertIn("duplicate_evidence_codes_removed", p["notes"])

    def test_errors(self):
        cases = {"requests_count": sel([]), "select_fields": '{"requests":[]}',
                 "duplicate_json_key": '{"a_requests":[],"a_requests":[]}', "invalid_json": "x"}
        for code, raw in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(ContractError) as cm:
                    contracts.parse_select(raw, A)
                self.assertEqual(cm.exception.code, code)

    def test_too_many(self):
        many = [f"주차장:제{i}조" for i in range(1, 15)]
        with self.assertRaises(ContractError) as cm:
            contracts.parse_select(sel([("x", ",".join(many))]), many)
        self.assertEqual(cm.exception.code, "too_many_evidence_ids")


class GenerateParse(unittest.TestCase):
    def test_answer(self):
        p = contracts.parse_generate(gen("1시간 200원입니다.", E2), A)
        self.assertEqual((p["decision"], p["evidence_ids"]), ("ANSWER", [E2]))

    def test_abstain_and_hedge(self):
        self.assertEqual(contracts.parse_generate(gen("확인 불가", ""), A)["decision"], "ABSTAIN")
        p = contracts.parse_generate(gen("확인 불가. 다만 관련 규정은 ...", E1), A)
        self.assertEqual(p["decision"], "ABSTAIN")
        self.assertEqual(p["evidence_ids"], [])
        self.assertIn("hedged_abstention_text_discarded", p["notes"])

    def test_answer_without_evidence_is_error(self):
        with self.assertRaises(ContractError) as cm:
            contracts.parse_generate(gen("설명입니다.", ""), A)
        self.assertEqual(cm.exception.code, "answer_without_evidence")

    def test_first_sentence_denial_becomes_abstention(self):
        p = contracts.parse_generate(gen("조례에 명시되어 있지 않아 확인할 수 없습니다. 다만 제6조는 ...", E1), A)
        self.assertEqual(p["decision"], "ABSTAIN")
        self.assertIn("first_sentence_denies_core", p["notes"])

    def test_later_caveat_is_not_denial(self):
        p = contracts.parse_generate(gen("1시간 200원입니다. 실제 운영은 확인할 수 없습니다.", E1), A)
        self.assertEqual(p["decision"], "ANSWER")

    def test_requests_carried_for_citation_union(self):
        reqs = [{"request": "x", "evidence_ids": [E3], "supported": True}]
        p = contracts.parse_generate(gen("답", E1), A, reqs)
        self.assertEqual(contracts.used_aliases(p), [E1, E3])


class ReviewParse(unittest.TestCase):
    def rv(self, issues):
        return json.dumps({"a_check": "c", "b_issues": issues}, ensure_ascii=False)

    def test_empty(self):
        self.assertEqual(contracts.parse_review(self.rv([]), A, "답"), [])

    def test_issue_with_anchor(self):
        out = contracts.parse_review(self.rv([{"a_kind": "citation_missing", "b_answer_quote": "200원",
                                                "c_evidence": E3, "d_reason": "별표 누락"}]), A, "1시간 200원입니다.")
        self.assertEqual((out[0]["kind"], out[0]["evidence_ids"], out[0]["anchored"]), ("citation_missing", [E3], True))

    def test_unanchored_quote_flagged(self):
        out = contracts.parse_review(self.rv([{"a_kind": "unsupported_claim", "b_answer_quote": "없는 문장",
                                                "c_evidence": "", "d_reason": "x"}]), A, "1시간 200원입니다.")
        self.assertFalse(out[0]["anchored"])

    def test_bad(self):
        for raw in (self.rv([{"a_kind": "other", "b_answer_quote": "", "c_evidence": "", "d_reason": ""}]),
                    '{"issues":[]}', self.rv([{"a_kind": "contradiction"}])):
            with self.assertRaises(ContractError):
                contracts.parse_review(raw, A, "답")


class PlanParse(unittest.TestCase):
    LAWS = ["서울특별시 도시공원 조례", "서울특별시 청년 기본 조례"]

    def test_ok(self):
        p = contracts.parse_plan('{"laws":["서울특별시 도시공원 조례"],"queries":["공원 이용료","공원 이용료"]}', self.LAWS)
        self.assertEqual(p["queries"], ["공원 이용료"])

    def test_unknown_law(self):
        with self.assertRaises(ContractError):
            contracts.parse_plan('{"laws":["마포구 조례"],"queries":["x"]}', self.LAWS)


class RequestBodies(unittest.TestCase):
    EV = [{"id": E1, "text": "T"}]

    def test_select_schema_orders_judgment_fields(self):
        body = contracts.build_select("질문", self.EV, "solar-pro4", 600)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["temperature"], 0)
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(user), {"question", "evidence"})  # ID·정답·점수 없음
        items = body["response_format"]["json_schema"]["schema"]["properties"]["a_requests"]["items"]
        self.assertEqual(list(items["properties"]), sorted(items["properties"]))

    def test_generate_and_correction_bodies(self):
        body = contracts.build_generate("질문", self.EV, "solar-pro4", 1000)
        self.assertEqual(list(body["response_format"]["json_schema"]["schema"]["properties"]), ["a_answer", "b_evidence"])
        draft = {"answer": "답", "evidence_ids": [E1], "decision": "ANSWER", "requests": []}
        body = contracts.build_generate("질문", self.EV, "solar-pro4", 1000, draft=draft, issues=[{"kind": "x"}])
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(user), {"question", "evidence", "draft", "review_issues"})
        self.assertEqual(user["draft"], {"a_answer": "답", "b_evidence": E1})


class Assemble(unittest.TestCase):
    def test_abstain_object_exact(self):
        out = contracts.assemble({"decision": "ABSTAIN"}, None)
        self.assertEqual(out, {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""})
        self.assertEqual(out, UNKNOWN)
        self.assertIsNot(out["citation"], UNKNOWN["citation"])


if __name__ == "__main__":
    unittest.main()
