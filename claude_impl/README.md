# claude_impl: 서울시 조례 Q&A (독립 구현)

BUNDLE-SOLAR R9와 별개로 작성한 구현입니다. 고정 조례 6개와 부속 자료만으로 `POST /ask`에 답합니다.
설계와 R9 대비 차이는 [DESIGN.md](DESIGN.md)에 있습니다.

## 상태 (2026-11-17, 최종 고정: PKG11.1)

| 구분 | 결과 |
|---|---|
| 오프라인 검사 | **172개 통과**. 자료·그래프·검색·계약·결합·HTTP·채점도구 검사이며 가짜 Solar를 씁니다. |
| 최종본 | **PKG11.1**: 명제 기반 지식그래프(`pipeline_profile: "pkg"`, 검토 모델 Pro4)인 PKG11에 교정 기권 외부 예외 수정을 더한 구성입니다. 시험용 플래그(PKG11 후보, PKG12, W2, W3)와 W1 그래프 보완은 모두 끔입니다. |
| 점수 (check.py 주지표) | 449 **0.9625**, 증강 100 **1.000**, 처음 보는 HOLD150 **1.000**입니다. 모두 PKG11 실제 실행의 캐시 재생입니다(PKG11 실제 실행은 449 0.9603, 증강 1.000, HOLD150 1.000). 449와 증강 100은 개발에 쓴 자료입니다. 같은 입력에서도 449 주지표가 ±0.012(95%) 움직이므로, PKG7(0.9570)~PKG11.1 사이 차이는 변동 범위 안입니다. |
| 기준선 | PKG2 0.9375, 원문 단위 V8b 0.895입니다. |
| 상세 | [RESULTS_PKG.md](RESULTS_PKG.md) 맨 앞 "최종 고정" 절 |
| 추가 평가기 | IAO 세 평가기(E1/E2/E3)로 V8과 PKG 버전들을 채점했습니다(RESULTS_449.md, RESULTS_PKG.md). |

오프라인 검사 통과는 출력 계약과 경로가 의도대로 연결됐다는 뜻입니다. 법률 정답 정확도나 점수가 아닙니다.

## 폴더

```
lawqa/corpus.py     ① 동결 원문 525단위, 별표 표 복원
lawqa/graph.py      ① 필수근거 간선(검토 간선 + 같은 조례 참조 + 정의)
lawqa/search.py     ②③ BM25(+선택 dense) RRF, 조 묶음 + 필수근거 문맥
lawqa/prompts.py    작업 단위별 지시문·schema
lawqa/contracts.py  ⑥ 요청 본문·엄격 파서·인용 조립, ⑦ 기권 결정
lawqa/llm.py        ⑧ Solar 호출·예산 원장·캐시·재전송 금지
lawqa/pipeline.py   ⑧ 전체 흐름
lawqa/server.py     HTTP POST /ask
lawqa/selfcheck.py  자료 점검(원본 check.py를 수정 없이 읽기 전용으로 로드)
tools/eval449.py    평가 실행·채점(수동 실행 전용)
data/               corpus.json(sha 66904d…), ordinances.json(sha 17902a…) 동결 사본
tests/              오프라인 검사
```

필요 환경: Python 3.10 이상, 표준 라이브러리만 사용합니다. numpy 등 추가 설치는 필요 없습니다.
원본 `check.py`는 `../handoff_r9/evaluation/check.py`에서 해시(4fcc48…)를 확인한 뒤 읽습니다.

## 오프라인 실행 (비용 없음)

```bash
cd claude_impl
```

```bash
python3 -m lawqa selfcheck
```

```bash
PYTHONPATH=tests:. python3 -m unittest discover -s tests -v
```

```bash
python3 -m lawqa context "공영주차장 주차요금은 얼마인가요?"
```

`context` 명령은 API를 호출하지 않습니다. 원문에서 모델 입력까지의 경로를 보여 줍니다(검색 순위, 포함 이유, 실제 답변 요청 본문).
질의계획과 dense 검색은 빠진 상태입니다.

## 실제 실행에 필요한 조건 (아직 하지 않음)

유료 호출은 아래 네 가지가 모두 갖춰져야 시작됩니다. 하나라도 없으면 운영 오류를 내고 호출하지 않습니다.

1. 설정 `"mode": "live"`
2. 실행 인자 `--allow-live`
3. `api_key_file`: Upstage 키 파일 경로
4. `budget_cap_usd` > 0

**예산 주의**: `runtime/ledger.json`은 이 구현 전용 로컬 원장입니다. 서버의 공유 원장을 보지 못합니다.
인계 시점 공유 차단선 잔여액은 약 USD 4.959입니다(실제 청구액은 조회하지 않음).
`budget_cap_usd`는 그 잔여액 이하로, 실행 직전 공유 원장 상태를 다시 확인한 뒤 정해야 합니다.
R9 live-guard는 닫힌 상태이며 이 구현은 그 상태를 바꾸지 않습니다.

비용 추정(오프라인 계산, 1글자=1토큰·출력 상한을 모두 쓴다고 가정한 상한):

| 항목 | 문항당 |
|---|---|
| 질의계획 | 약 $0.0012 |
| 답변(근거 약 1.6만 자) | 약 $0.0084 |
| 검토(축소 문맥) | 약 $0.0006 |
| 교정(일부 문항) | 약 $0.009 |

교정 비율을 30%로 잡으면 449문항 상한은 약 $5.8로 잔여 예산을 넘습니다.
실제 토큰 수와 출력 길이는 이보다 작을 것으로 보지만 측정하지 않았습니다.
그래서 **20문항 정도의 소규모 실측으로 문항당 실제 비용·지연을 먼저 재고**, 그 결과로 `context_budget_chars`와 전체 실행 여부를 정하는 순서를 권합니다.

dense 검색(선택): 서버 `qa/assets/vectors.json`(sha bbb621…)을 읽기 전용으로 `vectors_file`에 지정합니다.
질의 임베딩 호출이 추가됩니다(문항당 1회, 비용은 미미합니다).
이 파일 형식은 R9 로더 코드로 추정했으며 실제 파일로는 검증하지 않았습니다. 해시가 다르거나 행 수가 525가 아니면 시작을 거부합니다.

```bash
python3 -m lawqa serve --config live.json --allow-live
```

```bash
python3 tools/eval449.py run --url http://127.0.0.1:8000/ask --questions questions.json --out results/CLAUDE-IMPL-PILOT20 --limit 20
```

```bash
python3 tools/eval449.py score --responses results/CLAUDE-IMPL-PILOT20/responses.jsonl --gold gold.json --total 449
```

`questions.json`은 `[{"id","question"}]` 형식의 질문 전용 파일입니다. R6 questions.json(sha f45b73…)의 실제 형식은 확인하지 못했으니, 다르면 이 형식으로 변환해서 씁니다. `gold.json`은 평가자 전용 scores.json을 `{"id","gold"}` 형식으로 투영한 파일입니다.
부분 실행의 점수는 미실행 수와 함께 보고하며, 449개 전체 점수로 부르지 않습니다.

## HTTP 계약

- 입력: `{"question": "..."}`. 다른 필드는 무시하며 추론에 넣지 않습니다.
- 200 답변: `{"answer", "citation", "source_url", "effective_date"}` 네 필드
- 200 기권: `{"answer":"확인 불가","citation":[],"source_url":"","effective_date":""}`
- 운영 오류: 502, 503, 504와 `{"error": 코드}`. 통신 오류·시간 초과·계약 복구 실패·예산 초과는 기권으로 바꾸지 않습니다.
- 내부 마감은 수신 시점부터 85초이며, 대기 시간도 포함합니다.
- 실행 기록: `runtime/traces/*.jsonl`에 검색·모델 출력·결정·비용·시간을 남깁니다(질문 ID 없음).
