# Roadmap

Kopi is built in six phases, in this order. A phase is done when its Ward tests pass. Phases 0 through 4 run on the host, against the Compose harness. Phase 5 is the first time Kopi itself runs as an image.

The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`.

Hermetic Ward tests are the default. They cover buffer size, markers, argv with no password, schedule math, and the run state machine, using a fake storage implementation. A second set is tagged `integration` and is skipped unless Compose is up. `docker compose up` is a deliberate step before that set. A layout mistake in Garage must not fail a hash test.

Dev credentials live in a committed `.env.example`. They are not the pattern for the image.

## Phase 0 — Harness

One `compose.yaml` at the repo root, before any Kopi application code.

* **Postgres.** The official image, one current major version, a named volume, and a healthcheck. An init script creates a database and a few rows so a dump is not empty. Port `5432` is published to the host.
* **Garage.** One node, replication factor 1, metadata on a named volume, S3 on a published port. A one-shot init container waits until Garage is healthy, assigns the layout, and creates the bucket and the access key. Kopi uses put, get, list, delete, and multipart. boto3 uses a custom endpoint and path-style addressing.
* **`./devdata`.** Bind-mounted for Kopi's data and work files, so a worker running on the host can see them. Postgres and Garage keep named volumes.

Done when both containers are healthy, the bucket exists, a host-side `pg_dump` of that database succeeds, and a host-side boto3 multipart upload of a small file can be read back. The SHA-256 computed while the file is written is the checksum. Garage's ETag is not.

## Phase 1 — Engine

The Django project, the SQLite models, and the age identity. No pages.

A run child copies a producer through a 1 MiB buffer into `age`, writes ciphertext mode `0600`, hashes while it writes, `fsync`s, and leaves a marker. The first producer is `tar` and single-threaded `zstd`. Upload goes through the storage interface. The hermetic fake enforces the multipart part-size rule. Pre and post scripts, the stall timer, progress updates, and startup reconciliation are in this phase. A missing marker deletes the partial and runs the post script. A valid marker uploads again and does not archive again. Restore decrypts one marked file back to a directory, files mode `0600`. Secrets are not placed on an argv list.

The Postgres producer is in this phase too, because the server is already up: `pg_dump -Fc` and a `0600` pgpass file deleted when the child exits. Build the folder pipeline first, then the Garage round-trip, then Postgres.

Done when a folder round-trip works on the host, including a killed child and a retried upload. The integration set archives a directory, uploads it to Garage, downloads it, decrypts it, and compares. It also dumps the Compose Postgres and restores it into a second database.

## Phase 2 — Worker

The management command becomes the supervisor. One slot by default, the per-link overlap rule, cron and timezone, the exclusive lock, a child process group, and `PDEATHSIG`. `SIGTERM` stops new work and lets startup reconciliation finish the rest. "Run now" is inserting a row.

Done when a due link runs, a second link waits for a free slot, and killing the worker does not leave `tar`, `zstd`, `pg_dump`, or `age` behind.

## Phase 3 — Web

First-run setup creates the admin and `secret.key`. Admin and viewer roles, the session key, login pause, and the audit log land with the first forms. Then folder sources, S3 targets, links, hook paths, the run list, and the one-second progress poll. Postgres and restore, already working in the engine, get pages here. Real boto3 replaces the fake only behind the same interface. The fake stays for the hermetic tests.

Done when an admin can configure a folder and a database, watch the megabytes move, and restore both.

## Phase 4 — Operate

Retention, failure mail, and the digest. The digest is the heartbeat: monthly by default, and the Mail screen can change that cadence. HTTPS settings, `ALLOWED_HOSTS`, cookie flags, and the explicit plain-HTTP opt-in, so the app process is deployable before it is packaged.

Done when a failed run sends mail, a quiet installation still sends the digest, and old backups are deleted by the link's rule.

## Phase 5 — Image

Debian, s6-overlay, the unprivileged user, `zstd`, `age`, more than one `pg_dump`, the data and work volumes, and HTTPS from a mounted certificate. Operator docs in `docs/`, with the readme as the entry point. The image joins the Compose network from phase 0.

Done when one image, started with the two volumes and a certificate, performs the phase 3 round-trip.
