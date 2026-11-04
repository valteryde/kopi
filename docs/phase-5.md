# Phase 5 — Image

The phase is described in `docs/roadmap.md`. The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`.

This is the working split. Phases 1 through 4 run on the host. This phase packages that process. One container runs the web and the worker. The image joins the Compose network from phase 0, and `docker compose up` without the new profile still starts only Postgres and Garage.

Done when one image, started with the two volumes and a certificate, performs the phase 3 round-trip.

## Goal 1 — Image contents

Debian stable, one Dockerfile at the repo root. The build context excludes `.env`, `devdata`, and `secret.key`. The image contains none of them.

1. Move `boto3` into the project dependencies and add `gunicorn`. The image installs from `uv.lock` without the dev group, so Ward stays out.
2. Install `bash`, GNU `tar`, `zstd`, and `age`, with `age` and `age-keygen` on `PATH`.
3. Install Postgres client majors 17 and 18 from the PostgreSQL apt repository. Each major lives in its own directory, both directories on `PATH`: `pg_dump` and `pg_restore` side by side. The source form then lists `pg_dump 17` and `pg_dump 18`, and restore finds the `pg_restore` next to the dump binary the source stored. `pg_dump` has to be at least as new as the server. The harness is 18, so the round-trip uses 18.
4. User `kopi`, uid 10001, gid 10001. The data volume is `/data`. The work volume is `/work`. `KOPI_DATA_DIR` and `KOPI_WORK_DIR` point at them. `HOME` is `/home/kopi`.

## Goal 2 — s6

s6-overlay 3 is PID 1. The Dockerfile pins the tarball it installs. A shell script is not PID 1.

1. A root oneshot creates `/data` and `/work` and chowns them to `kopi`. It does not chown folder sources or hook scripts. Those are bind mounts.
2. An oneshot runs `manage.py migrate` as `kopi` after that. The web and worker longruns start after migrate, and neither depends on the other.
3. Both longruns use `s6-setuidgid kopi` and umask `077`. The web process is gunicorn, one worker, `--timeout 0`, bound to `0.0.0.0:8443`. Timeout 0 keeps a restore from being killed. The worker is `manage.py work`. s6 restarts the worker if it exits. The web service stays up while that happens.
4. The certificate and key are mounted at `/cert/tls.crt` and `/cert/tls.key`, readable by `kopi`. Gunicorn uses them, and the phase 4 flags see an HTTPS scheme from that socket. `KOPI_ALLOW_HTTP=1` binds the same port without the certificate. `KOPI_ALLOWED_HOSTS` unset refuses to start.
5. On `SIGTERM`, s6 signals both services and waits long enough for the worker's own stop path. The worker stops new claims and signals its children. Rows left `running` stay `running`. The next start reconciles them. The exclusive lock on `/data` is unchanged.
6. The container healthcheck requests `https://127.0.0.1:8443/` with the mounted cert and expects HTTP 200.

## Goal 3 — Compose round-trip

The service is in `compose.yaml` on the harness network, behind a profile named `app`. `docker compose --profile app up` starts it. A plain `docker compose up` does not.

1. Named volumes for `/data` and `/work`. The cert is a bind mount. Folder fixtures bind in at container paths the UI will store. The service depends on Postgres healthy and on `garage-init` finished. It reaches them as `postgres` and `garage`.
2. The service sets `KOPI_ALLOWED_HOSTS` and the cert paths. It does not set `KOPI_ALLOW_HTTP`, and it does not load `.env.example`. Port `8443` is published on `127.0.0.1`.
3. The round-trip generates a throwaway certificate. Nothing in git is that key. A Ward test tagged `image` skips unless the service answers, in the same way the integration tests skip when Compose is down.
4. The test drives the published HTTPS port: setup, a folder source, a Postgres source aimed at the harness database, a path-style target aimed at `http://garage:3900`, a link for each, Run now, and the status poll. It restores the folder onto a bind mount and compares the tree. It restores the dump into a second database and compares the rows. The session cookie is `Secure`. The test removes the objects and the database it created.

## Goal 4 — Operator docs

`docs/operate.md` is the operator guide. The readme stays the entry point: the harness section remains, and an Operate section links the guide ahead of the phase links.

The guide covers what the spec lists, for the image as built:

* Run the image: the published port, the certificate mount, `KOPI_ALLOWED_HOSTS`, the data and work volumes, how large the work volume has to be, and the bind mounts for folders and scripts. Paths saved in the UI are paths inside the container. `KOPI_ALLOW_HTTP=1` is the plain-HTTP opt-in.
* First-run setup, and keeping `secret.key` outside any folder source. Setup generates the key once.
* Sources, targets, links, schedules, and retention. The dump binary has to be at least as new as the server. The image has 17 and 18.
* Pre and post scripts: both files stay on disk, Kopi invokes each with bash, a pre-script failure skips the backup, and the post script still runs when the backup itself fails.
* Restore.
* Failure mail and the digest. The digest is the heartbeat, monthly unless the Mail screen changes it.
* Users, roles, and the audit log.
* What is lost if `secret.key` is lost. Objects written with it stay readable only with that key.
