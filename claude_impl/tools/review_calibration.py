"""검증기 보정 진단: 저장된 실행 기록의 답변 초안에 현재 검증 단계만 다시 실행한다.

답변 생성은 다시 하지 않는다(Mini4 검증 호출만). 정답은 결과 집계에만 쓴다.
  python3 tools/review_calibration.py --traces runs/FULL449/runtime_v6/traces \
      --responses runs/FULL449/v6/responses.jsonl --gold runs/FULL449/gold.json \
      --config runs/FULL449/calib.json --allow-live
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawqa import contracts, prompts  # noqa: E402
from lawqa.config import Config  # noqa: E402
from lawqa.llm import CallError, SolarClient  # noqa: E402
from lawqa.pipeline import Pipeline  # noqa: E402
from lawqa.search import Context  # noqa: E402
from lawqa.selfcheck import load_original_check  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traces", required=True)
    ap.add_argument("--responses", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--allow-live", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cfg = Config.load(a.config)
    pipe = Pipeline(cfg, allow_live=a.allow_live)
    check = load_original_check()
    gold = {g["id"]: g["gold"] for g in json.loads(Path(a.gold).read_text(encoding="utf-8"))}
    resp = {}
    for ln in Path(a.responses).read_text(encoding="utf-8").splitlines():
        r = json.loads(ln)
        resp[r["question"]] = r
    traces = {}
    for f in sorted(glob.glob(str(Path(a.traces) / "*.jsonl"))):
        for ln in open(f, encoding="utf-8"):
            t = json.loads(ln)
            traces[t["question"]] = t
    jobs = []
    for q, t in traces.items():
        d = t.get("draft")
        if not d or d.get("decision") != "ANSWER" or q not in resp:
            continue
        r = resp[q]
        g = gold[r["id"]]
        # 정답 여부: 근거 있음 문항은 최종 응답 점수, 근거 없음은 '답했다'는 것 자체가 오답.
        final = r["response"] if isinstance(r["response"], dict) else {}
        label = ("answerable_ok" if g and check.score_citation(final.get("citation", []), g) == 1
                 else "answerable_partial" if g else "trap_answered")
        jobs.append((q, t, d, label))

    def review(job):
        q, t, d, label = job
        aliases = t["retrieval"]["aliases"]
        units = [pipe.corpus.by_id[u] for u in aliases.values()]
        ctx = Context(sorted(units, key=lambda u: u.order), dict(aliases))
        evidence = ctx.payload()
        tr = {"calls": [], "recoveries": []}
        state = {"repair_used": False}
        try:
            issues = pipe._review(q, evidence, ctx, d, time.monotonic() + 60, tr, state, "review")
        except (CallError, contracts.ContractError) as e:
            return label, None, str(e)
        return label, issues, None

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        results = list(pool.map(review, jobs))
    tally = collections.Counter()
    kinds = collections.Counter()
    for label, issues, err in results:
        if err or issues is None:
            tally[(label, "error")] += 1
            continue
        sub = [i for i in issues if i["kind"] in ("unanswered_core", "unsupported_claim", "contradiction")]
        tally[(label, "substantive" if sub else "minor_only" if issues else "clean")] += 1
        for i in issues:
            kinds[(label, i["kind"])] += 1
    print(json.dumps({"n": len(results),
                      "tally": {f"{k[0]}|{k[1]}": v for k, v in sorted(tally.items())},
                      "issue_kinds": {f"{k[0]}|{k[1]}": v for k, v in sorted(kinds.items())},
                      "ledger": pipe.client.ledger.snapshot()["settled_usd"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
