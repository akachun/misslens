# 문제 생성 품질 평가 — 1라운드

- 날짜: 2026-11-05 17:49
- 인-아웃 세트: `evals/ccar-f/question_set.yaml` (세부 항목 20개)
- 판정기(LLM-as-Judge): `anthropic.claude-sonnet-4-6` v2 · 문제마다 정답을 숨기고 직접 풀기 1회 + 오답 보기별 세부 판정 1회(요약 판정은 코드가 계산)

## 이번 라운드

기준선. 기존 출제 지시문(D40)으로 만든 샌드박스 20문제를 판정한다. 같은 문제를 Claude Opus·GPT가 교차 검토한 결과(OK 1/20, 수정 필요 19)를 기대 판정으로 두고 판정기를 보정한다. 판정기 v1(항목별 참/거짓)은 OK 80%로 외부 검토와 25%만 일치해(round1_judge_v1_report.md) v2(오답 보기별 분해 판정, 요약은 코드 계산)로 다시 판정했다.

## 지표

| 지표 | 이번 라운드 |
|---|---|
| 판정기 OK 비율 | 25% |
| 오답 근거(violates) 유효 | 50% |
| 정답이 하나뿐 | 90% |
| 표시된 정답이 맞음 | 100% |
| 판정기가 직접 푼 답 = 정답 | 100% |
| 사실 확인됨 | 50% |
| 번역 정확 | 100% |
| 시험 적합성 | 65% |

문제 유형별 지적 수(판정기): 오답 근거(violates) 10, 복수 정답 2, 사실 오류 6, 번역 0, 시험 적합성 7, 지문 모순 2 · 사실 오류 6건

## 판정기 보정 (외부 검토와 비교)

- 외부 검토(Claude Opus·GPT 교차 검토) OK 비율 5%, 판정기와 OK/비OK 일치 80% (20문제)
- 외부 검토가 지적한 유형을 판정기도 찾은 비율:
  - 오답 근거(violates): 9/14
  - 복수 정답: 1/4
  - 사실 오류: 1/2
  - 번역: 0/3
  - 시험 적합성: 3/5
  - 지문 모순: 0/2

## 문제별 결과

| 문제 | 세부 항목 | 판정 | 직접 푼 답 일치 | 지적 유형 | 외부 판정 | 이유 |
|---|---|---|---|---|---|---|
| r1-q1 | 1.2 | ok | 예 | - | fix (violates) | The question is well-constructed and directly tests the hub-and-spoke principle from the reference material. The correct answer (B) aligns precisely with the documented guidance to route all subagent communication through the coordinator for observability and controlled error handling, and each wrong option fails for a clear, distinct reason tied to the stated requirements. |
| r1-q2 | 3.1 | fix | 예 | violates | fix (violates, fit) | Option C's `violates` phrase is incorrectly attributed: it quotes a background fact ('stored them in ~/.claude/CLAUDE.md on their local machine') rather than the actual requirement that C breaks ('without anyone having to copy instructions manually' / 'available to all CI runs and teammates'). Everything else—correct answer, translation, and factual accuracy—is solid. |
| r1-q3 | 4.1 | fix | 예 | violates, fact, fit | fix (multiple, violates, translation) | The correct answer (B) and its justification align well with the reference material's emphasis on explicit categorical criteria over confidence-based filtering. However, the 'violates' labels for options A and D are technically incorrect: A's label points to an observation rather than a requirement it breaks, and D's label implies 'only report high-confidence findings' is a goal to preserve, when in fact it describes the current failing approach. Additionally, A and D share the same misconception (confidence-based / vague filtering can fix false positives), flagging shared_misconception as true. |
| r1-q4 | 1.1 | fix | 예 | fit | fix (fit, stem) | The correct answer (D) directly mirrors the stem's two requirements word-for-word, making it trivially identifiable by matching language rather than by applying understanding. This is the main quality concern; the factual content and wrong-option distractors are otherwise sound and well-grounded in the reference material. |
| r1-q5 | 2.1 | fix | 예 | violates, fact | fix (violates) | Option B's `violates` phrase ("distinct inputs, outputs, and usage boundaries") is inaccurate: renaming `analyze_document` to `analyze_content` does not violate that requirement — the tools would still remain separate with distinct boundaries. The real reason B is wrong is that the guide explicitly states tool descriptions are the primary mechanism LLMs use for tool selection, and identical descriptions cause misrouting regardless of tool names; the violates phrase should reference the near-identical descriptions constraint instead. |
| r1-q6 | 1.3 | fix | 예 | violates, fact, fit | fix (stem, translation, violates) | The correct answer (A) is directly stated in the stem itself — the last sentence of the scenario literally says 'the coordinator's allowedTools is set to ["Read", "Grep", "Glob"]' with no "Task", which is the exact answer. This is a textbook answer-copies-stem problem: a careful reader can identify the correct option purely by pattern-matching the stem wording without applying any domain knowledge. |
| r1-q7 | 1.4 | ok | 예 | - | drop (fit, fact) | The question is well-constructed and accurately grounded in the reference material's distinction between prompt-based guidance (non-zero failure rate) and programmatic prerequisite gates (deterministic enforcement). All wrong options are clearly wrong for the reasons stated in their violates phrases, and the correct answer D is unambiguously supported. |
| r1-q8 | 1.5 | fix | 예 | fact | fix (violates) | The correct answer and distractors are logically sound and well-differentiated, but the reference material never mentions 'updatedToolOutput' as a PostToolUse return mechanism—only 'updatedInput' is explicitly named—so option A's key technical detail is unverified by the provided sources. |
| r1-q9 | 1.6 | fix | 예 | fit | fix (fit) | The correct answer is accurate and well-supported by the reference material, but option A copies the stem's wording almost verbatim ("generates its next subtask based on what it discovers at each step rather than a predetermined plan"), making it identifiable by matching language alone without applying the underlying concept. Additionally, options B and C both describe the same fundamental mistake (fixed pre-determined sequencing), which constitutes a shared misconception across two distractors. |
| r1-q10 | 1.7 | ok | 예 | - | fix (violates) | The question is well-constructed and the correct answer (C) is directly supported by the reference material's description of fork_session for creating parallel independent branches from a shared baseline. The wrong options each clearly violate a stated requirement, and the Korean translation faithfully preserves all conditions from the English stem. |
| r1-q11 | 3.2 | fix | 예 | violates | fix (violates) | The question is well-constructed and correctly identifies context: fork as the right answer per the reference material. The distractors test plausible but incorrect alternatives, and the Korean translation faithfully preserves all conditions and constraints from the English stem. |
| r1-q12 | 3.3 | fix | 예 | violates, fact | fix (multiple) | The correct answer (A) is well-grounded in the reference material. However, options B and D contain factual errors not supported by the reference: B falsely claims subdirectory CLAUDE.md cannot be version-controlled (contradicted by the material), and D invents a behavioral claim about session-wide loading that the material does not support. |
| r1-q13 | 4.2 | fix | 예 | violates, fact | fix (violates, fact) | Option B's assigned violates phrase is inaccurate: adding a JSON schema to a prompt is not the same as adding longer instructions, so the stated reason learners are shown for why B is wrong is misleading. The actual reason B fails is that it solves neither severity inconsistency nor false positives. Everything else in the question is sound. |
| r1-q14 | 4.3 | fix | 예 | multiple | fix (multiple, violates) | Option A is technically defensible: because only one tool is registered, tool_choice 'any' in practice always resolves to code_review, making it nearly as effective as the forced tool setting. However, the reference material explicitly distinguishes 'any' (choose among all registered tools) from forced tool selection (must call a specific named tool), so the marked answer D is the most precise and correct choice. All other options are clearly wrong per the reference material. |
| r1-q15 | 2.2 | ok | 예 | - | ok (-) | The question is well-constructed and all four options are clearly differentiated. The correct answer (C) precisely matches the reference material's distinction between transient errors (isRetryable: true) and validation errors (isRetryable: false), and each wrong option fails for a distinct, unambiguous reason tied to a stated requirement. |
| r1-q16 | 2.3 | ok | 예 | - | fix (multiple) | The question is well-constructed and accurate. The correct answer (B) precisely matches the reference material's guidance: synthesis-core tools (summarize_text, generate_citation) plus a single scoped cross-role tool (verify_fact) for the stated spot-checking need, with all retrieval/document tools removed. |
| r1-q17 | 3.4 | fix | 예 | violates, fit, stem | fix (violates, fit) | The correct answer and all wrong-option discriminators are well-grounded in the reference material. However, the scenario is somewhat unrealistic: plan mode inside an unattended CI/CD pipeline would produce a plan with no human to review and iterate on it before execution, which defeats its stated purpose per the reference ("get it to lay out a plan… actually read it"). |
| r1-q18 | 3.5 | fix | 예 | violates, multiple, fit, stem | fix (violates) | Option A's assigned 'violates' phrase is incorrect: the interview pattern is also performed *before* implementation, so it does not violate "before asking Claude to write any implementation code." The real reason A is wrong is that the team already has concrete representative records, making direct examples more effective than asking Claude to elicit requirements it already has. Additionally, the correct answer (B) closely mirrors the stem's own wording ("Two or three representative records illustrate each edge case clearly"), making it somewhat copy-the-stem guessable. |
| r1-q19 | 3.6 | fix | 예 | violates | fix (violates) | The question is well-constructed and the correct answer (B) is clearly supported by the reference material. The one minor uncertainty is whether --output-format json alone fully satisfies the machine-parseable requirement without --json-schema, but the reference treats them as a pair for stricter enforcement rather than requiring both in all cases. |
| r1-q20 | 4.4 | fix | 예 | fit | fix (violates, translation) | The correct answer (C) is accurate and well-supported by the reference material. However, the correct option wording nearly copies the stem verbatim ("original PR diff, the failed extraction output, and the specific validation errors"), making it trivially identifiable by pattern-matching rather than understanding. |
