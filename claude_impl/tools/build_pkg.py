"""PKG 명제 그래프를 결정적으로 다시 빌드한다(API 호출 없음).

  python3 tools/build_pkg.py --archive-tag pkg3
기존 data/pkg/graph.json·vectors.json을 data/pkg/archive/<tag>/로 복사한 뒤 새 그래프를 저장하고,
이전 그래프와의 차이(명제 추가·삭제, 간선 변화)를 출력한다. 벡터는 tools/embed_pkg.py로 갱신한다.
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawqa import pkg_graph as P  # noqa: E402
from lawqa.corpus import Corpus  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--archive-tag", required=True)
    ap.add_argument("--w1", action="store_true", help="W1 외부 참조 표시 보완(시험용, 최종 PKG11.1은 끔)")
    a = ap.parse_args()
    pkg = Path(a.data_dir) / "pkg"
    gpath = pkg / "graph.json"
    old = P.load(gpath) if gpath.exists() else {"propositions": [], "edges": []}
    arch = pkg / "archive" / a.archive_tag
    if arch.exists():
        raise SystemExit(f"archive exists: {arch}")
    arch.mkdir(parents=True)
    for f in ("graph.json", "vectors.json"):
        if (pkg / f).exists():
            shutil.copy2(pkg / f, arch / f)
    corpus = Corpus(a.data_dir)
    g = P.build(corpus, w1=a.w1)
    P.save(g, gpath)
    oid = {p["id"] for p in old["propositions"]}
    nid = {p["id"] for p in g["propositions"]}
    ek = lambda e: (e["from"], e["to"], e["role"])  # noqa: E731
    oe, ne = {ek(e) for e in old["edges"]}, {ek(e) for e in g["edges"]}
    units = {p["unit"] for p in g["propositions"]}
    print(json.dumps({
        "propositions": len(nid), "edges": len(ne), "units_covered": len(units), "units_total": len(corpus.units),
        "kinds": collections.Counter(p["kind"] for p in g["propositions"]),
        "added_props": sorted(nid - oid), "removed_props": sorted(oid - nid),
        "added_edges": len(ne - oe), "removed_edges": sorted(map(list, oe - ne)),
        "archived_to": str(arch),
    }, ensure_ascii=False, indent=1, default=dict))


if __name__ == "__main__":
    main()
