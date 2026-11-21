# Keep ownership and integration boundaries real in offline tests

Use real SQLite for admission, late claims, and process restart, while controlling when each transaction begins. Model POSIX process operations through the process module's platform bridge so pytest keeps its own host platform. For Windows release fixtures, attach the synchronous Popen object through the same transport-handle boundary used by the production job-object bridge.

Run CI shell steps under fail-fast Bash with pipefail. Give valid syntax fixtures a nonzero exit so executing them fails the test. Reuse the focused HTTP and SQLite webhook test with each workflow's effective environment so validation reaches the real admission path without recursively invoking the workflow tests. Model Compose file selection so an explicit `-f` wins over an inherited `COMPOSE_FILE`. Execute only the extracted helper from the checked-in evidence adapter in the tests that inspect its embedded program.

Use real cookie lookup, command construction, media transfers, leases, and callback cache round trips where those boundaries are under test. Fake external process I/O, HTTP responses, and media probing where the test supplies synthetic payloads. Shared fixture imports register pytest fixtures for the consuming modules. Tiny fixture reads and writes may complete inline, which avoids introducing test-only background ownership. Reset global callback queue state between tests so one full-queue case cannot affect another. A zero-capacity semaphore takes the queueing path, and a zero waiter limit rejects the request immediately.

The startup rollback fixture expects the janitor to be the sole background maintenance task. Updating dependencies belongs to the immutable image build.
