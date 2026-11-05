# MissLens — 아키텍처 정의서

| 항목 | 내용 |
|---|---|
| 문서 상태 | v1 확정 (2026-11-04), 구현 반영 갱신 (2026-11-05, Phase 3~4) |
| 근거 | 제품 인터뷰(/office-hours), 엔지니어링 리뷰(/plan-eng-review, 결정 D1~D25), 리전 추가(D26), 구현 중 결정(D27~D40) |
| 관련 문서 | [SERVICE.md](SERVICE.md) · [TASK.md](TASK.md) |

> 이 문서의 결정은 리뷰에서 확정된 것이다. 바꿔야 한다면 §12 기술 결정 기록에 새 항목을 추가하고 바꾼다.

---

## 1. 기술 스택

| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.11+ | 과정 지정. 개발 환경은 uv + Python 3.12 `.venv` |
| 에이전트 프레임워크 | LangChain, LangGraph | 과정 지정. `interrupt`/`Command(resume)`, `SqliteSaver` 사용 |
| LLM | Amazon Bedrock `ChatBedrockConverse` (`langchain-aws`) | 과정 지정. 역할별 모델과 리전 전환은 §6 참고 |
| 구조화 출력 | Pydantic + `with_structured_output` | 기본 방식(tool calling)을 쓴다. Sonnet 4.6에서 `method="json_schema"`도 동작함을 확인(2026-11-04, 문제 1개 기준 11.0초 대 12.1초, 둘 다 validator 통과) |
| 임베딩 | 로컬 `BAAI/bge-m3` (sentence-transformers 직접 사용, 모델 이름은 `config/models.yaml`의 `embedding`) | 계정에 Bedrock 임베딩 모델이 없음 |
| 검색 | FAISS(`faiss-cpu`, 의미 검색) + BM25(`rank_bm25`, 키워드 검색) → 가중 RRF 직접 구현(D36) | 청크 분할은 `langchain-text-splitters` |
| 웹 수집 | Crawl4AI (로그인 프로필, Markdown 추출) | 내부적으로 Playwright Chromium 사용 |
| 저장소 | SQLite 2개: `ledger.sqlite`(학습 기록), `checkpoints.sqlite`(LangGraph) | 로컬 파일 |
| UI | Streamlit(탭 4개) + CLI(`rich`) | CLI는 개발과 대체 경로용 |
| 테스트 | pytest, `GenericFakeChatModel` | 실호출은 `@pytest.mark.live`로 따로 실행 |
| 설정 | YAML(`exams/`, `config/`), 환경변수 | AWS 자격 증명은 저장소 루트 `.env`(git 제외)를 `python-dotenv`로 읽음 |

## 2. 시스템 구성

```
                     exams/<exam_id>/exam.yaml   (시험 팩: 영역·비중·세부 항목·언어·형식·version)
                     config/models.yaml          (역할 → 모델·타임아웃·폴백, 리전 엔드포인트·사용 가능 모델)
                                   │
 ┌─────────────────────────────────┴───────────────────────────────────────────┐
 │                         Supervisor (LangGraph)                                │
 │   입력: 탭 버튼·CLI 명령(intent 지정) ─▶ 조건 분기                             │
 │         자유 발화 ─▶ Haiku 4.5 구조화 출력 {intent, domain?, count?, ...}      │
 │                       └ 저신뢰 → 안내 후 종료(임의 실행 금지)                   │
 └──┬──────────┬──────────┬───────────────┬────────────────┬──────────────┬─────┘
    ▼          ▼          ▼               ▼                ▼              ▼
 Collector   Ingest     Planner       Generator        Grader &        Reporter
 (Crawl4AI)  (태깅·색인) (계획 제안)    (세트 출제)       Diagnostician    (현황)
    │ ⏸interrupt②        │ ⏸interrupt①     │                 │               │
    │ 로그인 대기         │ 계획 승인        │                 │               │
    ▼                     ▼                  ▼                 ▼               ▼
 ┌──────────────┐  ┌───────────────────────────────────────────────────────────┐
 │ FAISS(시험별)  │  │ ledger 모듈 (SQLite: 모든 SQL·통계 단독 소유)               │
 │ + BM25(메모리) │  │ quiz_sets · questions · attempts · missed_phrases ·       │
 └──────────────┘  │ materials · generation_failures                          │
                   └───────────────────────────────────────────────────────────┘
          ▲
   bge-m3 (프로세스당 1회 로드)
```

- 서브그래프 6개와 Supervisor로 구성한다(D1). 각 서브그래프는 자기 상태를 갖는다. 구현된 그래프 그림(Supervisor·Planner·Collector·Coach)은 README에 있고, `python scripts/draw_graph.py`로 컴파일된 그래프에서 `docs/graphs/*.mmd`로 다시 만든다(그림이 코드와 어긋나면 테스트 실패, D41).
- **통계는 ledger 모듈 한 곳에서만 계산한다**(D9). Generator, Planner, Reporter는 ledger의 통계 함수만 호출한다.
- **interrupt는 두 곳에만 둔다**(D20, D21). ① Planner의 계획 승인, ② Collector의 로그인 대기. 문제 풀이와 리뷰 흐름에는 interrupt가 없다.

## 3. 에이전트별 역할

| 에이전트 | 역할 | 입력 → 출력 | 모델 |
|---|---|---|---|
| **Supervisor** | intent가 지정된 입력은 조건 분기로 라우팅. 자유 발화는 의도와 인자를 추출해 라우팅 | 요청 → 대상 서브그래프 | Haiku 4.5 (자유 발화만) |
| **Collector** | `sources.yaml`의 URL을 로그인 프로필로 읽기 전용 수집, Markdown 추출, 세션 만료 감지 | URL 목록 → 문서(원본 URL, 수집 시각, content_hash) | 없음 |
| **Ingest** | 청크 분할 → 세부 항목(task_id) 태깅(묶음 호출, 'unassigned' 허용) → 임베딩·색인 | 파일·텍스트·문서 → FAISS·BM25·materials | Haiku 4.5 (태깅) |
| **Planner** | 남은 범위·정답률·실수 비율로 오늘의 계획 제안 → interrupt① → 승인된 계획으로 세트 생성 요청 | ledger 통계 → 계획 | Haiku 4.5 |
| **Generator** | 세부 항목 선택 → 검색 → 이중 언어 문제 생성 → validator → 근거 검증 → 순차·스트리밍 제공 | 계획·세부 항목 → questions | Sonnet 4.6 (생성), Haiku 4.5 (검증) |
| **Grader & Diagnostician** | 세트 제출 채점·저장 → 리뷰 시 재질문 폼(오답 + 미끼) → 자기 판단 → 판정·놓친 문구 → **코치 해설(tool 사용 에이전트, D44)** | 답안 → attempts·missed_phrases·miss_feedback·해설 | Sonnet 4.6 (해설, `search_material`·`learner_history` tool) |
| **Reporter** | 현황 지표 표시 | ledger 통계 → 화면 | 없음 |

## 4. 핵심 흐름

### 4.1 세트 출제와 풀이 (D8, D13, D18)

```
Generator                                   학습 탭 (Streamlit)
─────────                                   ──────────────────
select_tasks(N) ──▶ Q1: 검색 → 생성 → validator → 근거 검증 ─▶ 통과 ─▶ Q1 표시, 타이머 시작
                    Q2 … (백그라운드 스레드, 순차) ─────────────▶ 준비되면 다음 문제
                                                                  미준비면 '생성 중'
                    실패: 같은 항목 재생성(최초 1 + 2)                 │
                          3회 실패 → 다음 항목, generation_failures 기록
                          2항목 연속 실패 → 중단 메시지               ▼
                                                    세트 제출 → quiz_sets 1 + attempts N (트랜잭션 1회)
                                                    (정답·해설은 리뷰 전까지 숨김)
```

- 호출은 순차로 한다(동시 실행 1). 재시도는 botocore가 아니라 `llm.py` 라우터가 오류 종류별로 한다(§6.3, D32). 타임아웃은 Sonnet 180초, Haiku 60초다.
- `elapsed_ms`는 문제가 화면에 표시된 시점부터 잰다. 생성을 기다린 시간은 넣지 않는다.
- 백그라운드 스레드에서 예외가 나면 해당 문제를 `failed` 상태로 기록하고 화면에 표시한다. 다음 문제가 영원히 "생성 중"으로 남는 일을 막기 위해서다. `failed` 칸은 세트 크기 N에 세지 않고(준비된 문제가 N개가 될 때까지 다음 항목으로 진행), 연속 실패에는 센다. 문제 번호는 `seq`(실패 칸 포함)를 그대로 쓴다.
- 모델이 구조화 출력 없이 답하면(tool calling에서 도구를 부르지 않음) 생성 실패 1회로 세고 다시 생성한다.
- CLI에서 Ctrl-C를 누르면 생성을 취소하고 백그라운드 스레드가 멈춘 뒤에 ledger를 닫는다. 제출하지 않은 세트는 `generating`/`in_progress` 상태로 남고 통계에는 들어가지 않는다.

### 4.2 리뷰와 진단 (D8, D14, D20)

```
'리뷰 요청' → 재질문 폼 1장 구성
   = 오답 전체 + 미끼(맞힌 문제 약 30%, 최소 2개, 부족하면 있는 만큼), 무작위 순서
   - 각 문항: stem_native + options_native, 보기 순서 섞기(대응표 저장), 오답 여부 표시 안 함
   - 입력: 답 + 확신/추측
→ 폼 제출(일반 제출, interrupt 없음) → diagnose() 실행
```

| 대상 | 재질문 결과 | 판정 `miss_type` | 통계 처리 |
|---|---|---|---|
| 오답 | 정답 + 확신 | `reading` | 해석 실수 비율에 포함. `missed_phrase` = 처음 고른 오답 보기의 `violates` |
| 오답 | 정답 + 추측 | `uncertain` | 해석 실수 비율의 분모와 분자에서 모두 빼고 따로 집계. 정답률에서는 오답으로 계산 |
| 오답 | 오답 | `concept` | 개념 실수로 집계 |
| 미끼(맞힌 문제) | 정답/오답 | 판정 없음 (`is_decoy=1`) | 세트별 **기준선** = 미끼 재질문 정답률. 현황에 해석 실수 비율과 기준선을 **나란히** 표시(보정식·목표값 없음, D31) |

- 시험 팩에서 `exam_language`와 `learner_language`가 같으면 재질문을 건너뛰고, 오답을 모두 `concept`으로 기록한다.
- 판정 표는 `ledger/judge.py`의 순수 함수로 구현하고, ledger가 리뷰를 저장할 때(`record_review`) 직접 적용한다. 호출하는 쪽은 재질문 답과 확신 여부만 넘긴다(D28).
- `reask_choice`는 섞기 전의 **원래 보기 순서**로 바꿔서 저장한다. `missed_phrase`는 `reading`일 때만 기록한다(`uncertain`은 기록하지 않음).
- 리뷰에는 세트의 오답이 모두 들어 있어야 하고, 미끼는 처음에 맞힌 문제여야 한다. 어기면 저장하지 않는다. 리뷰 전에 '문제 이상'으로 표시한 오답은 재질문하지 않는다.
- **자기 판단(D43):** 재질문 제출 뒤, 진단을 보여 주기 전에 오답마다 정답만 보여 주고 "몰랐던 내용(개념) / 조건을 놓침(해석) / 모르겠음"을 고르게 한다(건너뛰기 가능). `miss_feedback`에 저장하고 `diagnosis_agreement`로 판정과의 일치율을 낸다. 해설을 본 뒤에 고르면 해석 실수 쪽으로 치우치므로(T1-a 관찰) 이 순서를 지킨다. `eval --export`는 T1-a 픽스처와 같은 형식으로 `data/eval/`에 저장한다.
- **코치 해설(D44):** `agents/grader/coach.py`의 ReAct 서브그래프(`bind_tools` + `ToolNode`). 읽기 전용 tool 두 개 — `search_material(query, task_id?)`(하이브리드 검색, 결과는 참고 자료 블록, 규칙 7), `learner_history(task_id)`(ledger 통계 함수만 호출, 규칙 1) — 를 최대 3회 부른 뒤 해설과 출처를 낸다. 모델 호출마다 라우터의 엔드포인트 전환이 적용된다(`RoleLLM.bind_tools`). 상한을 넘거나 실패하면 tool 없는 해설, 그것도 실패하면 저장된 해설. 기록은 하지 않는다(판정은 규칙 3의 표만).
- **코치 검색(D49):** `search_material(query, task_id?, source?)`. tool 설명에 색인 구성(가이드, 17labs, Skilljar 코스별 레슨 수)을 넣어 모델이 수집 레슨의 존재를 알게 하고, `source`(`skilljar`, 코스 이름, `guide`)로 범위를 좁힐 수 있다. 태그 일치 청크는 앞에 최대 2개(`max_tagged`). 시스템 프롬프트에 '웹은 못 보지만 Skilljar 레슨은 로컬 사본으로 있다, 다루지 않으면 가장 가까운 레슨을 알려라'를 둔다.
- **코치와 대화(D47):** 오답마다 코치 thread(`coach-<exam>-<attempt_id>`)를 체크포인터에 두고, 학습자가 이어서 질문하면 같은 그래프에 메시지를 더해 답한다(tool 상한은 질문마다 3회). 화면은 저장된 기본 해설을 그대로 두고 그 아래에 코치 해설과 대화를 붙인다. 질문 칸은 처음부터 보이며, 해설을 받지 않고 바로 질문해도 첫 메시지에 문제 정보가 함께 들어가 대화가 시작된다. 맞힌 문제도 펼쳐서 질문할 수 있고, 이때 코치는 '틀린 문제' 전제 없이 답한다. 출처는 URL(수집 레슨, 로그인한 브라우저에서 열림)은 링크로, PDF는 `guide.pdf p.N`으로 표시한다. 대화는 통계가 아니므로 ledger에 넣지 않는다.
- 구현: 재질문 폼과 대응표는 `agents/grader/reask.py`, 저장과 진단 화면 데이터는 `agents/grader/review.py`. 오답 해설은 explainer 역할(Sonnet 4.6)이 판정 유형(해석/불확실/개념)에 맞춰 쓰고, 호출이 실패하면 문제에 저장된 해설을 보여 준다.

### 4.3 계획 승인 (interrupt①, D21)

```
Planner: ledger.domain_summary → 규칙 초안(시험 비중 × (1 + 미출제 비율) × (1.5 − 정답률), 최대 잔여 반올림)
  → planner 모델(Haiku)이 문제 수 조정 + 한국어 근거 작성(검증 실패·호출 실패면 규칙 초안 사용)
  → interrupt({kind: "plan", plan, table, error}) ⏸  화면: 영역별 비중·출제 항목·정답률·오늘 문제 수
  ← Command(resume={"action": "approve", "counts"?: {...}}) / {"action": "cancel"}
     고친 문제 수가 틀리면(모르는 영역, 음수, 합계 0 또는 30 초과) 이유와 함께 다시 interrupt
  → Generator.run_set(plan=영역별 문제 수): 영역 상한 안에서 select_tasks 순서(범위 먼저, 그다음 약점)로 출제
```
- 구현: `agents/planner/`(계획·검증·서브그래프), `agents/reporter/`(F9 현황, 모델 없음), `agents/supervisor/`(자유 발화 라우터), `graph.py`(Supervisor 그래프, D41). CLI `plan [-n]`, `ask "<문장>"`, `status`는 Reporter.

### 4.4 로그인 대기 (interrupt②, D21)

```
Collector 그래프: START → fetch ⟲ (queue[0] 수집) → END
  fetch: queue[0] 수집
  ├─ 코스 페이지(expand: lessons) → 같은 코스의 레슨 링크를 queue 앞에 추가(D38)
  ├─ 레슨 → 메뉴 줄 제거 → data/sources/collected/<site>/*.md 저장 → 끝나면 Ingest로 전달
  ├─ 로그인 URL로 리다이렉트 / 401·403 / 레슨 0개인데 'Sign In' 링크만 있음(로그아웃 상태 코스 페이지)
  │     → wait_login: headless 브라우저를 닫고 interrupt({kind, site, login_url, url, attempt}) ⏸
  │       CLI: "y=로그인 창 열기 / n=이미 로그인함 / q=중단" → 보이는 브라우저에서 직접 로그인
  │     ← Command(resume="done") → 같은 URL부터 다시 / "abort" → 중단. 연속 3회 넘으면 중단
  ├─ checkout으로 리다이렉트 / 레슨 0개에 등록 링크만 → '수강 등록 필요'로 표시하고 다음 URL
  └─ 본문 200자 미만(영상만 있는 레슨 등) → '본문 없음'으로 표시, PDF/HTML 업로드 안내
```
- 상태는 `data/checkpoints.sqlite`(SqliteSaver)에 thread_id(`collect-<site>-<시각>`)별로 남는다. 로그인 대기에서 프로세스를 끄더라도 `collect run --thread <id>`로 멈춘 URL부터 이어간다.
- 페이지 하나가 그래프 한 단계라서 `recursion_limit`을 2000으로 올린다(기본값 25면 코스 중간에 멈춘다).

## 5. 데이터 모델 (D10, D22)

`ledger.sqlite`는 ledger 모듈만 읽고 쓴다.

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| `quiz_sets` | id, exam_id, size, status(`generating`/`in_progress`/`submitted`/`reviewed`), created_at, submitted_at, reviewed_at | 이중 제출은 status로 막는다 |
| `questions` | id, set_id, exam_id, task_id, domain, blueprint_version, seq, status(`ready`/`failed`), payload_json | payload는 §7의 `Question` 문서 전체 |
| `attempts` | id, set_id, question_id, chosen_index, is_correct, elapsed_ms, is_decoy, reask_choice, reask_confident, miss_type, invalid | 통계는 `invalid=0`인 행만 쓴다 |
| `missed_phrases` | attempt_id(FK), phrase, user_corrected | 순위를 낼 때 invalid인 attempt는 조인에서 뺀다 |
| `materials` | id, exam_id, source_type(`upload`/`paste`/`collect`), source_uri, content_hash, task_id(NULL 허용), collected_at | |
| `generation_failures` | id, exam_id, task_id, tries, reason, created_at | 근거 검증에서 실패한 이유를 기록한다. 현황 탭에 표시 |
| `miss_feedback` | attempt_id(PK, FK), learner_label(`concept`/`reading`/`unsure`), stage, created_at | 리뷰 중 학습자가 남긴 자기 판단(D43). 판정(`miss_type`)은 바꾸지 않고 진단 일치율 eval에만 쓴다. 2026-11-05 추가 |

- 인덱스: `attempts(question_id)`, `attempts(set_id)`, `questions(exam_id, task_id)`
- 구현 세부(2026-11-04): 세트 제출에는 `ready` 문제 전부의 답이 있어야 한다(안 푼 문제는 `chosen_index=NULL`로 오답 처리, 빠뜨리면 제출 거부). `attempts.question_id`는 UNIQUE(문제당 시도 1개), `missed_phrases.attempt_id`가 기본 키(시도당 문구 1개, 사용자가 고치면 `user_corrected=1`, `reading` 판정에만 허용), `failed` 문제는 `payload_json`이 NULL, `materials`는 `(exam_id, content_hash)`로 중복 제거. 연결은 프로세스당 1개 + Lock, 쓰기는 `BEGIN IMMEDIATE` 트랜잭션.
- `checkpoints.sqlite`: LangGraph `SqliteSaver` 전용이다(interrupt①·② 재개용).
- FAISS 인덱스: `data/index/<exam_id>/`. BM25는 시작할 때 메모리에 만든다.
- 시험 팩의 기준은 YAML 파일 하나다. SQLite에는 복사하지 않는다. 세부 항목이 바뀌면 version을 올리고 `task_id` 매핑표(old → new)로 기존 통계를 이어 붙인다.

## 6. 모델 설정 (D4, D5, D19, D26)

모델 호출은 모두 `src/misslens/llm.py`의 `get_llm(role)`을 거친다. 모델 ID, 리전, 타임아웃은 `config/models.yaml` 한 곳에서 관리한다. 코드에 모델 ID나 리전을 직접 적지 않는다.

### 6.1 역할별 모델

모델은 접두사(`us.`, `apac.`, `global.`)를 뺀 **기본 ID**로 적고, 실제 호출 ID는 엔드포인트의 접두사를 붙여서 만든다(예: `us.` + `anthropic.claude-sonnet-4-6`).

| 역할 | 기본 모델 | 타임아웃 | 모델 폴백(모든 엔드포인트를 시도한 뒤에만) |
|---|---|---|---|
| 출제(Generator), 오답 해설(Diagnostician) | `anthropic.claude-sonnet-4-6` | 180초 | `anthropic.claude-sonnet-4-5-20250929-v1:0` |
| 근거 검증, Ingest 태깅, Planner, Supervisor(자유 발화) | `anthropic.claude-haiku-4-5-20251001-v1:0` | 60초 | 태깅과 Planner만 `amazon.nova-2-lite-v1:0` |
| Reporter, Collector | (LLM 없음) | — | — |

- 출제와 근거 검증처럼 구조화 출력의 정확성이 중요한 역할에는 Nova를 폴백으로 쓰지 않는다.
- Nova Pro, Nova Lite(v1), Nova Micro, gpt-oss는 쓰지 않는다.

### 6.2 리전·추론 프로필 엔드포인트 (D26)

할당받은 리전은 3개다. 데이터 처리 지역이 좁은 프로필(`us.`, `apac.`)을 먼저 쓰고, 모두 막히면 `global.`로 넘어간다. 다만 아래 표처럼 `apac.`에는 쓸 모델이 없어서, 실제로는 `us.` 두 곳 다음에 `global.`로 넘어간다.

**리전별 사용 가능 모델** (계정 기준, 2026-11-04 제공 목록. ✓ = 사용 가능, — = 없음)

| 모델(기본 ID) | 이 프로젝트의 용도 | us-east-1 `us.` | us-east-1 `global.` | us-west-2 `us.` | us-west-2 `global.` | ap-northeast-2 `apac.` | ap-northeast-2 `global.` |
|---|---|---|---|---|---|---|---|
| `anthropic.claude-sonnet-4-6` | 출제·해설 | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `anthropic.claude-sonnet-4-5-20250929-v1:0` | Sonnet 폴백 | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `anthropic.claude-haiku-4-5-20251001-v1:0` | 검증·태깅·계획·라우팅 | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `amazon.nova-2-lite-v1:0` | 태깅·계획 폴백 | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| `amazon.nova-pro-v1:0` | 사용 안 함 | ✓ | — | ✓ | — | ✓ | — |
| `amazon.nova-lite-v1:0` | 사용 안 함 | ✓ | — | ✓ | — | ✓ | — |
| `amazon.nova-micro-v1:0` | 사용 안 함 | ✓ | — | ✓ | — | ✓ | — |
| `openai.gpt-oss-120b-1:0`, `openai.gpt-oss-20b-1:0` | 사용 안 함 | 접두사 없이 us-east-1만 | | | | | |

- ap-northeast-2의 `apac.` 프로필에는 이 프로젝트가 쓰는 모델(Claude 4.5/4.6, Nova 2 Lite)이 없다. 그래서 기본 전환 순서에서 뺀다. ap-northeast-2는 `global.`로만 쓴다.
- 결과적으로 `us.`로 처리할 수 있는 곳은 us-east-1과 us-west-2 두 곳이다. 둘 다 막히면 `global.`로 넘어간다.

**기본 전환 순서**

| 순서 | 엔드포인트 | 리전 | 접두사 | 데이터 처리 지역 |
|---|---|---|---|---|
| 1 | `use1-us` | us-east-1 | `us.` | 미국 리전 |
| 2 | `usw2-us` | us-west-2 | `us.` | 미국 리전 |
| 3 | `use1-global` | us-east-1 | `global.` | 전 세계 상용 리전 |
| 4 | `usw2-global` | us-west-2 | `global.` | 전 세계 상용 리전 |
| 5 | `apne2-global` | ap-northeast-2 | `global.` | 전 세계 상용 리전 |
| (제외) | `apne2-apac` | ap-northeast-2 | `apac.` | 사용할 모델이 없어 기본 순서에서 뺌. Nova Pro/Lite/Micro를 쓰게 되면 다시 검토 |

```yaml
# config/models.yaml (예시)
endpoints:            # 위에서부터 시도
  - {name: use1-us,      region: us-east-1,      prefix: us}
  - {name: usw2-us,      region: us-west-2,      prefix: us}
  - {name: use1-global,  region: us-east-1,      prefix: global}
  - {name: usw2-global,  region: us-west-2,      prefix: global}
  - {name: apne2-global, region: ap-northeast-2, prefix: global}
# 리전별로 존재하는 모델 (위 표와 같음). 여기에 없는 조합은 호출하지 않는다.
availability:
  use1-us:      [anthropic.claude-sonnet-4-6, anthropic.claude-sonnet-4-5-20250929-v1:0, anthropic.claude-haiku-4-5-20251001-v1:0, amazon.nova-2-lite-v1:0]
  usw2-us:      [anthropic.claude-sonnet-4-6, anthropic.claude-sonnet-4-5-20250929-v1:0, anthropic.claude-haiku-4-5-20251001-v1:0, amazon.nova-2-lite-v1:0]
  use1-global:  [anthropic.claude-sonnet-4-6, anthropic.claude-sonnet-4-5-20250929-v1:0, anthropic.claude-haiku-4-5-20251001-v1:0, amazon.nova-2-lite-v1:0]
  usw2-global:  [anthropic.claude-sonnet-4-6, anthropic.claude-sonnet-4-5-20250929-v1:0, anthropic.claude-haiku-4-5-20251001-v1:0, amazon.nova-2-lite-v1:0]
  apne2-global: [anthropic.claude-sonnet-4-6, anthropic.claude-sonnet-4-5-20250929-v1:0, anthropic.claude-haiku-4-5-20251001-v1:0, amazon.nova-2-lite-v1:0]
cooldown_seconds: 900
roles:
  generator:  {model: anthropic.claude-sonnet-4-6, timeout: 180, fallback_model: anthropic.claude-sonnet-4-5-20250929-v1:0}
  explainer:  {model: anthropic.claude-sonnet-4-6, timeout: 180, fallback_model: anthropic.claude-sonnet-4-5-20250929-v1:0}
  verifier:   {model: anthropic.claude-haiku-4-5-20251001-v1:0, timeout: 60}
  tagger:     {model: anthropic.claude-haiku-4-5-20251001-v1:0, timeout: 60, fallback_model: amazon.nova-2-lite-v1:0}
  planner:    {model: anthropic.claude-haiku-4-5-20251001-v1:0, timeout: 60, fallback_model: amazon.nova-2-lite-v1:0}
  router:     {model: anthropic.claude-haiku-4-5-20251001-v1:0, timeout: 60}
```

- **환경변수로 순서 바꾸기:** `BEDROCK_ENDPOINTS=usw2-us,use1-global`처럼 쉼표로 나열하면 그 순서만 쓴다. 특정 엔드포인트가 막힌 것을 알고 있을 때 수동으로 피하는 용도다.
- **사용 가능 모델:** `get_llm()`은 `availability`에 없는 조합을 호출하지 않는다. 모델 존재 여부는 위 표로 알고 있으므로, `scripts/probe_models.py`는 "지금 실제로 응답하는지(할당량 초과 여부 포함)"를 확인하는 용도로 쓴다. 결과는 `data/model_status.json`에 기록하고, 할당량 초과로 확인된 조합은 시작할 때부터 쿨다운 상태로 둔다.
  - 파일 형식: `probed_at`, `endpoints_order`, `results`(조합별 `status`·`latency_ms`·`error`), `quota_exceeded`(`"<endpoint>:<model>"` 목록, `llm.py`가 시작 쿨다운에 사용), `structured_output`(generator 모델 1회 결과), `role_coverage`(역할별 응답 조합)
  - probe는 재시도 없이(`total_max_attempts=1`) 첫 응답을 그대로 기록한다

### 6.3 전환 규칙

```
호출 ──▶ 엔드포인트 i (쿨다운 중이거나 사용 불가 조합이면 건너뜀)
          │
          ├─ 성공 ─▶ 결과 반환, 이 엔드포인트를 역할별 '최근 성공'으로 기억(다음 호출은 여기서 시작)
          │
          ├─ ThrottlingException(속도 제한)
          │     → 같은 엔드포인트에서 지수 백오프 재시도(1·2·4·8초 + 지터, 최대 5회 시도, D13·D32)
          │     → 그래도 실패 → 엔드포인트 i 쿨다운(15분) → i+1
          │
          ├─ ServiceQuotaExceededException / 토큰 한도 초과 메시지(일일·누적 할당량)
          │     → 재시도 없이 바로 쿨다운 → i+1 (일일 한도는 ThrottlingException에 메시지로 와서 메시지로 구분)
          │
          ├─ ValidationException(모델 ID가 이 리전에 없음) / AccessDeniedException(모델 접근 없음)
          │     → 이 조합을 프로세스 동안 '사용 불가'로 표시 → i+1
          │
          ├─ 그 밖의 ValidationException(입력 길이 초과 등 요청 오류)
          │     → 전환하지 않고 바로 호출 실패 (다른 엔드포인트도 같은 이유로 실패하므로)
          │
          ├─ 자격 증명 오류(NoCredentials, UnrecognizedClient, ExpiredToken)
          │     → 전환하지 않고 바로 중단, 자격 증명 확인 안내
          │
          └─ 타임아웃·연결 오류 → 1회 재시도 → 실패하면 i+1
                    │
 모든 엔드포인트 실패 ──▶ fallback_model로 같은 순서 반복 ──▶ 그래도 실패하면 호출 실패
                                                          (Generator: 생성 실패 1회로 처리, §4.1)
```

- 쿨다운과 '사용 불가'는 **(엔드포인트, 모델) 조합 단위**로 건다. 할당량은 모델별이라 Sonnet이 막혀도 같은 엔드포인트의 Haiku는 쓸 수 있어야 하기 때문이다(D29). '최근 성공'은 역할별로 기억하고, 다음 호출은 그 엔드포인트부터 시작한 뒤 나머지를 원래 순서대로 시도한다.
- 분류되지 않은 오류(구조화 출력 파싱 실패 등)는 전환하지 않고 바로 실패한다.
- botocore 자체 재시도는 끈다(`retries={"mode": "standard", "total_max_attempts": 1}`). 켜 두면 타임아웃·5xx·일일 한도까지 botocore가 먼저 재시도해서 위 규칙이 깨진다(D32). botocore의 `max_attempts: 1`은 '재시도 1회'라는 뜻이라 쓰지 않는다.
- 쿨다운과 '최근 성공' 기록은 프로세스 메모리에만 둔다. 앱을 시작할 때는 `data/model_status.json`(있으면)으로 쿨다운을 초기화하고 1번부터 시도한다. 단, probe 결과가 24시간보다 오래됐으면 쓰지 않는다.
- 속도 제한이 여러 엔드포인트에서 이어지면, 엔드포인트마다 재시도를 거치므로 한 번의 호출이 수 분까지 길어질 수 있다. 첫 사용일에 로그로 실제 대기 시간을 보고, 길면 재시도 횟수를 줄이는 결정을 §12에 추가한다.
- 실제 호출에 쓰인 엔드포인트와 모델은 로그에 남기고, 생성 실패 시에는 `generation_failures.reason`에 함께 기록한다.
- `ChatBedrockConverse` 인스턴스는 (리전, 모델 ID, 타임아웃) 조합마다 한 번만 만들고 재사용한다.
- 오류 분류는 `src/misslens/bedrock.py`의 `classify_error()` 한 곳에서 한다(probe와 `llm.py`가 같이 씀). 일일 토큰 한도는 `ThrottlingException`에 메시지로만 구분되어 오므로 메시지로 판별한다.
- 자격 증명은 기본 AWS 자격 증명 체인(프로필 또는 환경변수)을 쓰고(`.env`는 시작 시 환경변수로 읽음), 리전만 엔드포인트마다 바꾼다. 리전마다 다른 자격 증명이 필요하면 엔드포인트에 `profile` 필드를 추가한다.

## 7. 문제 스키마와 생성 규칙

```python
class Option(BaseModel):
    text_en: str
    text_native: str          # 학습자 언어
    violates: str | None      # 오답 보기가 어기는 key_constraint (정답은 None)

class QuestionDraft(BaseModel):  # 생성 모델이 채우는 부분. 필드 순서 = 생성 순서(D40)
    stem_en: str
    key_constraints: list[str]  # 2~4개, stem_en에서 글자 그대로 옮긴 짧은 구절(D33). 보기보다 먼저 쓰게 함
    stem_native: str          # key_constraint를 모두 담되, 정답 힌트는 넣지 않는다
    options: list[Option]     # 4개
    answer_index: int         # 정답 1개만(v1). 실제 시험의 복수 정답 문항은 v1에서 내지 않는다(D30)
    explanation_native: str   # 보기 순서를 섞으므로 A/B 같은 글자로 보기를 가리키지 않는다

class Question(QuestionDraft):    # 코드가 채우는 부분 + 저장 형태(questions.payload_json)
    task_id: str              # exam.yaml의 세부 항목 ID
    domain: str               # 영역 ID(d1~d5)
    scenario_id: str | None   # exam.yaml의 공식 시나리오
    grounding: str            # "retrieved" 또는 Phase 3 전의 "task_statement_only"

    # validator(필수, schema.validation_errors): 보기 4개, violates=None인 보기는 정답 하나뿐,
    # 나머지 violates는 모두 key_constraints 안에 있고, key_constraints는 모두 stem_en에 글자 그대로 있으며
    # (대소문자·공백 무시), 보기 문장은 서로 달라야 한다. 어기면 생성 실패 1회로 센다.
    # 통과하면 코드가 보기 순서를 섞는다(정답 위치 편향 제거, D33).
```

- 구현: `src/misslens/agents/generator/`(`schema.py`, `select.py`, `prompts.py`, `context.py`, `pipeline.py`). 다시 생성할 때는 직전 실패 이유를 프롬프트에 넣는다.
- 근거 자료는 `ContextProvider` 인터페이스로 받는다. 색인이 있으면 `RetrievalContext`(§8, `grounding="retrieved"`), 없으면 세부 항목 설명만 주는 `TaskStatementContext`(`task_statement_only`)를 쓴다.

**근거 검증 (D15).** Haiku 4.5에 검색한 청크, 문제, 정답, 오답별 `violates`를 함께 주고 아래 네 가지를 구조화 출력으로 판정하게 한다. 하나라도 실패하면 다시 생성한다. Haiku가 정답을 가린 채 문제를 푸는 방식(blind solve)은 쓰지 않는다.
1. 정답이 청크로 뒷받침되는가
2. 각 `violates`가 지문의 key_constraint로 실제로 있는가
3. 다른 보기가 정답일 여지는 없는가
4. 지문과 보기의 CLI 플래그·API 파라미터·파일 이름이 실제로 있고, 보기 안의 명령과 설명이 서로 맞는가(D34)

- **근거 자료가 없으면 이 검증은 제 역할을 못 한다(2026-11-04 실측).** 세부 항목 설명만 주면 Sonnet이 없는 플래그(`--no-interactive`)를 정답으로 낸 문제를 Haiku가 통과시켰다. 4번 검사를 넣자 이번에는 실제 플래그(`-p`)까지 지어낸 것으로 보고 떨어뜨렸다. 공식 문서 한 단락을 근거로 주면 둘을 정확히 구분했다. 그래서 검증은 엄격하게 두고, 실제 사용 전에 Phase 3(자료·검색)을 반드시 끝낸다.

**세부 항목 선택 (D11).**
1. 아직 한 번도 출제되지 않은 세부 항목이 있으면, 그중에서 영역 비중이 높은 순서로 세트를 채운다.
2. 모든 항목이 한 번 이상 출제됐으면, 가중치를 매겨 무작위로 뽑는다(같은 항목은 한 세트에 한 번만).
   - `weight = (영역 비중 ÷ 영역 내 항목 수) × (1 − 정답률)`
   - `정답률 = (정답 + 1) / (유효 시도 + 2)`
   - 항목별 최근 10회만 센다. `uncertain`은 오답으로 보고, `invalid`는 뺀다.
3. 이번 세션(thread_id 하나)에서 생성에 실패한 항목은 뺀다. '다시 시도'를 눌러도 제외 목록은 유지한다.

**프롬프트 규칙.**
- 영역과 세부 항목 목록은 시험 팩에서 받아 프롬프트에 넣는다. 프롬프트 문자열에 직접 적지 않는다.
- 수집한 본문은 "참고 자료(data)" 블록으로 감싸서 지시로 취급하지 않게 한다(프롬프트 주입 방어).
- 학습자가 넣은 샘플 문제는 문체를 보여 주는 예시(few-shot)로 쓴다(D39). 연습 문제 은행(`data/practice/<exam_id>/bank.jsonl`, `practice import`로 생성)에서 같은 세부 항목의 문항 2개를 골라 넣고, "사실의 근거가 아니며 상황·문장·보기를 재사용하지 말 것"을 함께 적는다. 중복 지문, 정답과 해설이 맞지 않는 문항, 출처가 주의를 표시한 문항은 예시로 쓰지 않는다.
- 베끼기 검사(D39): 생성한 지문이 같은 세부 항목의 은행 문항 지문 단어를 60% 넘게 담고 있으면 validator 실패와 같이 재생성한다(포함률. 실제 서로 다른 문항끼리는 최대 50%).
- 연습 문제 은행은 검색 색인에 넣지 않는다. 정답이 검증되지 않았으므로 근거 검증이 그것에 기대면 안 된다. 은행 문항을 그대로 학습자에게 출제하지도 않는다(규칙 2).
- 시험 팩의 `scenarios`(공식 시나리오 6개, 영역별 `primary_domains`)로 문제 상황을 잡고, `out_of_scope`(출제 제외 주제 16개)는 프롬프트에 '내지 말 것'으로 넣는다(`prompt_out_of_scope()`).

## 8. 검색 (D3, D17, D36)

```
질의(영역 이름 + 세부 항목 제목) ──┬─▶ FAISS(bge-m3, 코사인) 순위
                                  └─▶ BM25(한국어 글자 bigram 포함) 순위
                                         └─▶ 가중 RRF(0.5/0.5, c=60) → task_id 태그가 있는 청크 먼저, 모자라면 전체 순위로 채움 → 상위 k(기본 6)
```

- 구현: `src/misslens/retrieval/`(`tokenize.py`, `embed.py`, `index.py`), Generator 연결은 `agents/generator/context.py`의 `RetrievalContext`. 세부 항목 설명 1개 + 검색 청크 k개를 근거로 넘기고 `grounding="retrieved"`로 표시한다. 색인이 비었으면 세부 항목 설명만 쓰고 `task_statement_only`로 표시한다.
- 색인 파일: `data/index/<exam_id>/`(`chunks.jsonl`, `faiss.index`, `meta.json`). 다른 임베딩 모델로 만든 색인은 열지 않는다(다시 넣어야 함). BM25는 열 때와 추가할 때 메모리에 다시 만든다.
- bge-m3는 프로세스당 한 번만 불러온다(`get_embedder()`, Streamlit에서는 `st.cache_resource`로 감싼다). 모델 이름은 `config/models.yaml`의 `embedding`에 둔다. 로컬 캐시를 먼저 쓰고, 없을 때만 내려받는다.
- BM25는 공백으로만 단어를 나누면 한국어를 제대로 다루지 못한다(조사가 붙음). 그래서 한글은 글자 bigram으로, 영어는 `tool_choice`, `--allowedTools`, `.claude/settings.json` 같은 시험 용어를 한 단어로 두고 부분 단어도 함께 넣는다.
- task_id 필터를 '태그가 있는 청크만'으로 하지 않고 '먼저 + 채움'으로 한 이유: 태그가 틀리거나 없을 때 의미·키워드 검색이 안전망이 되게 하려는 것이다(D17).
- 질의 커버리지 가중(D51): BM25 가중치 = 0.5 × (질의 단어 중 색인 어휘에 있는 것들의 idf 합 ÷ 전체 단어 idf 합, 색인에 없는 단어는 가장 드문 단어의 idf로 셈). 영어 질의는 1.0이라 그대로이고, 한국어 질의에 흔한 영어 단어 몇 개만 섞인 경우 BM25가 의미 검색 순위를 끌어내리지 않는다.
- `search(..., max_tagged=N, where=fn)`: 태그 일치 청크를 앞에 최대 N개만 두고 나머지는 전체 관련도 순으로 채운다. `where`로 출처를 제한한다(코치의 `source` 필터, D49).
- 상위 k를 채우면서 이미 고른 청크와 단어 Jaccard가 0.8을 넘는 청크는 건너뛴다(D46). Bedrock·Google Cloud 코스가 Claude API 코스 레슨을 거의 그대로 반복하기 때문이다.

**검색 평가(D50).** 자세한 지표 정의·질의 추가법·기준선 절차·점수 기록은 [docs/testing.md](docs/testing.md). `retrieval/evaluate.py`, 정답 질의 `evals/<exam_id>/retrieval.yaml`, 기준선 `evals/<exam_id>/retrieval_baseline.json`.
- 정답 질의 30개(영역별 + 코스 전용, 3분의 1은 한국어): 청크 ID가 아니라 내용(포함 문구 + 출처)으로 정답을 판정해 다시 청킹해도 유지된다. 색인에 정답이 없는 질의는 '답할 수 없음'으로 따로 보고하고 채점하지 않는다.
- 점수: 세 가지 모드(일반 = 자유 질문, 코치 = 태그 ≤2, 출제 = 태그 우선 k=6)마다 hit@1·3·5·10, MRR@10, 영역별 hit@5, 상위 5개의 자료원 수와 중복 쌍. 출제 근거 품질: 30개 세부 항목마다 Generator 질의의 상위 6개 중 태그 일치 비율과 자료원 수.
- 색인 건강: 벡터 수·정규화, 잡음 패턴(알려진 패턴 + `sources.yaml` 규칙) 0건, 시험 팩에 없는 태그 0건, 40자 미만 청크 0건, 모든 세부 항목에 자료. 이 중 하나라도 실패하거나 지정한 지표(일반·코치·출제 hit@5, 일반 MRR, 태그 일치 비율)가 기준선보다 0.05 넘게 떨어지면 실패.
- 실행: `python -m misslens.cli retrieval-eval [--save-baseline] [--json]`, `pytest -m realdata`(기본 테스트에서는 제외). 2026-11-05 재정리 전 색인은 건강 검사 실패(잡음 555청크), 재정리 후 통과·hit@5 0.93 → 0.97.

**Ingest.** `agents/ingest/`(`loaders.py`, `tagger.py`, `pipeline.py`).
- 형식: PDF(쪽 번호 표시), MD/TXT, HTML(script·nav·header·footer 제거), 붙여넣기. 웹에서 저장한 파일 앞의 `BEGIN UNTRUSTED EXTERNAL CONTENT (source: URL)` 표시는 지우고 URL을 출처로 쓴다.
- 청크: `RecursiveCharacterTextSplitter`, 1200자, 겹침 150자, 40자 미만은 버림.
- 태깅: Haiku 4.5(`tagger` 역할), 8개씩 묶어 호출, 청크당 최대 3개, 시험 팩에 없는 ID는 버림, 호출이 실패하면 그 묶음은 미분류로 두고 계속한다. 청크 태그는 색인(`chunks.jsonl`)에 두고, `materials`에는 문서 단위 행만 둔다(`task_id`는 NULL).
- 중복: 공백을 정규화한 본문의 sha256(`content_hash`). 색인을 먼저 쓰고 `materials` 행을 나중에 넣는다. 중복 판단은 색인 기준이라, 중간에 멈춰도 '행은 있는데 청크는 없는' 상태가 생기지 않는다.
- CLI: `python -m misslens.cli ingest <파일|폴더>...`, `--paste <이름>`(표준 입력), `search "<질의>" [--task 2.1]`. `quiz`는 색인이 있으면 자동으로 검색 근거를 쓴다.

## 9. Collector (D12, D14, D16, D38)

| 항목 | 규칙 |
|---|---|
| 로그인 | `collect login <site>` → Crawl4AI `BrowserProfiler`로 보이는 Chromium을 띄우고 사용자가 직접 로그인·수강 등록, 터미널에서 q로 종료. 에이전트는 비밀번호를 입력·저장하지 않음 |
| 프로필 위치 | `secrets/profiles/<site>/` (권한 0700, git 제외, 로그에 남기지 않음). 쿠키는 OS 키체인 대신 Crawl4AI의 이식형 설정(`--use-mock-keychain`)으로 저장되고, 수집 브라우저도 같은 설정으로 읽는다 |
| 수집 대상 | `exams/<exam_id>/sources.yaml`에 적힌 URL만, headless, `BrowserConfig(user_data_dir=…, use_managed_browser=True)`. `expand: lessons`인 코스 페이지는 **같은 호스트·같은 코스 경로 바로 아래 한 단계** 링크만 레슨으로 추가(D38). 쿼리 문자열, logout·checkout 등 경로는 제외 |
| 대기 방식 | `wait_until="domcontentloaded"` + 2초. 분석 스크립트가 계속 통신하는 사이트에서는 `networkidle`이 끝나지 않음(Skilljar 실측) |
| 안전 | 읽기 전용(폼 제출·클릭·다운로드·스크립트 주입 없음), 요청 간격 `delay_seconds`(2초 이상, 설정 검증), https이고 `allowed_domains` 안의 URL만 |
| 중복 | 저장 파일은 내용이 같으면 그대로 둠. Ingest가 `content_hash`로 다시 한 번 거름 |
| 화면 장식 제거(D48) | `sources.yaml`의 사이트별 `drop_lines`(줄 단위 정규식)와 `drop_blocks`(시작~끝 줄, 영상 플레이어 메뉴 등)로 저장 전에 지운다. 규칙을 바꾸면 `collect reindex <site>`로 저장된 파일을 다시 정리하고 그 사이트의 청크만 색인에서 바꾼다(재수집 없음, 다른 자료는 그대로). 정리 후 본문이 없는 영상 전용 레슨은 색인에서 빠진다 |
| 반입 조건 | 사내 자료 반입 승인·교안 사이트 약관 확인 완료(2026-11-05) |
| 대체 경로 | 추출이 실패하거나 본문이 없는 레슨은 페이지를 PDF/HTML로 저장해 `ingest`로 업로드 |
| 구현 | `agents/collector/`(`sources.py` 설정 검증, `browser.py` 로그인·수집기, `graph.py` 서브그래프, `runner.py` interrupt 처리·재개). CLI `collect login|run [site] [--thread] [--limit] [--no-ingest]` |

## 10. Streamlit 연동 (D42)

| 항목 | 방식 |
|---|---|
| 탭 | 학습(바로 풀기·풀이·리뷰 폼·진단·문제 이상) / 자료(업로드·붙여넣기·수집·로그인 대기·색인 현황) / 현황(Reporter) / 계획(interrupt① 승인) + 사이드바 자연어 요청(F10) |
| 입력 | 사이드바 요청과 코치 질문은 여러 줄 입력 칸을 폼으로 감싸 **Ctrl+Enter(맥 ⌘+Enter)로 제출**, Enter는 줄바꿈. 사이드바 폭 380px, 보낸 요청을 그대로 다시 보여 줌 |
| 요청 처리 | 모든 에이전트 요청은 Supervisor 그래프로 보내고, thread_id별 **백그라운드 작업**으로 실행한다. 화면은 작업 상태를 확인해 진행 중이면 1.5초 뒤 다시 그리고, interrupt가 있으면 체크포인터에서 읽어 승인·로그인 화면을 보여 준다. 다시 그리기는 모든 탭을 그린 뒤 한 번만 한다(오래 걸리는 수집이 다른 탭을 가리지 않게) |
| interrupt 재개 | 계획 thread_id와 진행 중인 세트 id를 `st.query_params`(`?plan=…&set=…`)에 둔다. 새로고침하면 체크포인터에서 같은 interrupt를 다시 읽고, 세트는 ledger의 `ready` 문제로 다시 구성한다 |
| DB 연결 | `st.cache_resource`로 Ledger·Services·그래프·bge-m3를 프로세스당 하나만 만든다. Ledger는 연결 1개 + Lock, 체크포인트는 `checkpoints.sqlite`(SqliteSaver)로 학습 기록 DB와 분리 |
| 세트 스트리밍 | 백그라운드 스레드가 `questions`에 순서대로 기록하고, 화면은 ledger의 `ready` 문제와 `QuestionStream.poll()`(막히지 않는 이벤트 확인)로 다음 문제를 보여 준다. 풀이·리뷰는 일반 위젯이며 interrupt가 없다(규칙 4). 제출은 Supervisor `submit` 경로 |
| 로그인 | 화면에서 브라우저를 띄우지 않는다. 앱이 도는 컴퓨터에서 `collect login <site>`로 로그인한 뒤 '완료'를 누르면 같은 페이지부터 이어서 수집 |
| 구현 | `src/misslens/ui/app.py`, 진입점 `app/main.py`(`streamlit run app/main.py`). 테스트는 Streamlit AppTest + 메모리 DB·가짜 LLM(`tests/test_ui.py`) |

## 11. 디렉토리 구조 (2026-11-05 구현 기준)

```
misslens/
├── CLAUDE.md       # 에이전트 작업 지침 (매 세션 로드)
├── SERVICE.md  ARCHITECTURE.md  TASK.md  README.md  TODOS.md
├── pyproject.toml
├── config/models.yaml
├── exams/ccar-f/{exam.yaml, sources.yaml}   # sources.yaml: Collector 대상(§9)
├── src/misslens/
│   ├── exam_pack.py   llm.py   bedrock.py(설정 로드·오류 분류)   ledger/   retrieval/(tokenize·embed·index)
│   ├── practice.py    # 연습 문제 은행: 문체 예시·베끼기 검사(D39)
│   ├── paths.py       # 학습 기록·체크포인트 위치, 샌드박스(D45)
│   ├── agents/{supervisor, collector, ingest, planner, generator, grader, reporter}/
│   ├── cli.py      # rich CLI (`python -m misslens.cli quiz|plan|ask|status|ingest|search|collect|practice`)
│   ├── ui/app.py   # Streamlit 화면(§10)
│   └── graph.py    # Supervisor 그래프: route → planner/generate/report/collect_<site>/ingest/submit (D41)
├── app/            # Streamlit 진입점(main.py) → src/misslens/ui/app.py
├── tests/          # pytest (live 마커 분리), fixtures/eval_misses.yaml(T1-a 실제 오답, eval용)
├── scripts/probe_models.py   # 엔드포인트 × 모델 조합 응답 확인
├── scripts/draw_graph.py     # 그래프 그림 → docs/graphs/*.mmd (README에 포함)
├── docs/           # demo.md(데모 시나리오), graphs/(생성된 그래프 그림)
├── data/           # ledger.sqlite, checkpoints.sqlite, index/<exam>/, sources/{exam_guide,collected,practice_problems}/, practice/<exam>/bank.jsonl  (git 제외)
└── secrets/        # 로그인 프로필 (git 제외)
```

## 12. 기술 결정 기록

| ID | 결정 | 검토한 대안 | 이유 |
|---|---|---|---|
| D1 | 서브그래프 6개 + Supervisor | 단일 그래프 + 모듈 소유 | 멀티 에이전트 구조를 그대로 보여 줌 (사용자 선택) |
| D3/D17 | bge-m3 + BM25 하이브리드 | Titan 임베딩, BM25만, 태그 필터 + BM25 | 임베딩 모델 접근 없음. 조건 문구는 키워드 검색이 강하고, 태그가 틀렸을 때 의미 검색이 안전망이 됨 |
| D4 | 역할별 모델 | 모두 Sonnet, 출제만 Sonnet | 품질이 중요한 곳에만 Sonnet을 쓰고, 검증은 다른 모델로 해서 오류 상관을 줄임 |
| D5 | `us.` 프로필 기본 | `global.` 기본 | 데이터 처리 지역을 좁게 유지. 호출 제한에 걸리면 전환 (D26으로 확장) |
| D26 | 리전 3개 엔드포인트 순차 전환: us-east-1 `us.` → us-west-2 `us.` → 각 리전 `global.`. 리전별 사용 가능 모델 표(`availability`)에 없는 조합은 호출하지 않음 | 단일 리전 + `global.` 수동 전환(D5) | 과정 중 일부 Sonnet 할당량 초과로 리전 추가(사용자 요청). ap-northeast-2 `apac.`에는 이 프로젝트가 쓰는 모델이 없어 제외 |
| D27 | Bedrock 설정 로드·엔드포인트 순서·오류 분류를 `bedrock.py`로 분리. `ValidationException`은 모델 ID 오류일 때만 '사용 불가', 나머지는 요청 오류로 즉시 실패. 자격 증명 오류는 전환 없이 중단 | 모든 `ValidationException`을 '사용 불가'로 처리, 분류를 `llm.py` 안에 둠 | 요청 오류를 엔드포인트 문제로 오인하면 정상 엔드포인트가 모두 막히고 폴백 모델로 잘못 넘어감. probe와 `llm.py`가 같은 규칙을 쓰게 함 |
| D28 | 판정 표를 `ledger/judge.py` 순수 함수로 두고 ledger가 리뷰 저장 시 직접 적용 | Grader가 판정해 결과만 넘김 | 판정 결과를 밖에서 받으면 표와 다른 값이 조용히 저장될 수 있음(규칙 3). Grader는 화면 표시용으로 같은 함수를 씀 |
| D29 | 쿨다운·사용 불가를 (엔드포인트, 모델) 단위로 관리. 오래된(24시간 초과) probe 결과는 무시 | 엔드포인트 단위 쿨다운 | 할당량은 모델별. 엔드포인트 단위면 Sonnet 할당량 초과가 Haiku까지 막음 |
| D30 | v1은 정답 1개 문항(`scenario_mcq_4`)만 출제 | 실제 시험처럼 복수 정답 문항도 출제 | 시험 가이드상 복수 정답 문항이 있지만, 스키마(`answer_index`)·`violates`·판정 표·채점이 모두 단일 정답 기준이라 바꾸면 마감 위험이 큼. v1 이후 과제로 둠 |
| D31 | 미끼 기준선은 보정식이나 목표값 없이 해석 실수 비율과 나란히 보여 줌. 진단 효과는 같은 학습자의 기간별 변화로 판단 | 보정식(예: 해석 실수 비율 ÷ 기준선), 고정 목표값 | 기준선은 사람마다, 같은 사람도 학습 진행에 따라 달라짐(사용자 의견). 표본이 작은 초기에 보정식을 쓰면 값이 크게 흔들림 |
| D32 | 재시도를 botocore가 아니라 라우터가 오류 종류별로 수행(속도 제한: 지수 백오프 최대 5회 시도, 일시 오류: 1회, 할당량 초과: 없음). botocore 재시도는 끔 | botocore adaptive 모드(D13 원안) | adaptive는 타임아웃·5xx·일일 한도도 5회씩 재시도해서 §6.3 규칙과 어긋나고, 타임아웃이면 엔드포인트 하나에 최악 약 30분이 걸림(코드 리뷰에서 발견). 대신 adaptive의 클라이언트 측 속도 조절은 잃지만 호출이 순차라 영향이 작음 |
| D33 | `key_constraints`는 지문에 글자 그대로 있어야 하고(validator), 보기 순서는 코드가 섞는다 | 모델이 쓴 그대로 사용 | 리뷰에서 놓친 문구를 지문 안에 강조하려면 글자 그대로 있어야 함. 모델은 정답을 앞쪽에 두는 경향이 있음 |
| D34 | 근거 검증에 4번째 검사(지어낸 플래그·파라미터, 보기 내부 모순) 추가. 근거 자료 없이는 실사용하지 않음 | 3가지 검사 유지, 근거 없을 때 느슨하게 통과 | 실측에서 없는 플래그를 정답으로 한 문제가 통과됨. 틀린 사실을 외우게 하는 것은 시험 준비에 해로움 |
| D35 | CLI는 `src/misslens/cli.py` 모듈(`python -m misslens.cli`) | 저장소 루트 `cli/` 디렉터리(§11 원안) | CLAUDE.md의 실행 명령과 맞추고 패키지 안에서 import 경로를 단순하게 유지 |
| D36 | 하이브리드 검색을 직접 구현: FAISS(`IndexFlatIP`)를 바로 쓰고, RRF(가중치 0.5/0.5, c=60)는 EnsembleRetriever와 같은 공식으로 직접 계산. task_id 필터는 '태그 청크 먼저 + 전체 순위로 채움' | LangChain `EnsembleRetriever` + `langchain_community` FAISS·BM25Retriever(§8 원안) | langchain 1.x에서 `EnsembleRetriever`는 레거시 패키지(`langchain-classic`)로 옮겨졌고 `langchain-community`는 지원 종료 예정. 직접 구현이 30줄 정도이고 가짜 임베더로 테스트하기 쉬움. 필터만 쓰면 태그가 틀렸을 때 근거가 사라짐 |
| D37 | 시험 팩의 세부 항목 모델에 `extra="forbid"`, 제목은 따옴표로 감쌈(exam.yaml v3) | 기본값(모르는 키 무시) | YAML 흐름 매핑 `{id: x, title: A, B, C}`에서 쉼표 뒤가 별도 키로 읽혀 제목 4개(1.3·1.7·3.1·4.4)가 첫 쉼표에서 잘린 채 프롬프트에 들어가고 있었음. 전체 가이드 PDF(39쪽)와 30개 제목을 대조해 발견 |
| D38 | Collector가 `expand: lessons` 코스 페이지에서 그 코스의 레슨 링크(바로 아래 한 단계)만 따라감 | v1은 적힌 URL만(링크 따라가기는 v1 이후), 레슨 URL을 사람이 모두 적기 | Skilljar 레슨 URL은 로그인·수강 등록 후에만 보이고 코스 7개에 레슨이 수십 개라 손으로 적을 수 없음. 범위를 같은 코스 경로 한 단계로 묶어 도메인 밖이나 다른 페이지로 번지지 않게 함 |
| D39 | 학습자가 브라우저에서 직접 저장한 제3자 연습 문제(CertSafari 250문항, 자동 수집 아님: 규칙 7)를 구조화해 문체 예시와 베끼기 검사에만 사용 | 검색 색인에 추가, 변환해 바로 출제, 사용 안 함 | 형식·세부 항목 태그가 정확해 문체 예시로 좋음. 정답이 사이트 것이라 근거로 쓰면 검증이 순환하고, 그대로 출제하면 규칙 2(검증 통과 문제만)를 우회함. 실측에서 예시를 넣어도 validator 통과율은 떨어지지 않음(3/4 대 3/4) |
| D40 | `QuestionDraft` 필드 순서를 `stem_en → key_constraints → … → options`로 바꿈, 리스트 필드가 JSON 문자열로 오면 풀어서 받음 | 기존 순서(보기 다음에 key_constraints) | 근거 자료를 붙인 뒤 첫 시도 validator 통과율이 1/8로 떨어짐(모델이 가이드 문장을 key_constraints로 씀). 순서를 바꾸자 6/8. tool calling이 리스트를 문자열로 보내 실패하는 경우를 실측으로 확인 |
| D41 | Supervisor 그래프는 Planner·Collector를 서브그래프 노드로, Generator·Reporter·Ingest·Grader(제출)는 함수 노드로 둔다. 진행 중인 문제 스트림과 수집 브라우저는 상태가 아니라 `Services`에 두고 상태에는 id만 둔다. 영역별 통계는 ledger `domain_summary`로 추가 | 모든 에이전트를 서브그래프로, 스트림을 상태에 저장 | interrupt가 있는 두 에이전트만 서브그래프여야 부모 체크포인터로 재개됨. 스레드·브라우저 객체는 체크포인트에 직렬화할 수 없음. 풀이·리뷰는 interrupt 없이 UI가 진행하고 결과만 `submit`으로 보냄(규칙 4) |
| D42 | Streamlit은 Supervisor 요청을 thread_id별 백그라운드 작업으로 돌리고 상태를 폴링, 계획 thread_id·세트 id를 URL에 둠, 다시 그리기는 페이지 끝에서 한 번 | 요청을 스크립트 안에서 동기 실행, 탭마다 바로 `st.rerun` | 수집처럼 긴 작업이 페이지를 멈추지 않게 하고, interrupt는 체크포인터에서 읽어 새로고침에도 이어짐(T9 완료 기준). 탭 안에서 바로 다시 그리면 뒤 탭이 그려지지 않음 |
| D43 | 진단 eval 데이터(학습자 자기 판단)를 리뷰 흐름 안에서 모은다: 정답 공개 → 자기 판단 → 판정·해설. 별도 테이블 `miss_feedback`, 일치율은 ledger 통계 | 학습자가 따로 시간을 내 오답을 기록(T1-a 원안), 판정 화면에서 동의 여부만 묻기 | 따로 기록할 시간이 없음(학습자 의견). 판정을 먼저 보여 주면 동의 쪽으로, 해설을 먼저 보여 주면 해석 쪽으로 치우침. 판정 표(규칙 3)와 섞이지 않게 테이블을 분리 |
| D44 | 오답 해설을 tool 사용 에이전트(코치)로: 색인 검색·학습 기록 조회 tool을 모델이 골라 쓰고 근거 출처를 표시. 출제·판정·기록 흐름은 계속 코드가 결정 | Supervisor나 Generator를 tool 에이전트로, MCP 서버 공개, tool 없음 | 과정 평가에서 tool 사용을 보여 주되 규칙 1~3(통계·검증·판정)을 모델 호출 순서에 맡기지 않음. 해설은 읽기만 하므로 tool 선택을 맡겨도 기록이 틀어지지 않음. 실호출로 Converse toolUse/toolResult 동작 확인 |
| D45 | 샌드박스 모드(`MISSLENS_SANDBOX=1`): 학습 기록·체크포인트를 `data/sandbox/`로, 자료 넣기·수집은 CLI·Supervisor·화면에서 모두 거부 | 실제 DB로 검토, DB 경로 인자 | 수집이 실제 DB를 쓰는 동안에도 검토·리허설을 할 수 있고, 시연용 오답이 실제 통계(정답률·진단 일치율)에 섞이지 않음. 색인·수집본은 공유하므로 쓰기 명령은 막음 |
| D46 | 검색 결과에서 거의 같은 청크(단어 Jaccard > 0.8) 제거 | MMR, 색인 단계 중복 제거 | 코스 간 반복 문단이 상위를 채우면 근거 다양성이 떨어짐. 검색 단계 처리라 이미 만든 색인을 다시 만들 필요가 없음 |
| D47 | 코치 해설은 기본 해설을 대체하지 않고 아래에 붙이고, 같은 오답에 대해 이어서 질문할 수 있게 함(오답별 체크포인터 thread, 출처 링크) | 코치 해설로 덮어쓰기(T13 구현), 별도 채팅 화면 | 리허설에서 학습자가 기본 해설이 사라지는 것과 개념 질문을 이어 갈 곳이 없는 것을 지적. 코치가 이미 LangGraph 메시지 그래프라 thread만 두면 대화가 됨. 읽기 전용이라 규칙 1~4와 충돌 없음 |
| D48 | 사이트별 화면 장식 제거 규칙을 `sources.yaml`에 두고 저장 전에 적용, `collect reindex`로 기존 수집본을 재수집 없이 다시 정리·색인 | 코드에 Skilljar 전용 정리 로직, 다시 수집 | 수집 후 청크의 29%(491/1,674)에 영상 플레이어 메뉴 글자가 섞여 검색을 흐림. 사이트마다 장식이 다르므로 설정으로. 재수집은 1시간 이상 걸리고 불필요 |
| D49 | 코치 검색 tool에 `source` 필터와 색인 구성 설명, 태그 일치 청크 상한 2개 | 프롬프트만 수정, 태그 우선 그대로 | 리허설에서 코치가 'Skilljar 자료는 색인에 없다'고 잘못 답함: tool 설명에 수집 레슨이 없다는 정보가 없었고, 태그된 가이드 청크가 검색 결과를 모두 차지함 |
| D50 | 실제 색인을 점수로 보는 검색 평가(정답 질의 30개·세 모드·출제 근거 품질·색인 건강)와 기준선 회귀 검사 | 가짜 임베더 단위 테스트만, 눈으로 검색 확인 | 오늘 잡음 29%·코치의 'Skilljar 없음' 오답이 단위 테스트를 모두 통과한 채로 나옴. 청킹·정리·랭킹을 바꿀 때 숫자로 비교해야 함. 내용 기준 판정이라 다시 청킹해도 정답 세트가 유지됨 |
| D51 | RRF에서 BM25 가중치를 질의 커버리지(idf 기준)에 비례하게 | 질의 번역(Haiku), 한영 용어집, 단어 개수 비율 | 한국어 질의 'Claude Code 권한 모드…'에서 BM25가 흔한 단어 claude·code만 보고 정답을 41위로 밀어냄(의미 검색은 2위). 번역은 검색마다 모델 호출, 용어집은 관리 부담. 단어 개수 비율(0.2)로는 부족해 idf로(0.04). 결과: hit@5 일반·코치 0.97→1.00, 출제 0.93→0.97, 회귀 없음 |
| D6/D20 | 조건 분기 + 자유 발화만 LLM | 모든 입력을 LLM이 분류 | 버튼 입력의 지연과 오분류를 없애고 자연어 지시는 유지 |
| D8 | 세트 풀이 + 리뷰 시 진단 | 문제마다 즉시 채점 | 실전과 같은 조건, 풀이 시간 데이터, 구현 단순화 |
| D9 | ledger가 통계를 단독 소유 | 서브그래프마다 계산 | 숫자가 어긋나는 문제를 원천 차단 |
| D10 | 세트 + 문제별 행(6개 테이블) | 세트를 JSON 1행으로 저장 | 질의하는 값은 컬럼으로, 문제 본문만 JSON으로 |
| D11 | 범위 먼저, 그다음 약점 | 처음부터 가중 무작위 | 범위 80% 목표를 보장 |
| D12/D23 | pytest 단위 + E2E 2 + 스모크 + eval, 기능별로 함께 작성 | 단위만, 수동, 위험 순 작성 | 조용히 틀리는 규칙을 테스트로 고정 |
| D13/D18/D19 | 순차 생성 + 스트리밍 + 타임아웃 180/60초 | 동시 3, 다음 세트 미리 생성 | 호출 제한 안전성을 지키면서 첫 문제 대기를 줄임 |
| D14 | 재질문에 미끼 섞기 + 기준선 | 오답만 재질문 | '틀린 문제'라는 단서와 재독 효과를 보정 |
| D15 | 근거 자료로 검증 | 정답 가리고 풀기 | 사실 근거를 확인하고, 어려운 문제가 걸러지지 않게 함 |
| D16 | Collector v1 포함 | 시험 후로 연기 | 교안 대부분이 로그인 웹에 있음 (사용자 선택) |
| D21 | interrupt: 계획 승인 + 로그인 대기 | 되묻기, 태그 확인 | 사람이 직접 해야 하는 지점에만 둠 |
| D22 | exam.yaml version + 첫 세트 전 확정 | 나중에 확정 | 학습 기록의 기준 키를 보호 |
| Crawl4AI | Crawl4AI | Playwright 직접, AgentCore Browser, ego lite, browser-use, Stagehand | URL을 이미 알고 있고 본문만 필요함. Python 네이티브. 실패 시 Playwright로 내려갈 수 있음 |

## 13. 확인이 필요한 사항

| 항목 | 확인 방법 | 결과에 따른 영향 |
|---|---|---|
| 엔드포인트 5개 × 모델 4개 조합이 지금 응답하는지(할당량 초과 여부) | `scripts/probe_models.py` → `data/model_status.json` | 할당량 초과 조합은 시작부터 쿨다운. 모델 존재 여부는 §6.2 표로 확인 완료. **2026-11-04 확인: 20개 모두 응답, 할당량 초과 없음, Sonnet 4.6 구조화 출력 성공** |
| bge-m3 다운로드와 로드 | 개발 시작 직후 확인 | 실패하면 검색 방식을 다시 결정. **2026-11-04 확인: 1024차원, MPS 로드 성공 → D17 유지** |
| CCA-F 공식 세부 항목(30개 추정) | 과정 자료 | `exam.yaml` 확정(첫 실제 세트 전). **2026-11-04 확인: 시험 가이드 v1.0 기준 30개, `exam.yaml` version 2(final). 2026-11-05 전체 가이드 PDF로 재대조, 제목 잘림 수정해 version 3(D37)** |
| 사내 자료의 외부 모델 반입 승인 | 담당자 확인 | 승인 전에는 공개 자료만 사용. **2026-11-05 승인** |
| 교안 사이트 이용약관 | 약관 확인 | 허용하지 않으면 수동 저장 방식 사용. **2026-11-05 확인, Collector로 수집** |
| 과정 평가 기준(HITL·RAG·멀티 에이전트) | 강사 확인 | 데모 시나리오 조정. **여러 패턴 권장, 학습자 우선순위: 멀티 에이전트 > RAG > HITL** |
