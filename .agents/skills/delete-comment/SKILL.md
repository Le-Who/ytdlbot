---
name: delete-comment
description: Audit or remove prose comments in this repository when asked to clean up comments, explain comment debt, or run /delete-comment. Use the code-comment checker to inventory and verify the scoped cleanup.
---

# Delete comments

1. Resolve the requested paths, baseline revision, and review or edit scope.
   Read the applicable [repository instructions](../../../AGENTS.md). A review
   request authorizes findings. Clean up source when the user requests edits.
2. Run `python scripts/check-code-comments.py --inventory` from the repository
   root and filter its JSON to the requested paths. Cross-check it against
   language tokens and surrounding code. A hash inside a string, URL, quoted
   value, or data heredoc is data. Inspect embedded code with its own language
   syntax. Preserve docstrings, required notices, and operative directives
   such as shebangs, parser directives, and tool suppressions. Review prose
   appended to a directive separately. The checker can flag the whole token
   until you remove its prose suffix.
3. Classify each prose comment in context. Record the original path, line, and
   disposition. Classify multiline prose together and count its physical comment
   lines separately.

   | Category | Disposition |
   | --- | --- |
   | Tautology | Delete narration, section markers, and prose already stated by the code. |
   | Stale | Verify the old claim against current code, tests, or history and delete it. |
   | Excuse | Identify the internal workaround, observed consequence, and regression test. Link a real issue before deleting the comment. |
   | Decision record | Keep the constraint or trade-off in a concise record under `docs/adr/`. Cite an existing record if it already covers the decision. |

   An external protocol constraint with a deliberate, supported fallback is a
   decision. For an excuse, reuse an issue that covers the same defect. Create
   an issue in this repository only with explicit authorization. If no issue
   exists and filing is outside scope, keep the comment and report the
   proposed issue. Investigate ambiguous claims before editing them.
4. Remove only the reviewed prose. Preserve executable tokens, literal values,
   directives, and unrelated changes. Use the nearest file-native checks.
   Compare Python syntax trees with type comments enabled, ignoring source
   positions. For other files, compare the relevant parsed or rendered meaning.
   Inspect any formatter-only changes caused by deletions. Run
   `python scripts/check-code-comments.py --prune` when the cleanup retires
   existing allowances, followed by the checker without flags.
5. Report the baseline, scope, and verification. Give counts by category and
   distinguish physically removed standalone lines, inline prose removals,
   and prose stripped from retained directives. Link the ADRs and issue URLs.
   Include any unresolved comments with their original locations.
