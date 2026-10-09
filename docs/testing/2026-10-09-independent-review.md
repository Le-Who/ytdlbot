# Independent whole-change review

Review date: 2026-10-09. Baseline: `cc6e06edc1ad699c6540aa85ede771c53429bd9c`. Scope: current uncommitted production/test/config changes, the approved audit design and five-task coverage plan, audit report and complete source matrix, implementation evidence, and independent task reviews. This reviewer did not edit production/tests, use the index or change HEAD, commit, spawn agents, or access live providers, Docker, browser sessions, cookies, or clipboard. This report is the only intentional write.

## Final verdict

**SPEC COMPLIANCE: PASS.** The complete source inventory, risk-ordered necessary-coverage scheme, reproduced fixes, objective test replacements, scoped independent reviews, final stable-source verification, and explicit external-state limits satisfy the approved audit design and plan.

**CODE / TEST QUALITY: PASS.** The final lifecycle guard resolves the previously blocking ordering, and its regression plus startup-failure controls passed this reviewer's independent focused execution. No actionable code defect remains from this whole-change inspection. Earlier task-review findings are not counted as still open when their corrected behavior and re-review evidence resolve them.

No actionable finding remains in the reviewed changes. This verdict applies to the local corrected source and documented offline scope; it does not claim the remaining runtime/live acceptance layers have been executed.

## Resolved final lifecycle finding

### [P2, resolved] Failed replacement startup could close the old live runtime's shared Redis

Historical reference: the Redis-adoption guard in `app/main.py:103-109` before the final guard correction; full independent reproduction and scoped judgment are in `task-1-rereview.md`. Final correction: the single-lifespan ownership wrapper at current `app/main.py:248`, release checks at `app/main.py:278`, and dependency-close release at `app/main.py:131`.

The previous trigger was runtime A's bounded shutdown retaining a cancellation-resistant handler and the shared Redis client. Runtime B entered the actual lifespan, adopted the same client, started its bot and published its state, then failed to acquire the singleton still correctly owned by A. B's failure cleanup closed Redis although A's handler remained live. The current-bot identity check alone could not account for this ordering.

The scoped independent reviewer reproduced this with two real lifespans and one temporary SQLite database, replacing only Telegram/Redis boundaries. Its observation was `close_calls 1 old_handler_alive True old_handler_finish_unset True`; it then released and joined the retained work. The whole-change reviewer inspected the same production path and agrees with the finding; this report does not misrepresent the separate reviewer's probe as a second execution.

The final minimal correction makes the application's existing single-bot-runtime contract explicit. A lifespan owns a guard before any startup work or shared-client use; an overlapping lifespan is rejected before it can mutate dependencies or globals. The owner is retained through bounded/deferred shutdown and released only after worker termination and captured dependency closure. This also eliminates the adjacent global-binding concern: B cannot replace or clear A's pipeline/store/bot references while A remains live. The wrapper's synchronous entry check covers the starting-runtime ordering as well as an already-running or deferred runtime.

The strengthened actual-lifespan regression proves zero rejected-successor resource acquisition/closure, A's unchanged exact globals and usable Redis, A's eventual single close, and a legitimate subsequent lifespan after complete cleanup. This reviewer freshly executed the overlap case plus all four existing builder/worker/webhook/polling startup-failure cases: **5 passed in 1.17s, exit 0**. No production/test files were modified to run them.

The final scoped `task-1-rereview-2.md` also approves this correction after 44 fresh drain/webhook tests and separate actual-lifespan probes for builder-failure owner release, cancellation during gated real SQLite startup, rejection until that operation settles, and a legitimate successor afterward. Those separate probe results were inspected as independent supporting evidence, not represented as this reviewer's own execution.

## Scoped independent verdicts reconciled

| Scope | Final review artifact under `.superpowers/sdd/2026-10-09-test-coverage/` | Verdict and evidence |
| --- | --- | --- |
| Task 1: core/runtime | `task-1-rereview-2.md`, following `task-1-review.md` and `task-1-rereview.md` | SPEC + QUALITY PASS; 44 scoped tests and the independent lifecycle probes. |
| Task 2: media | `task-2-rereview.md`, following `task-2-review.md` | SPEC + QUALITY PASS; 35 focused codec/real-handler cases after restoring Back compatibility; prior artifact/equivalence review retained. |
| Task 3: legacy services | `task-3-review.md` | SPEC + QUALITY PASS; 150 tests and 9 subtests, plus independent clip/authority boundary probes. |
| Task 4: objective boundaries/map | This whole-change review | Code/test-quality PASS; 104 tests and 5 subtests freshly run by this reviewer, plus exact source-inventory reconciliation. |
| Task 5: evidence/tooling | `task-5-rereview.md`, following `task-5-review.md` | SPEC + QUALITY PASS; 115 scoped tests after persisted-state rejection was moved before identity/preflight. |

All numerical scoped evidence belongs to its named review; only the Task 4 run, five-case lifecycle follow-up, and inventory checks are this whole-change reviewer's fresh executions. Final combined verification is reported separately below.

## Specification assessment of the completed work

- Queue and disk release now share one shielded owner operation. The tests check concrete capacity, other owners' reservations, marker cleanup, concurrent release, and a grow operation already waiting for the budget lock.
- Worker startup/normal claim/restart acquisition remain tracked until noncancelable SQLite operations settle. Pending startup is shared across callers and participates in shutdown completion. The new gated SQLite tests assert job state, no old owner, no handler launch, immediately available replacement ownership, and exactly one supervisor/maintenance pair.
- Drain keeps resistant handlers tracked and retains dependencies after the shared cleanup budget expires. The work-grace plus five-second cleanup contract is documented; it does not claim forced termination of arbitrary Python code. The final single-lifespan guard resolves the shared Redis and global-state failure ordering described above.
- Materialized bytes remain factory-owned through validation, metadata publication, and resolver cleanup. Thread moves are joined before cleanup, race losers and a cancelled winner are released, and transform final paths are removed when a later size/deadline check fails. Tests use real temporary paths/reservations with controlled phase gates, including repeated cancellation and a surviving subscriber.
- Album fallback now projects all alternatives to the literal selection `(2, 0)` and verifies actual transfer/delivery order. Command-default output identity survives public-flight normalization and remains distinct from ordinary best, explicit quality, and Shorts policy. Audio removes stale visual height at the context boundary. The animation validator agrees with the documented 60-second transform cap.
- Callback correction preserves the registered payload-free Back control while retaining required payloads for Send. The action/arity table is backed by the real picker and Send handler regressions, avoiding reliance on codec roundtrips alone.
- Legacy-service changes verify actual files and sizes: failed optional audio returns no partial path; selected-file probe data controls codec metadata; split/compression reject oversized outputs and clean owned files; clips reach the downloader rather than silently selecting whole-media native paths. VK authority rewriting and HTTP error classification preserve their intended adjacent cases.
- Acceptance tests compose real metrics exposition and embedded parser functions, and actual SQLite receipt fields through adapter and collector. Unsupported or ambiguous delivery evidence cannot become confirmed full delivery. Persisted unknown/unsafe state is rejected before identity/preflight or publication, while valid resumes retain live identity checks.
- Cookie export tests use synthetic Netscape data, preserve spaced argv, cover cancelled profile selection and temporary cleanup, and explicitly deny unfaked `subprocess.run`/`Popen` calls at fixture teardown. They do not establish real browser extraction success.

## Independent Task 4 judgment

The two-line Instagram correction forwards the cached section through both selected-story and all-story callbacks. New tests call the real callbacks and observe the resulting request clip, authorization scope, and exact item IDs at the delivery boundary. The change is appropriately narrow.

The replacement quality tests call real webhook routes, the real download response, the format-picker callback, the real queue-admission path, and the actual private-message handler. Their expected status/persisted payload, escaped captions, response bytes/headers, selected queue, and deduplicated extraction outcomes are independent observable oracles. The gallery test requires an actual command invocation before inspecting separator/cookie positions; the progress-error test now injects the named Telegram failure and verifies download/send completion. Command preference tests preserve unrelated users/fields, and janitor tests execute a maintenance cycle and check literal pressure/recovery transitions.

The deduplication regression gates extraction and the second caller's cache miss, then proves one extraction and both callers' completed output. The revised heartbeat tests observe actual renewal events while delivery is blocked and check that the renewal task has exited before release. Timing deadlines bound test failure rather than serve as the behavioral oracle. Limited isolated fault-injection evidence was inspected; it demonstrates sensitivity for the selected boundaries, not a general mutation score.

Fresh reviewer execution:

```text
.worktrees/media-provider-racing/.venv/Scripts/python.exe -m pytest tests/test_bot_boundary_contracts.py tests/test_security.py tests/test_security_main.py tests/test_security_html_injection.py tests/test_injection_prevention.py tests/test_logic_stability.py tests/test_new_features.py tests/test_bot_callbacks.py tests/test_janitor_disk_state.py tests/test_janitor_service.py --no-cov -o addopts= -q -m 'not integration' --timeout=60
104 passed, 5 subtests passed, 2 dependency deprecation warnings in 2.77s; exit 0
```

## Inventory and measurement evidence

An independent Python/`git ls-files` comparison asserted exact equality between the CSV source set and tracked application, standalone Python/shell, and runtime/configuration/CI source candidates. Result: **106 unique tracked rows = 76 app Python files + 18 standalone tools/scripts/manual diagnostics + 12 configuration/CI files; zero missing or duplicate rows.** The successful inventory command exited 0. An earlier diagnostic print hit Windows stdout encoding on a Unicode arrow after printing the same inventory; the corrected assertion-only check completed successfully.

The refreshed matrix separates historical `baseline_observations` from current `contract`, `existing_tests`, and `remaining_risks`. The reviewer inspected the corrected janitor, exporter, diagnostic, acceptance, core, and entrypoint rows and reran the exact 106-source inventory assertion successfully. The historical audit reports remain explicitly historical. After the last full run, an independent check verified **all 137 populated Python line/branch metric fields** against `coverage-final.json` within the documented two-decimal rounding.

The reviewer read, rather than duplicated, the final centralized full-run artifacts on the corrected source:

```text
1571 passed, 1 skipped, 5 deselected, 42 subtests passed, 4 warnings in 219.72s; exit 0
Coverage: 10700/13801 statements (77.53%), 2816/4374 branches (64.38%), 74.37% combined
Configured 60% floor reached without lowering it
```

Independent XML inspection found zero failure/error elements and **1572 `<testcase>` elements** (1571 passing cases plus one skip); the suite's `tests` attribute is **1614**, including the 42 subtests. These are different reporting counts and are not represented as 1614 distinct testcase elements. The single skip is the POSIX executable-mode check on Windows. Five integration cases were explicitly deselected; dependency/Windows event-loop warnings were reported rather than hidden.

The reviewer also independently recalculated all **176 SHA-256 source/test/tool/config snapshot hashes** after the run and found zero differences. This ties the final measurements to the reviewed stable source. No additional broad suite run was launched by this reviewer.

| Measurement scope | Statement coverage | Branch coverage | Combined |
| --- | --- | --- | --- |
| Baseline app scope, omitting main.py | 9036/11767 = 76.79% | 2312/3622 = 63.83% | 73.74% |
| Final, same original app-file scope | 9508/11967 = 79.45% | 2478/3710 = 66.79% | 76.46% |
| Final, all app including main.py | 9716/12205 = 79.61% | 2522/3776 = 66.79% | 76.58% |
| Final scripts/tools | 984/1596 = 61.65% | 294/598 = 49.16% | 58.25% |
| Final all measured Python | 10700/13801 = 77.53% | 2816/4374 = 64.38% | 74.37% |

The different denominators are explicit. Shell/config and excluded manual diagnostics receive no invented Python percentages, and the unexecuted legacy evidence harness remains visible at 0%. Execution coverage is accompanied by behavioral evidence and remaining risks rather than used as a correctness score.

The parent reports 303 reverse-order tests passing, clean `git diff --check`, and separate Bash syntax checks for the six shell scripts. This reviewer additionally inspected the static-comparison artifacts: **56 changed Python files, Ruff 186 baseline diagnostics to 161 final diagnostics, zero introduced; mypy 31 baseline and 31 final errors, zero introduced**. The existing whole-project diagnostic debt remains explicit; this is not a clean Ruff/mypy claim.

Final evidence files are `artifacts/test-audit/pytest-final.{log,xml}`, `coverage-final.{json,xml}`, `measurement-summary.json`, `final-source-snapshot.json`, `ruff-comparison.json`, and `mypy-comparison.json`. The preceding 1570-pass snapshot is retained separately as `.before-last-lifecycle-fix` and is not the basis of the final verdict.

## Limits of the conclusion

Offline tests establish the covered local behavioral contracts and improve sensitivity over the replaced copied tests. They do not establish production provider availability, real browser extraction, Telegram delivery, FFmpeg output playability/codec quality, real Redis Lua/parser behavior, or Linux process-tree/flock/Compose permission behavior. Those remaining layers are identified in the risk-ordered coverage scheme rather than claimed as executed. Python cancellation cannot forcibly terminate a resistant coroutine or already-running thread, and the ownership tests should continue to prove retained responsibility and fencing rather than pretend otherwise.
