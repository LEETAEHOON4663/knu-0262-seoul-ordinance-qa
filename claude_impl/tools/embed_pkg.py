"""PKG 명제 검색 표현을 Solar Embedding 2 passage로 임베딩한다(구축 시, 유료 소액).

  python3 tools/embed_pkg.py --config runs/PKG/build.json --allow-live [--previous data/pkg/archive/<tag>/vectors.json]
출력: data/pkg/vectors.json  {model, max_chars, graph_sha256, ids, input_sha256, vectors, reused, embedded}

- 실제 임베딩 입력은 검색 표현의 앞 EMBED_MAX_CHARS자다. 그보다 긴 표현은 truncated 목록에 기록한다.
- 이전 벡터 파일이 있으면 입력 해시·모델이 같은 명제의 벡터는 재사용하고, 바뀐 명제만 새로 임베딩한다(F08).
- 실행기는 불러올 때 id·모델·입력 해시를 모두 대조한다(lawqa/pkg_runtime.PropIndex).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawqa import pkg_graph as P  # noqa: E402
from lawqa.config import Config  # noqa: E402
from lawqa.llm import SolarClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--allow-live", action="store_true")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--previous", help="재사용할 이전 vectors.json")
    a = ap.parse_args()
    cfg = Config.load(a.config)
    gpath = Path(cfg.data_dir) / "pkg" / "graph.json"
    g = P.load(gpath)
    ids = [p["id"] for p in g["propositions"]]
    full = [P.search_view(p) for p in g["propositions"]]
    views = [v[:P.EMBED_MAX_CHARS] for v in full]
    shas = [P.view_sha(v) for v in full]
    reuse = {}
    if a.previous and Path(a.previous).exists():
        old = json.loads(Path(a.previous).read_text(encoding="utf-8"))
        if old.get("model") == P.EMBED_MODEL and old.get("max_chars") == P.EMBED_MAX_CHARS:
            old_sha = old.get("input_sha256") or []
            if not old_sha:  # 해시 없이 저장된 이전 형식: 같은 규칙으로 이전 그래프의 표현을 다시 계산
                og = P.load(Path(a.previous).with_name("graph.json"))
                old_sha = [P.view_sha(P.search_view(p)) for p in og["propositions"]]
            reuse = dict(zip(old_sha, old["vectors"]))
    need = [i for i, s in enumerate(shas) if s not in reuse]
    vecs = [reuse.get(s) for s in shas]
    client = SolarClient(cfg, allow_live=a.allow_live) if need else None
    for k in range(0, len(need), a.batch):
        chunk = need[k:k + a.batch]
        out = client.embed(P.EMBED_MODEL, [views[i] for i in chunk], time.monotonic() + 120, 110.0, [])
        for i, v in zip(chunk, out):
            vecs[i] = v
        print(f"embedded {k + len(chunk)}/{len(need)}", flush=True)
    assert all(v is not None for v in vecs)
    dst = Path(cfg.data_dir) / "pkg" / "vectors.json"
    dst.write_text(json.dumps({
        "model": P.EMBED_MODEL, "max_chars": P.EMBED_MAX_CHARS,
        "graph_sha256": hashlib.sha256(gpath.read_bytes()).hexdigest(),
        "ids": ids, "input_sha256": shas,
        "truncated": [i for i, v in zip(ids, full) if len(v) > P.EMBED_MAX_CHARS],
        "reused": len(ids) - len(need), "embedded": len(need), "vectors": vecs}), encoding="utf-8")
    print("saved", dst, "reused", len(ids) - len(need), "embedded", len(need),
          "ledger", client.ledger.snapshot()["settled_usd"] if client else 0)


if __name__ == "__main__":
    main()
