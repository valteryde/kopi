# Tech stack

Kopi is one Django project in one Docker image. The UI is server-rendered Django views. `django.contrib.admin` is not installed. There is no separate frontend and no task queue.

The spec is `docs/specsheet.md`. This document is the stack that implements it.

## Web and scheduler

Django does not include a production web server or a scheduler. The same project still runs both jobs.

The web process is gunicorn serving the Django WSGI app. It handles login, setup, configuration, the audit log, and restore requests. A "run now" action inserts a run row. It does not start `pg_dump`, `tar`, or a hook script.

The scheduler is a Django management command in that same project. It uses the same settings and the ORM. The loop sleeps a short interval, finds links that are due in their timezone, and starts a run only when that link has none in progress and a slot is free. The default is one slot. Each run is its own child process in its own session, so a fault or a kill stays inside that backup. The command also sends failure mail and the status digest.

Both can live in one OS process: a thread started when gunicorn loads the app, with gunicorn pinned to one worker and a lock so a reload cannot start a second loop. We do not do that. A crashed or stuck backup would take the UI down with it, and the usual gunicorn worker model starts the loop once per worker. Two processes keep the site up when the worker dies, which is also when the digest stops and an operator needs the history screen.

`manage.py runserver` is Django's development server. It can serve the UI locally. The image runs gunicorn.

One container runs both processes under s6-overlay. s6 is PID 1: it reaps zombies from dumps and hook scripts, starts the web and worker services after migrations, restarts the worker if it exits, and on SIGTERM stops both. The web service stays up while the worker restarts. The worker takes an exclusive lock on the data volume and exits if another worker holds it.

## How the web and the worker talk

They share the SQLite run row. The run child updates `phase`, `bytes_moved`, and `updated_at` at most once a second, in a short autocommit transaction. It does not hold a transaction across the copy loop. Dump and encrypt bytes come from the fixed-size buffer the child copies into `age`. Upload bytes come from the storage client's progress callback. The page polls that row about once a second and renders the phase and the megabytes. Reloading the page keeps the counter, because the number is stored with the run.

A sync gunicorn worker would stay busy for the whole backup if the page held a server-sent stream open. Polling leaves the web process free between requests.

Celery, Huey, RQ, and django-q are queues. Each one still needs a broker or a second job table, and none of them can see how many bytes a pipe has moved. Kopi already has that table: a run row is the job, the history, and the overlap lock. A "run now" click inserts the row. The worker picks it up. Progress is a column on it.

## Large backups

The worker command supervises. The child runs one link: pre script, pipeline, upload, post script. Children set `PDEATHSIG` to `SIGKILL`, so a dead worker does not leave `pg_dump`, `tar`, `zstd`, or `age` running. On `SIGTERM` the worker stops taking runs, signals the children, and waits for them to exit. s6 then restarts it, and startup reconciliation finishes anything the signal cut short.

The pipeline never holds the archive in memory. The child reads and writes a 1 MiB buffer.

* **Postgres.** `pg_dump -Fc` stdout is copied into `age`.
* **Folders.** `tar` (posix format, permissions kept, symlinks stored as symlinks) is compressed with single-threaded `zstd` and copied into `age`.
* **Ciphertext.** `age` writes a new file on the work volume, mode `0600`. The same buffer updates a SHA-256 of the ciphertext. When `age` exits 0 the child `fsync`s the file and writes a sibling marker with the run id, the size, and the hash. The marker is what "finished" means.
* **Upload.** boto3 multipart upload from that file. S3 allows 10,000 parts and 5 TiB. The part size is at least 8 MiB and at least `ceil(file size / 10000)`, and the file is rejected above 5 TiB. In-flight parts stay inside a 256 MiB buffer budget, so a large part size uses one part at a time. Transient S3 errors use boto3's retries.
* **Stall.** Hooks use their own timeouts. Dump, compress, encrypt, and upload have no maximum runtime. If `bytes_moved` does not change for the stall interval (default 15 minutes), the child kills its process group and the run fails.
* **Disk.** The work volume has to hold the largest ciphertext. Free space below a configured floor refuses a new run. `ENOSPC` fails the run. A partial file, one without a marker, is deleted. A marked file is kept and the next attempt uploads it. It does not dump again.
* **Startup.** Rows left in `running` are reconciled before new work starts. A valid marker resumes at upload. Anything else deletes the partial, runs the post script when the pre script had succeeded, marks the run failed, and sends the failure mail.
* **Logs.** Captured stdout and stderr are capped at 1 MiB per run.

## Secrets

The threat model in the spec still holds: anyone who can read the Kopi host can read the data. The worker's job is to avoid extra copies.

* gunicorn, the worker, and every run child use one unprivileged user. s6 is PID 1. The services drop to that user with `s6-setuidgid`. Their umask is `077`.
* `secret.key` is the age identity. Django's session key is a different file on the data volume. Both are mode `0600`. The image build contains neither, and no database password or S3 key is baked into the image or the default environment.
* Credential columns are age ciphertext. A run child decrypts them for the length of the run. `pg_dump` reads a `0600` pgpass file that the child creates and deletes in a `finally`. S3 credentials go into the boto3 client. Neither is put on an argv list, into a hook environment, or into a log line. Hooks get a minimal environment: `PATH`, `HOME`, `LANG`, and the run id.
* A hook path has to resolve to a regular file. The child runs it as the Kopi user.
* Restore writes plaintext only at the destination the admin chose, with files mode `0600` and directories mode `0700`.
* `TMPDIR` is a private directory on the work volume. The dump itself is not a temp file. It is the pipe into `age`.
* The image serves HTTPS from a mounted certificate. Cookies are `Secure`, `HttpOnly`, and `SameSite`. Plain HTTP starts only when `KOPI_ALLOW_HTTP=1` is set. `DEBUG` is off. `ALLOWED_HOSTS` is required. Repeated login failures are stored in SQLite and pause that account for a short time.

## Application

* **Django** for views, templates, sessions, users, and migrations. Roles are groups on `django.contrib.auth`. The first request with an empty user table is the setup screen.
* **SQLite** through the Django ORM, on the data volume, in WAL mode, with a busy timeout. The web process and the worker both write. Rows are configuration, runs, and the audit log.
* **WhiteNoise** serves the app's own CSS and scripts. The container has no separate web server and listens on one port.
* **croniter** and the standard-library timezone database decide when a link is due. Cadences are cron expressions, stored and evaluated in the link's timezone, recorded in UTC.
* **boto3** is the S3 implementation behind a small storage interface (`put`, `get`, `list`, `delete`).
* **Django's email backend** sends SMTP failure mail and the digest.

Credential fields in SQLite are ciphertext. `age` encrypts and decrypts them with `secret.key`. Pages show whether a secret is set. They do not show the secret.

## In the image

Debian is the base. The image contains the app, `bash`, GNU `tar`, `zstd`, `age`, and more than one major version of `pg_dump`. A Postgres source stores the dump binary it should use. `pg_dump` has to be at least as new as the server it dumps.

The checksum stored with the object is the SHA-256 of the ciphertext, computed while it was written.

The data volume holds SQLite, `secret.key`, and the Django session key. The work volume holds staged ciphertext and the completion markers. Folder sources and hook scripts are bind mounts, and the paths saved on a source or a link are paths inside the container.

## Tests

Tests are Ward tests. They cover schedule math, the run state machine, retention, and the pre and post failure rules. They also feed the pipeline a stream larger than the buffer and check that memory stays flat, that a missing marker deletes the partial and runs the post script, that a valid marker uploads without calling `pg_dump` again, and that the `pg_dump` argument list contains no password. S3 is a fake storage implementation. Hook scripts, `tar`, `zstd`, and `age` run for real in a temporary directory. Postgres is only required by tests that take a dump.
The check lines have their own tests: `psql` is a stub script that records the argv it was given and the pgpass file it read, and the S3 probe runs against a fake client that answers the calls a run makes and fails each one on cue. Tests tagged `integration` open a real connection to the Compose Postgres and write a real probe object to Garage, then assert the bucket holds nothing under the probe prefix afterwards.

## Left out

Celery, Huey, RQ, django-q, and Redis. Django admin. A client-side frontend. `runserver` in the image. tini with a shell script as the process supervisor. A long-lived progress stream held open on a web worker. Loading an archive into memory, staging plaintext, and running the services as root.
