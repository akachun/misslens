# 문제 생성 품질 평가 — 2라운드

- 날짜: 2026-11-06 09:12
- 인-아웃 세트: `evals/ccar-f/question_set.yaml` (세부 항목 18개)
- 판정기(LLM-as-Judge): `anthropic.claude-sonnet-4-6` v2 · 문제마다 정답을 숨기고 직접 풀기 1회 + 오답 보기별 세부 판정 1회(요약 판정은 코드가 계산)

## 이번 라운드

출제 지시문 보강(D59): key constraint는 지켜야 할 요구사항·제한만(제안·관찰·진단·증상·배경 사실 금지), 오답은 그 요구사항을 어겨서 틀려야 함, 근거 자료가 인정하는 대안은 오답으로 쓰지 않음, 정답이 지문 문구를 옮겨 적지 않음, 오답끼리 같은 오개념 금지. 근거 확인(Haiku) 기준에도 같은 내용 추가. 같은 세부 항목 20개로 새로 생성(평가용 DB 세트 1). 판정기 v2로 채점(1라운드와 같은 판정기).

## 지표

| 지표 | 이번 라운드 | 앞 라운드 | 변화 |
|---|---|---|---|
| 판정기 OK 비율 | 44% | 25% | +19%p |
| 오답 근거(violates) 유효 | 50% | 50% | +0%p |
| 정답이 하나뿐 | 100% | 90% | +10%p |
| 표시된 정답이 맞음 | 100% | 100% | +0%p |
| 판정기가 직접 푼 답 = 정답 | 100% | 100% | +0%p |
| 사실 확인됨 | 83% | 50% | +33%p |
| 번역 정확 | 94% | 100% | -6%p |
| 시험 적합성 | 83% | 65% | +18%p |

문제 유형별 지적 수(판정기): 오답 근거(violates) 9, 복수 정답 0, 사실 오류 2, 번역 1, 시험 적합성 3, 지문 모순 2 · 사실 오류 2건

## 생성 과정

- 생성 18/20 · 첫 시도 통과 83% · 평균 시도 1.4회
- 거절 사유: generation_error 3, validator 7

## 판정기 보정 (외부 검토와 비교)

- 외부 검토(Claude Opus·GPT 교차 검토) OK 비율 22%, 판정기와 OK/비OK 일치 67% (18문제)
- 외부 검토가 지적한 유형을 판정기도 찾은 비율:
  - 오답 근거(violates): 8/14
  - 복수 정답: 0/2
  - 시험 적합성: 1/1
  - 지문 모순: 1/6
- 외부 OK 문제를 판정기도 OK로 봄 75%(너무 엄격한지), 외부가 수정으로 본 문제를 판정기가 OK로 넘김 5개(너무 관대한지)
- 오답 보기 단위(두 외부 검토가 같은 값으로 본 보기만):
  - violates 종류 분류 일치 92% (51개)
  - 유효 여부(요구사항이고 틀린 진짜 이유인가) 일치 74% (49개)
  - 외부가 무효로 본 보기를 판정기도 찾음 8/18 (44%), 외부가 유효로 본 보기를 무효로 봄 10%

## 문제별 결과

| 문제 | 세부 항목 | 판정 | 직접 푼 답 일치 | 지적 유형 | 외부 판정 | 이유 |
|---|---|---|---|---|---|---|
| r2-q1 | 1.2 | fix | 예 | violates, fit, stem | fix (violates, stem) | The question is well-constructed and the correct answer (D) accurately reflects the hub-and-spoke principle from the reference material. Options A and C share the same underlying misconception that direct peer communication is acceptable, while option B introduces a different but still incorrect alternative. |
| r2-q2 | 3.1 | fix | 예 | violates, translation | fix (violates, stem) | The answer and distractors are factually sound, but the Korean learner-language options are reordered relative to the English stem options, so the letter identifiers (A/B/C/D) no longer correspond between the two versions — this will cause confusion when learners try to match the violates labels to the Korean options. |
| r2-q4 | 1.1 | fix | 예 | violates | fix (violates) | The question is well-formed. The correct answer (A) directly mirrors the reference material's explicit guidance on stop_reason-based loop control. All three distractors are clearly wrong for distinct, non-overlapping reasons, and none is defensible as correct. |
| r2-q5 | 2.1 | fix | 예 | violates | fix (violates) | The question is well-constructed: each wrong option clearly violates one explicit constraint from the stem, the correct answer directly applies the reference material guidance on differentiating tool descriptions with purpose, inputs, boundary conditions, and when-to-use-versus-alternatives guidance, and there is no ambiguity in the Korean translation. |
| r2-q6 | 1.3 | fix | 예 | violates, fit, stem | fix (violates, multiple) | Option B's cited 'violates' phrase is a background fact ("coordinator's AgentDefinition already lists 'Task' in allowedTools") rather than the actual requirement it breaks; B is wrong because it adds an unrequested allowedTools change to the report-synthesizer, which is irrelevant to the stated requirements, but the violates label misidentifies the reason. Additionally, C and D share the same misconception that subagents can automatically inherit context, which is a shared misconception across two distractors. |
| r2-q7 | 1.4 | ok | 예 | - | fix (violates) | The question is well-constructed: the correct answer applies full programmatic gating to both dependency chains, each wrong option breaks exactly one named requirement, and the financial compliance context correctly motivates why prompt-only approaches are insufficient per the reference material. |
| r2-q8 | 1.5 | fix | 예 | fact | fix (violates, stem) | The question is well-constructed and the correct answer (A) is clearly right per the reference material. The only notable concern is the unverified field name `updatedToolOutput` in option A — the reference material documents `updatedInput` for PreToolUse rewrites but does not name a specific field for PostToolUse output rewriting, making this detail uncertain rather than confirmed wrong. |
| r2-q9 | 1.6 | ok | 예 | - | fix (violates) | The question is well-constructed and accurately tests the distinction between fixed sequential pipelines and dynamic adaptive decomposition as described in the reference material. All wrong options clearly violate at least one of the two stated requirements, and the correct answer aligns cleanly with the reference material's guidance on adaptive investigation plans. |
| r2-q10 | 1.7 | ok | 예 | - | fix (violates, multiple, stem) | The question is well-constructed and accurately tests fork_session knowledge. Each wrong option targets a distinct misconception (sequential forking, fresh-start summaries, or shared-session parallelism), and the violates phrases precisely match the stem constraints they break. |
| r2-q11 | 3.2 | ok | 예 | - | ok (-) | The question is well-constructed: the correct answer (B) is the only option that satisfies both the project-scope requirement (.claude/ directory for version-control sharing) and the context-isolation requirement (context: fork frontmatter). Each wrong option breaks exactly one of the two stated requirements, and the violates phrases are accurate and precise. |
| r2-q13 | 4.2 | fix | 예 | violates | fix (violates) | The question is well-constructed: the correct answer (D) aligns exactly with the reference material's guidance that few-shot examples are the highest-leverage next move when detailed instructions already fail, and the distractors each break a clearly stated constraint. No factual, translation, or structural issues found. |
| r2-q14 | 4.3 | ok | 예 | - | fix (violates) | The question is well-constructed and factually accurate per the reference material. The correct answer (B) is clearly supported, and all distractors each break a distinct stated requirement in a non-overlapping way. |
| r2-q15 | 2.2 | ok | 예 | - | ok (-) | The question is well-constructed and the correct answer (C) is the only option that satisfies all three stated requirements: per-category error classification with correct isRetryable flags and a human-readable description field on each error type. Each wrong option clearly violates exactly one stated requirement as identified by its violates tag. |
| r2-q16 | 2.3 | ok | 예 | - | ok (-) | The question is well-constructed and clearly grounded in the reference material. The correct answer (B) directly applies the guide's concept of scoped cross-role tools (e.g., a verify_fact tool for the synthesis agent) while routing complex cases through the coordinator. Each wrong option cleanly violates a distinct stated requirement, and the Korean translation faithfully preserves all conditions and negations from the English stem. |
| r2-q17 | 3.4 | ok | 예 | - | fix (violates, stem) | The question and all options are well-constructed and accurately reflect the reference material's guidance on plan mode vs. direct execution. The correct answer C cleanly matches each task to the appropriate mode, and the distractors each fail in a clear, non-overlapping way. |
| r2-q18 | 3.5 | fix | 예 | violates, fit | fix (violates, fit, stem) | The correct answer C is essentially a restatement of the proposed solution described in the stem ("concrete before-and-after examples" → "concrete input/output examples"), making it trivially selectable by vocabulary matching without understanding the underlying concept. The violates phrase for option A is misclassified as an observation when it is actually background/context, and option A is wrong for a different reason than stated. |
| r2-q19 | 3.6 | fix | 예 | violates, fact | fix (violates) | The correct answer C is well-supported by the reference material, which explicitly calls out including prior review findings in context and instructing Claude to report only new or still-unaddressed issues. Option A's 'violates' phrase is mislabeled: it cites the current symptom ('floods the PR with duplicate comments') rather than a requirement the option breaks; the real problem with A is that cross-run session reuse is unsupported and the reference warns against same-session review. |
| r2-q20 | 4.4 | fix | 예 | violates | ok (-) | The question is well-formed and the correct answer (B) is clearly supported by the reference material, which explicitly states that retries should include the original document, the failed extraction, and the specific validation error so the model can self-correct. All three wrong options are cleanly wrong for distinct and clear reasons. |
