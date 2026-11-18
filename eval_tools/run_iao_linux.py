"""IAO 세 평가기를 Linux에서 실행하는 호환 래퍼(평가 패키지 파일은 수정하지 않음).

evaluate.protocol()은 기준 파일 경로를 str(Path)로 해시한다. 원 패키지는 Windows에서 만들어져
경로 구분자가 '\\'이므로, Linux의 '/' 경로로는 동봉된 기존 판정(decisions.json)과 프로토콜이
달라진다. 같은 파일 내용·같은 해시 함수로 Windows 표기 경로만 재현한다. 판정 기준은 그대로다.
"""
import functools, hashlib, sys
from pathlib import Path, PureWindowsPath

PKG = Path(__file__).resolve().parent / "IAO_three_evaluators"
sys.path.insert(0, str(PKG))
sys.argv[0] = str(PKG / "evaluate.py")
import evaluate as E  # noqa: E402


@functools.lru_cache(maxsize=1)
def protocol():
    ROOT, KIT = E.ROOT, E.KIT
    files = [PKG / "evaluate.py", ROOT / "review_policy.py", KIT / "check.py",
             KIT / "semantic_evaluation/run_semantic_evaluation.py",
             ROOT / "merged_evaluation_20261108/evaluate_merged_answers.py",
             ROOT / "evaluation_triple449_v2_20261111/strict_eval.py",
             ROOT / "evaluation_triple449_v2_20261111/common_eval.py"]
    return E.digest({"e2_prompt": E.helper.PROMPT + E.ev.PROMPT_ADDITION, "e2_schema": E.helper.SCHEMA,
                     "blind": E.BLIND, "third": E.SUFFIX, "e3_prompt": E.se.PROMPT, "e3_review": E.se.REVIEW,
                     "corpus": E.CORPUS.sha,
                     "criteria_files": {str(PureWindowsPath(*p.relative_to(ROOT).parts)): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in files}})


E.protocol = protocol
if __name__ == "__main__":
    E.main()
