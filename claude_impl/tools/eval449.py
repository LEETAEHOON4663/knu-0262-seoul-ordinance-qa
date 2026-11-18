"""평가 실행·채점 도구. 자동으로 실행되지 않으며 이 구현에서 아직 실행하지 않았다.

  # 1) 실행: 질문만 서버에 보낸다(정답 파일을 읽지 않음). 출력 폴더는 새 폴더여야 한다.
  python tools/eval449.py run --url http://127.0.0.1:8000/ask \
      --questions questions.json --out results/RUN-NAME [--limit N]

  # 2) 채점: 원본 check.py의 score_citation을 수정 없이 불러 쓴다.
  python tools/eval449.py score --responses results/RUN-NAME/responses.jsonl \
      --gold gold.json --total 449

questions.json: [{"id": "...", "question": "..."}] (id는 기록용, 서버로 보내지 않음)
gold.json     : [{"id": "...", "gold": [[대안, ...], ...]}]  (빈 목록 = 근거 없음 문항)
                평가자 전용 scores.json에서 이 형식으로 투영해 쓴다(구현 입력과 분리).
주점수 = 문항별 부분점수 합 / total. 형식통과율·완전점수 비율과 다르다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lawqa.selfcheck import load_original_check  # noqa: E402

FIELDS = {"answer": str, "citation": list, "source_url": str, "effective_date": str}


def _post(url, q):
    data = json.dumps({"question": q["question"]}, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    t0 = time.time()
    status, body, err = None, None, None
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            status, body = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = json.loads(e.read())
        except ValueError:
            body = None
    except Exception as e:  # noqa: BLE001
        err = repr(e)
    return {"id": q["id"], "question": q["question"], "http_status": status,
            "seconds": round(time.time() - t0, 3), "response": body, "error": err}


def cmd_run(a):
    out = Path(a.out)
    if out.exists():
        sys.exit("refuse: output folder exists (no overwrite)")
    qs = json.loads(Path(a.questions).read_text(encoding="utf-8"))
    if a.limit:
        qs = qs[: a.limit]
    out.mkdir(parents=True)
    path = out / "responses.jsonl"
    from concurrent.futures import ThreadPoolExecutor
    with path.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=a.workers) as pool:
        # 결과는 입력 순서대로 기록한다. 병렬 실행 시 seconds에는 서버 대기 시간이 포함된다.
        for rec in pool.map(lambda q: _post(a.url, q), qs):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            body = rec["response"]
            print(rec["id"], rec["http_status"], rec["seconds"],
                  body.get("citation") if isinstance(body, dict) else rec["error"], flush=True)
    print("saved", path, "count", len(qs), "workers", a.workers)


def cmd_iao(a):
    """IAO 세 평가기 입력 형식(responses.json)으로 변환. 운영 기록은 실제 HTTP 결과를 그대로 넣는다."""
    rows = [json.loads(ln) for ln in Path(a.responses).read_text(encoding="utf-8").splitlines() if ln.strip()]
    out = []
    for r in rows:
        ok = r["error"] is None and r["http_status"] == 200 and isinstance(r["response"], dict) \
            and "answer" in r["response"]
        resp = r["response"] if ok else {"answer": "", "citation": [], "source_url": "", "effective_date": ""}
        err = None if ok else (r["error"] or f"http_{r['http_status']}:" + json.dumps(r["response"], ensure_ascii=False)[:200])
        out.append({"id": r["id"], "question": r["question"], "response": resp, "http_status": r["http_status"],
                    "http_seconds": r["seconds"], "operational_error": err})
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", a.out, len(out))


def valid_shape(resp) -> bool:
    if not isinstance(resp, dict) or set(resp) != set(FIELDS):
        return False
    if not all(isinstance(resp[k], t) for k, t in FIELDS.items()):
        return False
    if not all(isinstance(c, str) for c in resp["citation"]):
        return False
    is_abstain = resp == {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}
    return is_abstain or (resp["source_url"].startswith("http")
                          and re.fullmatch(r"\d{4}-\d{2}-\d{2}", resp["effective_date"]) is not None)


def cmd_score(a):
    check = load_original_check()
    gold = {g["id"]: g["gold"] for g in json.loads(Path(a.gold).read_text(encoding="utf-8"))}
    rows = [json.loads(ln) for ln in Path(a.responses).read_text(encoding="utf-8").splitlines() if ln.strip()]
    stats = {"with_gold": [0, 0.0], "no_gold": [0, 0.0], "errors": 0, "timeouts": 0,
             "shape_fail": 0, "over_abstain": 0}
    seen = set()
    for r in rows:
        g = gold[r["id"]]
        seen.add(r["id"])
        resp = r["response"]
        failed = r["error"] is not None or r["http_status"] != 200
        stats["errors"] += failed
        stats["timeouts"] += (r["seconds"] > 90)
        ok_shape = (not failed) and valid_shape(resp)
        stats["shape_fail"] += (not failed and not ok_shape)
        if failed or r["seconds"] > 90 or not ok_shape:
            sc = 0.0
        elif not g:
            sc = 1.0 if resp["answer"].startswith("확인 불가") and resp["citation"] == [] else 0.0
        else:
            sc = check.score_citation(resp["citation"], g)
            stats["over_abstain"] += resp["citation"] == []
        key = "with_gold" if g else "no_gold"
        stats[key][0] += 1
        stats[key][1] += sc
    total = a.total or len(gold)
    s = stats["with_gold"][1] + stats["no_gold"][1]
    print(json.dumps({
        "main_score_sum_over_total": round(s / total, 4), "total": total,
        "executed": len(rows), "not_run": total - len(seen),
        "with_gold": {"n": stats["with_gold"][0], "mean": round(stats["with_gold"][1] / max(stats["with_gold"][0], 1), 4)},
        "no_gold": {"n": stats["no_gold"][0], "mean": round(stats["no_gold"][1] / max(stats["no_gold"][0], 1), 4)},
        "errors": stats["errors"], "timeouts_over_90s": stats["timeouts"],
        "shape_fail": stats["shape_fail"], "over_abstain_with_gold": stats["over_abstain"],
        "note": "부분 실행이면 main_score는 전체 점수가 아니다(not_run 확인).",
    }, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--questions", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--limit", type=int)
    r.add_argument("--workers", type=int, default=1)
    i = sub.add_parser("iao")
    i.add_argument("--responses", required=True)
    i.add_argument("--out", required=True)
    s = sub.add_parser("score")
    s.add_argument("--responses", required=True)
    s.add_argument("--gold", required=True)
    s.add_argument("--total", type=int)
    a = ap.parse_args()
    {"run": cmd_run, "score": cmd_score, "iao": cmd_iao}[a.cmd](a)


if __name__ == "__main__":
    main()
