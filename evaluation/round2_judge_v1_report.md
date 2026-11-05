# 문제 생성 품질 평가 — 2라운드

- 날짜: 2026-11-05 17:53
- 인-아웃 세트: `evals/ccar-f/question_set.yaml` (세부 항목 18개)
- 판정기(LLM-as-Judge): `anthropic.claude-sonnet-4-6` · 문제마다 정답을 숨기고 직접 풀기 1회 + 기준 판정 1회

## 이번 라운드

출제 지시문 보강(D59): key constraint는 지켜야 할 요구사항·제한만(제안·관찰·진단·증상·배경 사실 금지), 오답은 그 요구사항을 어겨서 틀려야 함, 근거 자료가 인정하는 대안은 오답으로 쓰지 않음, 정답이 지문 문구를 옮겨 적지 않음, 오답끼리 같은 오개념 금지. 근거 확인(Haiku) 기준에도 같은 내용 추가. 같은 세부 항목 20개로 새로 생성.

## 지표

| 지표 | 이번 라운드 | 앞 라운드 | 변화 |
|---|---|---|---|
| 판정기 OK 비율 | 72% | 25% | +47%p |
| 오답 근거(violates) 유효 | 83% | 50% | +33%p |
| 정답이 하나뿐 | 94% | 90% | +4%p |
| 표시된 정답이 맞음 | 100% | 100% | +0%p |
| 판정기가 직접 푼 답 = 정답 | 100% | 100% | +0%p |
| 사실 확인됨 | 94% | 50% | +44%p |
| 번역 정확 | 94% | 100% | -6%p |
| 시험 적합성 | 100% | 65% | +35%p |

문제 유형별 지적 수(판정기): 오답 근거(violates) 3, 복수 정답 1, 사실 오류 0, 번역 1, 시험 적합성 0, 지문 모순 0 · 사실 오류 0건

## 생성 과정

- 생성 18/20 · 첫 시도 통과 83% · 평균 시도 1.4회
- 거절 사유: generation_error 3, validator 7

## 문제별 결과

| 문제 | 세부 항목 | 판정 | 직접 푼 답 일치 | 지적 유형 | 외부 판정 | 이유 |
|---|---|---|---|---|---|---|
| r2-q1 | 1.2 | fix | 예 | violates | - | Option B's `violates` phrase ("coordinator agent decomposes an incoming research query into subtopics") is not a requirement or limit stated in the stem that B actually breaks — decomposing the query further is not prohibited by that phrase; rather, B is wrong because it misrepresents what hub-and-spoke requires (it proposes unnecessary over-decomposition as the solution, not routing through the coordinator). The violates tag for B should reference the hub-and-spoke routing requirement, not the decomposition description. All other options and their violates phrases are sound. |
| r2-q2 | 3.1 | fix | 예 | translation | - | The learner-language stem options are shuffled differently from the English options: English A→Korean B, English B→Korean C, English C→Korean B (correct answer), English D→Korean A. The correct answer label shifts from C in English to C in Korean (option C in Korean is the @import option), so the correct answer letter is preserved — but the distractor option letters A, B, D map to different content between the two languages, which is inconsistent and potentially confusing. Specifically, what English labels as option A (monolithic) appears as Korean option D, and English option D (developer ~/.claude) appears as Korean option C — wait, let me re-examine: Korean A = monolithic (English D), Korean B = CI runner ~/.claude (English A), Korean C = @import correct (English C), Korean D = developer ~/.claude (English D)... The shuffling between English and Korean versions means the letter labels for wrong options differ, which is a translation/formatting issue that needs fixing for consistency. |
| r2-q4 | 1.1 | ok | 예 | - | - | The correct answer (A) precisely matches the reference material's guidance on using stop_reason "end_turn" to terminate and "tool_use" to continue; all distractors are clearly wrong for distinct reasons and their violates phrases accurately identify the stem constraint each option breaks. |
| r2-q5 | 2.1 | ok | 예 | - | - | The question is well-constructed: the correct answer (C) directly applies the reference material's guidance on writing differentiated tool descriptions with purpose, inputs, boundary conditions, and when-to-use guidance. Each wrong option's violates phrase accurately identifies the specific stem constraint it breaks, and no two wrong options share the same misconception. The Korean translation faithfully preserves all conditions and negations. |
| r2-q6 | 1.3 | fix | 예 | violates, multiple | - | Option B is technically valid on its own merits — emitting both Task calls in a single response and passing findings in the report-synthesizer's prompt both satisfy the stated requirements. Adding "Task" to the report-synthesizer's allowedTools is an extra step that doesn't violate any stem constraint; it merely adds optional capability. The `violates` phrase for B ("coordinator's AgentDefinition already lists 'Task' in allowedTools") is a background fact, not a requirement or limit that B actually breaks — having Task in the coordinator's allowedTools doesn't preclude also adding it to the report-synthesizer's allowedTools. Thus B cannot be ruled out as wrong, meaning A is not the single correct answer, and `single_answer` is false and `violates_valid` is false. |
| r2-q7 | 1.4 | ok | 예 | - | - | All four options are clearly differentiated, the correct answer matches the reference material's guidance on programmatic prerequisite gates for deterministic compliance, and each wrong option's violates phrase accurately identifies the stem condition it breaks. |
| r2-q8 | 1.5 | fix | 예 | - | - | Option A uses `updatedToolOutput` as the field name for rewriting tool results in a PostToolUse hook, but the reference material only documents `updatedInput` (for PreToolUse call rewriting) and does not mention `updatedToolOutput` — making this field name uncertain/unverified. Additionally, option D's violates phrase ("must ensure that by the time Claude processes results from either tool") is a goal/outcome statement rather than a crisp requirement the option breaks; the real reason D is wrong is that PreToolUse rewrites outgoing inputs and cannot guarantee what the external tool returns. These are minor fixable issues rather than fatal flaws. |
| r2-q9 | 1.6 | ok | 예 | - | - | The question cleanly tests the distinction between fixed sequential pipelines and dynamic adaptive decomposition (Task Statement 1.6), the correct answer is clearly supported by the reference material, and every wrong option's violates phrase is a genuine stem constraint that explains why the option fails. |
| r2-q10 | 1.7 | ok | 예 | - | - | All four options are clearly differentiated, the correct answer (fork_session from the current session) is well-supported by the reference material, and each wrong option's violates phrase accurately names a stem requirement that the option breaks. |
| r2-q11 | 3.2 | ok | 예 | - | - | Both requirements (project-scoped via .claude/skills/ for version-control sharing, and context: fork to isolate verbose output) are clearly supported by the reference material, and each distractor correctly targets exactly one violated constraint. |
| r2-q13 | 4.2 | ok | 예 | - | - | All four options are clearly distinguished, the correct answer aligns precisely with the reference material's guidance on few-shot examples for severity classification and false-positive reduction, and each wrong option's violates phrase accurately names a real stem constraint that the option breaks. |
| r2-q14 | 4.3 | ok | 예 | - | - | All four options are clearly distinct, the correct answer (B) is well-supported by the reference material stating tool_choice "any" forces a tool call while allowing the model to choose which one, and each wrong option's violates phrase accurately reflects the stem constraint it breaks. |
| r2-q15 | 2.2 | ok | 예 | - | - | The question accurately tests knowledge of structured MCP error responses using isError, errorCategory (transient/validation/permission), isRetryable, and human-readable descriptions, all of which are directly referenced in the guide. The correct answer (C) is the only option satisfying all three stated requirements, each wrong option clearly violates exactly one constraint from the stem, and the Korean translation faithfully preserves all conditions. |
| r2-q16 | 2.3 | ok | 예 | - | - | All conditions are correctly reflected in the stem, the correct answer aligns with the reference material's guidance on scoped cross-role tools (e.g., verify_fact) and routing complex cases through the coordinator, each wrong option's violates phrase is a genuine stem constraint, and the Korean translation faithfully preserves every condition and negation. |
| r2-q17 | 3.4 | ok | 예 | - | - | The correct answer aligns precisely with the reference material: direct execution for simple, well-scoped single-file fixes and plan mode for large-scale multi-file architectural migrations. All wrong options have valid violates phrases drawn from the stem, and no two wrong options share the same misconception. The Korean translation faithfully preserves all conditions and negations. |
| r2-q18 | 3.5 | ok | 예 | - | - | The correct answer (C) directly maps to the reference material's guidance on using concrete input/output examples when prose descriptions are interpreted inconsistently. All distractors are well-differentiated: A describes test-driven iteration (a different technique), B misapplies the "interacting problems" concept while violating the examples requirement, and D correctly fails on the no-interactive-steps constraint. The Korean translation faithfully preserves all conditions and negations. |
| r2-q19 | 3.6 | fix | 예 | violates | - | Option A's `violates` phrase ("re-runs and floods the PR with duplicate comments") is a description of the problem being solved (an observation/symptom in the stem), not a requirement or constraint imposed by the stem. The real reason A is wrong is the reference material's note that the same session that generated code is less effective at reviewing its own changes, and that CI sessions are stateless so persistent memory across pipeline runs is not supported. Option D's `violates` phrase ("must prevent duplicate comments on resolved issues") is a valid stem constraint and correctly identifies why D fails. The violates phrases for A and B should be revised. |
| r2-q20 | 4.4 | ok | 예 | - | - | The correct answer aligns directly with the reference material's guidance on retry-with-error-feedback (appending specific validation errors plus the original document and failed extraction). All wrong options clearly violate stated stem constraints, and the violates phrases are accurate stem conditions. |
