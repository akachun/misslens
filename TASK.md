# MissLens — 작업 목록과 진행 상황

> 다른 세션에서 이어서 작업할 때는 이 문서부터 읽는다. 무엇을 만드는지와 왜 만드는지는 [SERVICE.md](SERVICE.md), 어떻게 만드는지는 [ARCHITECTURE.md](ARCHITECTURE.md)에 있다.

| 항목 | 내용 |
|---|---|
| v1 제출 마감 | 2026-11-06(금) 오전 |
| 현재 단계 | **Phase 2 — 핵심 루프** (Phase 1 완료, `exam.yaml` v2 확정) |
| 마지막 갱신 | 2026-11-04 |

**상태 표기:** `[ ]` 할 일 · `[~]` 진행 중 · `[x]` 완료 · `[-]` 보류/제외(이유 기록) · `⚠` 블로커

---

## 진행 요약

| Phase | 내용 | 목표 시점 | 상태 |
|---|---|---|---|
| 0 | 사전 확인(모델 접근, 임베딩, 오답 10개 분류) | 11/4 저녁 첫 30분 | [x] |
| 1 | 기반(프로젝트 구조, 시험 팩, 설정, ledger) | 11/4 저녁 | [x] |
| 2 | 핵심 루프(Generator, Grader & Diagnostician, CLI) | 11/4 저녁 | [ ] |
| 3 | 자료(Ingest, 하이브리드 검색) + T2-c 완료 기준 확인 | 11/5 오전 | [ ] |
| 4 | Collector(로그인 수집, interrupt②) | 11/5 오후 | [ ] |
| 5 | Supervisor, Planner(interrupt①), Reporter, Streamlit | 11/5 저녁 | [ ] |
| 6 | 테스트 마무리, 문서, 데모 준비, 제출 | 11/6 오전 | [ ] |

**절대 빼지 않는 것:** 세트 풀이 → 리뷰(미끼 포함 재질문) → 개념/해석 진단 → ledger 기록.

---

## Phase 0 — 사전 확인 (블로커 해소)

- [x] **T1-a** 오답 10개 직접 분류: 최근 틀린 문제 10개를 한국어로 다시 풀고 개념/해석으로 나눈 뒤, 놓친 영어 문구를 기록한다 → `tests/fixtures/eval_misses.yaml` (eval 픽스처로 재사용)
  - 결과: 오답 4개(concept 1, uncertain 2, reading 1) + 미끼 3개. 해설을 이미 본 문제라 다시 풀지 않고 학습자가 직접 판정했다. 공부하면서 해설을 보기 전에 기록한 오답을 계속 추가한다
  - `key_constraints`·`violates`는 Claude 초안. `tests/test_eval_fixture.py`가 §7 validator 규칙과 맞는지 검사한다
  - 관찰: m02에서 규칙으로 정한 문구(처음 고른 보기의 `violates`)와 학습자가 놓쳤다고 한 문구가 다르다 → T10 eval에서 일치율로 확인한다
- [x] **T1-b** 엔드포인트 5개 × 모델 4개 조합이 지금 응답하는지 확인: `scripts/probe_models.py` → `data/model_status.json` (ARCHITECTURE §6.2)
  - 엔드포인트: us-east-1 `us.`/`global.`, us-west-2 `us.`/`global.`, ap-northeast-2 `global.` (`apac.`에는 쓸 모델이 없어 제외)
  - 모델: Sonnet 4.6, Haiku 4.5, Sonnet 4.5, Nova 2 Lite (모델 존재 여부는 제공 목록으로 확인 완료)
  - [x] 할당량 초과 상태인 조합을 이 문서의 '확인 대기' 아래에 기록한다 → 2026-11-04 16:20 기준 할당량 초과 없음(20개 조합 모두 응답)
  - 완료 기준: 역할마다 응답하는 조합이 1개 이상 있고, Sonnet 4.6의 구조화 출력이 1회 성공 → 충족(use1-us에서 성공)
  - 공용 모듈: `src/misslens/bedrock.py`(설정 로드, `BEDROCK_ENDPOINTS` 처리, §6.3 오류 분류, D27). T2-b의 `llm.py`가 그대로 쓴다
- [x] **T1-c** bge-m3 다운로드와 로드 확인. 임베딩 차원을 출력한다
  - 결과: 차원 1024, max_seq_length 8192, MPS 사용, 첫 로드(다운로드 포함) 45.6초, 3문장 인코딩 1.5초. 영-한 같은 의미 유사도 0.84, 무관한 문장 0.34 → D17 유지

## Phase 1 — 기반

- [x] **T2** 프로젝트 골격
  - [x] `pyproject.toml`(의존성: langchain, langchain-aws, langchain-community, langgraph, langgraph-checkpoint-sqlite, faiss-cpu, sentence-transformers, rank_bm25, boto3, crawl4ai, streamlit, rich, pydantic, pypdf, pyyaml, python-dotenv, pytest)
  - [x] `.gitignore`(`data/`, `secrets/`, `*.sqlite`, `.env`, 인덱스)
  - [x] pytest 설정(`live` 마커는 기본 실행에서 제외)
- [x] **T2-a** 시험 팩: `exams/ccar-f/exam.yaml`
  - [x] `version`, `exam_language: en`, `learner_language: ko`, `question_format: scenario_mcq_4`, `pass_criteria`, 영역별 비중, 세부 항목 목록과 ID
  - [x] `src/misslens/exam_pack.py`(로드·검증, 프롬프트용 목록 제공)
  - 처음에는 초안(v1)으로 만들고, 같은 날 T2-c에서 시험 가이드 v1.0으로 확정했다(v2)
- [x] **T2-b** 모델 설정: `config/models.yaml` + `src/misslens/llm.py` (ARCHITECTURE §6)
  - [x] 역할 → 기본 모델 ID, 타임아웃(Sonnet 180초, Haiku 60초), 모델 폴백
  - [x] 엔드포인트 5개 순차 전환(`us.` 2곳 → `global.` 3곳), `availability` 표에 없는 조합은 호출하지 않음, `BEDROCK_ENDPOINTS` 환경변수로 순서 덮어쓰기
  - [x] 오류별 처리: 속도 제한은 라우터의 지수 백오프 재시도(최대 5회 시도, D32) 후 전환, 할당량 초과는 즉시 전환, 모델 없음·접근 거부는 '사용 불가' 표시 후 전환, 쿨다운 15분, 역할별 '최근 성공' 기억
  - [x] `model_status.json`에서 할당량 초과로 확인된 조합은 시작부터 쿨다운
  - [x] 호출에 쓰인 엔드포인트와 모델을 로그로 남김
  - [x] 테스트: 가짜 예외로 전환 순서, 쿨다운, 모델 폴백, 전부 실패하는 경우 확인
  - [x] 실호출 스모크 `tests/test_llm_live.py`(router 일반 호출, generator 구조화 출력) 통과
- [x] **T3** ledger(`src/misslens/ledger/`)
  - [x] 테이블 6개 + 인덱스 3개(ARCHITECTURE §5)
  - [x] 세트 제출 트랜잭션 + 상태 가드(이중 제출 방지)
  - [x] 통계 함수: `task_accuracy`(Laplace, 최근 10회, uncertain=오답, invalid 제외), `coverage`, `miss_breakdown`(+ 미끼 기준선), `top_missed_phrases`, `avg_time`, `generation_failures`
  - [x] 테스트: 통계 규칙, invalid 제외(문구 순위 포함), 트랜잭션
  - [x] 리뷰 저장(`record_review`)이 판정 표(`ledger/judge.py`)를 직접 적용(D28)
  - [x] '기준선 대비 보정값' → 보정식·목표값 없이 나란히 표시하기로 결정(D31). 기간별 비교(초기 3일 vs 후기 3일)는 T8 Reporter에서 기간 인자를 추가한다

## Phase 2 — 핵심 루프 (CLI)

- [ ] **T4** Generator(`agents/generator/`)
  - [ ] `select_tasks`: 범위 먼저(영역 비중 순) → 가중 무작위(같은 항목은 한 번만) + 이번 세션의 실패 항목 제외
  - [ ] 문제 생성(Sonnet 4.6, `Question` 스키마, 이중 언어, 샘플 문제를 문체 예시로)
  - [ ] Pydantic validator(`violates`와 `key_constraints`의 정합성)
  - [ ] 근거 검증(Haiku 4.5: 근거·조건·다른 정답 여지)
  - [ ] 재생성 규칙(최초 1 + 2, 3회 실패 시 다음 항목, 2항목 연속 실패 시 중단)
  - [ ] 순차 스트리밍 큐(백그라운드 스레드, 예외가 나면 `failed`로 기록)
  - [ ] 테스트(GenericFakeChatModel)
- [ ] **T5** Grader & Diagnostician(`agents/grader/`)
  - [ ] 세트 제출 채점·저장(정답과 해설은 리뷰 전까지 숨김)
  - [ ] 재질문 폼 구성: 오답 + 미끼(약 30%, 최소 2개), 보기 섞기 + 대응표, 확신/추측
  - [ ] 판정 표(reading/uncertain/concept), `missed_phrase` = 처음 고른 오답의 `violates`, 미끼 기준선
  - [ ] 언어가 같은 시험이면 재질문 생략 분기
  - [ ] 오답 해설(Sonnet 4.6), '문제 이상'(invalid) 처리
  - [ ] 테스트
- [ ] **CLI**(`cli/`): 세트 생성 → 풀이(시간 기록) → 제출 → 리뷰 폼 → 진단 출력
  - 완료 기준: CLI에서 5문제 세트를 풀고 리뷰하면 attempts에 `miss_type`, `missed_phrase`, `is_decoy`가 기록된다

## Phase 3 — 자료와 검색

- [ ] **T6** Ingest(`agents/ingest/`)
  - [ ] 업로드(PDF/MD/TXT/HTML)·붙여넣기 → 청크 분할
  - [ ] 세부 항목 태깅(Haiku 4.5, 묶음 호출, 'unassigned' 허용, `materials`에 캐시)
- [ ] **T6-a** 검색(`retrieval/`)
  - [ ] bge-m3 + FAISS(시험별 디렉터리), 프로세스당 1회 로드
  - [ ] BM25(한국어 전처리) + EnsembleRetriever(RRF 0.5/0.5) + task_id 필터
  - [ ] 수집 본문은 '참고 자료' 블록으로 감싸기(프롬프트 주입 방어)
  - [ ] 테스트: 정확한 시험 용어가 들어간 문서가 상위 k에 포함되는지
- [~] **T2-c** 첫 실제 세트 전에 `exam.yaml` 확정: 공식 가이드로 세부 항목 30개(추정)를 확정하고 `version`을 올린다
  - [x] 시험 가이드 v1.0으로 확정 → version 2(final). 세부 항목 30개(ID 변화 없음, 2.5 제목만 수정), 공식 시나리오 6개, 출제 제외 주제 16개 추가. Domain 1은 공식 PDF, Domain 2~5 제목은 가이드를 그대로 옮긴 제3자 사이트(ccar-f.17labs.ai)로 확인
  - [x] 실제 시험에 복수 정답 문항이 있음을 확인 → v1은 단일 정답만(D30)
  - 완료 기준: 자료를 넣으면 해당 세부 항목의 문제가 근거 검증을 통과한다

## Phase 4 — Collector

- [ ] **T7** Collector(`agents/collector/`) — 3시간 안에 끝나지 않으면 멈추고 PDF/HTML 업로드로 대체한다
  - [ ] `collect login <site>`(Crawl4AI 프로필, 사용자가 직접 로그인, `secrets/profiles/<site>/`, 권한 0700)
  - [ ] `exams/ccar-f/sources.yaml`의 URL만 headless로 수집, Markdown 추출, `content_hash`로 중복 제거
  - [ ] 만료 감지(로그인 URL로 리다이렉트, 401/403) → **interrupt②** 로그인 대기 → 같은 URL부터 재개
  - [ ] 안전 규칙(읽기 전용, 2초 간격, 도메인 제한), 본문이 비었으면 실패 안내
  - [ ] ⚠ 사내 자료는 반입 승인 전에는 수집하지 않는다
  - [ ] 테스트(가짜 수집 결과)

## Phase 5 — 라우팅, 계획, 현황, UI

- [ ] **T8** Supervisor·Planner·Reporter
  - [ ] Supervisor: intent가 지정된 입력은 조건 분기, 자유 발화는 Haiku로 {intent, domain, count} 추출, 저신뢰면 안내 후 종료
  - [ ] Planner: 계획 초안 → **interrupt①** 승인·수정 → 세트 생성 요청
  - [ ] Reporter: 현황 지표(SERVICE F9)
  - [ ] 테스트: 라우터 분기
- [ ] **T9** Streamlit(`app/`)
  - [ ] 탭 4개(학습·자료·현황·계획)
  - [ ] 학습 탭: 스트리밍 풀이, 타이머, 제출, 리뷰 폼, 결과·해설, '문제 이상'
  - [ ] interrupt 재개: thread_id를 `session_state`와 URL에 보관, `checkpoints.sqlite`, 연결 1개 + Lock
  - 완료 기준: 탭에서 세트 풀이 → 리뷰 → 현황이 동작하고, 계획 승인 화면에서 새로고침해도 이어서 진행된다

## Phase 6 — 마무리와 제출

- [ ] **T10** 테스트 마무리
  - [ ] 실호출 스모크(`pytest -m live`): Sonnet 4.6 구조화 출력이 스키마를 통과하는지
  - [ ] E2E 2개: 세트 풀이 → 저장, 리뷰 → 진단(가짜 LLM)
  - [ ] eval: T1-a의 오답 10개로 진단 일치율과 조건 문구 번역 보존을 확인(수동 실행)
- [ ] **T11** 문서 갱신: README(설치·실행), 구현하면서 바뀐 점을 ARCHITECTURE에 반영, LangGraph 그래프 다이어그램
- [ ] **T12** TODOS.md 작성(아래 'v1 이후' 항목)
- [ ] 데모 시나리오: 자연어 지시 → 계획 승인(interrupt①) → 세트 풀이 → 리뷰·진단 → 현황 / 로그인 대기(interrupt②)
- [ ] 제출

---

## 마감이 위태로울 때 빼는 순서

1. Collector → PDF/HTML 저장 후 업로드
2. Planner → Reporter를 보고 직접 계획
3. Streamlit → CLI(세트 풀이와 리뷰 폼을 rich 프롬프트로)
4. LLM Supervisor → 명령별 진입점
5. LLM 태깅 → 파일명이나 폴더로 세부 항목 지정

> 주의: 1번과 2번을 빼면 interrupt 시연 지점(계획 승인, 로그인 대기)이 모두 사라진다.

## 확인 대기 (외부)

| 항목 | 누구/어디 | 상태 | 영향 |
|---|---|---|---|
| 사내 자료의 외부 모델 반입 승인 | 담당 부서 | [ ] | 승인 전에는 공개 자료만 사용, Collector 대상 제한 |
| 교안 사이트 이용약관 | 사이트 약관 | [ ] | 허용하지 않으면 수동 저장 |
| 과정 평가 기준(HITL·RAG·멀티 에이전트) | 강사 | [ ] | 데모 시나리오 조정 |
| CCA-F 공식 세부 항목 | 시험 가이드 v1.0 | [x] | T2-c(`exam.yaml` v2) |

## v1 이후 (TODOS.md로 옮길 항목)

- [ ] **동료 배포 준비**: 개인 AWS 자격 증명 없이 쓸 수 있는 방식(공용 계정, 사내 호스팅) 검토 → 설치 가이드
- [ ] **시험 팩 템플릿·작성 가이드**: `exams/_template/`, 새 시험 추가 절차, 두 번째 시험으로 검증
- [ ] Collector 고도화: 링크 따라가기, 사이트별 추출, AgentCore Browser 검토
- [ ] 놓친 문구 드릴 모드
- [ ] 복수 정답 문항(multiple-response) 출제·채점·진단(D30)
- [ ] LLM 호출 모니터링: Langfuse 연동 검토(LangSmith는 쓰지 않음, `.env`의 LANGSMITH 항목은 주석 처리함). 시간이 남을 때

---

## 세션 인계 메모

새 세션을 시작할 때 다음 순서로 진행한다.
1. 이 문서의 '진행 요약'과 현재 Phase에서 체크되지 않은 항목을 확인한다.
2. 작업을 마칠 때마다 체크박스와 '마지막 갱신' 날짜를 갱신하고, 바뀐 결정은 ARCHITECTURE §12에 기록한다.

**참고 기록**(이 컴퓨터의 로컬 파일, 저장소 밖):
- 설계 인터뷰 원문: `~/.gstack/projects/ax-sds/sechunpark-nobranch-design-20261104-125615.md`
- 엔지니어링 리뷰 리포트(결정 원문 D1~D25): `~/.gstack/projects/ax-sds/unknown-eng-review-20261104-134421.md`
- 테스트 계획: `~/.gstack/projects/ax-sds/sechunpark-unknown-eng-review-test-plan-20261104-142506.md`

### 작업 로그

| 날짜 | 내용 |
|---|---|
| 2026-11-04 | /office-hours로 서비스 정의, /plan-eng-review로 아키텍처 확정(결정 25개). 저장소 생성, SERVICE/ARCHITECTURE/TASK 문서 작성 |
| 2026-11-04 | CLAUDE.md 작성. Sonnet 할당량 초과로 리전 추가 → 엔드포인트 순차 전환 설계(D26, ARCHITECTURE §6.2~6.3) |
| 2026-11-04 | 리전별 사용 가능 모델 목록 반영. ap-northeast-2 `apac.`에는 쓸 모델이 없어 제외 → 엔드포인트 5개(`us.` 2곳 → `global.` 3곳) |
| 2026-11-04 | Phase 0: T1-b(probe 20개 조합 모두 응답, Sonnet 4.6 구조화 출력 성공), T1-c(bge-m3 1024차원, MPS) 완료. T2 골격(pyproject, .gitignore, pytest 설정, Python 3.12 `.venv`)과 `config/models.yaml`, `src/misslens/bedrock.py`, 테스트 18개 추가 |
| 2026-11-04 | CLAUDE.md 설치 명령을 uv 기준으로 갱신. LLM 모니터링은 LangSmith 대신 Langfuse로 하기로 하고 v1 이후 항목에 추가 |
| 2026-11-04 | T1-a: `tests/fixtures/eval_misses.yaml`(오답 4 + 미끼 3)과 일관성 테스트 추가. Phase 0 완료 |
| 2026-11-04 | Phase 0 문서 대조: `ValidationException` 분류를 §6.3대로 좁힘(D27), §1·§6.2·§6.3·§11 보강, T2 완료 처리 |
| 2026-11-04 | Phase 1 완료: `llm.py`(엔드포인트 전환, D29), `exam_pack.py` + `exams/ccar-f/exam.yaml`(이때는 초안 v1, 아래 T2-c에서 v2로 확정), ledger(테이블 6개, 제출·리뷰 트랜잭션, 통계 6종, 판정 표 D28). 테스트 111개 + 실호출 2개 통과 |
| 2026-11-04 | T2-c: 시험 가이드 v1.0으로 `exam.yaml` v2 확정(시나리오·출제 제외 주제 추가). 복수 정답 문항은 v1 제외(D30), 미끼 기준선은 보정식 없이 나란히 표시(D31) |
| 2026-11-04 | 커밋 전 독립 리뷰 반영: 재시도를 라우터로 옮기고 botocore 재시도 끔(D32, probe도 실제로 1회 재시도하던 문제 수정), 세트 제출 시 모든 문제 답 필수, 문구 수정은 reading만, 재질문 답 범위 검사. 테스트 120개 + 실호출 2개 통과 |
