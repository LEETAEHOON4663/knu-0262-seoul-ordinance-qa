"""오프라인 자료 점검(유료 호출 없음). 테스트와 CLI가 함께 쓴다."""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

from .config import ROOT
from .corpus import Corpus, render_linearized_tables
from .graph import EvidenceGraph

CHECK_PY = ROOT.parent / "handoff_r9" / "evaluation" / "check.py"
CHECK_SHA256 = "4fcc483fefdca00f2233ee6849c8b145c4e76a179a67045634a0e0a7707abc81"


def load_original_check(path: Path = CHECK_PY):
    """원본 check.py를 수정 없이 읽기 전용으로 불러온다(최상단 import config만 임시 대체)."""
    from .corpus import sha256_file
    if sha256_file(path) != CHECK_SHA256:
        raise RuntimeError("check_py_hash_mismatch")
    saved = sys.modules.get("config")
    sys.modules["config"] = types.SimpleNamespace(ROOT=Path(path).parent, CHUNK_UNIT="n/a",
                                                  TOP_K=0, USE_VERIFY=False)
    try:
        spec = importlib.util.spec_from_file_location("original_check_readonly", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = saved
    return mod


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def label_problems(corpus: Corpus, check_mod) -> list[str]:
    bad = []
    for u in corpus.units:
        p = check_mod.parse(u.label)
        if not p or p[0] != _norm(u.law):
            bad.append(u.label)
            continue
        if u.kind == "BODY":
            want_para = f"제{u.para}항" if u.para else None
            if p[1] != u.article or p[2] != want_para:
                bad.append(u.label)
    return bad


def grounding_problems(corpus: Corpus) -> list[str]:
    """본문 원문이 고정 조례 원문(ordinances.json)의 해당 조에 실제로 있는지."""
    arts = {}
    for o in corpus.ordinances:
        for a in o["articles"]:
            arts[(o["mst"], a["article"])] = _norm(a["text"])
    bad = []
    for u in corpus.units:
        if u.kind != "BODY":
            continue
        body = re.sub(r"^\[[^\]]*\]\n?", "", u.raw_text, count=1)
        body = re.sub(r"^[①-⑳]\s*", "", body)
        src = arts.get((u.doc_id, u.article))
        if src is None or _norm(body) not in src:
            bad.append(u.id)
    return bad


def table_problems(corpus: Corpus) -> list[str]:
    bad = []
    for u in corpus.units:
        if u.kind != "TABLE":
            continue
        rendered, cells = render_linearized_tables(u.raw_text)
        flat = _norm(rendered.replace(" / ", ""))
        for c in cells:
            if _norm(c.replace(" / ", "")) not in flat:
                bad.append(f"{u.id}:{c[:30]}")
    return bad


def metadata_problems(corpus: Corpus) -> list[str]:
    bad = []
    for s in corpus.raw_sources:
        meta = corpus.laws[s["doc_id"]]
        if s["source_url"] != meta["source_url"] or s["effective_date"] != meta["effective_date"]:
            bad.append(s["id"])
    return bad


def run(cfg) -> bool:
    corpus = Corpus(cfg.data_dir)
    graph = EvidenceGraph(corpus)
    check_mod = load_original_check()
    results = {
        "units": len(corpus.units),
        "label_problems": label_problems(corpus, check_mod),
        "grounding_problems": grounding_problems(corpus),
        "table_cell_loss": table_problems(corpus),
        "metadata_problems": metadata_problems(corpus),
        "graph_edges": sum(len(v) for v in graph.out.values()),
    }
    ok = not any(results[k] for k in ("label_problems", "grounding_problems",
                                      "table_cell_loss", "metadata_problems"))
    for k, v in results.items():
        print(f"{k}: {v if not isinstance(v, list) else (len(v), v[:5])}")
    print("SELFCHECK", "PASS" if ok else "FAIL")
    return ok
