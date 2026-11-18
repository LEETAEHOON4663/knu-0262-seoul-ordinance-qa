"""PKG11.1 최종 패키지 무료 검증(API 호출 없음).

  python -X utf8 verify_package.py            # 전체(해시·오프라인 검사·세 세트 응답 재생·재채점)
  python -X utf8 verify_package.py --quick    # 응답 재생을 세트마다 20문항만

검사 항목
  1. MANIFEST.json의 모든 파일 SHA256
  2. 오프라인 검사(unittest, 가짜 Solar)
  3. 명제 그래프 재빌드가 저장본과 같은지, 벡터의 id·모델·임베딩 입력 해시가 그래프와 맞는지
     (참고로 W1 빌드 옵션이 바꾸는 명제 수도 출력)
  4. PKG11 실행의 모델 응답 캐시로 PKG11.1 파이프라인을 다시 돌려(replay) 저장된 PKG11.1 응답과 글자 단위로 같은지
     (449·증강 100·HOLD150)
  5. 재생한 응답을 원본 check.py로 다시 채점해 저장된 점수와 같은지
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMPL = ROOT / "claude_impl"
sys.path.insert(0, str(IMPL))

SETS = (
    ("full449", 449, ROOT / "eval/full449/questions.json", ROOT / "eval/full449/gold.json"),
    ("aug100", 100, ROOT / "eval/aug100/questions100.json", ROOT / "eval/aug100/gold100.json"),
    ("hold150", 150, ROOT / "eval/hold150/questions150.json", ROOT / "eval/hold150/gold150.json"),
)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_manifest():
    m = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = [f for f, h in m["files"].items() if not (ROOT / f).is_file() or sha(ROOT / f) != h]
    return {"files": len(m["files"]), "mismatch_or_missing": bad[:20], "ok": not bad}


def run_tests():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(IMPL / "tests"), str(IMPL)]), PYTHONUTF8="1")
    env.pop("UPSTAGE_API_KEY", None)
    p = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"],
                       cwd=IMPL, env=env, capture_output=True, text=True, encoding="utf-8")
    tail = (p.stderr or p.stdout).strip().splitlines()[-3:]
    return {"ok": p.returncode == 0, "tail": tail}


def check_assets():
    from lawqa import pkg_graph as P
    from lawqa.corpus import Corpus
    from lawqa.pkg_runtime import PropIndex
    corpus = Corpus(IMPL / "data")
    saved = P.load(IMPL / "data/pkg/graph.json")
    rebuilt = P.build(corpus)
    same = rebuilt["propositions"] == saved["propositions"] and rebuilt["edges"] == saved["edges"]
    w1 = {p["id"]: p for p in P.build(corpus, w1=True)["propositions"]}
    w1_changed = sorted(p["id"] for p in saved["propositions"] if w1[p["id"]] != p)
    PropIndex(corpus, IMPL / "data", use_dense=True)  # id·모델·입력 해시가 다르면 예외
    return {"ok": same, "graph_rebuild_identical": same, "propositions": len(saved["propositions"]),
            "edges": len(saved["edges"]), "vectors_match_graph": True, "w1_option_would_change (off)": w1_changed}


def replay(tag: str, questions: Path, stored: Path, limit: int | None):
    from lawqa.config import Config
    from lawqa.pkg_pipeline import PkgPipeline
    cfg = Config.load(str(IMPL / "configs" / f"replay_pkg11_1_{tag}.json"), trace=False)
    pipe = PkgPipeline(cfg)
    qs = json.loads(questions.read_text(encoding="utf-8"))
    want = {json.loads(l)["id"]: json.loads(l) for l in stored.read_text(encoding="utf-8").splitlines() if l.strip()}
    rows, diff = [], []
    for q in qs[:limit] if limit else qs:
        status, sub, tr = pipe.answer(q["question"])
        if status == "ERROR":
            diff.append((q["id"], "error:" + sub["error"]))
            rows.append({"id": q["id"], "question": q["question"], "http_status": sub.get("http_status", 503),
                         "seconds": 0.0, "response": None, "error": sub["error"]})
            continue
        if sub != want[q["id"]]["response"]:
            diff.append((q["id"], "response_differs"))
        rows.append({"id": q["id"], "question": q["question"], "http_status": 200, "seconds": 0.0,
                     "response": sub, "error": None})
    return rows, diff


def rescore(rows, gold: Path, total: int, tag: str):
    out = ROOT / "verify_output" / f"replay_{tag}.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    p = subprocess.run([sys.executable, "-X", "utf8", "tools/eval449.py", "score", "--responses", str(out),
                        "--gold", str(gold), "--total", str(total)], cwd=IMPL, capture_output=True, text=True,
                       encoding="utf-8", check=True)
    return json.loads(p.stdout)["main_score_sum_over_total"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="응답 재생을 세트마다 20문항만")
    a = ap.parse_args()
    limit = 20 if a.quick else None
    report = {"manifest": check_manifest(), "offline_tests": run_tests(), "assets": check_assets()}
    for tag, total, q, g in SETS:
        stored = ROOT / f"results/{tag}/pkg11_1_replay/responses.jsonl"
        rows, diff = replay(tag, q, stored, limit)
        saved = json.loads((stored.parent / "score_check_py.json").read_text(encoding="utf-8"))["main_score_sum_over_total"]
        r = {"replayed": len(rows), "identical_responses": len(rows) - len(diff), "differences": diff[:10]}
        if not limit:
            r["rescored_main"] = rescore(rows, g, total, tag)
            r["saved_main"] = saved
            r["score_identical"] = r["rescored_main"] == saved
        r["ok"] = bool(not diff and (limit or r["score_identical"]))
        report[f"replay_{tag}"] = r
    report["ALL_OK"] = all(v.get("ok") for v in report.values() if isinstance(v, dict))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    sys.exit(0 if report["ALL_OK"] else 1)


if __name__ == "__main__":
    main()
