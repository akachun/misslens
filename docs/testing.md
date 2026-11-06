# MissLens 테스트와 평가

무엇을 어떤 테스트로 지키는지, 언제 무엇을 돌리는지, 검색 품질 점수를 어떻게 읽고 갱신하는지 정리한 문서다. 설계 근거는 [ARCHITECTURE.md](../ARCHITECTURE.md)(§8, D50·D51), 작업 규칙은 [CLAUDE.md](../CLAUDE.md)에 있다.

## 1. 테스트 계층

| 계층 | 무엇을 지키나 | 어디 | 실행 | 비용 |
|---|---|---|---|---|
| 단위·통합 | 규칙 1~7(통계는 ledger만, 검증 통과 문제만, 판정 표, interrupt 두 곳, 설정 위치, 보안), 각 에이전트 로직, 그래프 분기·재개 | `tests/test_*.py` | `pytest` (기본) | 없음. LLM은 `tests/fakes.py`의 `FakeRoleLLM`(GenericFakeChatModel), 임베딩은 `FakeEmbedder` |
| 화면 | 실제 앱 스크립트의 흐름(계획 승인 새로고침 복원 → 풀이 → 리뷰 → 자기 판단 → 진단 → 코치 대화 → 현황) | `tests/test_ui.py` | `pytest` (기본) | 없음. Streamlit AppTest + 메모리 DB |
| 실호출 스모크 | Bedrock이 실제로 구조화 출력·tool 호출을 받아 주는지(문제 생성, 라우터, Planner, 코치) | `tests/test_*_live.py` | `pytest -m live` | Bedrock 할당량 사용(9개 약 1분) |
| **검색 품질(실제 색인)** | 실제 색인의 검색 점수와 건강 상태가 기준선보다 나빠지지 않았는지 | `tests/test_retrieval_realdata.py`, `evals/ccar-f/` | `pytest -m realdata` 또는 `python -m misslens.cli retrieval-eval` | 없음(로컬 bge-m3, 약 8초) |
| 진단 일치율 | 시스템 판정(개념/해석)이 학습자 자기 판단과 맞는지 | 앱 사용 중 자동 수집(`miss_feedback`) | `python -m misslens.cli eval [--export]` | 없음 |

기본 `pytest`는 `live`와 `realdata`를 뺀다(`pyproject.toml`의 `addopts`). 모든 테스트는 수집 폴더를 임시 폴더로 바꿔 실제 `data/`에 쓰지 않는다(`tests/conftest.py`).

### 테스트 파일 지도

| 파일 | 무엇을 | 관련 결정 |
|---|---|---|
| `test_bedrock_config.py`, `test_llm.py` | 모델 설정 로드, 엔드포인트 전환·쿨다운·폴백, `bind_tools`도 전환 | D26·D27·D29·D32·D44 |
| `test_exam_pack.py` | 시험 팩 검증(제목 잘림 방지 포함) | D37 |
| `test_ledger.py` | 테이블·제출/리뷰 트랜잭션·통계 규칙 | D9·D10·D28 |
| `test_generator.py` | 세부 항목 선택, validator(따옴표 정규화 포함), 근거 검증, 재생성, 스트림 | D11·D33·D34·D40 |
| `test_grader.py`, `test_cli_e2e.py` | 재질문 폼·판정·해설, CLI 전체 흐름(자기 판단 포함) | D14·D28·D43 |
| `test_ingest.py`, `test_retrieval.py` | 로더·청크·태깅·중복 제거, 하이브리드 검색·RRF·태그 상한·출처 필터·중복 제거·커버리지 가중 | D36·D46·D49·D51 |
| `test_collector.py` | `sources.yaml` 검증, 레슨 펼치기, 로그인 대기 interrupt와 재개, 화면 장식 제거, 재색인 | D38·D48 |
| `test_practice.py` | 연습 문제 은행 파싱, 문체 예시, 베끼기 검사 | D39 |
| `test_supervisor.py` | 라우팅, 계획 interrupt, 수집 경유 interrupt, 제출, 그래프 그림 동기화 | D41 |
| `test_coach.py` | tool 사용 코치, 대화 이어 가기, 해설 없이 질문, 맞힌 문제 질문, 출처 | D44·D47·D49 |
| `test_eval_feedback.py`, `test_eval_fixture.py` | 자기 판단 저장·일치율·내보내기, T1-a 픽스처 형식 | D43 |
| `test_sandbox.py` | 샌드박스 경로, 공유 자료 쓰기 거부 | D45 |
| `test_resume.py` | 계획 기록, 제출 전 답안, 생성 재개, 세트 기록 출처 | D52·D53 |
| `test_vocab.py` | 단어장 추출(글자 그대로·긴 구절 필터·빠진 문제 재요청), 누적·외움 | D54 |
| `test_handoff.py` | 평가용 zip: 거부 목록, 자격 증명 검사, SQLite 복사, 포함 목록(git 체크아웃에서만) | D56 |
| `test_portability.py` | Windows 대비: 텍스트 파일 읽기·쓰기에 `encoding='utf-8'` 명시(한국어 Windows 기본 cp949) | D56 |
| `test_trace.py` | 로컬 트레이스: 라우터의 모델 호출 기록(전환·토큰·본문 제외), span 중첩·오류, 그래프 노드와 노드 안 호출, 끄기, 요약·CLI | D60 |
| `test_question_eval.py` | 문제 품질 라운드 평가: 인-아웃 세트 파일, 직접 풀기(정답 숨김)·분해 판정·요약 계산, 생성 통계, 리포트·앞 라운드 비교, CLI(1라운드·다시 채점) | D59 |
| `test_qa.py` | 질의 API: 입력 가드레일(막을 것·통과시킬 시험 주제), 차단 입력은 모델 호출·trace 없음, 출력 가드레일, 기능 요청 안내·현황 수치, 근거·단계 기록, `POST /query` 규약, `test_queries.csv` 스키마, 통과 판정·리포트, RAGAS import | D61 |
| `test_review_export.py` | 상위 모델 검토용 내보내기: 제출한 세트만, 정답·violates·근거 부록, 근거 모드, 자료 속 Markdown을 코드 블록으로 | D58 |
| `test_model_facing_text.py` | 모델에 보내는 구조화 출력 스키마(`QuestionDraft`·`PlanDraft`)와 코치 도구 설명이 영어로 남아 있는지(주석은 한국어) | D57 |
| `test_ui.py` | Streamlit AppTest: 전체 흐름, 새로고침 복원, 코치 대화, 입력 폼, 이어 풀기, 지난 세트, 단어장 | D42·D47·D52~D54 |
| `test_retrieval_eval.py` / `test_retrieval_realdata.py` | 검색 평가 로직 / 실제 색인 회귀(`-m realdata`) | D50 |
| `test_*_live.py` | Bedrock 실호출 스모크 9개(`-m live`) | — |

2026-11-06 최종 확인: 기본 359 통과(질의 API·RAGAS 평가 D61, 단어장 표시 수정, Codex 리뷰 반영 이후), `-m realdata` 1 통과(T21 이후), `-m live` 9 통과(T20 시점, 이후 작업은 모델 호출을 바꾸지 않음).

**Windows 확인(2026-11-05, 학습자 PC):** Windows · Python 3.14.8 · `python -m venv` + `pip install -e ".[dev]"`(uv 없이)로 압축을 푼 평가용 패키지에서 `pytest` → **309 passed, 1 skipped**(git 체크아웃이 아니라 `test_handoff.py`의 포함 목록 테스트를 건너뜀). 경고 13건은 langsmith 내부의 Python 3.16 폐기 예정 함수 사용으로 MissLens와 무관.

## 2. 언제 무엇을 돌리나

| 바꾼 것 | 돌릴 것 |
|---|---|
| 코드 전반 | `pytest` → 커밋 전 `/verify` |
| 프롬프트, 스키마, 모델 설정 | `pytest` + `pytest -m live` |
| **청킹, 데이터 정리(`sources.yaml`의 `drop_lines`·`drop_blocks`), 태깅, 검색 순위(RRF·중복 제거·커버리지)** | `pytest` + `python -m misslens.cli retrieval-eval` (나아졌으면 `--save-baseline`) |
| 정답 질의 세트(`evals/<exam>/retrieval.yaml`) | `pytest tests/test_retrieval_eval.py` + `retrieval-eval --save-baseline` |
| 화면(`ui/app.py`) | `pytest tests/test_ui.py` + 샌드박스 서버로 눈으로 확인 |
| 시연 전 | `python scripts/probe_models.py` + `pytest -m live` |

## 3. 검색 품질 평가 (D50)

문제 출제(Generator)와 코치 답변은 모두 색인 검색에 기댄다. 가짜 임베더 단위 테스트는 검색 '로직'만 확인하므로, 실제 색인은 점수로 따로 본다. 2026-11-05에 청크의 29%에 영상 플레이어 메뉴가 섞여 있었고, 코치가 "Skilljar 자료는 없다"고 잘못 답했는데 단위 테스트는 모두 통과했던 것이 계기다.

### 3.1 정답 질의 세트 — `evals/ccar-f/retrieval.yaml`

```yaml
- {id: d3-permission-ko, domain: d3, query: "Claude Code 권한 모드는 어떤 것들이 있나요",
   expect: [{text: [permission], source: claude-code-in-action}]}
```

- **내용으로 판정한다.** 검색된 청크가 `text`의 모든 문구를 포함하고(대소문자 무시), `source`가 있으면 출처에 그 문자열이 들어 있을 때 정답이다. `expect`는 대안 목록이라 하나만 맞으면 된다. 청크 번호를 쓰지 않으므로 청킹이나 정리 규칙을 바꿔도 세트를 그대로 쓴다.
- `domain`은 영역별 점수용(`course`는 시험 세부 항목과 무관한 코스 주제), `task_id`는 코치·출제 모드에서 쓰는 세부 항목 필터다.
- 지금 30개: 영역마다 4~6개, 코스 전용 3개, **3분의 1은 한국어**(학습자가 코치에게 묻는 방식).

**질의를 추가하는 법**
1. 학습자가 실제로 한 질문이나 시험 세부 항목에서 질의를 고른다. 코치 대화에서 답이 아쉬웠던 질문이 가장 좋다.
2. 정답 청크를 판정할 문구는 너무 흔하지 않게(예: `tool` 대신 `tool_choice`), 너무 길지 않게 고른다. 다시 청킹해도 한 청크 안에 남을 만한 짧은 구절이어야 한다.
3. `pytest tests/test_retrieval_eval.py`로 형식을 검사하고, `retrieval-eval`로 '답할 수 없는 질의' 목록에 들어가지 않는지 확인한다(색인에 정답이 하나도 없으면 채점에서 빠지고 따로 보고된다).
4. 새 질의가 들어가면 점수의 분모가 바뀌므로 `--save-baseline`으로 기준선을 다시 저장한다.

### 3.2 지표

세 가지 검색 모드로 각각 잰다. 실제로 검색을 쓰는 세 가지 방식이다.

| 모드 | 흉내 내는 호출 | 설정 |
|---|---|---|
| 일반 | 학습자 자유 질문 그대로 | 세부 항목 필터 없음 |
| 코치 | 코치의 `search_material` | 태그 일치 청크를 앞에 최대 2개(D49) |
| 출제 | Generator의 `RetrievalContext` | 태그 일치 청크 먼저, k=6 |

| 지표 | 뜻 |
|---|---|
| hit@k | 정답 청크가 상위 k개 안에 하나라도 있는 질의의 비율 |
| MRR@10 | 첫 정답 순위의 역수 평균(1위 = 1, 2위 = 0.5 …, 10위 밖 = 0) |
| 영역별 hit@5 | 영역마다 hit@5 |
| 상위5 자료원 | 상위 5개에 섞인 자료원 종류 수 평균(가이드·코스별). 높을수록 근거가 다양 |
| 상위5 중복쌍 | 상위 5개 안에서 거의 같은(Jaccard > 0.8) 청크 쌍 수. 0이어야 함(D46) |
| 5위 밖 | hit@5에 실패한 질의 id. 개선 대상 |
| 출제 근거: 태그 일치 비율 | 세부 항목 30개마다 Generator 질의의 상위 6개 중 그 항목에 태그된 청크 비율의 평균 |

**색인 건강 검사**(하나라도 실패하면 평가 실패)

| 검사 | 기준 |
|---|---|
| `vectors_match_chunks` | FAISS 벡터 수 = 청크 수, 표본 벡터 길이가 1(정규화) |
| `no_junk_chunks` | 알려진 잡음 패턴(영상 플레이어 메뉴 등)과 `sources.yaml` 정리 규칙에 걸리는 줄이 있는 청크 0개 |
| `no_invalid_tags` | 시험 팩에 없는 세부 항목 태그 0개 |
| `no_short_chunks` | 40자 미만 청크 0개 |
| `every_task_has_material` | 30개 세부 항목 모두 태그된 청크가 1개 이상 |

참고 값(실패 조건 아님): 완전 중복 청크 수, 미분류 비율, 자료가 적은 세부 항목(5청크 미만), 출처별 청크 수.

### 3.3 기준선과 회귀

- 기준선: `evals/ccar-f/retrieval_baseline.json`(점수만 있고 본문은 없어서 커밋한다).
- 회귀 판정: 건강 검사 실패, 또는 일반·코치·출제 hit@5, 일반 MRR@10, 출제 근거 태그 일치 비율 중 하나가 기준선보다 **0.05 넘게** 떨어지면 실패(`--max-drop`으로 조정). 답할 수 있는 질의 수가 줄어도 실패.
- 갱신 절차: 바꾼다 → `retrieval-eval`로 비교 → 나아졌거나 의도한 변화면 `retrieval-eval --save-baseline` → 기준선 파일을 함께 커밋. 건강 검사가 실패하면 저장이 거부된다.

### 3.4 점수 기록

| 날짜 | 바꾼 것 | 건강 | 일반 hit@5 / MRR | 코치 hit@5 / MRR | 출제 hit@5 / MRR | 비고 |
|---|---|---|---|---|---|---|
| 2026-11-05 | 수집 직후 색인(1,674청크) | 실패: 잡음 555 | 0.93 / 0.881 | 0.93 / — | 0.90 / — | 백업 색인으로 사후 측정 |
| 2026-11-05 | 화면 장식 제거·재색인(D48, 1,278청크) | 통과 | 0.97 / 0.875 | 0.97 / 0.908 | 0.93 / 0.906 | 첫 기준선. 5위 밖: d3-permission-ko(한국어) |
| 2026-11-05 | 질의 커버리지 가중 RRF(D51) | 통과 | **1.00 / 0.89** | **1.00 / 0.92** | **0.97 / 0.92** | 현재 기준선. 5위 밖: 출제 모드 d2-tool-choice |

### 3.5 결과를 읽는 법과 흔한 원인

| 증상 | 의심할 것 |
|---|---|
| 건강 검사 `no_junk_chunks` 실패 | 사이트 화면이 바뀌어 정리 규칙이 안 맞음 → `sources.yaml` 규칙 추가 후 `collect reindex` |
| 한국어 질의만 5위 밖 | BM25가 질의의 영어 일부(흔한 단어)만 보고 순위를 끌어내림 → 커버리지 가중(D51) 확인 |
| 출제 모드만 낮음 | 태그 우선 규칙이 덜 관련된 태그 청크로 자리를 채움 → 태깅 품질이나 태그 상한 검토 |
| 상위5 자료원이 1에 가까움 | 한 자료(가이드)가 결과를 독점 → `max_tagged`, 중복 제거 확인 |
| 답할 수 없는 질의 증가 | 정리 규칙이 본문까지 지웠거나 자료가 빠짐 → 해당 출처 파일 확인 |

## 4. 진단 일치율 (D43)

리뷰 중 '자기 판단' 단계(정답 공개 → 개념/해석/모르겠음, 해설 전)에서 남긴 라벨을 판정과 비교한다. 공부만 하면 데이터가 쌓인다.

```bash
python -m misslens.cli eval                 # 일치율과 판정→내 판단 표
python -m misslens.cli eval --export        # data/eval/misses-<날짜>.yaml (tests/fixtures/eval_misses.yaml과 같은 형식)
```

## 5. 생성 문제 사후 검토 (D58)

앱 안의 검사(validator, Haiku 근거 확인)는 형식과 '정답이 자료로 뒷받침되는지'만 본다. 정답이 하나뿐인지, 오답 보기의 `violates`가 맞는지, 사실·번역 오류는 상위 모델로 표본 검토한다.

```bash
python -m misslens.cli export-questions                    # 제출한 세트 전부, 근거 발췌 포함
python -m misslens.cli export-questions --set 4 --evidence sources   # 한 세트, 출처 이름만(파일이 작아짐)
```

1. 만든 파일을 Claude Opus 대화에 첨부한다(검토 지시문과 답변 형식이 맨 위에 있다).
2. 같은 계열의 착각을 피하려면 같은 파일을 GPT에도 넣어 두 결과를 비교한다.
3. '수정·폐기'가 몰리는 유형(복수 정답, violates 오류, 사실 오류 등)을 보고 생성 지시문이나 근거 확인을 고친다. 검토 결과는 학습 통계에 넣지 않는다.

주의: 근거 발췌에는 수집한 강의 자료가 들어 있다. 개인 대화에 원문을 넣고 싶지 않으면 `--evidence sources`나 `none`을 쓴다. 근거는 출제 때와 같은 질의로 다시 검색한 것이라 출제 뒤 색인이 바뀌었으면 다를 수 있다.

## 6. 문제 생성 품질 라운드 평가 (D59)

외부 검토는 정확하지만 사람이 붙여 넣어야 해서 반복이 느리다. 그래서 같은 기준의 판정기를 저장소에 두고, 외부 검토로 판정기를 보정한 뒤 라운드를 반복한다. 방법·라운드 요약·판정기 보정·발견은 [evaluation/README.md](../evaluation/README.md).

```bash
python -m misslens.cli question-eval --round 1 --existing --notes "기준선"          # 1라운드 문제(샌드박스)를 판정
python -m misslens.cli question-eval --round 3 --notes "바꾼 것"                    # 같은 세부 항목 20개로 새로 생성 → 판정
python -m misslens.cli question-eval --round 3 --from-set 2 --notes "판정기 수정"    # 이미 만든 라운드 세트를 다시 채점
python -m misslens.cli question-eval --round 2 --relabel                         # 외부 라벨을 넣은 뒤 재계산(모델 호출 없음)
```

- 실호출이다(생성 Sonnet 약 25회 + 근거 확인 Haiku + 판정 Sonnet 40회). 할당량을 확인하고 실행한다.
- 출제 지시문, 근거 확인, 판정기를 바꾸면 새 라운드를 돌리고 리포트의 '앞 라운드' 열로 비교한다. 판정기를 바꿨으면 앞 라운드도 같은 판정기로 다시 채점해야 비교가 공정하다.
- 라운드가 만든 `data/eval/round<N>_questions.md`를 외부 검토(§5)에 넣어 판정기 점수를 확인한다. 두 검토의 최종 YAML 합의를 `evals/ccar-f/question_set.yaml`의 `round<N>`에 넣고(판정, 두 쪽 모두 지적한 유형, 보기별 종류·유효 여부), `--relabel`로 판정기 보정 지표를 다시 낸다.

## 7. 질의 API 평가 (D61)

`test_queries.csv`(id·category·input·expected_traits·forbidden·expected_tools·note, 세미콜론 구분)로 `POST /query` 경로를 잰다. 라운드 요약은 [evaluation/README.md](../evaluation/README.md).

```bash
python -m misslens.cli query "tool_choice를 any로 두면 어떻게 동작해?"     # 하나만(단계·근거 표시, --json)
python -m misslens.cli query-eval --round 3 --notes "바꾼 것"            # 20건 + RAGAS → evaluation/query_round3_report.md
python -m misslens.cli query-eval --round 3 --no-ragas --limit 5         # 빠른 확인
```

- 실호출이다(질의당 router Haiku 1회 + 답변 Sonnet 1~4회 + 판정 Sonnet 1회, RAGAS는 질의당 Sonnet 약 5~10회). 20건 + RAGAS가 약 10분.
- `expected_tools`는 trace의 단계 이름(`search_material`, `learner_history`, `build_report`, `guard_input`)이나 경로(`route:quiz`, `route:status`)와 비교한다. 비워 두면 도구 조건이 없다.
- 질의를 추가하면 `test_qa.py`의 스키마 검사(카테고리, 가드레일 질의는 실제로 입력 가드레일에 걸림)도 통과해야 한다.
- 질의응답 지시문(`qa.py` `QA_SYSTEM`), 라우터 지시문, 판정기를 바꾸면 새 라운드를 돌린다. 판정기를 바꿨으면 앞 라운드도 다시 실행해 같은 판정기로 비교한다.

## 8. 실행 환경 메모

- **샌드박스**(`MISSLENS_SANDBOX=1`): 학습 기록·체크포인트를 `data/sandbox/`에 따로 둔다. 화면 검토와 리허설은 여기서 한다. 색인은 공유(읽기 전용)라 검색 평가 결과는 같다.
- **`/verify`**: 커밋 전에 실행한다. 테스트가 `tests/`에 따로 있어 매핑이 `~/.gstack/projects/misslens/pregate.json`에 있다. 새 소스 폴더를 만들면 거기에 테스트 매핑을 추가한다. 녹색일 때만 커밋한다.
- **실호출**: 할당량을 쓴다. 할당량 초과가 의심되면 먼저 `python scripts/probe_models.py`.
