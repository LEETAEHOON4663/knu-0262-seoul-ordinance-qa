"""검색 문맥의 정답 근거 포함률(오프라인 진단, API 없음).

모델 성능이 아니라 '답변 모델 입력에 정답 근거가 들어 있었는가'만 본다.
질문 벡터는 IA 패키지에 저장된 solar-embedding-2-query 성공 응답만 재사용한다(동일 문자열).
질의계획 검색어는 API가 필요하므로 이 진단에는 없다.
정답은 채점용으로만 읽고, 검색 입력에는 질문 문자열만 쓴다.

  python3 tools/retrieval_recall.py --ia ../ia_ref/IA_normalized
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawqa.corpus import Corpus  # noqa: E402
from lawqa.graph import EvidenceGraph  # noqa: E402
from lawqa.search import NodeDenseIndex, Retriever  # noqa: E402
from lawqa.selfcheck import load_original_check  # noqa: E402

MODEL = "solar-embedding-2-query"


def cached_query_vectors(ia: Path) -> dict[str, list[float]]:
    out = {}
    m = json.loads((ia / "merged_evaluation_20261108/query_embeddings.json").read_text(encoding="utf-8-sig"))
    if m["configuration"]["model"] == MODEL:
        for r in m["vectors_by_question_hash"].values():
            out[r["question"]] = r["vector"]
    for f in sorted(ia.glob("cascade_evaluation_20261109/*/query_embedding_batch_cache/*.json")):
        e = json.loads(f.read_text(encoding="utf-8-sig"))
        if e.get("status") == "success" and e["request_payload"]["model"] == MODEL:
            for t, v in zip(e["request_payload"]["input"], e["vectors"]):
                out.setdefault(t, v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ia", required=True)
    ap.add_argument("--budget", type=int, default=16000)
    a = ap.parse_args()
    ia = Path(a.ia)
    check = load_original_check()
    qs = json.loads((ia / "merged_evaluation449_20261110/merged_questions.json").read_text(encoding="utf-8-sig"))
    qv = cached_query_vectors(ia)
    corpus = Corpus()
    graph = EvidenceGraph(corpus)
    sparse = Retriever(corpus, graph)
    dense = Retriever(corpus, graph, NodeDenseIndex(corpus, ia / "tree_retrieval_20261109", None, None))
    rows = {"bm25": [], "bm25+node_dense": []}
    no_vec = 0
    for q in qs:
        if not q["gold"]:
            continue
        vec = qv.get(q["question"])
        no_vec += vec is None
        for name, r in (("bm25", sparse), ("bm25+node_dense", dense)):
            ranked = r.rank(q["question"], qvec=vec if name != "bm25" else None)
            ctx = r.build_context(ranked, a.budget)
            labels = [u.label for u in ctx.units]
            rows[name].append((check.score_citation(labels, q["gold"]), ctx.trace["chars"]))
    n = len(rows["bm25"])
    print(json.dumps({
        "questions_with_gold": n, "missing_cached_query_vector": no_vec,
        "budget_chars": a.budget,
        **{k: {"oracle_citation_mean": round(sum(s for s, _ in v) / n, 4),
               "full_recall_ratio": round(sum(s == 1 for s, _ in v) / n, 4),
               "mean_context_chars": round(sum(c for _, c in v) / n)} for k, v in rows.items()},
        "note": "정답 근거가 문맥에 있으면 모델이 완벽히 인용할 때의 상한. 실제 점수 아님.",
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
