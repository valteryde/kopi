# Phase 1 — Engine

The phase is described in `docs/roadmap.md`. The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`.

This is the working split. The run child is invoked directly. The supervisor loop, slots, and cron stay in phase 2. Build the folder pipeline first, then the Garage round-trip, then Postgres.

Done when a folder round-trip works on the host, including a killed child and a retried upload. The integration set archives a directory, uploads it to Garage, downloads it, decrypts it, and compares. It also dumps the Compose Postgres and restores it into a second database.

## Goal 1 — Project and models

Django, SQLite, and the tables the engine writes. No views.

1. Add Django, `manage.py`, and a project package. Point SQLite at the data directory (`./devdata/data` in development), turn on WAL, and set a busy timeout.
2. Models for a source (folder or Postgres), an S3 target, a link, and a run. Credential columns are opaque text until goal 2 encrypts them. A link stores cadence, timezone, retention, and the pre and post script paths. A run stores status, phase, bytes moved, timestamps, size, checksum, object name, and the capped log.
3. Initial migration.
4. Ward test: migrate a temporary database and round-trip one folder source, one link, and one run.

## Goal 2 — Age identity

`secret.key` is the age identity. Tests generate their own. Nothing in git is a real key.

1. Generate the identity with `age-keygen` and write it mode `0600`. Load it from a path.
2. Encrypt and decrypt bytes with that identity. Credential columns and the pipeline both use this helper.
3. Ward tests: decrypt matches, the file mode is `0600`, and a missing or bad key fails.

## Goal 3 — Folder pipeline and directory restore

Prove `tar`, `zstd`, and `age` before any upload or hook logic.

1. Copy a producer through a 1 MiB buffer into `age`. Update a SHA-256 from that same buffer. The loop does not accumulate the stream.
2. Folder producer: GNU `tar` (posix format, permissions kept, symlinks stored as symlinks) into single-threaded `zstd`, then into the copy loop. A tree that would include `secret.key` skips that file.
3. When `age` exits 0, `fsync` the ciphertext, set mode `0600`, and write a sibling marker with the run id, the size, and the hash. Any other exit leaves no marker.
4. Restore decrypts one marked file and extracts it into a directory. Files are mode `0600`. Directories are mode `0700`.
5. Ward tests: a tree round-trip (contents, a symlink, permissions), the marker hash equals the ciphertext hash, a stream larger than the buffer stays flat in memory, and a killed producer leaves no marker.

## Goal 4 — Storage interface

`put`, `get`, `list`, and `delete` behind one interface. Hermetic tests use the fake. Garage comes in goal 6.

1. Object names carry the source, the timestamp, and the ciphertext checksum.
2. Fake storage. Multipart part size is at least 8 MiB and at least `ceil(file size / 10000)`. Reject a file above 5 TiB. In-flight parts stay inside a 256 MiB budget, so a large part size sends one part at a time.
3. The fake can fail a `put` once, so a retry can be tested without Garage.
4. Ward tests for the part-size rule, the 5 TiB rejection, list, get, delete, and a failed put.

## Goal 5 — Run child

One child runs one link: pre script, pipeline, upload, post script. Tests call it directly and use the fake from goal 4. `tar`, `zstd`, `age`, and `bash` run for real in a temporary directory.

1. Phases are pre, archive, upload, and post. Update `phase`, `bytes_moved`, and `updated_at` at most once a second, in a short autocommit transaction. `TMPDIR` is a private directory on the work volume.
2. Each script is `bash <path>` with its own timeout. Stdout and stderr are capped at 1 MiB. The environment is `PATH`, `HOME`, `LANG`, and the run id. The path has to be a regular file. The age identity and passwords stay off the command line and out of that environment.
3. A pre-script failure or timeout fails the run and skips the archive, the upload, and the post script.
4. The post script runs when the pre script was skipped or exited 0, including when the upload failed. A post-script failure keeps a successful upload and marks the run failed.
5. Upload reads the marked file through the storage interface. A successful upload deletes the local ciphertext. If `bytes_moved` does not change for the stall interval, the child kills its process group and the run fails. Tests use a short interval.
6. Reconciliation. No marker deletes the partial and runs the post script when the pre script had succeeded. A valid marker uploads again and does not start `tar` again.
7. Ward tests for each branch above, including a killed child and a retried upload.

## Goal 6 — Garage round-trip

The same storage interface, with boto3 pointed at the harness.

1. boto3 implementation: custom endpoint, path-style addressing, the `garage` region, multipart upload, and boto3's retries. The stored checksum is the SHA-256 from the marker. Garage's ETag is not.
2. Integration test, skipped unless Compose is up: archive a directory, upload it to the harness bucket, download it, decrypt it, and compare it to the source tree.

## Goal 7 — Postgres producer

The folder path and the Garage path already work. This goal adds the second producer.

1. `pg_dump -Fc` writes into the same 1 MiB buffer and `age` pipeline. The child creates a mode `0600` pgpass file and deletes it in a `finally`. The argument list contains no password. The database password is age ciphertext in the source row.
2. Restore decrypts one marked dump and loads it into a named database.
3. Hermetic Ward test: the `pg_dump` argument list contains no password, and the pgpass file does not survive the child.
4. Integration test: dump the Compose database, upload it, download it, restore it into a second database, and compare the rows.
