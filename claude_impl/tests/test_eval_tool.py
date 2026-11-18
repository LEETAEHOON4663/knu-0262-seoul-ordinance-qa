"""채점 도구가 원본 check.py 규칙을 따르는지(가짜 응답·가짜 정답) 확인."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import tmpdir

ROOT = Path(__file__).resolve().parent.parent
LAW = "서울특별시 주차장 설치 및 관리 조례"


class EvalTool(unittest.TestCase):
    def test_score(self):
        d = Path(tmpdir())
        ok = {"answer": "a", "citation": [f"{LAW} 제6조"], "source_url": "https://x", "effective_date": "2026-10-01"}
        unk = {"answer": "확인 불가", "citation": [], "source_url": "", "effective_date": ""}
        rows = [
            {"id": "1", "http_status": 200, "seconds": 3, "response": ok, "error": None},   # 항 누락 0.5
            {"id": "2", "http_status": 200, "seconds": 3, "response": unk, "error": None},  # 기권 정답 1
            {"id": "3", "http_status": 504, "seconds": 3, "response": {"error": "t"}, "error": None},  # 0
            {"id": "4", "http_status": 200, "seconds": 3, "response": unk, "error": None},  # 과유보 0
        ]
        gold = [{"id": "1", "gold": [[f"{LAW} 제6조 제1항"]]}, {"id": "2", "gold": []},
                {"id": "3", "gold": []}, {"id": "4", "gold": [[f"{LAW} 제6조 제1항"]]},
                {"id": "5", "gold": []}]
        (d / "r.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        (d / "g.json").write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "eval449.py"), "score",
                              "--responses", str(d / "r.jsonl"), "--gold", str(d / "g.json")],
                             capture_output=True, text=True, check=True).stdout
        res = json.loads(out)
        self.assertEqual(res["total"], 5)
        self.assertEqual(res["not_run"], 1)
        self.assertAlmostEqual(res["main_score_sum_over_total"], 1.5 / 5)
        self.assertEqual(res["over_abstain_with_gold"], 1)
        self.assertEqual(res["errors"], 1)

    def test_run_refuses_existing_output(self):
        d = tmpdir()
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "eval449.py"), "run", "--url",
                            "http://127.0.0.1:9/ask", "--questions", "x.json", "--out", d],
                           capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refuse", p.stderr)


if __name__ == "__main__":
    unittest.main()
