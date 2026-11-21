# Retain resource ownership through cancellation

The task that acquires a queue slot, leases a path, or spawns a subprocess owns
it until cleanup settles or ownership transfers. Claim lease release before the
first await and forward a slot if cancellation races a grant. Leave a sidecar
under another owner to expire. Renew leases until owned media work has exited.
Reserve disk for both input and output while a transform has both on disk.

Use separate resolve and materialization singleflight groups to prevent a
factory from joining itself. Factories own bytes before publication and hand
them to a live subscriber atomically. Join losing attempts and retrieve terminal
exceptions even if subscribers disappear. Join optional progress callbacks before
their UI can outlive its operation.

Process ownership captures a cancellation generation before scheduling a launch.
A workload budget bounds independent jobs, and a separate process budget permits
the two children in a media pipe. POSIX completion reaps remaining descendants
after the leader exits. Process teardown remains bounded after the strongest kill.
Windows opens the suspended child through its public process ID, assigns it to a
kill-on-close job, and resumes its primary thread through Win32. The supervisor
owns the job until teardown, even if the original process exits first.
Clean up yt-dlp fragments by the exact UUID stem, including an appended format ID.
FFmpeg two-pass log files have the same owner as the transform.

Use a supervised join for disk workers that can outlive a cancelled await.
Small local descriptor opens, snapshots, and closed-file unlinks can instead
run synchronously so ownership stays with the request. HTTP stream cleanup
cancels and joins an unfinished curl task before closing its session, because
waiting for the body would turn a header probe into a full download. The
60-second FFmpeg stream cutoff also closes its unread source through the owner
instead of waiting for natural EOF.

See [offline lifecycle regressions](../acceptance.md#offline-acceptance).
