"""설정. 기본값은 유료 호출이 불가능한 offline 모드다."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# R9 provider 계약의 단가(USD / 1M tokens: 입력, 출력). 실제 청구액과 대조된 값이 아니다.
DEFAULT_RATES = {
    "solar-pro4": (0.30, 1.20),
    "solar-mini4": (0.10, 0.40),
    "embedding": (0.02, 0.0),
}


@dataclass(frozen=True)
class Config:
    mode: str = "offline"  # offline | replay | live
    data_dir: str = str(ROOT / "data")
    runtime_dir: str = str(ROOT / "runtime")
    api_key_file: str | None = None
    budget_cap_usd: float = 0.0  # 0이면 live 불가
    vectors_file: str | None = None  # 서버 vectors.json (선택)
    vectors_sha256: str | None = "bbb621d36c1bc6129c585ca90b3eb9b49ce300cb1bef12eb74536939484289ed"
    # IA 패키지 트리 노드 벡터(선택): tree_retrieval_20261109 폴더
    node_vectors_dir: str | None = None
    node_catalog_sha256: str | None = "e81ff88d3e5f56643f0cde1fcd75a3c009a54f02cb4074658de52c803c89b69c"
    node_vectors_sha256: str | None = "20d4eaa3a1a6c06b598227fb7ee8d46ad0f58c55b35576af4d6eccf8ea88836b"
    host: str = "127.0.0.1"
    port: int = 8000
    max_concurrent_requests: int = 1
    # HTTP 90초 안쪽 내부 마감: 90초 − 응답 직렬화·로컬 전송 여유 3초(임의 상한이 아니라 여유분)
    request_deadline_seconds: float = 87.0
    max_question_chars: int = 2000
    # 제출 모드: 운영 오류·대기 초과·잘못된 입력·예상 못 한 예외도 HTTP 200 + 정확한 기권 객체로 응답한다.
    # 대회 공지(오류 응답은 0점, '확인 불가'가 0점보다 낫다)에 맞춘 것. 원인은 trace와 /health에 남긴다.
    submission_abstain_on_error: bool = False
    answer_model: str = "solar-pro4"
    plan_model: str = "solar-pro4"
    review_model: str = "solar-mini4"
    pipeline_profile: str = "v8"  # v8 | pkg
    pkg_accept_external_correction_abstain: bool = True  # G-09(PKG8): 외부 의존이 그래프로 확인되면 교정 기권 수용
    pkg_select_max_ids: int = 0
    pkg_render_external_note: bool = False  # PKG11 후보 A: 외부 법령 목록 참조 구간을 생성·검토 원문 note로 전달
    pkg_render_designation_note: bool = False  # PKG11 후보 A-지정(시험용)
    pkg_render_definition_note: bool = False   # PKG11 후보 A-정의(시험용)
    pkg_render_junyong_note: bool = False      # PKG11 후보 C(시험용)
    pkg_external_list_check: bool = False      # PKG11 후보 B(시험용): 외부 목록 명제를 쓴 답 요청을 교정에 확인 요청  # 요청당 선택 id 상한(0=끔). PKG8의 16은 정당한 넓은 선택까지 오류로 만들어 PKG9에서 끔
    pkg_external_dependency_check: bool = False  # PKG12: 답변 후 외부 의존 확인(점검+반대 확인 일치 시 기권)
    pkg_render_outside_note: bool = False        # W3: 외부 법령에 맡긴 조각 표시를 생성·검토·교정 원문에 붙임
    pkg_generate_external_rule: bool = False     # W2: 생성 지시문 DIRECT/EXTERNAL 경계 명시(외부 목록·정의 소속 판정)
    pkg_extdep_imported_terms: bool = False      # PKG12 B: 질문의 하위 종류 용어 → 외부 정의 명제도 검사 대상
    pkg_abstention_recovery: bool = False        # PKG12 C: 핵심 근거 부족 기권을 위치 확인·재생성·검토로 복구
    pkg_accept_correction_abstain: bool = False  # F01 규칙(핵심 미응답 지적 + 원문 부재 판정). PKG5 실측 순손실로 기본 끔
    pkg_candidates: int = 40  # 명제 후보 수: 449 오프라인 진단(정답 근거 포함 0.994)으로 정함
    query_planning: bool = True
    review: bool = True
    rereview: bool = False  # 재검증 거절 시 기권(실측상 순손실이라 기본 off)
    plan_max_tokens: int = 400
    select_max_tokens: int = 600
    answer_max_tokens: int = 1200
    review_max_tokens: int = 800
    retrieval_depth: int = 40
    context_budget_chars: int = 10000
    expand_article_chars: int = 2500
    min_seconds_for_review: float = 15.0
    stage_caps: dict = field(default_factory=lambda: {
        "plan": 15.0, "embed": 10.0, "select": 30.0, "generate": 30.0, "review": 30.0, "rereview": 30.0, "correct": 30.0})
    rates: dict = field(default_factory=lambda: dict(DEFAULT_RATES))
    trace: bool = True

    def __post_init__(self):
        if self.mode not in ("offline", "replay", "live"):
            raise ValueError("invalid_mode")
        if self.pipeline_profile not in ("v8", "pkg"):
            raise ValueError("invalid_pipeline_profile")
        if not 0 < self.request_deadline_seconds <= 88:
            raise ValueError("deadline_must_leave_margin_under_90s")
        if self.budget_cap_usd < 0:
            raise ValueError("invalid_budget_cap")
        for m in (self.answer_model, self.plan_model, self.review_model):
            if m not in self.rates:
                raise ValueError("model_without_rate:" + m)

    @classmethod
    def load(cls, path: str | None = None, **overrides) -> "Config":
        raw = {}
        if path:
            p = Path(path)
            raw = json.loads(p.read_text(encoding="utf-8"))
            unknown = set(raw) - {f.name for f in fields(cls)}
            if unknown:
                raise ValueError("unknown_config_fields:" + ",".join(sorted(unknown)))
            for k in ("data_dir", "runtime_dir", "api_key_file", "vectors_file", "node_vectors_dir"):
                if raw.get(k) and not Path(raw[k]).expanduser().is_absolute():
                    raw[k] = str((p.parent / raw[k]).resolve())
        raw.update({k: v for k, v in overrides.items() if v is not None})
        if "rates" in raw:
            raw["rates"] = {k: tuple(v) for k, v in raw["rates"].items()}
        return cls(**raw)
