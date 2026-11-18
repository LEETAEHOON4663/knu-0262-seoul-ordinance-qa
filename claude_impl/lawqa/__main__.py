"""CLI.

  python -m lawqa context "질문"            # API 없이 모델에 전달될 근거·요청 본문 확인
  python -m lawqa selfcheck                 # 자료 해시·라벨·표 렌더링 점검(오프라인)
  python -m lawqa ask "질문" --config F [--allow-live]
  python -m lawqa serve --config F [--allow-live]
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import Config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lawqa")
    ap.add_argument("command", choices=["context", "selfcheck", "ask", "serve"])
    ap.add_argument("question", nargs="?")
    ap.add_argument("--config")
    ap.add_argument("--allow-live", action="store_true",
                    help="mode=live 설정일 때만 실제 유료 호출 허용")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", help="서버 바인딩 주소(예: 0.0.0.0). 설정 파일 값보다 우선")
    a = ap.parse_args(argv)
    cfg = Config.load(a.config, port=a.port, host=a.host)

    if a.command == "selfcheck":
        from .selfcheck import run
        ok = run(cfg)
        sys.exit(0 if ok else 1)

    if a.command == "context":
        from . import contracts
        from .corpus import Corpus
        from .graph import EvidenceGraph
        from .search import Retriever
        c = Corpus(cfg.data_dir)
        r = Retriever(c, EvidenceGraph(c))
        ranked = r.rank(a.question, depth=cfg.retrieval_depth)
        ctx = r.build_context(ranked, cfg.context_budget_chars, cfg.expand_article_chars)
        body = contracts.build_select(a.question, ctx.payload(), cfg.answer_model,
                                      cfg.select_max_tokens)
        print(json.dumps({"note": "offline: 질의계획·밀집검색 없이 원질문 BM25만 사용",
                          "ranked_top10": [(c.by_id[u].label, s) for u, s in ranked[:10]],
                          "context_chars": ctx.trace["chars"],
                          "reasons": {c.by_id[k].label: v for k, v in ctx.trace["reasons"].items()},
                          "select_request_body": body}, ensure_ascii=False, indent=1))
        return

    if cfg.pipeline_profile == "pkg":
        from .pkg_pipeline import PkgPipeline as Pipeline
    else:
        from .pipeline import Pipeline
    pipe = Pipeline(cfg, allow_live=a.allow_live)
    if a.command == "ask":
        status, sub, tr = pipe.answer(a.question)
        print(json.dumps({"status": status, "response": sub, "trace": tr},
                         ensure_ascii=False, indent=1, default=str))
    else:
        from .server import serve
        serve(pipe, cfg)


if __name__ == "__main__":
    main()
