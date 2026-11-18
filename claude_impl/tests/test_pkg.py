"""PKG-v2 오프라인 검사: 그래프 구간·속성·닫힘·카드, 계약 파서, 실행 흐름(가짜 Solar)."""
import json
import re
import unittest

from lawqa.contracts import ContractError, UNKNOWN
from lawqa.corpus import Corpus
from lawqa import pkg_graph as P
from lawqa.pkg_runtime import PropIndex
from lawqa.pkg_pipeline import PkgPipeline, parse_ids
from lawqa.pkg_verify import AnswerVerifier

from helpers import FakeSolar, TIMEOUT, make_cfg, tmpdir

C = Corpus()
G = P.load(C and __import__("pathlib").Path(__file__).resolve().parent.parent / "data/pkg/graph.json")
X = PropIndex(C, __import__("pathlib").Path(__file__).resolve().parent.parent / "data", use_dense=False)
Q = "공영주차장에서 다둥이 행복카드로 주차요금 할인을 받을 수 있나요?"


class Graph(unittest.TestCase):
    def test_every_unit_covered_and_spans_exact(self):
        self.assertEqual({p["unit"] for p in G["propositions"]}, {u.id for u in C.units})
        for p in G["propositions"]:
            if p["span_basis"] == "raw":
                s, e = p["span"]
                self.assertEqual(C.by_id[p["unit"]].raw_text[s:e], p["text"])

    def test_rebuild_is_deterministic(self):
        self.assertEqual(P.build(C)["propositions"], G["propositions"])

    def test_items_and_attributes(self):
        by = {p["id"]: p for p in G["propositions"]}
        self.assertIn("주차장:제7조제1항제7호의3", by)  # 가지 번호 호
        item = by["주차장:제7조제1항제7호"]
        self.assertEqual(item["kind"], "ITEM")
        self.assertIn("100분의 50", item["attrs"]["values"])
        self.assertEqual(by["주차장:제27조제2항"]["attrs"]["delegation"]["to"], "MAYOR")
        self.assertEqual(by["주차장:제27조제2항"]["attrs"]["value_status"], "DELEGATED")
        self.assertEqual(by["도시공원:제22조제1항"]["attrs"]["external"]["what"], "LIST")
        self.assertIsNone(by["주차장:제9조"]["attrs"]["external"])  # '다음 각 호'는 내부 목록
        self.assertEqual(by["주차장:제19조"]["attrs"]["delegation"]["to"], "NONE")  # 내부 조문 준용
        self.assertTrue(by["금연:제5조제4항"]["attrs"]["designation_required"])
        self.assertFalse("금연:제5조제1항제4호" in by or "금연:제5조제1항제8호" in by)  # '삭제' 호는 명제가 아님

    def test_table_rows_keep_header(self):
        rows = [p for p in G["propositions"] if p["id"].startswith("주차장:별표1#행")]
        self.assertEqual(len(rows), 3)
        self.assertIn("노 상 주 차 장", rows[0]["header"])
        self.assertIn("500", rows[0]["attrs"]["values"])

    def test_facility_rows_are_separate_and_notes_kept(self):
        """F02·F03: 시설별 행이 머리글로 빨려 들어가지 않고, 표 뒤 비고도 항목별 명제가 된다."""
        by = {p["id"]: p for p in G["propositions"]}
        for annex, n_rows, n_notes in (("주차장:별표2", 11, 11), ("주차장:별표3", 8, 13)):
            rows = [p for p in G["propositions"] if p["id"].startswith(annex + "#행")]
            notes = [p for p in G["propositions"] if p["id"].startswith(annex + "#비고")]
            self.assertEqual((len(rows), len(notes)), (n_rows, n_notes))
            for r in rows:
                self.assertLess(len(r["header"]), 100)
        self.assertIn("단독주택", by["주차장:별표2#행5"]["text"])
        self.assertTrue(by["주차장:별표3#비고7"]["text"].lstrip().startswith("7."))
        self.assertIn("2. 휴양시설", by["도시공원:별표2#행17"]["header"])  # 표 사이 소제목
        sub = [p for p in G["propositions"] if p["id"].startswith("도시공원:별표6#행")][1]
        self.assertTrue(sub["header"].startswith("1. 도시계획시설"))  # 목 행은 앞 번호 행 문맥 유지

    def test_history_and_alias_do_not_make_internal_refs(self):
        """F14: 이동·종전 이력의 조문 번호, F13: 약칭 선언 뒤의 외부 법령 조문은 내부 참조가 아니다."""
        out = {}
        for e in G["edges"]:
            out.setdefault(e["from"], []).append(e)
        to = lambda pid: {e["to"] for e in out.get(pid, []) if e["role"] == "REFERENCE"}  # noqa: E731
        self.assertFalse(any(t.endswith(("제26조", "제30조제1항")) for t in to("동물보호:제28조제8호")))
        self.assertFalse(any(t.endswith("제22조") for t in to("청년:제23조제2항")))
        self.assertFalse(any(t.endswith(("제3조제1항", "제3조제2항")) for t in to("동물보호:제2조제6호")))
        self.assertFalse(any(":제13조제" in t for t in to("도시공원:제4조")))
        by = {p["id"]: p for p in G["propositions"]}
        self.assertEqual(by["동물보호:제2조제6호"]["external_refs"][0]["law"], "동물보호법 시행규칙")
        self.assertIn("[제26조에서 이동", by["동물보호:제28조제8호"]["text"])  # 이력 원문은 보존
        self.assertEqual(P.ref_text('「동물보호법」(이하 "법"이라 한다) 제3조 [제2조에서 이동]').strip(), "「동물보호법」 제3조")

    def test_range_exception_calculation_and_definition_restored(self):
        """F16 중간 항, F17 다른 항의 예외·계산 규칙, F18 '라 함은' 정의가 전달 닫힘에 들어온다(인용 닫힘엔 안 들어옴)."""
        units = lambda pid: [u.split(":")[-1] for u in X.deliver([pid]).units]  # noqa: E731
        self.assertIn("제13조제2항", units("청년:제13조제4항"))
        self.assertIn("제16조제2항", units("주차장:제16조제1항"))
        self.assertIn("제22조제2항", units("도시공원:제22조제1항"))
        self.assertIn("제6조제4항", units("도시공원:제6조제2항"))
        self.assertIn("제7조제2항", units("주차장:제7조제1항제9호"))
        self.assertIn("제2조", units("자전거:제11조제1항"))
        self.assertNotIn("unit:2171833:제16조제2항", X.citation_closure(["주차장:제16조제1항"]))
        by = {p["id"]: p for p in G["propositions"]}
        self.assertEqual(by["자전거:제2조제2호"]["attrs"]["modality"], "DEFINITION")
        self.assertEqual(P.definition_terms('2. "자전거보관소"라 함은 … 시설을 말한다.'), ["자전거보관소"])

    def test_adopted_standard_cells_are_not_direct_values(self):
        """F19: 외부 기준을 준용하는 표 셀은 직접 값이 아니라 위임 값이다."""
        by = {p["id"]: p for p in G["propositions"]}
        for pid, to in (("도시공원:별표3#행6", "EXTERNAL_LAW"), ("도시공원:별표3#행7", "OTHER_ORDINANCE")):
            a = by[pid]["attrs"]
            self.assertEqual((a["value_status"], a["values"], a["delegation"]["to"]), ("DELEGATED", [], to))
        self.assertEqual(by["도시공원:별표3#행1"]["attrs"]["value_status"], "DIRECT")
        self.assertIn("값:위임", X.card("도시공원:별표3#행7")["attrs"])

    def test_definition_terms_nested_quotes(self):
        """F04: 중첩 따옴표 약칭에서 '이라 한다)'를 용어로 뽑지 않는다."""
        t = '3. "유실ㆍ유기동물(이하 "유기동물"이라 한다)"이란 제1호에 따른 동물 중 ...'
        self.assertEqual(P.definition_terms(t), ["유실ㆍ유기동물", "유기동물"])
        defs = [e for e in G["edges"] if e["role"] == "DEFINITION"]
        self.assertFalse(any("한다)" in e["basis"] for e in defs))
        self.assertTrue(defs and all(e["required"] is False for e in defs))  # 정규식 연결은 읽기 문맥

    def test_edges_exist_and_no_external_internalized(self):
        ids = {p["id"] for p in G["propositions"]}
        for e in G["edges"]:
            self.assertIn(e["from"], ids)
            self.assertIn(e["to"], ids if e["to_kind"] == "prop" else C.by_id)
        roles = {(e["from"], e["role"]) for e in G["edges"]}
        self.assertIn(("주차장:제7조제1항제7호", "COMMON"), roles)


class Runtime(unittest.TestCase):
    def test_delivery_closure_transitive_and_ids_exposed(self):
        d = X.deliver(["주차장:제6조제1항"])
        self.assertIn("unit:2171833:별표1", d.units)  # 별표 1과 같다
        for u in d.units:
            for p in X.by_unit[u]:
                self.assertIn(p["id"], d.prop_ids)
        rendered = X.render(d)
        shown = {pt["id"] for s in rendered for pt in s["parts"] if pt["id"]}
        self.assertEqual(shown, set(d.prop_ids))

    def test_cards_include_lead_once(self):
        cards = X.candidate_cards([("주차장:제7조제1항제7호", 1), ("주차장:제7조제1항제8호", 0.9)])
        ids = [c["id"] for c in cards]
        self.assertEqual(ids[0], "주차장:제7조제1항")
        self.assertEqual(ids.count("주차장:제7조제1항"), 1)
        self.assertIn("값(DIRECT):100분의 50", cards[1]["attrs"])

    def test_rank_finds_member_item(self):
        ranked = [p for p, _ in X.rank("다둥이 행복카드 주차요금 할인", depth=10)]
        self.assertIn("주차장:제7조제1항제7호", ranked)

    def test_main_card_shows_proviso_and_cut_is_marked(self):
        """F09: 본문 카드에 분리된 단서가 보이고, F03: 카드를 자르면 표시가 남는다."""
        c = X.card("주차장:제5조제2항")
        self.assertEqual(c["proviso"]["id"], "주차장:제5조제2항단서")
        self.assertIn("전통시장", c["proviso"]["text"])
        from lawqa.pkg_runtime import CUT_MARK, _cut
        self.assertTrue(_cut("가" * 1000, 900).endswith(CUT_MARK))
        self.assertEqual(_cut("가나", 900), "가나")

    def test_vectors_checked_by_input_hash(self):
        """F08: 같은 id라도 임베딩 입력이 바뀌면 오래된 벡터로 판정한다."""
        import pathlib
        import shutil
        import tempfile
        src = pathlib.Path(__file__).resolve().parent.parent / "data"
        d = pathlib.Path(tempfile.mkdtemp())
        shutil.copytree(src, d / "data", ignore=shutil.ignore_patterns("archive"))
        v = json.loads((d / "data/pkg/vectors.json").read_text())
        PropIndex(Corpus(d / "data"), d / "data", use_dense=True)  # 현재 자산은 통과
        v["input_sha256"][0] = "0" * 64
        (d / "data/pkg/vectors.json").write_text(json.dumps(v))
        with self.assertRaises(ValueError):
            PropIndex(Corpus(d / "data"), d / "data", use_dense=True)

    def test_citation_closure_adds_referenced_table(self):
        units = X.citation_closure(["주차장:제6조제1항"])
        self.assertIn("unit:2171833:별표1", units)


def GW(*items):
    """생성 출력 wire: items = (요청 id, 주장 [(문장, ids)], 상태[, 원문 지위])."""
    out = []
    for it in items:
        rid, claims, status = it[:3]
        basis = it[3] if len(it) > 3 else ("DIRECT" if status == "ANSWERED" else "SILENT")
        out.append({"a_request": rid, "b_asked": "묻는 값", "c_basis": basis,
                    "d_claims": [{"a_text": t, "b_ids": i} for t, i in claims], "e_status": status})
    return json.dumps({"a_requests": out}, ensure_ascii=False)


class Contracts(unittest.TestCase):
    def test_parse_ids(self):
        allowed = {"주차장:제7조제1항", "주차장:제7조제1항제7호"}
        self.assertEqual(parse_ids("주차장: 제7조제1항제7호, 주차장:제7조제1항", allowed, "x"),
                         ["주차장:제7조제1항제7호", "주차장:제7조제1항"])
        with self.assertRaises(ContractError):
            parse_ids("주차장:제99조", allowed, "x")  # 조용히 지우지 않음

    def test_parse_generate(self):
        allowed = {"a:1", "a:2"}
        ok = GW(("r", [("가능합니다.", "a:1")], "ANSWERED"))
        p = PkgPipeline.parse_generate(ok, allowed)
        self.assertEqual((p["decision"], p["used"], p["answer"]), ("ANSWER", ["a:1"], "가능합니다."))
        self.assertEqual(PkgPipeline.parse_generate(GW(("r", [], "CORE_MISSING")), allowed)["decision"], "ABSTAIN")
        miss2 = GW(("r", [("원문에 규정이 없다.", "")], "CORE_MISSING"))
        self.assertEqual(PkgPipeline.parse_generate(miss2, allowed)["decision"], "ABSTAIN")  # 설명은 무시
        # F05: 고정 요청과 수·id가 다르면 계약 오류(복구 대상)
        with self.assertRaises(ContractError):
            PkgPipeline.parse_generate(ok, allowed, ["요청1", "요청2"])
        with self.assertRaises(ContractError):
            PkgPipeline.parse_generate(GW(("R2", [("x", "a:1")], "ANSWERED")), allowed, ["요청1"])
        good = GW(("R1", [("x", "a:1")], "ANSWERED"))
        self.assertEqual(PkgPipeline.parse_generate(good, allowed, ["요청1"])["requests"][0]["request"], "요청1")
        for bad in (GW(("r", [], "ANSWERED")), GW(("r", [("x", "")], "ANSWERED")), GW(("r", [("확인 불가", "a:1")], "ANSWERED"))):
            with self.assertRaises(ContractError):
                PkgPipeline.parse_generate(bad, allowed)

    def test_answered_with_missing_basis_is_core_missing(self):
        """F15: 원문이 묻는 값을 주지 않는다고 스스로 판정(SILENT·DELEGATED·EXTERNAL)했는데 ANSWERED로 낸 요청은 기권."""
        allowed = {"a:1"}
        for basis in ("SILENT", "DELEGATED", "EXTERNAL"):
            p = PkgPipeline.parse_generate(GW(("R1", [("금액은 조례에 정해져 있지 않다.", "a:1")], "ANSWERED", basis)),
                                           allowed, ["요청1"])
            self.assertEqual(p["decision"], "ABSTAIN")
            self.assertTrue(p["requests"][0]["status_from_basis"])
        p = PkgPipeline.parse_generate(GW(("R1", [("가능합니다.", "a:1")], "ANSWERED", "APPLIED")), allowed, ["요청1"])
        self.assertEqual(p["decision"], "ANSWER")

    def test_correction_abstain_rule(self):
        """F01: 핵심 미응답 지적 + 교정자가 원문에 없다고 판정한 요청이 있을 때만 교정 기권을 받는다."""
        corrected = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "DELEGATED")), {"a:1"}, ["요청1"])
        self.assertTrue(PkgPipeline.correction_abstain_ok([{"kind": "unanswered_core", "claim": "R1C1"}], corrected))
        self.assertTrue(PkgPipeline.correction_abstain_ok([{"kind": "unanswered_core", "claim": "R1"}], corrected))
        self.assertFalse(PkgPipeline.correction_abstain_ok([{"kind": "missing_condition", "claim": "R1C1"}], corrected))
        applied = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "APPLIED")), {"a:1"}, ["요청1"])
        self.assertFalse(PkgPipeline.correction_abstain_ok([{"kind": "unanswered_core", "claim": "R1"}], applied))


def frame(body):
    return json.dumps({"a_requests": [{"a_quote": "할인을 받을 수 있나요", "b_request": "다둥이 카드 할인 가능 여부"}],
                       "b_queries": ["다둥이 행복카드 주차요금 감면"]}, ensure_ascii=False)


def select(ids):
    return lambda body: json.dumps({"a_requests": [{"a_request": "R1", "b_ids": ids}]}, ensure_ascii=False)


def gen(ids, status="ANSWERED", text="네, 시장은 다둥이 행복카드 소지자의 주차요금을 100분의 50 감면할 수 있습니다.",
        basis=None):
    claims = [(text, ids)] if status == "ANSWERED" else []
    return lambda body: GW(("R1", claims, status) + ((basis,) if basis else ()))


def review(issues=(), verdict="ANSWERED"):
    return json.dumps({"a_requests": [{"a_request": "R1", "b_check": "c", "c_verdict": verdict}], "b_issues": [
        {"a_kind": k, "b_claim": c, "c_ids": ids, "d_reason": "r"} for k, c, ids in issues]}, ensure_ascii=False)


LEAD, ITEM = "주차장:제7조제1항", "주차장:제7조제1항제7호"


def run(handlers, **kw):
    fake = FakeSolar(handlers)
    pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", **kw), transport=fake)
    status, sub, tr = pipe.answer(Q)
    return status, sub, tr, fake


class Flow(unittest.TestCase):
    H = {"pkg_frame": frame, "pkg_select": select(f"{LEAD}, {ITEM}"), "pkg_generate": gen(f"{ITEM}, {LEAD}"),
         "pkg_review": review()}

    def test_happy_path_claim_based_answer_and_citation(self):
        status, sub, tr, fake = run(dict(self.H))
        self.assertEqual(status, "ANSWER")
        self.assertEqual(sub["citation"][0], "서울특별시 주차장 설치 및 관리 조례 제7조 제1항")
        self.assertTrue(sub["answer"].startswith("네, 시장은"))
        self.assertEqual([n for n in fake.names() if n != "embed"], ["pkg_frame", "pkg_select", "pkg_generate", "pkg_review"])
        gen_in = json.loads([b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][1]["content"])
        shown = {pt["id"] for s in gen_in["sources"] for pt in s["parts"] if pt["id"]}
        self.assertIn(ITEM, shown)
        sel_in = json.loads([b for n, b in fake.calls if n == "pkg_select"][0]["messages"][1]["content"])
        self.assertEqual(set(sel_in), {"question", "requests", "cards"})  # 정답·ID 없음

    def test_select_empty_is_abstention(self):
        status, sub, tr, fake = run(dict(self.H, pkg_select=select(""), pkg_generate=None, pkg_review=None))
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["route"], "select_core_missing")

    def test_generate_core_missing_is_abstention(self):
        status, sub, tr, fake = run(dict(self.H, pkg_generate=gen("", "CORE_MISSING"), pkg_review=None))
        self.assertEqual(sub, UNKNOWN)

    def test_unknown_id_goes_to_repair_not_silent_drop(self):
        h = dict(self.H, pkg_select=select("주차장:제99조"))
        h["pkg_select#repair"] = select(ITEM)
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(tr["recoveries"][0]["error"].startswith("select_unknown_id"))

    def test_review_issue_then_correction_keeps_or_rejects(self):
        # 조건 누락 지적에서 나온 교정 기권은 버린다.
        h = dict(self.H, pkg_review=review([("missing_condition", "R1C1", "")]),
                 pkg_correct=gen("", "CORE_MISSING", basis="DELEGATED"))
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["correction"]["status"], "abstain_rejected")
        # 기본값: 핵심 미응답 지적에서 나온 교정 기권도 버린다(PKG5 실측 순손실).
        h = dict(self.H, pkg_review=review([("unanswered_core", "R1C1", "")]),
                 pkg_correct=gen("", "CORE_MISSING", basis="DELEGATED"))
        status, sub, tr, fake = run(h)
        self.assertEqual(tr["correction"]["status"], "abstain_rejected")
        # 설정을 켜면 F01 규칙(핵심 미응답 지적 + 원문 부재 판정)으로 기권을 받는다.
        status, sub, tr, fake = run(h, pkg_accept_correction_abstain=True)
        self.assertEqual(sub, UNKNOWN)
        self.assertEqual(tr["correction"]["status"], "abstain_accepted")

    def test_review_verdict_becomes_structured_issue(self):
        """F15: 검토 판정이 핵심 미응답인데 조치 목록이 비어 있으면 그 판정을 지적으로 옮겨 교정한다."""
        h = dict(self.H, pkg_review=review([], verdict="UNANSWERED_CORE"), pkg_correct=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(h)
        self.assertIn("pkg_correct", fake.names())
        self.assertEqual(tr["review"]["issues"][0]["claim"], "R1")
        self.assertTrue(tr["review"]["issues"][0]["from_verdict"])

    def test_proviso_shown_on_card_is_selectable(self):
        """F09 부작용 방지: 본문 카드에 딸려 보인 단서 id를 골라도 없는 id가 아니다."""
        import lawqa.pkg_pipeline as PPL
        orig = X.rank
        main, prov = "주차장:제5조제2항", "주차장:제5조제2항단서"
        try:
            PPL.PropIndex.rank = lambda self, *a, **k: [(main, 1.0)]
            h = dict(self.H, pkg_select=select(f"{main}, {prov}"), pkg_generate=gen(main))
            status, sub, tr, fake = run(h)
        finally:
            PPL.PropIndex.rank = orig.__func__ if hasattr(orig, "__func__") else orig
        self.assertEqual(tr["selection"][0]["ids"], [main, prov])
        self.assertFalse(tr["recoveries"])

    def test_review_can_flag_unanswered_request_by_id(self):
        """F05: 검토자는 요청 id로 핵심 미응답을 지적할 수 있다(주장이 없는 요청도)."""
        h = dict(self.H, pkg_review=review([("unanswered_core", "R1", "")]), pkg_correct=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(h)
        self.assertEqual(tr["review"]["issues"][0]["claim"], "R1")
        self.assertIn("pkg_correct", fake.names())
        rv_in = json.loads([b for n, b in fake.calls if n == "pkg_review"][0]["messages"][1]["content"])
        self.assertEqual(rv_in["requests"][0]["id"], "R1")
        self.assertEqual(rv_in["requests"][0]["status"], "ANSWERED")
        self.assertIn("basis", rv_in["requests"][0])

    def test_review_issue_on_unknown_claim_dropped(self):
        status, sub, tr, fake = run(dict(self.H, pkg_review=review([("unsupported_claim", "R9C9", "")])))
        self.assertNotIn("pkg_correct", fake.names())
        self.assertEqual(len(tr["review"]["dropped"]), 1)

    def test_transport_error_is_error(self):
        status, sub, tr, fake = run(dict(self.H, pkg_select=TIMEOUT))
        self.assertEqual(status, "ERROR")
        self.assertNotEqual(sub, UNKNOWN)

    def test_frame_failure_falls_back(self):
        status, sub, tr, fake = run(dict(self.H, pkg_frame="garbage"))
        self.assertEqual(status, "ANSWER")
        self.assertIn("frame_fallback", tr)


if __name__ == "__main__":
    unittest.main()


class SideMissing(unittest.TestCase):
    def test_side_missing_answers_core_and_all_side_abstains(self):
        allowed = {"a:1"}
        p = PkgPipeline.parse_generate(GW(("핵심", [("가능합니다.", "a:1")], "ANSWERED"), ("서류", [], "SIDE_MISSING")), allowed)
        self.assertEqual((p["decision"], p["answer"]), ("ANSWER", "가능합니다."))
        self.assertEqual(PkgPipeline.parse_generate(GW(("서류", [], "SIDE_MISSING")), allowed)["decision"], "ABSTAIN")
        core = GW(("핵심", [], "CORE_MISSING"), ("부수", [("x", "a:1")], "ANSWERED"))
        self.assertEqual(PkgPipeline.parse_generate(core, allowed)["decision"], "ABSTAIN")


class SameRequest(unittest.TestCase):
    """F07: 같은 유료 요청이 동시에 들어오면 한 번만 보내고, 앞선 요청이 처리 불명이면 기다린 쪽이 재전송하지 않는다."""

    def _client(self, transport):
        from lawqa.llm import SolarClient
        return SolarClient(make_cfg(tmpdir(), mode="live", api_key_file="/dev/null"), transport=transport,
                           allow_live=True)

    def _body(self):
        return {"model": "solar-pro4", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                "max_tokens": 10, "response_format": {"type": "json_schema", "json_schema": {"name": "n", "schema": {}}}}

    def test_concurrent_identical_requests_call_once(self):
        import threading
        import time as _t
        calls = []

        def slow(url, body, timeout):
            calls.append(1)
            _t.sleep(0.3)
            return {"model": "solar-pro4-260806", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        c = self._client(slow)
        out = []
        ts = [threading.Thread(target=lambda: out.append(c.call("chat", self._body(), _t.monotonic() + 30, 10, [])))
              for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(calls), len(out)), (1, 3))
        self.assertFalse(list(c.cache_dir.glob("*.tmp")))

    def test_waiter_does_not_resend_after_uncertain(self):
        import threading
        import time as _t
        from lawqa.llm import CallError
        calls = []

        def timeout_once(url, body, timeout):
            calls.append(1)
            _t.sleep(0.3)
            raise CallError("provider_timeout", uncertain=True)
        c = self._client(timeout_once)
        errs = []

        def go():
            try:
                c.call("chat", self._body(), _t.monotonic() + 30, 10, [])
            except CallError as e:
                errs.append(e.code)
        ts = [threading.Thread(target=go) for _ in range(2)]
        ts[0].start()
        _t.sleep(0.05)
        ts[1].start()
        [t.join() for t in ts]
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(errs), ["provider_timeout", "same_request_uncertain"])



class PKG7Graph(unittest.TestCase):
    """PKG7: Claude 점검(G01~G20)과 GPT 검토(F20~F29)에서 확정한 그래프 문제의 재현 조건."""
    by = {p["id"]: p for p in G["propositions"]}

    def edges_from(self, pid, role=None):
        return [e for e in G["edges"] if e["from"] == pid and (role is None or e["role"] == role)]

    def units(self, pid):
        return [u.split(":")[-1] for u in X.deliver([pid]).units]

    def test_values_and_conditions(self):  # G01·F23·F21
        v = lambda pid: self.by[pid]["attrs"]["values"]  # noqa: E731
        self.assertIn("10만원", v("금연:제10조제1항"))
        self.assertIn("1천만원", v("주차장:제30조제1항"))
        self.assertIn("3년", v("주차장:제3조의2제3호"))
        self.assertIn("70％", v("주차장:제4조제1항"))
        self.assertTrue({"1대", "0.5대", "0.8대"} <= set(v("주차장:별표2#행6")))
        self.assertTrue(any("0.5 미만" in c for c in self.by["주차장:별표2#비고6"]["attrs"]["conditions"]))

    def test_deleted_paragraphs_not_searchable(self):  # G03
        dels = [p["id"] for p in G["propositions"] if p["kind"] == "DELETED"]
        self.assertEqual(len(dels), 4)
        ranked = [pid for pid, _ in X.rank("주차장 삭제 제25조 제4항", depth=200)]
        self.assertFalse(set(dels) & set(ranked))

    def test_long_note_split_and_annex_once(self):  # G04·G02
        notes = [p for p in G["propositions"] if p["id"].startswith("주차장:별표1#비고")]
        self.assertGreaterEqual(len(notes), 10)
        self.assertTrue(all(len(p["text"]) <= 900 for p in notes))
        self.assertTrue(any("지불수단" in p["text"] for p in notes))
        r = X.render(X.deliver(["주차장:별표1#행1"]))
        annex = next(s for s in r if s["unit"].endswith("별표 1"))
        self.assertFalse(any(pt["id"] is None for pt in annex["parts"]))  # 전체 원문 중복 없음
        self.assertIn("단위", annex["parts"][1]["text"] + annex["parts"][0]["text"])

    def test_inner_proviso_split(self):  # G05
        self.assertIn("주차장:제7조제1항제2호단서", self.by)
        self.assertTrue(self.edges_from("주차장:제7조제1항제2호", "EXCEPTION"))
        self.assertIn("proviso", X.card("주차장:제7조제1항제2호"))

    def test_subjects_and_designation(self):  # G09·G10
        self.assertEqual(self.by["주차장:제12조제1항"]["attrs"]["subject"], "관리수탁자")
        self.assertEqual(self.by["자전거:제3조제5항"]["attrs"]["subject"], "자치구청장")
        for pid in ("동물보호:제7조제2항", "자전거:제14조제2항", "주차장:제4조제2항", "금연:제7조제1항"):
            self.assertTrue(self.by[pid]["attrs"]["designation_required"], pid)

    def test_definitions_aliases_and_scope(self):  # G11·G13·F22
        to = lambda pid: {e["to"] for e in self.edges_from(pid, "DEFINITION")}  # noqa: E731
        self.assertIn("주차장:제10조제1항", to("주차장:제12조제1항"))  # 관리수탁자 약칭
        self.assertEqual(self.by["금연:제2조제3호"]["attrs"]["modality"], "DEFINITION")
        self.assertIn("제2조", self.units("금연:제9조제5항"))  # 간접흡연 정의
        self.assertIn("제3조", self.units("청년:제24조"))  # 청년 19~39세

    def test_supplementary_provisions_and_expiry(self):  # F20·G19
        self.assertFalse([e for e in G["edges"] if e["from"] == "주차장:제7조제3항" and e["to"].endswith(":제2조")])
        cites = X.citation_closure(["주차장:제7조제3항"])
        self.assertFalse([u for u in cites if u.endswith(":제2조")])
        self.assertTrue(self.by["도시공원:제20조제4항"]["attrs"].get("expired"))
        self.assertFalse(self.by["주차장:제7조제1항"]["attrs"].get("expired"))  # 삭제된 12호 안내는 도입문에 붙지 않음
        self.assertIn("효력만료", X.card("도시공원:제20조제4항제1호")["attrs"])

    def test_ditto_external_branch_and_notwithstanding(self):  # G20·F24·F26
        self.assertIn("연액", self.by["도시공원:별표3#행3"]["resolved"])
        self.assertEqual(self.by["자전거:제2조제11호"]["external_refs"][0]["article"], "제2조제19호의2")
        self.assertIn("제7조제1항", self.units("주차장:제6조제1항"))
        self.assertIn("제21조의2", self.units("주차장:제21조제2항"))

    def test_missing_unit_caption_supplemented(self):  # F29
        sup = self.by["자전거:별표#보충단위"]
        self.assertEqual((sup["text"], sup["span_basis"]), ("(단위 : 원/구획)", "supplement"))
        self.assertIn("원/구획", self.by["자전거:별표#행1"]["header"])


class PKG7Contract(unittest.TestCase):
    def test_basis_status_conflict_strict_and_lenient(self):  # F27
        allowed = {"a:1"}
        bad = GW(("R1", [("가능합니다.", "a:1")], "SIDE_MISSING", "APPLIED"))
        with self.assertRaises(ContractError):
            PkgPipeline.parse_generate(bad, allowed, ["요청1"], strict=True)
        p = PkgPipeline.parse_generate(bad, allowed, ["요청1"], strict=False)
        self.assertEqual(p["decision"], "ABSTAIN")
        self.assertEqual(p["requests"][0]["basis_status_conflict"], "APPLIED/SIDE_MISSING")

    def test_conflict_goes_to_one_repair(self):  # F27 흐름
        h = dict(Flow.H, pkg_generate=lambda body: GW(("R1", [], "CORE_MISSING", "APPLIED")))
        h["pkg_generate#repair"] = gen(f"{ITEM}, {LEAD}")
        status, sub, tr, fake = run(h)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(tr["recoveries"][0]["error"].startswith("basis_status_conflict"))

    def test_background_request_filter(self):  # F28
        from lawqa.pkg_pipeline import filter_background_requests as F
        q = "사고 나면 어디에 뭘 내야 할지 모르겠어요. 시나 구청에서 이런 절차를 안내해 줘야 하나요?"
        kept, dropped = F(q, [{"a_quote": "어디에 뭘 내야 할지", "b_request": "청구 서류"},
                              {"a_quote": "이런 절차를 안내해 줘야 하나요", "b_request": "안내 의무"}])
        self.assertEqual([k["b_request"] for k in kept], ["안내 의무"])
        q2 = "전자담배도 과태료가 나오나요? 니코틴 없는 것도 똑같은지 궁금해요."
        kept, dropped = F(q2, [{"a_quote": "과태료가 나오나요", "b_request": "a"}, {"a_quote": "똑같은지", "b_request": "b"}])
        self.assertEqual((len(kept), dropped), (2, []))


class PKG8(unittest.TestCase):
    """PKG8: Codex PKG7 검토(F30, F25 잔여, G-09, U030)와 A-07 퇴행 수정."""

    def test_alias_scope_after_declaration_only(self):  # F30
        defs = {(e["from"], e["to"]) for e in G["edges"] if e["role"] == "DEFINITION" and e["basis"] == "법인"}
        self.assertNotIn(("주차장:제10조제1항제1호", "주차장:제12조제2항"), defs)
        self.assertNotIn(("주차장:제10조의2제2항", "주차장:제12조제2항"), defs)
        self.assertIn(("주차장:제12조제3항", "주차장:제12조제2항"), defs)
        self.assertNotIn("제12조제2항", [u.split(":")[-1] for u in X.deliver(["주차장:제10조의2제2항"]).units])
        bad = [e for e in G["edges"] if e["role"] == "DEFINITION" and e["basis"] == "민간단체" and e["from"] == "자전거:제2조제9호"]
        self.assertFalse(bad)  # '자전거이용민간단체' 안의 일부 문자열

    def test_applies_to_note(self):  # F25
        by = {p["id"]: p for p in G["propositions"]}
        self.assertEqual(by["도시공원:제23조제2항"]["attrs"]["applies_to"], ["제1항에 따른 과태료 금액"])
        r = X.render(X.deliver(["도시공원:제23조제2항"]))
        notes = [pt.get("note", "") for s in r for pt in s["parts"] if pt["id"] == "도시공원:제23조제2항"]
        self.assertTrue(notes and "적용 대상" in notes[0])
        self.assertNotIn("applies_to", by["주차장:제7조제1항제1호"]["attrs"])  # 외부 법령의 '제4조제1항에 따른'은 제외

    def test_background_request_is_nonblocking(self):  # A-07 퇴행 수정
        allowed = {"a:1"}
        raw = GW(("R1", [("가능합니다.", "a:1")], "ANSWERED"), ("R2", [], "CORE_MISSING", "SILENT"))
        p = PkgPipeline.parse_generate(raw, allowed, ["물음", "배경"], background=[False, True])
        self.assertEqual(p["decision"], "ANSWER")
        self.assertTrue(p["requests"][1]["background_nonblocking"])
        p = PkgPipeline.parse_generate(raw, allowed, ["물음", "배경"], background=[False, False])
        self.assertEqual(p["decision"], "ABSTAIN")
        from lawqa.pkg_pipeline import background_flags
        q = "어디에 신고하나요? 신고하면 강아지는 어떻게 되는지도 걱정돼요."
        self.assertEqual(background_flags(q, [{"a_quote": "어디에 신고하나요", "b_request": "a"},
                                              {"a_quote": "어떻게 되는지도", "b_request": "b"}]), [False, True])

    def test_select_too_many_ids_goes_to_repair(self):  # U030 (상한을 1로 낮춰 같은 경로 검사)
        h = dict(Flow.H, pkg_select=select(f"{LEAD}, {ITEM}"))
        h["pkg_select#repair"] = select(ITEM)
        status, sub, tr, fake = run(h, pkg_select_max_ids=1)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(tr["recoveries"][0]["error"].startswith("select_too_many_ids"))

    def test_external_correction_abstain(self):  # G-09
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        draft = PkgPipeline.parse_generate(GW(("R1", [("금지된다.", "도시공원:제22조제1항")], "ANSWERED", "DIRECT")),
                                           {"도시공원:제22조제1항"}, ["드론"])
        corr = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "EXTERNAL")), {"a"}, ["드론"])
        issues = [{"kind": "unanswered_core", "claim": "R1"}]
        self.assertTrue(pipe.correction_external_ok(issues, corr, draft))
        corr2 = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "SILENT")), {"a"}, ["드론"])
        self.assertTrue(pipe.correction_external_ok(issues, corr2, draft))  # PKG11.1: Pro4가 주로 적는 SILENT도 수용
        corr3 = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "DELEGATED")), {"a"}, ["드론"])
        self.assertFalse(pipe.correction_external_ok(issues, corr3, draft))  # 값 위임 판정은 계속 거절
        self.assertFalse(pipe.correction_external_ok([{"kind": "missing_condition", "claim": "R1C1"}], corr2, draft))
        draft2 = PkgPipeline.parse_generate(GW(("R1", [("가능.", "주차장:제7조제1항")], "ANSWERED", "DIRECT")),
                                            {"주차장:제7조제1항"}, ["x"])
        self.assertFalse(pipe.correction_external_ok(issues, corr, draft2))  # 그래프상 외부 의존이 없으면 거절
        self.assertFalse(pipe.correction_external_ok(issues, corr2, draft2))
        draft3 = PkgPipeline.parse_generate(GW(("R1", [("따른다.", "청년:제3조제1호단서")], "ANSWERED", "DIRECT")),
                                            {"청년:제3조제1호단서"}, ["x"])
        self.assertTrue(pipe.correction_external_ok(issues, corr2, draft3))  # 외부 법령 위임도 외부 참조
        draft4 = PkgPipeline.parse_generate(GW(("R1", [("규칙으로.", "도시공원:제20조제2항")], "ANSWERED", "DIRECT")),
                                            {"도시공원:제20조제2항"}, ["x"])
        self.assertFalse(pipe.correction_external_ok(issues, corr2, draft4))  # 시행규칙 위임은 외부 참조가 아님


class PKG9(unittest.TestCase):
    """PKG9: PKG8 실측에서 확인한 퇴행 수정과 F25 결정적 검사."""

    def test_scope_violation_detected(self):  # F25
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        ok_ids = {"도시공원:제23조제1항", "도시공원:제23조제2항", "도시공원:제23조제4항"}
        bad = PkgPipeline.parse_generate(GW(("R1", [("음주 과태료 10만원은 2분의 1 범위에서 감경될 수 있다.",
                                                     "도시공원:제23조제4항, 도시공원:제23조제2항")], "ANSWERED")), ok_ids, ["음주"])
        v = pipe.scope_violations(bad)
        self.assertEqual(len(v), 1)
        self.assertIn("제1항에 따른 과태료 금액", v[0]["detail"])
        good = PkgPipeline.parse_generate(GW(("R1", [("제1항 과태료는 2분의 1 범위에서 조정된다.",
                                                      "도시공원:제23조제1항, 도시공원:제23조제2항")], "ANSWERED")), ok_ids, ["조정"])
        self.assertEqual(pipe.scope_violations(good), [])

    def test_background_request_without_evidence_does_not_block_selection(self):  # B-09
        q = "사고 나면 어디에 뭘 내야 할지 모르겠어요. 할인을 받을 수 있나요?"
        frame2 = lambda body: json.dumps({"a_requests": [  # noqa: E731
            {"a_quote": "어디에 뭘 내야 할지", "b_request": "청구 서류"},
            {"a_quote": "할인을 받을 수 있나요", "b_request": "할인 여부"}], "b_queries": ["주차요금 감면"]},
            ensure_ascii=False)
        sel2 = lambda body: json.dumps({"a_requests": [{"a_request": "R1", "b_ids": ""},  # noqa: E731
                                                       {"a_request": "R2", "b_ids": f"{LEAD}, {ITEM}"}]}, ensure_ascii=False)
        gen2 = lambda body: GW(("R1", [], "CORE_MISSING", "SILENT"),  # noqa: E731
                               ("R2", [("네, 할인됩니다.", f"{ITEM}, {LEAD}")], "ANSWERED"))
        rv2 = lambda body: json.dumps({"a_requests": [{"a_request": "R1", "b_check": "c", "c_verdict": "SIDE"},  # noqa: E731
                                                      {"a_request": "R2", "b_check": "c", "c_verdict": "ANSWERED"}],
                                       "b_issues": []}, ensure_ascii=False)
        fake = FakeSolar({"pkg_frame": frame2, "pkg_select": sel2, "pkg_generate": gen2, "pkg_review": rv2})
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=fake)
        status, sub, tr = pipe.answer(q)
        self.assertEqual(status, "ANSWER")
        self.assertTrue(tr["frame_background_nonblocking"])

    def test_select_cap_off_by_default(self):
        from lawqa.config import Config
        self.assertEqual(Config().pkg_select_max_ids, 0)


class PKG10(unittest.TestCase):
    def test_quote_spanning_two_sentences_is_found(self):  # R3-REAL-S-09
        from lawqa.pkg_pipeline import background_flags
        q = "이웃이 금연구역만 아니면 다 흡연구역이라고 하네요. 정말 그런가요? 담배 연기 때문에 힘들어도 그냥 참아야 하는 건지 모르겠어요."
        items = [{"a_quote": "이웃이 금연구역만 아니면 다 흡연구역이라고 하네요. 정말 그런가요?", "b_request": "흡연구역 여부"},
                 {"a_quote": "담배 연기 때문에 힘들어도 그냥 참아야 하는 건지 모르겠어요.", "b_request": "참아야 하나"}]
        self.assertEqual(background_flags(q, items), [False, True])

    def test_basis_rule_for_different_criterion(self):  # PORTAL26-P12·G07
        from lawqa.pkg_prompts import GENERATE_SYSTEM
        self.assertIn('"X가 아니라 Y"가 DIRECT', GENERATE_SYSTEM)
        self.assertIn("질문이 다른 기준을 들어도 SILENT가 아니다", GENERATE_SYSTEM)


class PKG11(unittest.TestCase):
    def test_verdict_survives_issue_with_unknown_claim_number(self):
        """검토 판정이 핵심 미응답이고, 같은 요청의 핵심 미응답 지적이 없는 주장 번호(R1C9)를 달면,
        그 지적은 버리되 요청 단위 판정은 남아 교정에 넘어가야 한다(검토 의견 무조건 수용은 아님)."""
        h = dict(Flow.H, pkg_review=review([("unanswered_core", "R1C9", "")], verdict="UNANSWERED_CORE"),
                 pkg_correct=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(h)
        self.assertIn("pkg_correct", fake.names())
        self.assertEqual([(i["claim"], i.get("from_verdict")) for i in tr["review"]["issues"]], [("R1", True)])
        self.assertEqual([i["claim"] for i in tr["review"]["dropped"]], ["R1C9"])

    def test_existing_claim_issue_still_counts_as_flagged(self):
        """있는 주장 번호(R1C1)의 핵심 미응답 지적이 있으면 판정을 중복해 합성하지 않는다(기존 동작 유지)."""
        h = dict(Flow.H, pkg_review=review([("unanswered_core", "R1C1", "")], verdict="UNANSWERED_CORE"),
                 pkg_correct=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(h)
        self.assertEqual([i["claim"] for i in tr["review"]["issues"]], ["R1C1"])

    def test_image_unreviewed_status_reaches_generate_and_review(self):
        """별도 3~6(그림)의 미검토 상태가 카드뿐 아니라 생성·검토에 주는 원문 조각에도 남는다."""
        from lawqa.pkg_runtime import Delivery
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        idx = pipe.index
        for n in (3, 4, 5, 6):
            pid = f"주차장:별도{n}"
            self.assertEqual(idx.card(pid).get("status"), "IMAGE_UNREVIEWED")
            src = idx.render(idx.deliver([pid]))[0]
            self.assertEqual([p.get("status") for p in src["parts"]], ["IMAGE_UNREVIEWED"] * len(src["parts"]))
        # 상태가 없는 단위에는 표시가 생기지 않는다.
        plain = idx.render(idx.deliver([ITEM]))
        self.assertFalse(any("status" in p for s in plain for p in s["parts"]))


class PKG11A(unittest.TestCase):
    """GPT PKG11 검토 1.1·1.2·1.3 반영과 후보 A(외부 목록 참조 note)."""

    @classmethod
    def setUpClass(cls):
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        cls.idx = pipe.index

    def test_designation_card_quotes_phrase_without_strengthening(self):
        c = self.idx.card("주차장:제7조제1항제4호")["attrs"]  # 지정 지역을 '제외한' 곳의 규정
        self.assertNotIn("지정된 경우에만", c)
        self.assertIn("지정 관련 구절: «시장이 지정한 녹색교통진흥특별대책지역»", c)
        self.assertIn("«지정된 공공기관»", self.idx.card("도시공원:제22조제2항제1호")["attrs"])  # 공공기관 분기에만 걸림

    def test_designation_patterns_positive_and_negative(self):
        a = lambda pid: self.idx.by_id[pid]["attrs"]["designation_required"]  # noqa: E731
        self.assertTrue(a("도시공원:제20조제2항제1호"))   # '시장이 지정하는 공원'
        self.assertTrue(a("금연:제8조제1항"))             # '금연구역으로 지정되는'
        self.assertFalse(a("금연:제5조제2항"))            # 절차 규정 '지정하는 경우' 는 지정 전제가 아님
        self.assertFalse(a("금연:제5조제3항"))

    def test_external_card_quotes_reference_span(self):
        self.assertIn("외부 법령 참조: «법 제49조제1항 및 제2항 각 호의 행위»", self.idx.card("도시공원:제22조제1항")["attrs"])
        self.assertIn("«「도로교통법」 제2조제19호의2»", self.idx.card("자전거:제2조제11호")["attrs"])
        self.assertNotIn("외부법령의존", self.idx.card("도시공원:제2조제1호")["attrs"])

    def test_external_list_note_only_when_enabled_and_only_for_list(self):
        d = self.idx.deliver(["도시공원:제22조제1항", "도시공원:제2조제1호"])
        off = self.idx.render(d)
        self.assertFalse(any("외부 법령 참조" in p.get("note", "") for s in off for p in s["parts"]))
        on = self.idx.render(d, external_list_note=True)
        notes = {p["id"]: p.get("note", "") for s in on for p in s["parts"]}
        self.assertEqual(notes["도시공원:제22조제1항"],
                         "외부 법령 참조: «법 제49조제1항 및 제2항 각 호의 행위» (이 조각은 그 목록을 직접 열거하지 않음)")
        self.assertEqual(notes["도시공원:제2조제1호"], "")  # 외부 정의(DEFINITION)에는 붙이지 않음(첫 비교 범위 밖)
        # 원문 조각 자체는 바뀌지 않는다(메타데이터 note만 추가)
        self.assertEqual([(p["id"], p["text"]) for s in off for p in s["parts"]],
                         [(p["id"], p["text"]) for s in on for p in s["parts"]])


class PKG11Candidates(unittest.TestCase):
    """후보 시험용 플래그(기본 끔): A-지정, A-정의, B, C."""

    @classmethod
    def setUpClass(cls):
        cls.pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        cls.idx = cls.pipe.index

    def test_junyong_phrase_internal_only(self):
        a = lambda pid: self.idx.by_id[pid]["attrs"]["junyong"]  # noqa: E731
        self.assertEqual(a("금연:제6조제2항"), "제5조제2항과 제5조제3항의 규정을 준용한다")
        self.assertEqual(a("주차장:제19조"), "제15조제2항을 준용하되")
        self.assertEqual(a("도시공원:제30조제3호"), "")  # 「공익사업…」 제71조 준용(외부)

    def test_candidate_notes_only_with_flags(self):
        d = self.idx.deliver(["금연:제6조제2항", "금연:제5조제4항", "도시공원:제2조제1호"])
        off = {p["id"]: p.get("note", "") for s in self.idx.render(d) for p in s["parts"]}
        self.assertEqual(off["금연:제6조제2항"] + off["금연:제5조제4항"] + off["도시공원:제2조제1호"], "")
        on = {p["id"]: p.get("note", "") for s in self.idx.render(d, designation_note=True, definition_note=True,
                                                                     junyong_note=True) for p in s["parts"]}
        self.assertEqual(on["금연:제6조제2항"], "준용: «제5조제2항과 제5조제3항의 규정을 준용한다»")
        self.assertEqual(on["금연:제5조제4항"], "지정 관련 구절: «지정된 금연구역»")
        self.assertIn("외부 법령 참조: «「도시공원 및 녹지 등에 관한 법률」", on["도시공원:제2조제1호"])

    def test_external_list_check_flags_only_list_based_answers(self):
        allowed = {"도시공원:제22조제1항", "도시공원:제23조제1항"}
        uses_list = PkgPipeline.parse_generate(GW(("R1", [("공원에서 금지됩니다.", "도시공원:제22조제1항")], "ANSWERED")),
                                               allowed, ["금지 여부"])
        v = self.pipe.external_list_dependencies(uses_list)
        self.assertEqual([(i["claim"], i["kind"], i["ids"]) for i in v], [("R1", "unanswered_core", ["도시공원:제22조제1항"])])
        self.assertIn("법 제49조제1항 및 제2항 각 호의 행위", v[0]["detail"])
        other = PkgPipeline.parse_generate(GW(("R1", [("과태료가 부과됩니다.", "도시공원:제23조제1항")], "ANSWERED")),
                                           allowed, ["과태료"])
        self.assertEqual(self.pipe.external_list_dependencies(other), [])



def xd_inspect(verdict="DEPENDS_ON_OUTSIDE", a="금지됩니다", src="E1", needed="법률의 금지 행위 목록"):
    return json.dumps({"a_checks": [{"a_request": "R1", "b_answer_quote": a, "c_source": src, "d_needed": needed,
                                     "e_verdict": verdict}]}, ensure_ascii=False)


def xd_counter(core="CORE", basis="OUTSIDE", missing="금지 행위 목록"):
    return json.dumps({"a_core": core, "b_source": "", "c_missing": missing, "d_basis": basis}, ensure_ascii=False)


class PKG12(unittest.TestCase):
    """PKG12b 답변 후 외부 의존 확인(기본 끔): 점검 → 코드 검증 → 반대 확인이 모두 맞을 때만 기권."""
    PARK = "도시공원:제22조제1항"
    QP = "공원에서 그 행위를 해도 되나요?"
    FRAME = {"requests": ["금지 여부"], "quotes": ["그 행위를 해도 되나요"]}

    def setup(self, handlers, pid=PARK, text="공원에서 그 행위는 금지됩니다.", frame=None):
        import time
        fake = FakeSolar(handlers)
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_external_dependency_check=True),
                           transport=fake)
        final = PkgPipeline.parse_generate(GW(("R1", [(text, pid)], "ANSWERED")), {pid}, ["금지 여부"])
        sources = pipe.index.render(pipe.index.deliver([pid]))
        tr = {"calls": []}
        out = pipe.verify.external_dependency(self.QP, frame or self.FRAME, final, sources, time.monotonic() + 60, tr)
        return out, tr, fake

    def body(self, fake, name):
        return json.loads([b for n, b in fake.calls if n == name][0]["messages"][1]["content"])

    def test_outside_reference_external_law_only(self):
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        self.assertEqual(pipe.verify.outside_reference(self.PARK), "외부 법령 목록 참조: «법 제49조제1항 및 제2항 각 호의 행위»")
        self.assertTrue(pipe.verify.outside_reference("도시공원:제2조제1호").startswith("외부 법령 정의 참조"))
        self.assertTrue(pipe.verify.outside_reference("청년:제3조제1호단서").startswith("외부 법령에 맡김"))
        for internal in ("자전거:제12조의3제3항", "도시공원:제20조제2항", "청년:제23조제2항"):  # 시장·규칙·다른 조례
            self.assertEqual(pipe.verify.outside_reference(internal), "")
        self.assertEqual(pipe.verify.outside_reference(ITEM), "")
        self.assertFalse(make_cfg(tmpdir(), pipeline_profile="pkg").pkg_external_dependency_check)  # 기본 끔

    def test_request_quote_uses_verbatim_frame_quote_else_question(self):
        q = "공원에서 그 행위를 해도 되나요?"
        self.assertEqual(AnswerVerifier.request_quote(q, {"quotes": ["그 행위를  해도 되나요"]}, "R1"), "그 행위를  해도 되나요")
        self.assertEqual(AnswerVerifier.request_quote(q, {"quotes": ["행위 허용 여부"]}, "R1"), q)  # 요약문이면 질문 전체
        self.assertEqual(AnswerVerifier.request_quote(q, {"requests": [q]}, "R1"), q)  # 분해 실패(인용 없음)
        self.assertEqual(AnswerVerifier.request_quote(q, {"quotes": ["그 행위를 해도 되나요"]}, "R2"), q)

    def test_abstains_only_when_inspect_and_blind_counter_agree(self):
        out, tr, fake = self.setup({"pkg_extdep_inspect": xd_inspect(), "pkg_extdep_counter": xd_counter()})
        self.assertTrue(out)
        self.assertEqual(tr["external_dependency"]["status"], "abstain_external_dependency")
        insp = self.body(fake, "pkg_extdep_inspect")
        self.assertEqual(insp["evidence"][0]["refers_outside"], "외부 법령 목록 참조: «법 제49조제1항 및 제2항 각 호의 행위»")
        self.assertEqual(insp["requests"][0]["quote"], "그 행위를 해도 되나요")
        cnt = self.body(fake, "pkg_extdep_counter")
        self.assertEqual(set(cnt), {"question", "outcome", "sources"})  # 답변·점검 판정을 주지 않음
        self.assertEqual(cnt["outcome"], {"question_quote": "그 행위를 해도 되나요", "request": "금지 여부"})
        self.assertNotIn("금지됩니다", json.dumps(cnt, ensure_ascii=False))

    def test_keeps_answer_when_counter_disagrees(self):
        for c in (xd_counter(basis="LOCAL", missing=""), xd_counter(core="BACKGROUND"), xd_counter(missing="")):
            out, tr, _ = self.setup({"pkg_extdep_inspect": xd_inspect(), "pkg_extdep_counter": c})
            self.assertFalse(out)
            self.assertEqual(tr["external_dependency"]["status"], "kept_counter_disagreed")

    def test_unverified_inspection_never_reaches_counter(self):
        for bad in (xd_inspect(a="답변에 없는 구절"), xd_inspect(src="E9"), xd_inspect(needed=""),
                    xd_inspect(verdict="ANSWERED_BY_TEXT"), xd_inspect(verdict="UNCLEAR")):
            out, tr, fake = self.setup({"pkg_extdep_inspect": bad, "pkg_extdep_counter": None})
            self.assertFalse(out)
            self.assertEqual(tr["external_dependency"]["status"], "kept_no_verified_dependency")
            self.assertNotIn("pkg_extdep_counter", fake.names())

    def test_errors_keep_answer(self):
        for bad in ("{bad", json.dumps({"a_checks": [{"a_request": "R1", "e_verdict": "UNCLEAR"}]})):
            out, tr, _ = self.setup({"pkg_extdep_inspect": bad, "pkg_extdep_counter": None})
            self.assertFalse(out)
            self.assertEqual(tr["external_dependency"]["status"], "unavailable")

    def test_not_gated_makes_no_call(self):
        for pid in (ITEM, "도시공원:제20조제2항"):  # 외부 참조 없음, 내부 위임(규칙)
            out, tr, fake = self.setup({}, pid=pid)
            self.assertFalse(out)
            self.assertEqual(tr["external_dependency"]["status"], "not_applicable")
            self.assertEqual(fake.calls, [])

    def test_flow_integration(self):
        h = dict(Flow.H, pkg_extdep_inspect=xd_inspect(a="100분의 50 감면할 수 있습니다"), pkg_extdep_counter=xd_counter())
        fake = FakeSolar(h)
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_external_dependency_check=True), transport=fake)
        pipe.verify.outside_reference = lambda pid: "외부 법령 목록 참조: «x»" if pid == ITEM else ""  # 게이트만 대체
        status, sub, tr = pipe.answer(Q)
        self.assertEqual((sub, tr["route"]), (UNKNOWN, "external_dependency_abstain"))
        self.assertEqual(self.body(fake, "pkg_extdep_counter")["outcome"]["question_quote"], "할인을 받을 수 있나요")
        status, sub, tr, fake = run(dict(Flow.H))  # 기본 끔: 추가 호출 없음
        self.assertEqual(status, "ANSWER")
        self.assertNotIn("pkg_extdep_inspect", fake.names())


class PKG12Terms(unittest.TestCase):
    """B: 질문이 외부 정의 용어의 하위 종류를 말하면 그 정의 명제도 외부 의존 검사 대상."""

    @classmethod
    def setUpClass(cls):
        cls.pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_external_dependency_check=True,
                                        pkg_extdep_imported_terms=True), transport=FakeSolar({}))

    def final(self, pid, allowed=None):
        return PkgPipeline.parse_generate(GW(("R1", [("금연구역에서는 흡연할 수 없습니다.", pid)], "ANSWERED")),
                                          allowed or {pid}, ["가능 여부"])

    def test_variant_of_externally_defined_term(self):
        v, f = self.pipe.verify, self.final("금연:제5조제4항")
        self.assertEqual(v.imported_terms("정류장에서 전자담배를 피워도 되나요?", f), ["금연:제2조제1호"])
        self.assertEqual(v.imported_terms("정류장에서 담배를 피워도 되나요?", f), [])  # 용어 자체는 아님
        park = self.final("도시공원:제22조제1항")
        self.assertEqual(v.imported_terms("정류장에서 전자담배를 피워도 되나요?", park), [])  # 다른 조례 근거
        animal = self.final("동물보호:제2조제6호")
        self.assertEqual(v.imported_terms("반려동물 등록은 어떻게 하나요?", animal), [])  # 조례가 따로 정의한 낱말

    def test_imported_definition_enters_gate_and_counter_sources(self):
        import time
        h = {"pkg_extdep_inspect": xd_inspect(a="흡연할 수 없습니다", src="E2"), "pkg_extdep_counter": xd_counter()}
        fake = FakeSolar(h)
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_external_dependency_check=True,
                                    pkg_extdep_imported_terms=True), transport=fake)
        f = self.final("금연:제5조제4항")
        sources = pipe.index.render(pipe.index.deliver(["금연:제5조제4항"]))
        tr = {"calls": []}
        self.assertTrue(pipe.verify.external_dependency("정류장에서 전자담배를 피워도 되나요?", {"quotes": []}, f,
                                                        sources, time.monotonic() + 60, tr))
        self.assertEqual(tr["external_dependency"]["gated"], ["금연:제2조제1호"])
        insp = json.loads([b for n, b in fake.calls if n == "pkg_extdep_inspect"][0]["messages"][1]["content"])
        self.assertEqual([e["key"] for e in insp["evidence"]], ["E1", "E2"])
        self.assertTrue(insp["evidence"][1]["refers_outside"].startswith("외부 법령에 맡김"))
        cnt = json.loads([b for n, b in fake.calls if n == "pkg_extdep_counter"][0]["messages"][1]["content"])
        added = [p for s in cnt["sources"] for p in s["parts"] if p.get("id") == "금연:제2조제1호"]
        self.assertEqual(len(added), 1)
        # 플래그를 끄면 하위 종류 용어만으로는 검사 대상이 아니다.
        off = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_external_dependency_check=True),
                          transport=FakeSolar({}))
        tr = {"calls": []}
        self.assertFalse(off.verify.external_dependency("정류장에서 전자담배를 피워도 되나요?", {}, f, sources,
                                                        time.monotonic() + 60, tr))
        self.assertEqual(tr["external_dependency"]["status"], "not_applicable")


def loc(src, span, status="DECIDES", outcome="감면받을 수 있다"):
    return json.dumps({"a_checks": [{"a_request": "R1", "b_source": src, "c_span": span, "d_outcome": outcome,
                                     "e_status": status}]}, ensure_ascii=False)


class PKG12Recovery(unittest.TestCase):
    """C: 핵심 근거 부족 기권을 위치 확인 → 코드 검증 → 재생성 → 검토 → 외부 의존 확인을 모두 통과할 때만 복구."""

    def setup(self, handlers, gate_item=False):
        import time
        fake = FakeSolar(handlers)
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg", pkg_abstention_recovery=True,
                                    pkg_external_dependency_check=True), transport=fake)
        if gate_item:
            pipe.verify.outside_reference = lambda pid: "외부 법령 목록 참조: «x»" if pid == ITEM else ""
        d = pipe.index.deliver([LEAD, ITEM])
        sources, allowed = pipe.index.render(d), set(d.prop_ids)
        span = next(p["text"] for s in sources for p in s["parts"] if p.get("id") == ITEM)[:25]
        frame_ = {"requests": ["다둥이 카드 할인 가능 여부"], "quotes": ["할인을 받을 수 있나요"]}
        abstained = PkgPipeline.parse_generate(GW(("R1", [], "CORE_MISSING", "SILENT")), allowed, frame_["requests"])
        tr, state = {"calls": [], "recoveries": []}, {"repair_used": False}
        handlers_span = span
        return pipe, fake, (Q, frame_, abstained, sources, allowed, time.monotonic() + 80, tr, state), handlers_span

    def span(self):
        pipe = PkgPipeline(make_cfg(tmpdir(), pipeline_profile="pkg"), transport=FakeSolar({}))
        d = pipe.index.deliver([LEAD, ITEM])
        return next(p["text"] for s in pipe.index.render(d) for p in s["parts"] if p.get("id") == ITEM)[:25]

    def test_recovers_only_when_every_step_passes(self):
        sp = self.span()
        h = {"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(f"{ITEM}, {LEAD}"), "pkg_review": review()}
        pipe, fake, args, _ = self.setup(h)
        out = pipe.verify.recover(*args)
        tr = args[6]
        self.assertEqual((out["decision"], tr["recovery"]["status"]), ("ANSWER", "recovered"))
        loc_in = json.loads([b for n, b in fake.calls if n == "pkg_locate"][0]["messages"][1]["content"])
        self.assertEqual(set(loc_in), {"question", "requests", "sources"})  # 초안·기권 사유를 보지 않음
        self.assertEqual(loc_in["requests"][0]["quote"], "할인을 받을 수 있나요")
        rec_in = json.loads([b for n, b in fake.calls if n == "pkg_recover"][0]["messages"][1]["content"])
        self.assertEqual(rec_in["located"], [{"request": "R1", "source": ITEM, "span": sp}])
        self.assertNotIn("review", tr)  # 복구 검토는 recovery 아래에 따로 남긴다
        self.assertEqual(tr["recovery"]["review"]["status"], "ok")

    def test_each_failed_step_keeps_abstention(self):
        sp = self.span()
        cases = [
            ({"pkg_locate": loc(ITEM, sp, status="NOT_DECIDED")}, "kept_not_located"),
            ({"pkg_locate": loc(ITEM, sp, status="OUTSIDE")}, "kept_not_located"),
            ({"pkg_locate": loc(ITEM, "원문에 없는 구절")}, "kept_not_located"),
            ({"pkg_locate": loc("주차장:제99조", sp)}, "kept_not_located"),
            ({"pkg_locate": loc(ITEM, sp, outcome="")}, "kept_not_located"),
            ({"pkg_locate": loc(ITEM, sp), "pkg_recover": gen("", "CORE_MISSING", basis="SILENT")},
             "kept_regenerate_abstained"),
            ({"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(LEAD)}, "kept_regenerate_inconsistent"),
            ({"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(ITEM),
              "pkg_review": review([("missing_condition", "R1C1", "")])}, "kept_review_flagged"),
            ({"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(ITEM), "pkg_review": review(verdict="SIDE")},
             "kept_review_flagged"),
            ({"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(ITEM), "pkg_review": "{bad"}, "kept_review_unavailable"),
            ({"pkg_locate": "{bad"}, "unavailable"),
        ]
        for h, status in cases:
            pipe, fake, args, _ = self.setup(h)
            self.assertIsNone(pipe.verify.recover(*args), status)
            self.assertEqual(args[6]["recovery"]["status"], status)

    def test_external_dependency_blocks_recovery(self):
        sp = self.span()
        h = {"pkg_locate": loc(ITEM, sp), "pkg_recover": gen(ITEM), "pkg_review": review(),
             "pkg_extdep_inspect": xd_inspect(a="100분의 50 감면할 수 있습니다"), "pkg_extdep_counter": xd_counter()}
        pipe, fake, args, _ = self.setup(h, gate_item=True)
        self.assertIsNone(pipe.verify.recover(*args))
        self.assertEqual(args[6]["recovery"]["status"], "kept_external_dependency")
        self.assertEqual(args[6]["recovery_extdep"]["status"], "abstain_external_dependency")

    def test_flow_recovery_and_default_off(self):
        sp = self.span()
        h = dict(Flow.H, pkg_generate=gen("", "CORE_MISSING", basis="SILENT"), pkg_locate=loc(ITEM, sp),
                 pkg_recover=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(dict(h), pkg_abstention_recovery=True)
        self.assertEqual((status, tr["route"]), ("ANSWER", "recovered:generate"))
        self.assertEqual(sub["citation"][0], "서울특별시 주차장 설치 및 관리 조례 제7조 제1항")
        status, sub, tr, fake = run(dict(h, pkg_locate=None, pkg_recover=None, pkg_review=None))  # 기본 끔
        self.assertEqual((sub, tr["route"]), (UNKNOWN, "generate_core_missing"))
        self.assertNotIn("pkg_locate", fake.names())


class PKG11Weak(unittest.TestCase):
    """PKG11.1 점검의 약한 고리: W1 그래프 표시 보완(빌드 옵션, 최종 PKG11.1은 끔), W2 생성 지시문 경계(기본 끔),
    W3 외부 참조 표시 전달(기본 끔)."""

    W1_IDS = ("도시공원:제23조제1항", "동물보호:제2조제6호", "동물보호:제2조제16호")

    def test_w1_graph_marks_three_missing_external_clauses_only_with_option(self):
        from lawqa.pkg_runtime import outside_reference as v
        on = {p["id"]: p["attrs"] for p in P.build(C, w1=True)["propositions"]}
        self.assertEqual(v(on["도시공원:제23조제1항"]), "외부 법령에 맡김: «영 제51조제1항 별표 4와 같다»")
        self.assertTrue(v(on["동물보호:제2조제6호"]).startswith("외부 법령 정의 참조: «「동물보호법 시행규칙」"))
        self.assertTrue(v(on["동물보호:제2조제16호"]).startswith("외부 법령 정의 참조: «「동물보호법 시행령」"))
        self.assertEqual(v(on["도시공원:제23조제2항"]), "")  # 같은 조의 감경 규정은 조례가 직접 정함
        off = {p["id"]: p["attrs"] for p in G["propositions"]}  # 최종 PKG11.1 그래프
        self.assertEqual([v(off[i]) for i in self.W1_IDS], ["", "", ""])
        changed = [i for i in on if on[i] != off[i]]
        self.assertEqual(sorted(changed), sorted(self.W1_IDS))  # W1이 바꾸는 명제는 이 셋뿐

    def test_w3_outside_note_only_with_flag(self):
        d = X.deliver(["도시공원:제22조제1항", "도시공원:제23조제1항", "동물보호:제2조제6호", ITEM])
        off = {p["id"]: p.get("note", "") for s in X.render(d) for p in s["parts"] if p["id"]}
        self.assertFalse(any("외부 법령" in n for n in off.values()))
        on = {p["id"]: p.get("note", "") for s in X.render(d, outside_note=True) for p in s["parts"] if p["id"]}
        self.assertIn("외부 법령 목록 참조: «법 제49조제1항 및 제2항 각 호의 행위»", on["도시공원:제22조제1항"])
        self.assertTrue(on["도시공원:제22조제1항"].endswith("(맡겨진 내용 자체는 이 자료에 없음)"))
        self.assertFalse(any("외부 법령" in on[i] for i in ("도시공원:제23조제1항", "동물보호:제2조제6호")))  # W1 끔
        self.assertEqual(on[ITEM], "")

    def test_w2_generate_rule_only_with_flag(self):
        import lawqa.pkg_prompts as PPm
        status, sub, tr, fake = run(dict(Flow.H))
        sysmsg = [b for n, b in fake.calls if n == "pkg_generate"][0]["messages"][0]["content"]
        self.assertEqual(sysmsg, PPm.GENERATE_SYSTEM)  # 기본은 PKG11과 같은 지시문
        h = dict(Flow.H, pkg_review=review([("unsupported_claim", "R1C1", "")]), pkg_correct=gen(f"{ITEM}, {LEAD}"))
        status, sub, tr, fake = run(h, pkg_generate_external_rule=True)
        for name in ("pkg_generate", "pkg_correct"):
            sysmsg = [b for n, b in fake.calls if n == name][0]["messages"][0]["content"]
            self.assertIn("DIRECT가 아니라 EXTERNAL이다.", sysmsg)
            self.assertIn("그 내용은 원문에 없는 것이다(EXTERNAL)", sysmsg)
