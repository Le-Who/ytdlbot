# Separate test watchdogs from application deadlines

Start cancellation timing after the child fixture acquires its socket and output file. Bound fixture startup independently and reap real child processes even when a test fails. Use event-loop ordering and explicit lease-renewal signals for races where a sleep would make the result depend on file or task scheduling.

Give the Windows release fixtures extra startup time for MSYS process creation while keeping the Linux watchdog and production drain and readiness assertions. Allow the gated SQLite checkpoint test time to enter the executor transaction, and prove the shorter cleanup deadline separately with the coroutine test.
