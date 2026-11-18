"""선택적 밀집 검색 경로와 공통 마감 오프라인 검사(합성 벡터·가짜 Solar)."""
import json
import time
import unittest
from pathlib import Path

from lawqa.corpus import Corpus
from lawqa.pipeline import Pipeline
from lawqa.search import DenseIndex

from helpers import FakeSolar, make_cfg, plan, tmpdir
from test_pipeline import OK, Q, good_generate, good_select

NO_ISSUES = '{"a_check":"","b_issues":[]}'


def synthetic_vectors(path: Path, corpus: Corpus, as_dict=False):
    # 합성 2차원 벡터: 주차장 조례만 [1,0], 나머지 [0,1]
    vec = lambda u: [1.0, 0.0] if u.doc_id == "2171833" else [0.0, 1.0]  # noqa: E731
    data = {"sources": {u.id: vec(u) for u in corpus.units}} if as_dict else \
        {"sources": [vec(u) for u in corpus.units]}
    path.write_text(json.dumps(data))


class Dense(unittest.TestCase):
    def test_dense_index_formats(self):
        c = Corpus()
        for as_dict in (False, True):
            p = Path(tmpdir()) / "v.json"
            synthetic_vectors(p, c, as_dict)
            d = DenseIndex(c, p, None)
            s = d.scores([1.0, 0.0])
            self.assertEqual(len(s), 525)

    def test_hash_mismatch_rejected(self):
        p = Path(tmpdir()) / "v.json"
        synthetic_vectors(p, Corpus())
        with self.assertRaises(ValueError):
            DenseIndex(Corpus(), p, "0" * 64)

    def test_pipeline_uses_query_embeddings(self):
        tmp = tmpdir()
        p = Path(tmp) / "v.json"
        synthetic_vectors(p, Corpus())
        fake = FakeSolar(dict(OK, lawqa_plan=plan(queries=["주차요금", "급지"])))
        pipe = Pipeline(make_cfg(tmp, vectors_file=str(p), vectors_sha256=None), transport=fake)
        status, sub, tr = pipe.answer(Q)
        self.assertEqual(status, "ANSWER")
        embed = [b for n, b in fake.calls if n == "embed"][0]
        self.assertEqual(embed["input"], [Q, "주차요금", "급지"])
        self.assertEqual(embed["model"], "solar-embedding-2-query")


class Deadline(unittest.TestCase):
    def test_wait_time_counts_against_deadline(self):
        fake = FakeSolar(dict(OK))
        pipe = Pipeline(make_cfg(tmpdir()), transport=fake)
        # 수신 후 86초가 이미 지난 요청: 새 호출을 시작하지 않고 시간 오류로 끝난다.
        status, sub, tr = pipe.answer(Q, started=time.monotonic() - 86)
        self.assertEqual(status, "ERROR")
        self.assertEqual(sub["http_status"], 504)
        self.assertEqual(fake.calls, [])

    def test_review_skipped_when_time_short(self):
        fake = FakeSolar({k: v for k, v in OK.items() if k != "lawqa_review"})
        pipe = Pipeline(make_cfg(tmpdir()), transport=fake)
        status, sub, tr = pipe.answer(Q, started=time.monotonic() - 74)
        self.assertEqual(status, "ANSWER")
        self.assertEqual(tr["review"]["status"], "skipped_insufficient_time")


if __name__ == "__main__":
    unittest.main()
