# PKG11.1 — 명제 기반 지식그래프 RAG 서울시 조례 Q&A (최종 패키지)

공공 법령 Q&A AI 경진대회 · 경북대 0262팀의 채점용 엔진(`POST /ask`)입니다. 대회 제출용 실행 방법은 아래 **0절**에 있습니다.

2026-11-17. 고정된 서울시 조례 6종과 별표만으로 `POST /ask`에 답하는 구현의 최종본(PKG11.1)입니다. 실측 결과, 응답, 모델 응답 캐시를 함께 담았습니다.

**API 없이 같은 결과를 다시 만들 수 있습니다.**
- 저장된 모델 응답 캐시로 파이프라인을 다시 돌리면(replay) 449, 증강 100, HOLD150의 최종 응답이 글자 단위로 같게 나옵니다. 원본 check.py 점수도 같습니다.
- 새 API 실행도 같은 코드·자산·설정으로 할 수 있습니다. 다만 모델 출력이 실행마다 바뀌므로 점수가 정확히 같지는 않을 수 있습니다.

## 0. 대회 제출용 실행 (채점 API)

이 저장소는 PKG11.1에 **제출용 보완**만 더한 판입니다. 답변을 만드는 경로(질문 분해·검색·선택·생성·검토·교정)와 데이터는 PKG11.1과 같습니다.

| 보완 | 내용 |
|---|---|
| 오류도 정해진 JSON으로 | 설정 `submission_abstain_on_error: true`이면, `POST /ask`가 운영 오류(API 실패·429·시간 초과·대기 초과)와 잘못된 입력, 예상 못 한 예외에도 HTTP 200과 `{"answer":"확인 불가","citation":[],"source_url":"","effective_date":""}`를 돌려줍니다. 원인은 `runtime/submit/traces`와 `GET /health`의 `fallback_abstain`에 남습니다 |
| 외부 접속 | 제출 설정은 `host` 0.0.0.0, `port` 8000. `--host`와 `--port`로 바꿀 수 있습니다 |
| 예산 상한 | 제출 설정은 10달러입니다(실행 폴더 누적, 문항당 약 0.003~0.0034달러) |
| 키 | 설정의 `api_key_file` → 환경변수 `UPSTAGE_API_KEY` → `claude_impl/.env` → 루트 `.env` 순서로 찾습니다. `.env`는 올리지 않습니다(`.gitignore`) |

Python 3.10 이상, 표준 라이브러리만 씁니다(설치할 패키지 없음).

```bash
cp .env.example .env    # UPSTAGE_API_KEY= 뒤에 키 입력
cd claude_impl
python3 -X utf8 -m lawqa serve --config configs/submit_pkg11_1.json --allow-live
```

- 주소는 `POST http://<서버>:8000/ask`, 본문은 `{"question": "..."}`입니다.
- 상태는 `GET /health`에서 봅니다.
- 오프라인 검사: `cd claude_impl && PYTHONPATH=tests:. python3 -X utf8 -m unittest discover -s tests`

## 1. 결과 요약

check.py 주지표는 문항별 인용·기권 부분점수의 평균입니다. 통과율이나 내용 정확도가 아니며, 불필요한 인용을 더해도 감점하지 않습니다.

| 세트 | 구성 | 주지표 | 근거 있음 | 근거 없음 | 과유보 | 함정에 답함 | 부분 인용 | 운영 오류 | 지연 중앙값 / 최대 |
|---|---|---|---|---|---|---|---|---|---|
| 449 (개발 자료) | PKG10 실제 실행 | 0.9608 | 0.968 | 0.945 | 6 | 8 | 12 | 0 | 9.7 / 26.1초 |
| | PKG11 실제 실행 | 0.9603 | 0.968 | 0.945 | 6 | 8 | 13 | 0 | 8.3 / 33.7초 |
| | **PKG11.1 (캐시 재생)** | **0.9625** | 0.968 | 0.952 | 6 | 7 | 13 | 0 | — |
| 증강 100 (일부 개발에 사용) | PKG10 실제 실행 | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 | 0 | 11.0 / 20.2초 |
| | PKG11 실제 실행 | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 | 0 | 9.2 / 17.6초 |
| | **PKG11.1 (캐시 재생)** | **1.000** | 1.000 | 1.000 | 0 | 0 | 0 | 0 | — |
| HOLD150 (처음 보는 세트) | PKG11 실제 실행 | 1.000 | 1.000 | 1.000 | 0 | 0 | 0 | 0 | 12.3 / 45.9초 |
| | **PKG11.1 (캐시 재생)** | **1.000** | 1.000 | 1.000 | 0 | 0 | 0 | 0 | — |

- **PKG11.1 점수의 성격:**
  - PKG11 실제 실행에서 받은 모델 응답을 PKG11.1 코드로 다시 돌린 결과입니다.
  - 449에서는 함정 문항 R3-REAL-G-07 하나만 바뀌었습니다(답함 → 기권).
  - PKG11.1을 처음부터 실제로 돌린 실행은 없습니다.
- **변동:**
  - 질문 분해·근거 선택을 고정해도 449 주지표는 실행마다 ±0.012(95%) 움직입니다.
  - 그래서 PKG7(0.9570)부터 PKG11.1까지의 차이는 변동 범위 안입니다.
  - 생성·검토·교정을 새로 부른 3표본(선택 고정)의 449 평균은 0.9639였습니다.
- **자료의 성격:**
  - 449와 증강 100은 실패 사례를 보고 고친 개발 자료라, 점수가 일반화 성능이 아닙니다.
  - HOLD150은 결과를 보기 전에 평가 방법을 고정하고 처음 돌린 세트입니다. 다만 같은 방식(GPT 생성)으로 만든 증강 100과 함께 만점이라, 이 두 세트로는 버전 간 개선을 구별하지 못합니다.
- **내용 평가(E2/E3, PKG11 실제 실행):**
  - 449: E1 완전 422, E2 내용 정답 434(미판정 1), E3 통과 375(미판정 47)입니다.
  - 증강 100: E1 100, E2 100(0), E3 87(13)입니다.
  - 답한 정답 문항 중 E2 확정 오답은 0건입니다. E2/E3는 IAO 세 평가기의 AI 보조 판정이고, 공식 점수나 법률 전문가 검수가 아닙니다.
- **실행 조건:**
  - 로컬 HTTP `/ask`, 동시 4건, 내부 마감 87초입니다.
  - 모델은 Solar Pro4(질문 분해·선택·생성·검토·교정)와 Solar Embedding 2입니다.
  - 1회 생성 비용(로컬 원장 추정)은 449 약 $1.33, 증강 100 약 $0.32, HOLD150 약 $0.40입니다.

## 2. PKG10 대비 바뀐 것

| 버전 | 변경 | 결과 영향 |
|---|---|---|
| PKG11 | 검토 판정 처리 순서 결함 수정(잘못된 주장 번호가 '핵심 미응답' 판정을 지우던 문제) | 저장 실행 4,739건에서 발생 0. 잠재 결함 수정 |
| PKG11 | 그림(주차장 별도 3~6)의 미검토 상태를 생성·검토 입력에 전달 | 해당 자료가 쓰인 질문 0. 잠재 결함 수정 |
| PKG11 | 선택 카드 표시를 원문 인용으로 바꿈('지정 관련 구절', '외부 법령 참조 «구간»'), 지정 표현 누락 2건 보완 | 선택 단계에만 영향. 449 실제 실행 차이는 변동 범위 안 |
| PKG11.1 | 교정 단계 기권의 외부 법령 예외가 EXTERNAL만 받던 것을 EXTERNAL+SILENT로 확장(Pro4는 기권할 때 거의 SILENT를 씀). 외부 참조 판단에 외부 법령 위임 포함. DELEGATED는 받지 않음 | 449 G-07 하나가 기권으로 바뀜(0.9603 → 0.9625) |

- **시험했지만 넣지 않은 것(코드에는 있고 기본 끔):**
  - PKG11 후보 note들
  - PKG12 답변 후 검증(외부 의존 확인, 용어 정의, 기권 복구)
  - W1 그래프 외부 참조 표시 보완(빌드 옵션 `--w1`)
  - W2 생성 지시문 경계
  - W3 외부 참조 표시 전달
- **기록:** 모두 사전에 고정한 기준에서 판정 보류나 기각이었습니다. `claude_impl/RESULTS_PKG.md`와 `claude_impl/design/`에 있습니다.

## 3. 무료 재현 검증 (API 키 필요 없음)

Python 3.10 이상과 표준 라이브러리만 필요합니다(이 PC는 Linux, Python 3.13). Linux/macOS:

```bash
cd lawqa_pkg11_1_final_20261117
python3 -X utf8 verify_package.py          # 전체(약 3~4분)
python3 -X utf8 verify_package.py --quick  # 응답 재생은 세트마다 20문항만
```

Windows PowerShell: `py -3 -X utf8 verify_package.py`

| 검사 | 이 PC 결과 |
|---|---|
| MANIFEST.json의 모든 파일 SHA256 | 일치 |
| 오프라인 검사(가짜 Solar) | 172개 실행, 171개 통과, 1개 건너뜀. 건너뛴 것은 PKG가 쓰지 않는 옛 경로(팀 IA 노드 벡터)로, 그 자료가 있을 때만 돕니다. |
| 명제 그래프 재빌드 = 저장본, 벡터 id·모델·임베딩 입력 해시 = 그래프 | 일치(명제 1,127, 간선 1,406) |
| 캐시 재생 449 → 저장된 PKG11.1 응답 | 449/449 동일, check.py 0.9625 재현 |
| 캐시 재생 증강 100 | 100/100 동일, 1.000 재현 |
| 캐시 재생 HOLD150 | 150/150 동일, 1.000 재현 |

- `ALL_OK: true`가 나오면 패키지가 이 결과를 그대로 재현한 것입니다.
- 재생한 응답은 `verify_output/`에 저장됩니다.
- Windows 대응(원장 파일 잠금 `msvcrt`, `-X utf8`)은 코드에 넣었지만, Windows에서 실제로 돌려 보지는 않았습니다.

## 4. 실제 API로 새로 실행

```bash
cd claude_impl
export UPSTAGE_API_KEY=...        # 또는 configs/live_pkg11_1.json의 api_key_file에 키 파일 경로
python3 -X utf8 -m lawqa serve --config configs/live_pkg11_1.json --allow-live
```

- **주소와 요청:** `http://127.0.0.1:8000/ask`에 `{"question": "..."}`를 보냅니다.
- **응답:** 필드는 `answer`, `citation`, `source_url`, `effective_date`입니다.
- **근거가 없을 때:** `{"answer":"확인 불가","citation":[],"source_url":"","effective_date":""}`를 돌려줍니다.
- **운영 오류:** 통신 오류·시간 초과·예산 초과는 기권으로 바꾸지 않고 502/503/504와 `{"error": 코드}`를 돌려줍니다.
- **상태:** `GET /health`에서 봅니다.
- **예산:** `budget_cap_usd`(2.5)는 실행 폴더(`claude_impl/runtime/pkg11_1_live`)의 누적 예산 상한입니다. 상한에 닿으면 유료 호출을 막고 오류를 돌려줍니다.

평가 실행과 채점은 다른 터미널에서 합니다.

```bash
cd claude_impl
python3 -X utf8 tools/eval449.py run --url http://127.0.0.1:8000/ask --questions ../eval/full449/questions.json --out ../runs_new/full449 --workers 4
python3 -X utf8 tools/eval449.py score --responses ../runs_new/full449/responses.jsonl --gold ../eval/full449/gold.json --total 449
```

- 증강 100: `../eval/aug100/questions100.json`, `../eval/aug100/gold100.json`, `--total 100`
- HOLD150: `../eval/hold150/questions150.json`, `../eval/hold150/gold150.json`, `--total 150`

### 그래프·벡터를 다시 만들 때 (선택)

```bash
python3 -X utf8 tools/build_pkg.py --archive-tag before_rebuild
python3 -X utf8 tools/embed_pkg.py --config configs/live_pkg11_1.json --allow-live --previous data/pkg/archive/before_rebuild/vectors.json
```

- `build_pkg.py`는 그래프를 결정적으로 다시 만듭니다(API 없음). `--w1`을 붙이면 시험용 W1 그래프가 됩니다(최종본은 끔).
- `embed_pkg.py`는 임베딩 입력이 바뀐 명제만 다시 임베딩합니다(현재 자산에서는 0개).

## 5. E2/E3 평가 (선택, 유료)

- 팀의 `IAO_three_evaluators` 패키지를 따로 받아야 합니다(이 패키지에는 없음).
- Linux에서는 `eval_tools/run_iao_linux.py`를 그 패키지 옆에 두고 실행합니다. Windows 경로 표기로 만든 판정 프로토콜 해시를 재현하는 호환 래퍼이며, 평가 기준은 바꾸지 않습니다.
- 응답 변환: `python3 tools/eval449.py iao --responses <responses.jsonl> --out <iao.json>`
- 증강 100은 `--questions ../eval/aug100/audit100.json`, HOLD150은 `../eval/hold150/source/audit150.json`을 씁니다.

## 6. 구성

| 위치 | 내용 |
|---|---|
| `claude_impl/lawqa/` | 구현. PKG 경로: `pkg_pipeline.py`, `pkg_runtime.py`, `pkg_graph.py`, `pkg_prompts.py`, `pkg_verify.py`(PKG12, 기본 끔). HTTP `server.py`, 호출·예산 `llm.py` |
| `claude_impl/data/` | 고정 원문(`corpus.json`, `ordinances.json`), 명제 그래프(`pkg/graph.json`), 명제 벡터(`pkg/vectors.json`) |
| `claude_impl/configs/` | 실제 실행 설정(`live_pkg11_1.json`, 키 없음)과 세트별 캐시 재생 설정 3개 |
| `claude_impl/tests/` | 오프라인 검사 172개 |
| `claude_impl/tools/` | 그래프 빌드·임베딩·HTTP 실행·채점·평가 형식 변환 |
| `claude_impl/RESULTS_PKG.md`, `design/` | 구현·실측 기록, 검토와 반영 기록, 사전 고정 프로토콜 |
| `handoff_r9/evaluation/check.py` | 원본 채점기(수정 없음, SHA256 4fcc48…) |
| `eval/` | 평가 자료(질문·정답). 모델 입력에는 쓰지 않습니다. HOLD150 원본 파일은 `eval/hold150/source/` |
| `replay/` | PKG11 실제 실행에서 받은 모델 응답 캐시(449, 증강 100, HOLD150) |
| `results/` | 세트별 PKG11 실제 실행(응답·점수·실행 설정·trace·E2/E3), PKG11.1 재생 응답·점수, PKG10 응답·점수 |
| `MANIFEST.json`, `verify_package.py` | 파일 해시와 무료 재현 검증 |

## 7. 주의와 알려진 약점

- **검토 모델:** 실행 설정에서 Pro4로 지정합니다(`configs/live_pkg11_1.json`). 코드 기본값은 Mini4입니다.
- **449에 남은 오류(PKG11.1 재생 기준):**
  - 과유보 6건
  - 함정에 답함 7건. 6건이 실제 시민 질문에 가까운 R3-REAL 문항이고, 외부 법령 목록에 달린 금지 행위(G-09, G-10)도 여기에 남아 있습니다.
  - 부분 인용 13건. 예: R3-REAL-P-06은 주장이 제6조①의 적용 범위를 쓰고도 인용에서 빠졌습니다.
- **외부 법령 의존 판단:** 그래프의 외부 참조 표시와 모델의 근거 유형(SILENT/EXTERNAL)에 기대는 정도입니다. 이를 강화한 후보(W1~W3, PKG12)는 이득과 손해가 함께 나와 넣지 않았습니다.
- **API 키는 들어 있지 않습니다.** `UPSTAGE_API_KEY` 환경변수나 키 파일로 넣습니다.
