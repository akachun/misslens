# MissLens — 작업 목록과 진행 상황

> 다른 세션에서 이어서 작업할 때는 이 문서부터 읽는다. 무엇을 만드는지와 왜 만드는지는 [SERVICE.md](SERVICE.md), 어떻게 만드는지는 [ARCHITECTURE.md](ARCHITECTURE.md)에 있다.

| 항목 | 내용 |
|---|---|
| v1 제출 마감 | 2026-11-06(금) 오전 |
| 현재 단계 | **Phase 6 — 마무리** (구현 T1~T26 완료. 수집 314레슨·재색인 1,278청크. CLI·Streamlit 전체 루프, 코치 대화, 이어 풀기·세트 기록, 단어장, 진행 표시 정리, 평가용 zip, 주석 한국어화, 상위 모델 검토용 내보내기, 문제 품질 라운드 평가(evaluation/), 로컬 트레이스·가드레일 정리. 테스트 기본 334·realdata 1·live 9 통과(Windows 평가용 패키지 309·1 건너뜀). 남은 것: 화면 리허설, 데모 직전 점검, 제출. 제출 후 1순위: 모델 제공자 전환(TODOS)) |
| 마지막 갱신 | 2026-11-05 |

**상태 표기:** `[ ]` 할 일 · `[~]` 진행 중 · `[x]` 완료 · `[-]` 보류/제외(이유 기록) · `⚠` 블로커

---

## 진행 요약

| Phase | 내용 | 목표 시점 | 상태 |
|---|---|---|---|
| 0 | 사전 확인(모델 접근, 임베딩, 오답 10개 분류) | 11/4 저녁 첫 30분 | [x] |
| 1 | 기반(프로젝트 구조, 시험 팩, 설정, ledger) | 11/4 저녁 | [x] |
| 2 | 핵심 루프(Generator, Grader & Diagnostician, CLI) | 11/4 저녁 | [x] |
| 3 | 자료(Ingest, 하이브리드 검색) + T2-c 완료 기준 확인 | 11/5 오전 | [x] |
| 4 | Collector(로그인 수집, interrupt②) | 11/5 오후 | [x] 레슨 314개 수집·색인 완료 |
| 5 | Supervisor, Planner(interrupt①), Reporter, Streamlit | 11/5 저녁 | [x] |
| 6 | 테스트 마무리, 문서, 데모 준비, 제출 | 11/6 오전 | [~] 남은 것: 화면 시연 리허설, 데모 직전 `probe_models.py`, 제출 |

**절대 빼지 않는 것:** 세트 풀이 → 리뷰(미끼 포함 재질문) → 개념/해석 진단 → ledger 기록.

---

## Phase 0 — 사전 확인 (블로커 해소)

- [x] **T1-a** 오답 10개 직접 분류: 최근 틀린 문제 10개를 한국어로 다시 풀고 개념/해석으로 나눈 뒤, 놓친 영어 문구를 기록한다 → `tests/fixtures/eval_misses.yaml` (eval 픽스처로 재사용)
  - 결과: **목표 10개 중 오답 4개**(concept 1, uncertain 2, reading 1) + 미끼 3개로 형식을 먼저 확정. 해설을 이미 본 문제라 다시 풀지 않고 학습자가 직접 판정했다(방식 변경)
  - [x] ~~공부하면서 해설을 보기 전에 기록한 오답을 추가~~ → 앱 안에서 자동 수집으로 바꿈(D43): 리뷰 중 '자기 판단' 단계에서 남긴 라벨이 `miss_feedback`에 쌓이고 `eval --export`로 같은 형식 파일이 나온다. 학습자는 평소처럼 풀기만 하면 된다
  - `key_constraints`·`violates`는 Claude 초안. `tests/test_eval_fixture.py`가 §7 validator 규칙과 맞는지 검사한다
  - 관찰: m02에서 규칙으로 정한 문구(처음 고른 보기의 `violates`)와 학습자가 놓쳤다고 한 문구가 다르다 → T10 eval에서 일치율로 확인한다
- [x] **T1-b** 엔드포인트 5개 × 모델 4개 조합이 지금 응답하는지 확인: `scripts/probe_models.py` → `data/model_status.json` (ARCHITECTURE §6.2)
  - 엔드포인트: us-east-1 `us.`/`global.`, us-west-2 `us.`/`global.`, ap-northeast-2 `global.` (`apac.`에는 쓸 모델이 없어 제외)
  - 모델: Sonnet 4.6, Haiku 4.5, Sonnet 4.5, Nova 2 Lite (모델 존재 여부는 제공 목록으로 확인 완료)
  - [x] 할당량 초과 상태인 조합을 기록한다 → 2026-11-04 16:20 기준 할당량 초과 없음(20개 조합 모두 응답). 기록할 조합이 없어 '확인 대기' 표에는 추가하지 않았다
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
  - [x] 오류별 처리: 속도 제한은 라우터의 지수 백오프 재시도(최대 5회 시도, D32) 후 전환, 할당량 초과는 즉시 전환, 모델 없음·접근 거부는 '사용 불가' 표시 후 전환, 타임아웃·연결·5xx는 1회 재시도 후 전환, 요청 오류·자격 증명 오류는 전환 없이 실패(D27), 쿨다운 15분(엔드포인트×모델 단위, D29), 역할별 '최근 성공' 기억
  - [x] `model_status.json`에서 할당량 초과로 확인된 조합은 시작부터 쿨다운(24시간 넘은 probe 결과는 무시)
  - [x] 호출에 쓰인 엔드포인트와 모델을 로그로 남김
  - [x] 테스트: 가짜 예외로 전환 순서, 쿨다운, 모델 폴백, 전부 실패하는 경우 확인
  - [x] 실호출 스모크 `tests/test_llm_live.py`(router 일반 호출, generator 구조화 출력) 통과
- [x] **T3** ledger(`src/misslens/ledger/`)
  - [x] 테이블 6개 + 인덱스 3개(ARCHITECTURE §5). 2026-11-05 `miss_feedback` 추가로 7개(D43)
  - [x] 세트 제출 트랜잭션 + 상태 가드(이중 제출 방지)
  - [x] 통계 함수: `task_accuracy`(Laplace, 최근 10회, uncertain=오답, invalid 제외), `coverage`, `miss_breakdown`(+ 미끼 기준선), `top_missed_phrases`, `avg_time`, `generation_failures`
  - [x] 테스트: 통계 규칙, invalid 제외(문구 순위 포함), 트랜잭션
  - [x] 리뷰 저장(`record_review`)이 판정 표(`ledger/judge.py`)를 직접 적용(D28)
  - [x] '기준선 대비 보정값' → 보정식·목표값 없이 나란히 표시하기로 결정(D31). 기간별 비교(초기 3일 vs 후기 3일)는 T8 Reporter에서 기간 인자를 추가한다

## Phase 2 — 핵심 루프 (CLI)

- [x] **T4** Generator(`agents/generator/`)
  - [x] `select_tasks`: 범위 먼저(영역 비중 순) → 가중 무작위(같은 항목은 한 번만) + 이번 세션의 실패 항목 제외
  - [x] 문제 생성(Sonnet 4.6, `QuestionDraft` → `Question`, 이중 언어, 샘플 문제를 문체 예시로 받는 인자)
    - [x] 시험 팩의 `scenarios`로 상황을 잡고 `prompt_out_of_scope()`를 '내지 말 것'으로 넣는다. 정답 1개 문항만(D30)
    - [x] 구조화 출력 방식 확인: tool calling과 `json_schema` 둘 다 동작(11.0초 대 12.1초). 기본 방식 유지, §1 수정
  - [x] Pydantic validator(`violates`와 `key_constraints`의 정합성 + key_constraints가 지문에 글자 그대로, D33), 통과 후 보기 순서 섞기
  - [x] 근거 검증(Haiku 4.5: 근거·조건·다른 정답 여지 + 지어낸 플래그·보기 내부 모순, D34)
  - [x] 재생성 규칙(최초 1 + 2, 직전 실패 이유를 다음 프롬프트에 넣음, 3회 실패 시 다음 항목 + `generation_failures`, 2항목 연속 실패 시 중단)
  - [x] 순차 스트리밍 큐(`QuestionStream`, 백그라운드 스레드 1개, 예외가 나면 `failed`로 기록, 취소 지원)
  - [x] 테스트(GenericFakeChatModel 기반 `tests/fakes.py`) + 실호출 1문제 생성 `tests/test_generator_live.py`
  - ⚠ **근거 자료 없이는 실사용 불가(D34).** 세부 항목 설명만 주면 지어낸 플래그가 검증을 통과하거나(검사 3개일 때), 실제 플래그까지 떨어진다(검사 4개일 때). 문서 한 단락을 주면 정확히 구분했다 → Phase 3를 먼저 끝내야 CLI로 실제 공부를 할 수 있다
- [x] **T5** Grader & Diagnostician(`agents/grader/`)
  - 판정·채점·기준선·문제 이상 처리는 ledger가 한다(D28). Grader는 폼 구성과 입력 변환만 한다
  - [x] 세트 제출 흐름(정답과 해설은 리뷰 전까지 숨김) → `submit_set`
  - [x] 재질문 폼 구성: 오답 + 미끼(약 30%, 최소 2개), 보기 섞기 + 대응표, 확신/추측 (`reask.py`)
  - [x] 재질문 답을 원래 보기 순서로 되돌려 `record_review`에 넘김. '문제 이상'으로 먼저 표시한 오답은 재질문하지 않음
  - [x] 언어가 같은 시험이면 재질문 생략 분기(`submit_review`)
  - [x] 오답 해설(Sonnet 4.6, 판정 유형별 초점, 실패하면 저장된 해설), 놓친 문구를 지문 안에 강조, '문제 이상' → `mark_invalid`
  - [x] 테스트
- [x] **CLI**(`src/misslens/cli.py`, D35): 세트 생성 → 풀이(시간 기록) → 제출 → 리뷰 폼 → 진단 출력, `status`(현황 요약)
  - 완료 기준: CLI에서 5문제 세트를 풀고 리뷰하면 attempts에 `miss_type`, `missed_phrase`, `is_decoy`가 기록된다 → `tests/test_cli_e2e.py`로 확인(가짜 LLM). 실제 모델로 CLI 전체를 돌리는 것은 Phase 3 이후

## Phase 3 — 자료와 검색

- [x] **T6** Ingest(`agents/ingest/`) — ARCHITECTURE §8 'Ingest'
  - [x] 업로드(PDF/MD/TXT/HTML, 폴더 재귀)·붙여넣기(`--paste`) → 청크 분할(1200자, 겹침 150)
  - [x] 세부 항목 태깅(Haiku 4.5, 8개씩 묶음, 최대 3개, 'unassigned' 허용, 실패한 묶음은 미분류로 계속). 중복은 `content_hash`로 건너뛰고 `materials`에 문서 행 기록
  - [x] CLI `ingest`, `search`
- [x] **T6-a** 검색(`retrieval/`)
  - [x] bge-m3 + FAISS(`data/index/<exam_id>/`), 프로세스당 1회 로드, 로컬 캐시 우선
  - [x] BM25(한글 bigram) + 가중 RRF(0.5/0.5) 직접 구현(D36) + task_id 태그 우선·전체 순위로 채움
  - [x] 참고 자료 블록(`reference_block`, 출처 속성 이스케이프 추가)
  - [x] 테스트: 시험 용어 문서가 상위 k에 포함, 한국어 질의, 필터·채움, 저장·재로드, 다른 임베더 거부 → `tests/test_retrieval.py`, `tests/test_ingest.py`(25개)
  - [x] Generator 연결: `RetrievalContext`, `quiz`는 색인이 있으면 자동 사용(없으면 경고)
- [x] **T6-b** 연습 문제 은행(학습자가 브라우저에서 직접 저장, `data/sources/practice_problems/`, CertSafari 5세트 250문항, 규칙 7 확인 2026-11-05) — D39
  - [x] 파서(`misslens/practice.py`) + `practice import` → `data/practice/ccar-f/bank.jsonl`. 형식 검사(보기 4개, 정답 1개, 세부 항목이 시험 팩에 있음)
  - [x] 품질 표시: 중복 지문 20, 정답·해설 불일치 24, 출처 주의 2(2세트 32번, 5세트 10번) → 예시 사용 가능 207개, 30개 세부 항목 모두 2개 이상
  - [x] Generator에 세부 항목별 문체 예시 2개 + 베끼기 검사(포함률 60% 초과면 재생성). `quiz`는 은행이 있으면 자동 사용
  - [x] 실측 A/B에서 문제 발견·수정: 근거 자료를 붙인 뒤 첫 시도 validator 통과 1/8 → 스키마 순서 변경으로 6/8(D40), 리스트 필드 JSON 문자열 처리, LLM 오류 메시지에 원인 포함
  - 은행은 검색 색인에 넣지 않고, 학습자에게 그대로 출제하지 않는다(규칙 2). 문제를 직접 풀어 보는 용도로는 원본 파일을 쓴다
- [x] **T2-c** 첫 실제 세트 전에 `exam.yaml` 확정: 공식 가이드로 세부 항목 30개를 확정하고 `version`을 올린다
  - [x] 시험 가이드 v1.0으로 확정 → version 2(final). 세부 항목 30개(ID 변화 없음, 2.5 제목만 수정), 공식 시나리오 6개, 출제 제외 주제 16개 추가. Domain 1은 공식 PDF, Domain 2~5 제목은 가이드를 그대로 옮긴 제3자 사이트(ccar-f.17labs.ai)로 확인. 받은 자료는 `data/sources/exam_guide/`(git 제외)에 있다
  - [x] 전체 공식 PDF(39쪽)와 대조: 세부 항목 30개 제목·출제 제외 주제 16개 모두 일치. 대조 중 **제목 4개(1.3·1.7·3.1·4.4)가 YAML 쉼표 때문에 잘려 프롬프트에 들어가던 버그** 발견 → 따옴표 처리, `extra="forbid"`, version 3(D37)
  - [x] 실제 시험에 복수 정답 문항이 있음을 확인 → v1은 단일 정답만(D30)
  - 완료 기준: 자료를 넣으면 해당 세부 항목의 문제가 근거 검증을 통과한다 → **충족**(2026-11-05, 시험 가이드 6개 문서 115청크 색인 후 3.2·2.3을 실제 모델로 생성, 둘 다 1회 만에 `grounding=retrieved`로 통과)

## Phase 4 — Collector

- [x] **T7** Collector(`agents/collector/`) — ARCHITECTURE §4.4, §9
  - [x] `exams/ccar-f/sources.yaml`: Skilljar 대비 코스 7개(`expand: lessons`, D38), 설정 검증(https, 허용 도메인, 간격 2초 이상)
  - [x] `collect login <site>`(Crawl4AI 프로필, 사용자가 직접 로그인, `secrets/profiles/<site>/`, 권한 0700)
  - [x] 적힌 URL만 headless로 수집, 코스 페이지는 레슨 링크 한 단계만, 메뉴 줄 제거 후 `data/sources/collected/`에 저장, 끝나면 Ingest
  - [x] 만료 감지(로그인 URL 리다이렉트, 401/403, 로그아웃 상태 코스 페이지) → **interrupt②** 로그인 대기 → 같은 URL부터 재개. SqliteSaver로 프로세스를 다시 켜도 `--thread`로 재개
  - [x] 안전 규칙(읽기 전용, 간격, 도메인 제한), 본문이 비었거나 수강 등록이 필요하면 안내
  - [x] 사내 자료 반입 승인 확인(2026-11-05). 수집 대상 URL은 `sources.yaml`에 적힌 것만
  - [x] 테스트(가짜 수집기, 메모리·SQLite 체크포인터) `tests/test_collector.py` 19개. 실제 사이트로 로그아웃 상태 → interrupt② → 중단까지 확인
  - [x] 학습자 로그인(Mac 화면 공유로 `collect login skilljar`, 2026-11-05). 코스 1(Claude Code in Action) 레슨 10개 수집, 본문 품질 확인(강의 텍스트 4~6천 자, 상단 버튼 문구 몇 줄만 섞임)
  - [x] 7개 코스 전체 수집 + 색인 완료(2026-11-05, thread `collect-skilljar-20261105-102235`): 코스 페이지 7개 → 레슨 **314개 저장, 본문 없음·수강 등록 필요·오류 0건**. 코스별 레슨 Claude API 85 · Google Cloud 93 · Bedrock 83 · Claude 101 14 · AI Fluency 15 · MCP 14 · Claude Code in Action 10
  - 색인 1,674청크(가이드 115 포함), 세부 항목 30/30에 자료, 미분류 750(AI Fluency·Claude 101 등 일반 내용). 자료가 10청크 미만인 항목: 1.3, 1.7, 3.3, 3.5, 4.6, 5.2, 5.4
  - Bedrock·Google Cloud 코스가 Claude API 코스와 많이 겹침(API 청크의 25% 이상이 Jaccard 0.9 이상 사본) → 검색 중복 제거(D46)로 처리, 기준 0.8은 실측 분포로 확인

## Phase 5 — 라우팅, 계획, 현황, UI

- [x] **T8** Supervisor·Planner·Reporter — ARCHITECTURE §4.3, D41
  - [x] Supervisor(`graph.py`): intent 지정은 조건 분기, 자유 발화는 Haiku로 {intent, domain, count, confidence} 추출, 0.6 미만·unknown은 안내 후 종료(`agents/supervisor/`). 실호출 5문장 모두 바르게 분류
  - [x] Planner(`agents/planner/`): ledger `domain_summary`(신규 통계) → 규칙 초안 + Haiku 조정·근거 → **interrupt①** 승인·수정·취소, 잘못 고치면 이유와 함께 재질문 → 영역별 문제 수로 세트 생성(`run_set(plan=...)`)
  - [x] Reporter(`agents/reporter/`): F9 현황을 한 보고서로, CLI `status`와 Supervisor가 같은 렌더러 사용
  - [x] Collector·Ingest·Grader(제출)도 Supervisor에서 라우팅. interrupt②가 부모 그래프로 올라오고 재개됨
  - [x] CLI `plan [-n]`, `ask "<문장>"`. 테스트 `tests/test_supervisor.py` 18개(라우터 분기, 계획 interrupt, 취소, 수집 경유, 제출, CLI 흐름)
- [x] **T9** Streamlit(`app/main.py` → `src/misslens/ui/app.py`) — ARCHITECTURE §10, D42
  - [x] 탭 4개(학습·자료·현황·계획) + 사이드바 자연어 요청
  - [x] 학습 탭: 바로 풀기(문제 수·영역), 생성 중 폴링, 문제별 시간, 제출(Supervisor `submit`), 리뷰 폼(재질문·확신), 진단(놓친 문구 강조, 코치 해설 버튼), '문제 이상'
  - [x] 자료 탭: 업로드·붙여넣기 색인, 수집 시작·로그인 대기(완료/중단)·결과, 색인 현황
  - [x] interrupt 재개: thread_id를 `session_state`와 URL에 보관, `checkpoints.sqlite`, 연결 1개 + Lock
  - 완료 기준: 탭에서 세트 풀이 → 리뷰 → 현황이 동작하고, 계획 승인 화면에서 새로고침해도 이어서 진행된다 → **충족**(AppTest `tests/test_ui.py`: 새 세션에 URL의 plan 값만 주고 승인 화면 복원 → 승인 → 풀이 → 제출 → 리뷰 → 진단 → 현황)
  - [x] 실제 서버로 띄워 확인: 샌드박스(`MISSLENS_SANDBOX=1 streamlit run app/main.py`)로 콘솔 오류 없이 4개 탭, 수집 후 재시작해 새 색인(30/30, 미분류 750) 반영 확인

## Phase 6 — 마무리와 제출

- [~] **T10** 테스트 마무리
  - [x] 실호출 스모크(`pytest -m live`) 8개 통과(2026-11-05): 실제 `Question` 스키마로 문제 생성(D40 순서 변경 후 재확인, `tests/test_generator_live.py`), 라우터 한국어 4문장·Planner 모델 계획(`tests/test_supervisor_live.py`), LLM 기본 2개
  - [x] E2E 2개: 세트 풀이 → 저장, 리뷰 → 진단(가짜 LLM) → `tests/test_cli_e2e.py`(Phase 2에서 작성)
  - [~] eval: 진단 일치율은 `python -m misslens.cli eval`(현황 탭에도 표시)로 확인. 데이터는 공부하면서 자동으로 쌓임(D43, 테스트 `tests/test_eval_feedback.py`). 조건 문구 번역 보존은 진단 화면의 놓친 문구 강조로 눈으로 확인(자동화는 TODOS)
- [x] **T11** 문서 갱신: `README.md`(구조·설치·처음 쓰는 순서·명령·데이터와 보안·테스트), ARCHITECTURE는 작업마다 반영(D27~D42), LangGraph 그래프 다이어그램은 컴파일된 그래프에서 생성(`scripts/draw_graph.py` → `docs/graphs/*.mmd`, README에 포함)
- [x] **T12** `TODOS.md` 작성(v1 이후 항목 + 구현 중 발견한 항목)
- [x] **T13** 코치 해설을 tool 사용 에이전트로(D44, 학습자 선택 A): `agents/grader/coach.py`(ReAct: `bind_tools` + `ToolNode`, tool `search_material`·`learner_history`, 최대 3회, 단계별 대체 경로), `RoleLLM.bind_tools`(엔드포인트 전환 유지), CLI·Streamlit 진단에 근거·사용한 도구 표시. 테스트 `tests/test_coach.py` 5개 + 라우터 1개, 실호출 `tests/test_coach_live.py` 통과(실제로 두 tool 호출, 출처 표시)
- [x] **T14** 코치와 이어서 대화(D47, 학습자 리허설 피드백): 기본 해설 유지 + 코치 해설을 아래에, 오답별 대화 thread(체크포인터), 질문마다 tool 상한 3회, 출처 링크(레슨 URL·`guide.pdf p.N`). 테스트 coach 3개·UI 2개 추가, 실호출로 후속 질문(코드 예시 요청)에 다시 자료를 찾아 답하는 것 확인
- [x] **T15** 리허설 피드백 2차(D48·D49): 코치가 'Skilljar 자료 없음'으로 잘못 답한 원인 3가지 수정 — 검색 tool에 색인 구성·`source` 필터, 태그 일치 청크 상한 2개, 영상 플레이어 메뉴 글자(청크 29%) 제거 규칙(`sources.yaml`)과 `collect reindex`. 질문 칸을 해설 없이도, 맞힌 문제에도 제공
- [x] **T16** 검색 품질 점수화(D50, 학습자 요청): 정답 질의 30개(내용 기준 판정, 한국어 10개), 세 모드 hit@k·MRR, 출제 근거 품질, 색인 건강 5개 검사, 기준선(`evals/ccar-f/retrieval_baseline.json`) 회귀 검사. CLI `retrieval-eval`, `pytest -m realdata`. 기준선: hit@5 일반·코치 0.97, 출제 0.93. 재정리 전 색인은 건강 검사 실패로 잡힘
- [x] **T17** 한국어 질의 약점 개선(D51): 원인 진단(의미 검색 2위, BM25가 흔한 영어 단어만 보고 41위) → 질의 커버리지(idf) 가중 RRF. hit@5 일반·코치 1.00, 출제 0.97, 회귀 없음, 새 기준선 저장. 테스트 문서 `docs/testing.md`(계층·실행 시점·지표·정답 질의 추가법·기준선 절차·점수 기록)
- [x] **T18** 계획 기록·이어 풀기·생성 재개(D52, 학습자 질문에서): `plans`·`draft_answers` 테이블, 계획 탭 '최근 계획', 학습 탭 '이어 풀기' 목록, 답을 넘길 때마다 저장, `run_set` 재개 안전화 + Supervisor `resume`. 테스트 `tests/test_resume.py` 5개 + UI 2개
- [x] **T19** 세트 기록(D53, 학습자 요청): 계획 외에 바로 풀기·자연어 요청·CLI 세트도 출처와 함께 기록, 학습 탭 '지난 세트'(점수·개념/해석·리뷰/진단 다시 열기), 풀이 중 '목록으로'. 테스트 3개 추가
- [x] **T20** 단어장(D54, 학습자 제안): 진단 아래 세트 단어장(백그라운드 생성·저장), '단어장' 탭(누적·외움·검색), `vocab`·`vocab_known` 테이블, `vocab` 모델 역할. 실호출에서 1번 문제만 답하던 문제 → 빠진 문제 재요청, 긴 구절 필터. 테스트 `tests/test_vocab.py` 6개 + UI 1개
- [x] **T21** 리허설 피드백(D55): 진단에 한국어 재질문 결과·판정 근거·판정 설명, 로딩 표시를 상태 줄 하나로(fragment 부분 갱신, 변화 때만 전체 다시 그리기), '사용법' 안내, `.streamlit/config.toml` 최소 툴바. 브라우저로 Deploy 숨김·요청 처리 중 전체 다시 그리기 1회 확인
- [x] 데모 시나리오: [`docs/demo.md`](docs/demo.md) — 준비 점검표, 7단계(구조 → 자연어 → 계획 승인·새로고침 → RAG 풀이 → 리뷰·자기 판단·진단 → 현황 → 로그인 대기), 실패할 때 대체 경로
  - [x] CLI 리허설(샌드박스, 실제 색인·모델): 계획 → 2문제 → 재질문 → 자기 판단 → 해석 실수 판정 → 코치 해설(수집한 Skilljar 레슨 인용) → 현황, 83초
  - [~] Streamlit 화면으로 시연 리허설(학습자, `docs/demo.md` 순서): 진단 화면·코치 대화·이어 풀기·평가용 패키지(Windows) 화면까지 확인, 피드백은 T14~T21·헤드리스 설정으로 반영. 남은 것: 데모 당일 전체 순서 한 번
- [x] **T22** 평가용 전달 방식(D56, 학습자 결정): 공개 저장소(데이터 제외) + 비공개 zip. `scripts/make_handoff_zip.py`(화이트리스트·거부 목록·SQLite 백업 복사·자격 증명 검사·HANDOFF.md). 새 clone에서 데이터·자격 증명 없이 기본 테스트 305 통과, 앱 기동 확인. zip 압축을 풀어 status·retrieval-eval(회귀 없음) 확인. 테스트 `tests/test_handoff.py` 4개
  - [ ] 제출 직전에 zip 생성(`python scripts/make_handoff_zip.py`) → 강사님께 비공개 전달(헤드리스 설정 `a3386ee` 이후 zip이어야 이메일 질문이 안 나옴)
- [x] **T23** 마무리(D57, 학습자 요청): 코드 주석과 docstring을 한국어로(모델에 보내는 프롬프트·스키마·도구 설명은 영어 유지, `tests/test_model_facing_text.py`로 고정). 주석 외 코드 변화 없음을 AST 비교로 확인. 문서 최종 교차 확인(TASK 수치·경로, testing.md 수치, CLAUDE.md 언어 규칙)
- [x] **T24** 상위 모델 검토용 내보내기(D58, 학습자 요청): `export-questions`(제출한 세트만, 정답·violates·해설, 세부 항목별 근거 발췌 부록, `--evidence full|sources|none`, 자료 속 Markdown은 코드 블록). `src/misslens/review_export.py`, 테스트 `tests/test_review_export.py` 4개. 샌드박스 20문제 내보내기 확인
- [x] **T25** 문제 품질 라운드 평가(D59, Day 9 과제 '인-아웃 세트 자체 평가·라운드 리포트'): 외부 교차 검토(Opus·GPT, 20문제 중 OK 1) → 원인(지시문에 key constraint 기준 없음, Haiku 근거 확인이 관대) → 출제·근거 확인 지시문 보강. `question-eval`(인-아웃 세트 `evals/ccar-f/question_set.yaml`, 별도 DB, 판정기 `judge` 역할), 판정기 v1→v2 보정(외부 검토와 OK 일치 25%→80%). 1·2라운드 리포트(`evaluation/`): OK 25→44%, 정답 하나 90→100%, 사실 50→83%, 적합성 65→83%, violates 유효 50% 그대로(개념 오답 구조 문제 → TODOS 3라운드). 테스트 `tests/test_question_eval.py` 9개
- [x] **T26** 트레이스·가드레일(D60, Day 9 과제): 로컬 JSONL 트레이스(`trace.py`: 라우터의 모든 모델 호출, LangGraph 노드 콜백, span), `trace` 요약 명령, 실호출로 노드·토큰·엔드포인트 기록 확인. README '가드레일' 표(단계별 장치·위치·테스트)와 '트레이스' 절, 프로필 권한 0700 테스트 추가, demo.md 8단계(평가와 관측). 테스트 `tests/test_trace.py` 7개
- [ ] 제출

---

## 마감이 위태로울 때 빼는 순서

1. Collector → PDF/HTML 저장 후 업로드
2. Planner → Reporter를 보고 직접 계획
3. Streamlit → CLI(세트 풀이와 리뷰 폼을 rich 프롬프트로)
4. LLM Supervisor → 명령별 진입점
5. LLM 태깅 → 파일명이나 폴더로 세부 항목 지정

> 주의: 1번과 2번을 빼면 interrupt 시연 지점(계획 승인, 로그인 대기)이 모두 사라진다.
> 2026-11-05: 1~5번이 모두 구현되어 지금은 빼지 않는다. 시연 중 문제가 생기면 같은 순서로 대체 경로(CLI, 업로드)를 쓴다.

## 확인 대기 (외부)

| 항목 | 누구/어디 | 상태 | 영향 |
|---|---|---|---|
| 사내 자료의 외부 모델 반입 승인 | 담당 부서 | [x] 2026-11-05 승인(학습자 확인) | 승인 전에는 공개 자료만 사용, Collector 대상 제한 |
| 교안 사이트 이용약관 | 사이트 약관 | [x] 2026-11-05 수집 진행 가능(학습자 확인) | 허용하지 않으면 수동 저장 |
| 과정 평가 기준(HITL·RAG·멀티 에이전트) | 강사 | [x] 여러 패턴을 쓰면 좋음. 학습자 우선순위: 멀티 에이전트 > RAG > HITL | 데모 시나리오 조정 |
| CCA-F 공식 세부 항목 | 시험 가이드 v1.0 | [x] | T2-c(`exam.yaml` v2) |

## v1 이후

[TODOS.md](TODOS.md)로 옮겼다(2026-11-05, T12). 구현 중 발견한 항목(코스 간 중복 청크, 증분 수집, Skilljar 버튼 문구 등)도 함께 있다.

---

## 세션 인계 메모

**다음 작업(2026-11-05 갱신):** 구현은 끝났다(T1~T26). 생성 문제 품질은 `export-questions`로 내보내 Opus·GPT로 검토한다(docs/testing.md §5). 남은 것: 학습자 Streamlit 리허설(`MISSLENS_SANDBOX=1`, `docs/demo.md` 순서) → 데모 30분 전 `python scripts/probe_models.py`·`pytest -m live` → 제출. 학습자가 쓰는 서버는 재시작 전에 제출 전 세트가 있는지 확인하고 묻는다(재시작하면 생성이 멈춤, '이어 풀기'로 재개). 청킹·정리·검색을 다시 손대면 `retrieval-eval`로 기준선과 비교한다(docs/testing.md). 진단 eval(T10)은 공부하며 자기 판단이 쌓이면 `eval`로 확인한다.
- 근거 자료 주의: `guide.pdf`에는 공식 샘플 문제와 정답이 들어 있다. 생성 문제가 샘플과 비슷해질 수 있으니 실제로 풀어 보며 확인한다.

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
| 2026-11-04 | TASK.md 전체 대조: T1-a 미달(4/10) 명시, T4·T5에 Phase 1에서 이미 만든 함수 표시(판정 로직 중복 방지), T2-c·T10 잔여 작업 추가, 누락된 v1 이후 항목 추가 |
| 2026-11-04 | Phase 2 완료: Generator(선택·생성·validator·근거 검증·재생성·스트리밍), Grader(재질문 폼·리뷰·해설), CLI(`quiz`, `status`). 실호출로 문제 생성 확인 중 근거 없는 검증의 한계를 발견해 4번째 검사 추가(D33~D35). 테스트 160개 + 실호출 3개 |
| 2026-11-04 | Phase 2 독립 리뷰 반영: 구조화 출력이 비어 오면(`None`) 재생성으로 처리, 실패 칸은 세트 크기에 세지 않음, CLI 문제 번호를 `seq`로 통일('문제 이상' 번호 어긋남 수정), 세트 점수를 ledger `set_score`로 이동(규칙 1), Ctrl-C 시 생성 취소 후 ledger 닫기, key_constraints 2~4개 검사. 테스트 165개 |
| 2026-11-05 | Phase 3 완료: Ingest(로더·청크·Haiku 태깅·중복 제거), 하이브리드 검색(bge-m3 + FAISS + BM25 + RRF 직접 구현, D36), `RetrievalContext`, CLI `ingest`/`search`. 시험 가이드 115청크 색인(30/30 세부 항목에 자료), 실호출 문제 2개 근거 검증 통과(T2-c 완료). 전체 가이드 PDF 대조로 제목 잘림 버그 수정(exam.yaml v3, D37). 테스트 192개 |
| 2026-11-05 | T7 Collector: Skilljar 대비 코스 7개 `sources.yaml`, 로그인 프로필, LangGraph 서브그래프(fetch ⟲, wait_login의 interrupt②, SqliteSaver 재개), 코스→레슨 한 단계 펼치기(D38), CLI `collect login|run`. 실측으로 `networkidle` 대기 → `domcontentloaded`로 변경, 로그아웃 상태 코스 페이지 감지 추가. 테스트 211개 |
| 2026-11-05 | T6-b 연습 문제 은행: CertSafari 250문항 구조화(예시 사용 207), 세부 항목별 문체 예시 + 베끼기 검사(D39). A/B 실측으로 key_constraints 실패 원인을 찾아 스키마 순서 변경(첫 시도 통과 1/8 → 6/8, D40). 테스트 219개 |
| 2026-11-05 | T8: Supervisor 그래프(6개 에이전트 라우팅, 자유 발화 라우터), Planner(interrupt①, 규칙 초안 + Haiku 조정), Reporter, ledger `domain_summary`, Generator 영역별 계획 상한, CLI `plan`/`ask`(D41). 수집 진행 중에 코드만 바꾸고 데이터 파일은 건드리지 않음. 테스트 237개 |
| 2026-11-05 | T9 Streamlit: 탭 4개 + 사이드바 자연어, Supervisor 요청을 백그라운드 작업으로 실행·폴링, URL로 계획 승인·세트 복원(D42), `QuestionStream.poll()`, 붙여넣기 ingest 경로. AppTest로 완료 기준 확인. 테스트 242개 |
| 2026-11-05 | 문서·진행 대조(디렉터리 지도·인계 메모 정리). T10 실호출 스모크 8개 통과(라우터·Planner 추가), T11 README·그래프 다이어그램(컴파일된 그래프에서 생성), T12 TODOS.md. 수집은 계속 진행 중 |
| 2026-11-05 | 오답 eval 데이터 자동 수집(D43): 리뷰 뒤 '자기 판단' 단계(정답 공개 → 개념/해석/모르겠음 → 판정·해설), `miss_feedback` 테이블, `diagnosis_agreement` 통계, 현황에 일치율, CLI `eval [--export]`. 학습자가 따로 기록할 필요 없음. 테스트 247개 |
| 2026-11-05 | T13 tool 사용 코치(D44): MCP·tool이 없다는 점을 확인하고 학습자와 A안 선택. 해설 에이전트가 색인 검색·학습 기록 tool을 골라 쓰고 출처 표시. 출제·판정·기록은 계속 코드가 결정. 테스트 253개 + 실호출 9개 |
| 2026-11-05 | 수집 중 검토: 샌드박스 모드(D45) 추가 후 실제 모델·색인으로 CLI 전체 흐름 점검(167초, 해석 실수 판정·코치 해설까지). 발견: Sonnet 4.6 일일 할당량 초과 3곳(자동 우회, probe 기록), 1.1 생성 실패(따옴표·백틱 → validator 정규화), 코치 해설이 장황함(프롬프트 조정). Streamlit 샌드박스 서버 확인. 검색 중복 제거(D46). README에 에이전트별 그래프 4개(생성) + 순서도 3개, 구조·패턴 표. 테스트 264개 |
| 2026-11-05 | 수집 완료(레슨 314개, 실패 0, 색인 1,674청크). 중복 제거 기준 0.8을 실측 분포로 확인. Streamlit 샌드박스 재시작(새 색인), 실제 색인으로 CLI 리허설 83초 통과(코치가 수집 레슨 인용). 참고: Sonnet 4.6 일일 할당량이 `us.` 2곳·`use1-global`에서 초과 상태 — 데모 직전 probe로 재확인 |
| 2026-11-05 | 학습자 화면 리허설 중 모델 토큰 소진 → `.env` 키 교체. 진행 중 세트(5문제 생성 완료)는 그대로 끝까지 풀고 진단까지 기록(제출·리뷰·판정은 모델 불필요). 새 키 probe 20/20 응답, 서버 재시작 후 `?set=3`으로 진단 화면 복원 확인. 화면에서 첫 문제 표시 때 세트 상태를 in_progress로 바꾸는 호출 누락 수정 |
| 2026-11-05 | T14 리허설 피드백 반영: 코치 해설이 기본 해설을 덮어쓰던 것을 아래에 붙이도록, 같은 오답에 대해 코치와 이어서 대화(출처 링크). 실호출 확인 |
| 2026-11-05 | T15: 코치가 수집 레슨을 못 찾던 문제(도구 설명·태그 독점·플레이어 잡음) 수정, 수집본 재정리·재색인 실행(재수집 없음, 이전 색인은 scratchpad 백업). 코치 질문을 해설 전·맞힌 문제에도 허용. 테스트 278개 |
| 2026-11-05 | 재색인 완료: 1,278청크(플레이어 잡음 0, 미분류 750→546, 영상 전용 36개 제외). 실수 발견·수정: reindex 테스트가 기본 경로 때문에 실제 수집 폴더에 파일 1개를 써서 색인에 들어감 → 파일·청크·materials 정리, `save_page` 기본 경로를 호출 시점에, conftest에서 모든 테스트의 수집 경로를 임시 폴더로 고정. 실호출: 'skilljar 강의노트에서 찾아줘'에 코치가 source 필터로 레슨만 검색해 가장 가까운 레슨을 링크로 답함 |
| 2026-11-05 | T16 검색 평가 구조: 정답 질의 30개·지표·건강 검사·기준선, 재정리 전후 비교로 검증(전: 잡음 555·hit@5 0.93 / 후: 0·0.97). 테스트 283개 + realdata 1개 |
| 2026-11-05 | T17: 한국어 질의 약점을 질의 커버리지 가중 RRF로 개선(D51), 기준선 갱신. `docs/testing.md` 작성, CLAUDE.md 문서 지도·README에 연결 |
| 2026-11-05 | 데모 피드백: 자연어 요청 칸이 한 줄이라 잘리고 Enter로 제출되지 않던 문제 → 여러 줄 입력 + 폼(Ctrl/⌘+Enter 제출, Enter 줄바꿈), 사이드바 380px, 보낸 요청 표시. 코치 질문 칸도 같게. 실제 브라우저로 Enter·Ctrl+Enter·비우기 확인 |
| 2026-11-05 | 문서 전체 대조: TASK 머리말·인계 메모 갱신, ARCHITECTURE 결정 범위(D51)·§2 서브그래프 구성·§11(docs/testing·evals·evaluate)·결정 표에 빠져 있던 D2·D7(대체됨)·D24·D25(TODO) 추가, README 결정 범위, SERVICE 흐름에 자기 판단·코치 질문, CLAUDE.md 문서 지도에 demo.md |
| 2026-11-05 | 데모 피드백: 사이드바를 접어도 380px 자리가 남아 풀이 화면이 넓어지지 않던 문제 → 폭 고정 CSS를 펼친 상태(`data-collapsed='false'`)에만 적용. 브라우저로 펼침 380px / 접힘 시 본문 0~1400px 전체 / 다시 펼침 확인 |
| 2026-11-05 | T18: 계획을 다시 볼 수 없고 서버 재시작 때 생성·답안이 사라지던 문제(제 재시작으로 세트 #4가 6/10에서 멈춤) → 계획 기록, 답안 임시 저장, 이어 풀기, 남은 문제 생성(D52). 앞으로 서버 재시작 전 진행 중 세트 확인 |
| 2026-11-05 | 서버 재시작(학습자 승인, 세트 #4 '이어 풀기' 노출 확인). T19 세트 기록: 직접 만든 세트도 출처와 함께 '지난 세트'에서 다시 열기(D53) |
| 2026-11-05 | 서버 재시작(학습자 승인, '지난 세트' 확인). T20 단어장(D54): 실호출 세트 #3에서 5문제 모두 16개 표현(조건 7·기술 9). 문서 갱신 스크립트가 README에서 멈춰 일부 문서가 빠진 채 커밋됨 → 바로 보완 |
| 2026-11-05 | 서버 재시작(학습자 승인, 단어장 탭 16개 표현 확인). 전체 검토: 기본 302·realdata 1·live 9 통과. 문서 보완 — 결정 범위 D54, §10 탭 5개, §11 grader 구성, TASK 머리말·인계 메모, README '화면' 절(이어 풀기·지난 세트·단어장), testing.md 테스트 파일 지도, demo.md 복구 경로, CLAUDE.md 재시작 전 확인 규칙 |
| 2026-11-05 | T21: 한국어 재질문 결과 표시, 로딩 깜빡임(상태 줄 하나·부분 갱신), 사용법·최소 툴바(D55). 확인용 서버를 8502에 따로 띄워 학습자 서버(8501)는 건드리지 않음 |
| 2026-11-05 | 서버 재시작(제출 전 세트 0 확인). 모델 제공자 전환(개인 구독 GPT·Claude) 영향도 검토 → TODOS '제출 후 1순위'·ARCHITECTURE §13. 문서 최종 대조: 결정 범위 D55, §11 `.streamlit`, TASK T21, testing.md 최종 수치(기본 305·realdata 1·live 9) |
| 2026-11-05 | T22: 강사 테스트에 필요한 데이터 검토(저장소 공개 확인, 새 clone 재현) → 비공개 zip 방식과 생성 스크립트(D56). zip 6.9MB, 금지 파일 0건, 압축 해제 후 status·검색 평가 정상 |
| 2026-11-05 | 학습자 Windows 실행 피드백(PowerShell에서 `&&`·`source` 오류): HANDOFF.md·README에 Windows(PowerShell·cmd) 안내 추가. Windows 점검: 인코딩 없는 텍스트 입출력 8곳(테스트) 수정 + `test_portability.py`로 고정. 압축 해제 패키지에서 git 의존 테스트 1개 실패 발견 → git 체크아웃이 아니면 건너뜀·스크립트는 이유를 알리고 중단. 압축 해제 패키지 308 통과·1 건너뜀, 저장소 310 통과 |
| 2026-11-05 | Windows 실기 확인(학습자 PC): Python 3.14.8, uv 없이 venv + pip, 평가용 패키지에서 `pytest` 309 통과·1 건너뜀(의도). README·HANDOFF.md에 uv 없는 설치 경로 추가 |
| 2026-11-05 | 학습자 Windows에서 `streamlit run` 시 Streamlit 첫 실행 이메일 질문 → `.streamlit/config.toml`에 `server.headless=true`(질문 없음, 터미널 주소를 직접 열기), HANDOFF.md·README 안내 |
| 2026-11-05 | T23: 코드 주석·docstring 한국어화(src·scripts·tests 73개 파일, 4개 에이전트 분담). 주석 외 코드 변화 0건을 AST 비교로 확인, 모델에 보내는 docstring 3곳은 영어 유지 + `test_model_facing_text.py`. 문서 최종 교차 확인: TASK 헤더(T1~T23·테스트 수)·T11 그래프 경로·리허설 상태, testing.md 수치·테스트 지도, CLAUDE.md 언어 규칙, ARCHITECTURE D57 |
| 2026-11-05 | T24: 생성 문제 상위 모델 검토 방식 논의 → 앱 밖 표본 검토로 결정(D58). `export-questions` 추가, 샌드박스 20문제·근거 부록(세부 항목 20개) 내보내기 확인. 자동화는 TODOS |
| 2026-11-05 | 외부 교차 검토 결과 공유(Opus·GPT: OK 1/20, violates 오류 다수, 정답 오류 0). T25: 지시문 보강 + `question-eval` 라운드 평가. 1라운드 판정기 v1이 관대(OK 80% vs 외부 5%) → v2 분해 판정으로 보정(일치 80%). 2라운드 18/20 생성(Sonnet 할당량 초과로 엔드포인트 전환 발생), 개선 지표와 남은 구조 문제 기록. Day 9 교육 일정 검토 |
| 2026-11-05 | 세션을 tmux + Remote Control로 옮김(모바일 이어 하기). T26: 로컬 트레이스와 가드레일 정리. 첫 테스트 실행이 실제 `data/traces/`에 기록을 남긴 것을 발견(206줄, 모두 테스트) → 삭제하고 conftest에서 tmp로 돌림 |
