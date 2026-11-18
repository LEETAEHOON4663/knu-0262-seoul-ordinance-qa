"""② 색인 + ③ 검색·근거 후보 구성.

- 희소 검색: 어절 + 문자 bigram BM25 (외부 의존·API 없음).
- 밀집 검색(선택): 동결 passage 벡터가 있고 질의 임베딩을 받을 수 있을 때만.
- 융합: 가중 RRF. 원질문 목록은 항상 포함(질의계획이 틀려도 원질문 후보 보호).
- 문맥 구성: 순위대로 조(별표) 묶음을 넣고, 필수근거 1단계를 함께 넣는다.
  원문 하나를 중간에서 자르지 않는다. 문자 예산을 넘는 하위 후보는 통째로 뺀다.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Corpus, Unit, sha256_file
from .graph import EvidenceGraph

_WORD = re.compile(r"[0-9A-Za-z가-힣]+")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for w in _WORD.findall(text.lower()):
        if len(w) >= 2:
            toks.append("w:" + w)
            toks.extend("b:" + w[i:i + 2] for i in range(len(w) - 1))
        else:
            toks.append("w:" + w)
    return toks


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = Counter(tokenize(query))
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * ln / self.avg)
            for t, qn in q.items():
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + norm)
            out.append(s)
        return out


class DenseIndex:
    """동결 passage 벡터(서버 vectors.json 재사용). 없으면 사용하지 않는다."""

    def __init__(self, corpus: Corpus, path: Path, expected_sha256: str | None):
        if expected_sha256 and sha256_file(path) != expected_sha256:
            raise ValueError("vectors_hash_mismatch")
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        vecs = raw["sources"] if isinstance(raw, dict) and "sources" in raw else raw
        if isinstance(vecs, dict):
            rows = [vecs[u.id] for u in corpus.units]
        else:
            if len(vecs) != len(corpus.units):
                raise ValueError("vectors_count_mismatch")
            rows = vecs
        self.rows = [self._unit(v) for v in rows]
        self.dim = len(self.rows[0])

    @staticmethod
    def _unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def scores(self, qvec: list[float]) -> list[float]:
        q = self._unit(qvec)
        return [sum(a * b for a, b in zip(q, r)) for r in self.rows]


class NodeDenseIndex:
    """IA 패키지의 동결 트리 노드 벡터(1,272개, Solar Embedding 2 passage)를 재사용한다.

    노드 인용 라벨 → 이 구현의 원문 단위로 사상하고, 단위 점수 = 사상된 노드 코사인의 최댓값.
    노드 텍스트는 상위 조·항 문맥이 붙은 호/항/표행 단위라 세밀한 질의에 더 잘 맞는다.
    재임베딩·API 호출 없음(질의 임베딩만 실행 시 필요).
    """

    CATALOG = "data/chunks_tree_context.json"
    VECTORS = "passage_embeddings.npy"

    def __init__(self, corpus: Corpus, root: Path, catalog_sha: str | None, vectors_sha: str | None):
        import numpy as np
        root = Path(root)
        cat_path, vec_path = root / self.CATALOG, root / self.VECTORS
        if catalog_sha and sha256_file(cat_path) != catalog_sha:
            raise ValueError("node_catalog_hash_mismatch")
        if vectors_sha and sha256_file(vec_path) != vectors_sha:
            raise ValueError("node_vectors_hash_mismatch")
        catalog = json.loads(cat_path.read_text(encoding="utf-8-sig"))
        vecs = np.load(vec_path, allow_pickle=False).astype(np.float32)
        if vecs.shape[0] != len(catalog) or not np.isfinite(vecs).all():
            raise ValueError("node_vectors_shape")
        self.np = np
        self.vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
        by_label = {u.label: [i] for i, u in enumerate(corpus.units)}
        law_doc = {u.law: u.doc_id for u in corpus.units}
        self.node_units: list[list[int]] = []
        self.node_text = [n["text"] for n in catalog]
        self.node_kind = [n["kind"] for n in catalog]
        unmapped = []
        for n in catalog:
            label = n["citation_label"]
            units = by_label.get(label)
            if units is None and n.get("law") in law_doc and n.get("article"):
                group = f"{law_doc[n['law']]}:{n['article']}"
                units = [corpus.units.index(u) for u in corpus.groups.get(group, [])]
            if not units:
                unmapped.append(label)
                units = []
            self.node_units.append(units)
        self.unmapped = unmapped
        self.n_units = len(corpus.units)
        self.dim = vecs.shape[1]

    def node_sims(self, qvec: list[float]):
        np = self.np
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        return (self.vecs @ q).tolist()

    def focus(self, unit_index: int, sims: list[float], unit_len: int, k: int = 2) -> list[str]:
        """원문 단위 안에서 질의와 가장 가까운 하위 노드(호·목·표행·비고) 발췌. 원문을 대신하지 않는다."""
        cands = [(sims[i], self.node_text[i]) for i, us in enumerate(self.node_units)
                 if unit_index in us and self.node_kind[i] in ("ho", "mok", "row", "row_group", "note")
                 and len(self.node_text[i]) < 0.6 * unit_len]
        cands.sort(key=lambda x: -x[0])
        out = []
        for _, t in cands:
            t = re.sub(r"<[^>]{0,120}>", "", t)  # 개정 이력 표시 제거(발췌만, 원문 text는 그대로)
            t = re.sub(r"\s+", " ", t).strip()
            if t and t not in out:
                out.append(t[:300])
            if len(out) >= k:
                break
        return out

    def scores(self, qvec: list[float]) -> list[float]:
        np = self.np
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) or 1.0)
        sims = self.vecs @ q
        out = [0.0] * self.n_units
        for s, units in zip(sims.tolist(), self.node_units):
            for u in units:
                if s > out[u]:
                    out[u] = s
        return out


LAW_SHORT = {
    "서울특별시 주차장 설치 및 관리 조례": "주차장",
    "서울특별시 동물보호 조례": "동물보호",
    "서울특별시 자전거이용 활성화에 관한 조례": "자전거",
    "서울특별시 도시공원 조례": "도시공원",
    "서울특별시 청년 기본 조례": "청년",
    "서울특별시 금연환경 조성 및 간접흡연 피해방지조례": "금연",
}


def alias_of(u: Unit) -> str:
    """의미 있는 근거 ID(예: 주차장:제18조제2항, 도시공원:별표2).

    순번 ID(E1, E2…)는 긴 목록에서 모델이 이웃 번호를 고르는 오류가 실측됐다.
    조문 위치를 담은 ID는 본문 머리말과 일치해 잘못 짚기 어렵다.
    """
    short = LAW_SHORT.get(u.law, u.law)
    if u.kind == "BODY":
        return f"{short}:{u.article}" + (f"제{u.para}항" if u.para else "")
    return f"{short}:" + re.sub(r"\s+", "", u.label.split(u.law, 1)[1])


@dataclass
class Context:
    units: list[Unit]
    aliases: dict[str, str]  # 근거 id(예: 주차장:제6조제1항) → unit id
    trace: dict = field(default_factory=dict)
    focus: dict = field(default_factory=dict)  # unit id → 질의와 가까운 하위 항목 발췌

    def payload(self) -> list[dict]:
        rev = {v: k for k, v in self.aliases.items()}
        out = []
        for u in self.units:
            e = {"id": rev[u.id]}
            if self.focus.get(u.id):
                e["focus"] = self.focus[u.id]
            e["text"] = u.text
            out.append(e)
        return out

    def unit(self, alias: str) -> Unit:
        return next(u for u in self.units if u.id == self.aliases[alias])


def attach_focus(ctx: "Context", corpus, dense, qvecs: list, top_units: int = 15):
    """상위 근거마다 질의와 가장 가까운 호·목·표행 발췌를 붙인다(노드 벡터가 있을 때만)."""
    if dense is None or not hasattr(dense, "focus") or not qvecs:
        return
    sims_list = [dense.node_sims(v) for v in qvecs if v is not None]
    if not sims_list:
        return
    sims = [max(col) for col in zip(*sims_list)]
    index = {u.id: i for i, u in enumerate(corpus.units)}
    for u in ctx.units[:top_units]:
        f = dense.focus(index[u.id], sims, len(u.text))
        if f:
            ctx.focus[u.id] = f


class Retriever:
    def __init__(self, corpus: Corpus, graph: EvidenceGraph, dense: DenseIndex | None = None):
        self.corpus, self.graph, self.dense = corpus, graph, dense
        docs = [f"{u.law} {u.label} {u.title} {u.text}" for u in corpus.units]
        self.bm25 = BM25(docs)

    # -------------------------------------------------------------- ranking
    def _ranked(self, scores: list[float], allowed: set[str] | None, limit: int) -> list[int]:
        idx = [i for i, s in enumerate(scores) if s > 0 and
               (allowed is None or self.corpus.units[i].law in allowed)]
        idx.sort(key=lambda i: (-scores[i], i))
        return idx[:limit]

    def rank(self, question: str, queries: list[str] = (), laws: list[str] = (),
             qvec: list[float] | None = None, qvecs: list[list[float]] = (),
             depth: int = 40, rrf_k: int = 60) -> list[tuple[str, float]]:
        lists: list[tuple[float, list[int], str]] = []
        lists.append((1.0, self._ranked(self.bm25.scores(question), None, depth), "bm25:question"))
        if self.dense is not None and qvec is not None:
            lists.append((1.0, self._ranked(self.dense.scores(qvec), None, depth), "dense:question"))
        allowed = set(laws) if laws else None
        for i, q in enumerate(queries):
            lists.append((0.6, self._ranked(self.bm25.scores(q), allowed, depth), f"bm25:plan{i}"))
            if self.dense is not None and i < len(qvecs) and qvecs[i] is not None:
                lists.append((0.6, self._ranked(self.dense.scores(qvecs[i]), allowed, depth), f"dense:plan{i}"))
        fused: dict[int, float] = {}
        for w, ranked, _ in lists:
            for r, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + w / (rrf_k + r + 1)
        order = sorted(fused, key=lambda i: (-fused[i], i))[:depth]
        return [(self.corpus.units[i].id, round(fused[i], 6)) for i in order]

    # -------------------------------------------------------------- context
    def build_context(self, ranked: list[tuple[str, float]], budget_chars: int = 20000,
                      expand_group_chars: int = 2500) -> Context:
        chosen: dict[str, str] = {}  # unit id → 포함 이유
        used = 0
        blocks = 0
        skipped = []
        for uid, _ in ranked:
            if uid in chosen:
                continue
            group = self.corpus.group_units(uid)
            gsize = sum(len(u.text) for u in group)
            block = [u.id for u in group] if gsize <= expand_group_chars else [uid]
            reasons = {b: ("hit" if b == uid else "same_article") for b in block}
            for b in list(block):
                for e in self.graph.required(b):
                    if e.dst not in reasons:
                        reasons[e.dst] = f"required:{e.kind}:{b}"
            new = {k: v for k, v in reasons.items() if k not in chosen}
            add = sum(len(self.corpus.by_id[k].text) for k in new)
            if chosen and used + add > budget_chars:
                skipped.append(uid)
                continue
            chosen.update(new)
            used += add
            blocks += 1
        # 검색 순위 순서(필수근거는 그 근거 뒤)로 제시한다. 상위 근거가 목록 앞에 온다.
        units = [self.corpus.by_id[k] for k in chosen]
        aliases = {alias_of(u): u.id for u in units}
        return Context(units, aliases, {"chars": used, "blocks": blocks, "reasons": chosen,
                                        "skipped_for_budget": skipped})
