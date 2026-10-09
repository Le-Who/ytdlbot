# Lint/type cleanup and version alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Clean Ruff/mypy findings, update and align static-analysis tooling, preserve verified behavior.

**Architecture:** Freeze the post-audit working source and old/new analyzer findings. Parent owns config/CI and mechanical formatting; disjoint agents own bot type contracts and remaining runtime/tool lint boundaries. Scoped reviewers and a final combined review verify correctness.

**Tech Stack:** Python 3.12, Ruff 0.16.10, mypy 2.4.0, pytest; existing pinned runtime requirements unchanged.

**Spec:** docs/superpowers/specs/2026-10-09-lint-cleanup-design.md

## Global Constraints

- Preserve current dirty checkout, user files and prior audited fixes; no reset/commit/push/deploy.
- Stable versions verified from official registry; same rules/versions across environment, hooks and CI.
- No broad ignores/unsafe auto-fixes; explain narrowly justified exception/I/O rule suppressions.
- Reproduce and test semantic changes; preserve delivery receipts and cancellation/lease ownership.
- Offline tests only; no real browser/clipboard/provider/Telegram/Docker actions.
- Subagents reasoning >= high; concurrent edits have exclusive file scopes; parent runs final suite.

## Review Focus

- Bool versus DeliveryReceipt must retain partial/uncertain semantics.
- Exception variable lifetime must not mask errors or introduce UnboundLocalError.
- Annotation narrowing follows real Message/path/tuple contracts and optional UI failures.
- Async I/O refactoring must preserve resource ownership through started thread/process operations.
- Hooks must actually use project dependencies and identical analyzer versions, including newly added source files.

### Task 1: Reproducible tooling and mechanical cleanup — parent

**Files:** requirements-ci.txt, pyproject.toml, .pre-commit-config.yaml, .github/workflows/test.yml; imports/formatting across explicitly inventoried Python files before exclusive agent scopes begin.

- [x] Capture working-source baseline and exact old analyzer commands/findings; verify latest stable releases/mirror tags.
- [x] Pin updated tooling; retain current rule set, set Python 3.12 target and exclude generated artifacts/worktrees explicitly.
- [x] Make hooks/CI run identical lint/type scope with installed runtime/type dependencies; validate configuration and real hook execution without install/push.
- [x] Apply safe mechanical fixes, inspect diff, format scoped files and split remaining findings by exclusive file ownership.

### Task 2: Bot type/result contracts

**Files:** app/bot/{callbacks,commands,messages,group_logic,ig_callbacks}.py; associated bot tests only if behavior changes.

- [x] Read actual APIs and new mypy diagnostics; give independent receipt/bool/error variables and precise collections/parameter types.
- [x] Preserve best-effort UI boundaries; typed delivery outcomes remain authoritative.
- [x] Resolve scoped nonmechanical Ruff findings with narrow catches or documented boundary handling; no broad rule suppression.
- [x] Run focused bot/media entrypoint tests and scoped type/lint; independent review resolves all findings.

### Task 3: Core/services ownership and error-boundary lint

**Files:** app/core/, app/tasks/, app/main.py, app/services/; no app/bot edits.

- [x] Fix actual remaining typing findings and nonmechanical lint without weakening audited cancellation/ownership behavior.
- [x] Inspect broad catches and async I/O; reproduce semantic defects before changing behavior, justify truly broad external boundaries locally.
- [x] Run focused core/media/service tests and scoped static checks; independent review.

### Task 4: Tests/scripts/tools lint and final verification

**Files:** tests/, scripts/, tools/, tracked root Python diagnostics; docs/testing/2026-10-09-lint-cleanup.md.

- [x] Resolve remaining lint/format findings without weakening assertions or removing needed fault injection; isolate all external boundaries.
- [x] Review script/test exception and I/O rationale; test any behavior changes at actual boundaries.
- [x] Run full Ruff check, formatter check, mypy app, real hooks, diff --check and full offline suite after integration.
- [x] Fresh independent whole-change review; save exact version/check results and remaining external limitations.


## Итог выполнения

Все четыре задачи завершены; независимые scoped и whole-change SPEC/QUALITY verdicts PASS. Итог: [отчёт](../../testing/2026-10-09-lint-cleanup.md), [независимое заключение](../../testing/2026-10-09-lint-cleanup-review.md).

Ruff0.16.10 check и format184files проходят; mypy2.4.0 app76files проходит без ошибок (31→0). Runnerpre-commit4.6.2, Ruffhook0.16.10, hygienehooks6.0.0; seven actual hooks PASS. Runtime dependencies unchanged. Exact413enabledrules preserved; all182Python sources discovered; no addedtypeignore.

Full final offline suite1597pass42subtests1Windows/POSIXskip5integrationdeselected, exit0 in208.48s; all192frozenhashes unchanged. ConfirmedBytesIO/exporter/durable-boundary bugs have actualRED/GREEN regressions. Original3238assert/mockoracles retained; final3309. Existing critical SQLite/cancellation/lease/deadline/proxy invariants remain covered.

No commits, push, deployment, liveprovider/browser/clipboard/Telegram/Docker operations. Historical audit measurements preserved; later lint coverage fields added to106rowmatrix. Retained localbroad-catch/sync-I/O rationale and runtime/live limits explicit in report.
