# 문제 생성 품질 평가 — 1라운드

- 날짜: 2026-11-05 17:45
- 인-아웃 세트: `evals/ccar-f/question_set.yaml` (세부 항목 20개)
- 판정기(LLM-as-Judge): `anthropic.claude-sonnet-4-6` · 문제마다 정답을 숨기고 직접 풀기 1회 + 기준 판정 1회

## 이번 라운드

기준선. 기존 출제 지시문(D40)으로 만든 샌드박스 20문제를 판정한다. 같은 문제를 Claude Opus·GPT가 교차 검토한 결과(OK 1/20, 수정 필요 19)를 기대 판정으로 두고 판정기를 보정한다.

## 지표

| 지표 | 이번 라운드 |
|---|---|
| 판정기 OK 비율 | 80% |
| 오답 근거(violates) 유효 | 80% |
| 정답이 하나뿐 | 95% |
| 표시된 정답이 맞음 | 100% |
| 판정기가 직접 푼 답 = 정답 | 100% |
| 사실 확인됨 | 95% |
| 번역 정확 | 100% |
| 시험 적합성 | 100% |

문제 유형별 지적 수(판정기): 오답 근거(violates) 4, 복수 정답 1, 사실 오류 0, 번역 0, 시험 적합성 0, 지문 모순 0 · 사실 오류 0건

## 판정기 보정 (외부 검토와 비교)

- 외부 검토(Claude Opus·GPT 교차 검토) OK 비율 5%, 판정기와 OK/비OK 일치 25% (20문제)
- 외부 검토가 지적한 유형을 판정기도 찾은 비율:
  - 오답 근거(violates): 3/14
  - 복수 정답: 1/4
  - 사실 오류: 0/2
  - 번역: 0/3
  - 시험 적합성: 0/5
  - 지문 모순: 0/2

## 문제별 결과

| 문제 | 세부 항목 | 판정 | 직접 푼 답 일치 | 지적 유형 | 외부 판정 | 이유 |
|---|---|---|---|---|---|---|
| r1-q1 | 1.2 | ok | 예 | - | fix (violates) | The correct answer aligns with the reference material's guidance on routing all subagent communication through the coordinator for observability and error handling. All wrong options clearly violate distinct stated requirements, and the violates phrases accurately reflect the stem conditions they break. |
| r1-q2 | 3.1 | fix | 예 | violates | fix (violates, fit) | Option C's `violates` phrase ("stored them in ~/.claude/CLAUDE.md on their local machine") is a background fact/diagnosis from the stem, not a requirement or constraint being imposed on the solution — it describes the current broken state, not a rule the option violates. The real reason C is wrong is that it requires manual prompt editing for every CI run and does not automatically deliver standards to teammates, i.e., it violates "without anyone having to copy instructions manually" and "automatically receive them." The violates phrase should reference one of those actual stem constraints. |
| r1-q3 | 4.1 | fix | 예 | violates | fix (multiple, violates, translation) | Option A's `violates` phrase ("Developers are dismissing most of the comment-accuracy findings as noise") is an observation/background fact from the stem, not a requirement or limit that the option actually breaks. Option A is wrong because it replicates the same type of vague, confidence-based filtering ("be conservative") that the reference material explicitly says fails to improve precision — the correct violates phrase should reference the requirement to use specific categorical criteria rather than vague filtering. Option D's `violates` phrase is equally problematic: "only report high-confidence findings" is the existing (failed) instruction being replaced, not a constraint the correct answer must satisfy, so it mislabels D's flaw. D is wrong for the same reason as A (confidence-based filtering doesn't work), but the violates phrase shown is not a stem requirement being violated. |
| r1-q4 | 1.1 | ok | 예 | - | fix (fit, stem) | All four options are clearly differentiated, the correct answer matches both requirements stated in the stem, each wrong option's `violates` phrase pinpoints the exact stem condition it breaks, and the factual content aligns with the reference material on agentic loop lifecycle and tool result handling. |
| r1-q5 | 2.1 | ok | 예 | - | fix (violates) | The question accurately reflects guide content on misrouting caused by near-identical descriptions; the correct answer directly applies the described fix (rewriting descriptions with purpose, inputs, outputs, and usage guidance); all wrong options have valid and precise violates phrases drawn from stem constraints. |
| r1-q6 | 1.3 | ok | 예 | - | fix (stem, translation, violates) | The correct answer aligns exactly with the reference material stating "allowedTools must include 'Task' for a coordinator to invoke subagents." All distractors are clearly wrong and test distinct misconceptions without overlapping, and the violates phrases accurately reflect stem conditions. |
| r1-q7 | 1.4 | ok | 예 | - | drop (fit, fact) | The correct answer (D) is well-supported by the reference material stating that prompt instructions alone have a non-zero failure rate and that programmatic prerequisites block downstream tool calls until prerequisite steps complete. All violates phrases are exact stem conditions, wrong options are clearly distinguishable, and the Korean translation accurately preserves all conditions and negations. |
| r1-q8 | 1.5 | fix | 예 | violates | fix (violates) | Option D's `violates` phrase ("guaranteed to happen on every tool call") is incorrect: a PostToolUse hook fires deterministically on every tool call, so the real flaw of D is that it only adds a conversion *note* for the model to act on (probabilistic, not guaranteed normalization), which means the correct violates phrase should reference "normalized to ISO 8601 so the model reasons over one consistent format" (i.e., the model still receives the raw timestamp and may not convert it). Additionally, `updatedToolOutput` is not explicitly named in the reference material (which mentions `updatedInput` for PreToolUse), making its existence for PostToolUse uncertain; however, PostToolUse hooks for data normalization are clearly described in the guide, so the concept is correct even if the exact field name is uncertain. |
| r1-q9 | 1.6 | ok | 예 | - | fix (fit) | The question cleanly tests the distinction between dynamic adaptive decomposition and fixed sequential/parallel patterns, directly supported by the reference material. All violates phrases are genuine stem constraints, the correct answer is unambiguous, and the Korean translation faithfully preserves every condition and negation. |
| r1-q10 | 1.7 | ok | 예 | - | fix (violates) | The correct answer (C) aligns directly with the reference material's description of fork_session for creating independent branches from a shared analysis baseline. All distractors break clearly stated stem constraints, and violates phrases are accurate stem conditions. |
| r1-q11 | 3.2 | ok | 예 | - | fix (violates) | All facts align with the reference material: context: fork isolates skill output from the main conversation, allowed-tools restricts tool access (not output isolation), argument-hint is not a context-isolation mechanism, and .claude/skills/ (project-scoped) vs ~/.claude/skills/ (personal) distinction is correctly applied. The correct answer C is unambiguous and the violates phrases for wrong options accurately reflect the stem constraints they break. |
| r1-q12 | 3.3 | fix | 예 | violates, multiple | fix (multiple) | Option C is also a plausible correct answer: a src/tests/ CLAUDE.md cannot cover integration test files spread outside that directory (same real problem as A), AND it conflates two separate issues. More critically, Option D's `violates` phrase ("load API-specific review conventions only when touching API handler files") describes the desired goal stated in the stem, not a constraint that makes the option wrong — directory-level CLAUDE.md files do load for the whole session, which is actually a valid flaw of the approach, making D potentially correct too. Both C and D have `violates` phrases that are not strictly "requirements stated in the stem that the option breaks" but rather background context or goals. Option B's factual claim that subdirectory CLAUDE.md files "cannot be committed to version control" contradicts the reference material (the guide notes project directory CLAUDE.md is shared through version control), making it a factually false distractor — but the `violates` phrase ("instead of using .claude/rules/ files with glob patterns") is not a requirement that option B breaks; it's the approach being evaluated. The question has multiple defensible correct answers (A, C, and D all identify real problems), undermining single-answer integrity. |
| r1-q13 | 4.2 | ok | 예 | - | fix (violates, fact) | The correct answer aligns with the reference material's guidance on few-shot examples for severity consistency and false positive reduction. All distractors are correctly disqualified by legitimate stem constraints, and the Korean translation faithfully preserves all conditions and negations. |
| r1-q14 | 4.3 | ok | 예 | - | fix (multiple, violates) | All facts align with the reference material: tool_choice "auto" allows plain-text, "any" forces a tool call but not a specific one, and {"type": "tool", "name": "code_review"} is the only setting that guarantees the specific tool is called every time. The violates phrases are accurate and distinct, and the Korean translation faithfully preserves all conditions. |
| r1-q15 | 2.2 | ok | 예 | - | ok (-) | All four options are clearly distinguished, the correct answer (C) maps 503→transient/retryable and bad path→validation/not-retryable exactly as the reference material defines, and each wrong option's violates phrase accurately names the stem constraint it breaks. |
| r1-q16 | 2.3 | ok | 예 | - | fix (multiple) | The question accurately reflects the reference material's guidance on scoped tool access, the correct answer is the only reasonable choice, and each distractor is ruled out by a legitimate stem constraint. |
| r1-q17 | 3.4 | ok | 예 | - | fix (violates, fit) | The correct answer aligns perfectly with reference material: direct execution for simple single-file style checks and plan mode for complex multi-file architectural decisions. All violates phrases are genuine stem constraints, and wrong options are clearly distinguishable by distinct misconceptions. |
| r1-q18 | 3.5 | ok | 예 | - | fix (violates) | All four options are clearly distinct, the correct answer aligns with the reference material's guidance on using 2-3 concrete input/output examples when prose produces inconsistent results, and each wrong option's violates phrase accurately reflects a real constraint stated in the stem. |
| r1-q19 | 3.6 | ok | 예 | - | fix (violates) | All four options are clearly differentiated: A introduces a non-existent --batch flag (confirmed by reference material), C omits --output-format json so the downstream parser still fails, D omits -p so the job still hangs, and only B combines both documented fixes correctly. The violates phrases map precisely to stem requirements, and all factual claims about -p and --output-format json are supported by the reference material. |
| r1-q20 | 4.4 | ok | 예 | - | fix (violates, translation) | The correct answer aligns exactly with the reference material's guidance on retry-with-error-feedback (appending specific validation errors, the failed extraction, and the original document). All wrong options break clearly stated stem constraints, and the violates phrases are accurate and appropriate. |
