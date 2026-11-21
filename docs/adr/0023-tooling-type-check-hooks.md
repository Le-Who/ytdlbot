# Run local type-check hooks in the project environment

Run both mypy platform checks in the activated project virtual environment. They resolve the runtime dependencies used by CI and the tool versions pinned in `requirements-ci.txt`, which keeps platform results comparable to the release gate.
