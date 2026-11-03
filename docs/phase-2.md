# Phase 2 — Worker

The phase is described in `docs/roadmap.md`. The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`.

This is the working split. The phase 1 child still performs one run. This phase decides when to start it. Pages stay in phase 3. Retention, failure mail, and the digest stay in phase 4. s6 stays in phase 5.

Done when a due link runs, a second link waits for a free slot, and killing the worker does not leave `tar`, `zstd`, `pg_dump`, or `age` behind.

## Goal 1 — When a link is due

`croniter` and the standard-library timezone database. No processes.

1. Add `croniter`. A cadence is a cron expression, evaluated in the link's timezone. Instants are compared and stored in UTC.
2. A link is due when the latest cron instant is at or before now and no run for that link was created at or after that instant. Several missed instants make the link due once, not once per miss.
3. A run inserted by hand counts. Its `created_at` covers the latest instant at or before it.
4. An unknown timezone, or a cadence `croniter` rejects, is not due. The check returns that result and does not raise out of the caller.
5. Ward tests: a fixed clock in a non-UTC zone, a link that already ran for this instant is not due, three missed instants are one due result, and a bad cadence is not due.

## Goal 2 — Claim a run

One tick, with a stand-in child. The tick inserts and claims rows. It does not spawn a process.

1. A due link with no pending or running row gets one pending row. A later tick does not insert another while that row is pending or running.
2. Claim promotes the oldest pending row to `running` and sets `started_at`, in a short transaction. The transaction ends before the child starts. The slot count defaults to 1. Tests pass 2. A second link stays pending while every slot is taken.
3. A link that already has a running row is not claimed. A pending row is claimed even when the cadence is not due. Inserting that row is "run now". The tick does not care which process inserted it.
4. Free space on the work directory (`./devdata/work` in development) below the configured floor leaves a new claim pending. The floor defaults to 0. A row that is already running is still handed to the child.
5. Rows left `running` are handed to the child before any new claim.
6. Ward tests: a due link is claimed, a second link waits for a free slot, the same link does not overlap, a pending row runs off cadence, the floor refuses a new claim, and a running row is taken before a pending one.

## Goal 3 — The worker command and the lock

`manage.py work` is the supervisor. Tests keep calling the tick directly.

1. The command takes an exclusive `flock` on a lock file on the data volume. Another worker that finds the lock held exits non-zero. The lock lasts until the process exits. The lock is not a SQLite transaction.
2. The command loops the tick, then sleeps. The sleep is a setting. One call of the tick does not sleep.
3. Ward tests: a second process exits while the lock is held and acquires it after the holder exits. One tick of the command claims a due link.

## Goal 4 — Child process

The phase 1 child gains a process boundary. Phase 1 tests still call it in-process.

1. The supervisor spawns `manage.py runchild <run id>` in its own session. The argv is that command and the run id. Passwords stay off the argv and out of the environment, as in phase 1.
2. The child sets `PDEATHSIG` to `SIGKILL` when the host has it, then exits if the parent is already gone. Every process it starts (`bash`, `tar`, `zstd`, `age`, `pg_dump`) does the same. The worker also holds a pipe the child reads. When the pipe closes, the child kills its process group. That covers a host without `PDEATHSIG`, including the parent dying before the death signal is set.
3. The supervisor reaps its children. It does not hold a database transaction while a child runs.
4. Ward tests: a claimed link runs the phase 1 child. Killing the worker during a folder archive leaves no `tar`, `zstd`, or `age`. Killing it while `pg_dump` is in progress leaves no `pg_dump`.

## Goal 5 — Stop and reconcile

Startup reconciliation is the phase 1 child, run from the supervisor. Failure mail stays in phase 4.

1. On startup, and when a reaped child leaves its row `running`, those rows are reconciled before any new claim. A valid marker uploads again and does not archive again. Anything else deletes the partial, runs the post script when the pre script had succeeded, and marks the run failed.
2. `SIGTERM` stops new claims, signals each child session, waits for those processes to exit, and then the command exits. Rows left `running` stay `running`. The next start reconciles them.
3. Ward tests, with the fake storage from phase 1: `SIGTERM` during an archive leaves no `tar`, `zstd`, or `age`. The next start uploads a marked file and does not start `tar`. A partial with no marker is deleted, and the post script runs when the pre script had succeeded.
