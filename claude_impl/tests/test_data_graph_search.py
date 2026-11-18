"""① 자료 · 필수근거 그래프 · ② ③ 검색/문맥 오프라인 검사."""
import unittest

from lawqa.corpus import Corpus, render_linearized_tables
from lawqa.graph import EvidenceGraph
from lawqa.search import Retriever, tokenize
from lawqa import selfcheck

C = Corpus()
G = EvidenceGraph(C)
R = Retriever(C, G)
PARK = "unit:2171833:"


class DataTests(unittest.TestCase):
    def test_counts_and_kinds(self):
        self.assertEqual(len(C.units), 525)
        kinds = [u.kind for u in C.units]
        self.assertEqual(kinds.count("BODY"), 511)
        self.assertEqual(kinds.count("TABLE"), 10)
        self.assertEqual(kinds.count("DIAGRAM"), 4)
        self.assertEqual(len(C.law_names), 6)

    def test_labels_parse_with_original_check_py(self):
        mod = selfcheck.load_original_check()
        self.assertEqual(selfcheck.label_problems(C, mod), [])
        # 자기 라벨은 원본 채점 함수에서 1점
        for u in C.units[::25]:
            self.assertEqual(mod.score_citation([u.label], [[u.label]]), 1.0)

    def test_body_text_grounded_in_frozen_ordinances(self):
        self.assertEqual(selfcheck.grounding_problems(C), [])

    def test_metadata_from_frozen_ordinances(self):
        self.assertEqual(selfcheck.metadata_problems(C), [])
        u = C.by_id[PARK + "제6조제1항"]
        self.assertEqual(u.effective_date, "2026-10-01")
        self.assertTrue(u.source_url.startswith("https://www.law.go.kr/"))

    def test_table_render_keeps_every_cell(self):
        self.assertEqual(selfcheck.table_problems(C), [])

    def test_table_render_restores_merged_cells(self):
        t = C.by_id["unit:2171767:별표"].text
        self.assertIn("| 주차요금 | 200 | 1,000 | 15,000 |", t)
        park = C.by_id[PARK + "별표1"].text
        self.assertIn("공시지가 변수", park)  # 전체 폭 비고 셀은 문단으로 보존
        self.assertNotIn("[셀 행", park)

    def test_render_handles_plain_text(self):
        out, cells = render_linearized_tables("제목\n본문")
        self.assertEqual(out, "제목\n본문")
        self.assertEqual(cells, [])

    def test_diagram_flag(self):
        u = C.by_id[PARK + "별도5"]
        self.assertIn("diagram_original_unavailable", u.flags)


class GraphTests(unittest.TestCase):
    def dsts(self, uid):
        return {e.dst for e in G.required(uid)}

    def test_paragraph_reference(self):
        self.assertIn(PARK + "제3조제1항", self.dsts(PARK + "제3조제2항"))

    def test_article_to_annex_and_back(self):
        self.assertIn(PARK + "별표1", self.dsts(PARK + "제6조제1항"))
        self.assertIn(PARK + "제6조제1항", self.dsts(PARK + "별표1"))

    def test_external_law_articles_not_followed(self):
        # 별표1 비고 "법 제7조제1항 및 제12조제1항"은 주차장법 조문이다.
        self.assertNotIn(PARK + "제12조제1항", self.dsts(PARK + "별표1"))
        self.assertNotIn(PARK + "제7조제1항", self.dsts(PARK + "별표1"))

    def test_definition_edges_skip_ubiquitous_terms(self):
        doc = "2171829"
        n = sum(1 for u in C.units if u.doc_id == doc)
        users = [k for k, v in G.out.items() if k.startswith("unit:" + doc)
                 and any(e.kind == "DEFINITION" for e in v)]
        self.assertGreater(len(users), 0)
        self.assertLess(len(users), n)

    def test_edges_point_to_existing_units(self):
        for src, edges in G.out.items():
            self.assertIn(src, C.by_id)
            for e in edges:
                self.assertIn(e.dst, C.by_id)
                self.assertNotEqual(e.src, e.dst)


class SearchTests(unittest.TestCase):
    def test_tokenize_bigrams(self):
        self.assertIn("b:주차", tokenize("공영주차장"))
        self.assertIn("w:공영주차장", tokenize("공영주차장"))

    def test_fee_question_gets_rule_and_annex(self):
        ranked = R.rank("공영주차장 주차요금은 얼마인가요?")
        top = [u for u, _ in ranked[:5]]
        self.assertTrue({PARK + "제6조제1항", PARK + "별표1"} & set(top))
        ctx = R.build_context(ranked)
        ids = {u.id for u in ctx.units}
        self.assertTrue({PARK + "제6조제1항", PARK + "별표1"} <= ids)

    def test_context_never_truncates_and_aliases_unique(self):
        ctx = R.build_context(R.rank("공원 축구장 이용료"))
        for e, u in zip(ctx.payload(), ctx.units):
            self.assertEqual(e["text"], u.text)
        self.assertEqual(len(set(ctx.aliases.values())), len(ctx.aliases))
        self.assertIn("도시공원:별표2", ctx.aliases)
        self.assertTrue(all(":" in a for a in ctx.aliases))

    def test_budget_respected_after_first_block(self):
        ctx = R.build_context(R.rank("주차장 설치 기준"), budget_chars=4000)
        if ctx.trace["chars"] > 4000:
            self.assertEqual(ctx.trace["blocks"], 1)  # 초과는 첫 묶음(+필수근거)일 때만
        small = R.build_context(R.rank("주차장 설치 기준"), budget_chars=1)
        self.assertGreaterEqual(len(small.units), 1)  # 첫 묶음은 항상 포함

    def test_original_question_list_survives_wrong_plan(self):
        q = "공영주차장 주차요금은 얼마인가요?"
        ranked = R.rank(q, queries=["청년 기본계획"], laws=["서울특별시 청년 기본 조례"])
        self.assertIn(PARK + "제6조제1항", [u for u, _ in ranked[:10]])

    def test_plan_law_restriction_applies_to_plan_lists(self):
        law = "서울특별시 자전거이용 활성화에 관한 조례"
        ranked = R.rank("zzzz", queries=["주차장 요금"], laws=[law])
        self.assertTrue(ranked)
        self.assertTrue(all(C.by_id[u].law == law for u, _ in ranked))

    def test_no_lexical_overlap_gives_empty_ranking(self):
        self.assertEqual(R.rank("zzzz qqqq"), [])

    def test_deterministic(self):
        q = "길고양이 중성화 지원"
        self.assertEqual(R.rank(q), R.rank(q))


if __name__ == "__main__":
    unittest.main()


class FocusTests(unittest.TestCase):
    def test_focus_attached_from_sub_nodes(self):
        from pathlib import Path
        from lawqa.search import NodeDenseIndex, attach_focus
        root = Path(__file__).resolve().parent.parent.parent / "ia_ref/IA_normalized/tree_retrieval_20261109"
        if not root.exists():
            self.skipTest("IA node vectors not available")
        d = NodeDenseIndex(C, root, None, None)
        r = Retriever(C, G, d)
        uid = "unit:2147709:제20조제1항"
        q = d.vecs[[i for i, us in enumerate(d.node_units) if C.units.index(C.by_id[uid]) in us
                    and "다둥이" in d.node_text[i] and d.node_kind[i] == "ho"][0]].tolist()  # 다둥이 호 노드 벡터를 질의로
        ctx = r.build_context(r.rank("공원 입장료 면제", qvec=q), 10000)
        attach_focus(ctx, C, d, [q])
        payload = {e["id"]: e for e in ctx.payload()}
        self.assertIn("다둥이", " ".join(payload["도시공원:제20조제1항"].get("focus", [])))
        self.assertEqual(payload["도시공원:제20조제1항"]["text"], C.by_id[uid].text)  # 원문은 그대로
