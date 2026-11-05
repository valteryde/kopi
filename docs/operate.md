# Operate

Kopi is one container. It serves the site and runs the worker. The image is stateless. A data volume holds SQLite, `secret.key`, and the separate key Django uses to sign sessions. A work volume holds the ciphertext while it is encrypted and uploaded.

The process user is `kopi`, uid 10001, gid 10001. s6-overlay is PID 1. It migrates, then starts the site and the worker. The worker restarts if it exits. The site stays up while that happens.

## Run the image

The process listens on `8443`.

* Publish that port. The development Compose file publishes it on `127.0.0.1:8443`.
* Mount a certificate at `/cert/tls.crt` and the key at `/cert/tls.key`. Both have to be readable by uid 10001. Gunicorn uses them, and the site sees HTTPS from that socket.
* Set `KOPI_ALLOWED_HOSTS` to the hostnames you will use, separated by commas. If the variable is unset, the web process refuses to start.
* Mount a data volume at `/data` and a work volume at `/work`. `KOPI_DATA_DIR` and `KOPI_WORK_DIR` point at them.
* The work volume has to hold the largest ciphertext. Kopi does not stage the plaintext. A restore downloads the ciphertext onto the work volume again before it writes plaintext at the destination, so leave room for that file as well.
* `KOPI_DISK_FLOOR` is the free bytes the work volume must have before a new run starts. The default is 67108864 (64 MiB). `0` turns the check off. A run that is already going is not stopped by the floor. Set the floor to at least the largest ciphertext if the volume is shared.
* Bind-mount each folder you want to back up, and bind-mount any pre or post script. A path saved in the UI is a path inside the container.

`KOPI_ALLOW_HTTP=1` is the plain-HTTP opt-in. It binds the same port without the certificate. The image healthcheck requests `https://127.0.0.1:8443/` with the mounted certificate and expects HTTP 200, so the opt-in does not satisfy that check.

`docker stop` needs about 30 seconds. Compose sets `stop_grace_period: 40s`. On SIGTERM, s6 signals the site and the worker and waits for the worker's own stop path. The worker stops claiming new runs and signals its children. A row left `running` stays `running`. The next start finishes it.

The development file is `compose.yaml`. The Kopi service is on the `app` profile, on the same network as Postgres and Garage. `docker compose up` without the profile starts only those two. `harness/image-cert.sh` writes a throwaway certificate under `devdata/cert` before `docker compose --profile app up`.

```yaml
services:
  kopi:
    image: kopi
    ports:
      - "127.0.0.1:8443:8443"
    environment:
      KOPI_ALLOWED_HOSTS: kopi.example
      KOPI_TLS_CERT: /cert/tls.crt
      KOPI_TLS_KEY: /cert/tls.key
    volumes:
      - kopi_data:/data
      - kopi_work:/work
      - ./tls:/cert:ro
      - ./uploads:/srv/uploads:ro
      - ./hooks:/hooks:ro
      - ./restore:/restore
    stop_grace_period: 40s
```

## First run

Open the site. The first request with an empty user table is the setup screen. It creates the admin and generates `secret.key`, then offers that file once. Download it and keep the copy outside any folder source. The working copy stays at `/data/secret.key`, mode `0600`.

A folder backup skips a file named `secret.key` wherever it sits in the tree. Do not point a folder source at `/data`. The data volume is where the key and the database live, and it is not a backup source.

## Replacing secret.key

An admin replaces the key from the Users screen. The form asks them to confirm they already have a copy of the current key. Kopi generates a new key, re-encrypts the database passwords and S3 secrets stored in SQLite, and offers the new key once. Objects already in the bucket stay readable only with the previous key. Keep that copy until those objects age out. Kopi does not re-encrypt them.

## Sources, targets, links, schedules, and retention

A source is one Postgres database or one folder tree. A target is an S3 bucket. A link connects one source to one target and stores the schedule, the retention rule, and the optional script paths.

The schedule is a cron expression in the link's timezone. One link does not overlap itself. The worker runs one backup at a time. A backup has no maximum runtime. If it moves no bytes for 15 minutes, the run fails.

A Postgres source stores the `pg_dump` binary it should use. The binary has to be at least as new as the server. The image has Postgres client 17 and 18, side by side:

* `/usr/lib/postgresql/17/bin/pg_dump`
* `/usr/lib/postgresql/18/bin/pg_dump`

The source form lists them as `pg_dump 17` and `pg_dump 18`. Restore uses the `pg_restore` next to the dump binary the source stored.

A Postgres source names the **server**, never a connection pooler. `pg_dump` empties the search path for the rest of its own session:

```sql
SELECT pg_catalog.set_config('search_path', '', false);
```

On the server that is harmless, because the backend closes with the dump. A pooler in transaction or statement mode keeps that backend and hands it to the next client, and that client then finds none of its own tables. Every read answers `relation "…" does not exist`, and a create answers `no schema has been selected to create in`. Nothing was dropped and nothing was restored: the data is where it always was, and a restart of the server or the pooler clears it. That is why a dump through a pooler looks like a lost schema hours later, to whichever service drew that backend.

Kopi refuses a source whose host holds `bouncer`, `pooler`, `pgpool`, `pgcat`, `pgdog`, `odyssey`, or `pgproxy`, or whose port is 6432, 9999, or 5000. The admin can tick *Run it through a pooler anyway*. That tick is not stored, so it has to be made again on every save. A source that already names a pooler keeps running, and the run log leads with the same warning. If a pooler has to sit in front of the server for the backup, it has to reset every backend it returns to the pool: `server_reset_query_always = 1` with `server_reset_query = DISCARD ALL` on PgBouncer 1.16 or newer. In transaction mode, `server_reset_query` on its own does not run.

Folder paths and script paths are paths inside the container. Include and exclude rules are globs, one per line. An empty include list is the whole tree. A pattern with no slash matches that name in any directory. `**` crosses directories, and any other wildcard stays inside one path segment. Exclude wins. `secret.key` is skipped even when an include would match it. Symlinks are stored as symlinks.

An admin can remove a source or a target that no link uses. A link with no runs can be removed. A link that has history can be retired: the schedule stops, Run now is refused, and the runs stay. Resume puts that link back on the schedule.

Each link keeps the last N successful backups, or deletes objects older than a number of days. The rule runs after the backup. An object left by a failed post script still counts, because it is in the bucket.

## Test before you save

Every source, target, and link form carries a **Test** button beside Save. It posts the values that are sitting in the form and answers with one line for each thing it tried. Nothing is saved, no run is queued, and no backup byte moves.

The folder check reads the tree and counts what the include and exclude rules match. Zero matches is a refused line, not a note: that run would have written an empty archive.

The Postgres check opens one connection with the `psql` that sits beside the `pg_dump` the source names, and reads `server_version` back. The password goes into a pgpass file, mode `0600`, deleted before the button answers, exactly as the dump does it. The check also compares that dump binary's version with the server's, and refuses a dump binary older than the server.

The target check performs the calls a run makes: list the bucket, start and abort a multipart upload, then write, read back, and delete a few bytes under `.kopi-check/`. That name is not `source/timestamp/checksum`, so a bucket listing cannot return it and retention cannot mistake it for a backup. The probe is gone before the check answers. If the credentials cannot delete, the line says so and names what was left behind.

The link check tries both ends, prints the next instant the cron owes in the link's own timezone, checks that each hook path is a regular file Kopi may run, and reports the free space on the work volume against `KOPI_DISK_FLOOR`.

On a saved row the password and credential boxes are usually left blank. A blank box is tried with the ciphertext already stored. When `secret.key` cannot open it, the line says that instead of naming an endpoint.

The probes are short, because they run inside a web request. `psql` gets 8 seconds to connect and the probe is cut at 12. The S3 client gets 5 seconds to connect, 10 seconds to read, and no retries. A check that reached the network leaves one line in the audit log. A form that does not validate is never probed.

The check runs in the web process, as a restore does, and the image serves with one worker. A bucket that answers slowly keeps that worker busy until the probe is cut, so the lines come back late rather than in the background.

A check answers whether the job can start. It does not measure how large the backup will be, and it does not run the pre script, so a link that passes can still fail later in the dump or in a script.

## Pre and post scripts

Both files stay on disk. Kopi does not store the script body. It runs each as `bash <path>`, as the user `kopi`. An empty path skips that script.

A pre-script failure, a missing file, or a pre-script timeout skips the backup. The post script does not run in that case. The post script still runs when the backup itself fails, as long as the pre script was skipped or exited 0. A successful upload is kept if the post script then fails. The run is still marked failed.

Each script has its own timeout. A post script has to be safe to run again. If the worker stops after the pre script succeeded and before the post script finishes, the next start runs the post script again.

The script's environment is `PATH`, `HOME`, `LANG`, and the run id. The database password, the S3 secret, and `secret.key` are not on the command line and not in that environment. A file named `secret.key` or `session.key` is refused, including through a symlink, and so is a path on the work volume or the database file. A line from the script that contains an age secret key is left out of the run log.

## Restore

An admin restores one finished backup that still has an object. A folder restore names an absolute path inside the container, outside `/data` and `/work`, on a mount the `kopi` user can write. Files are mode `0600` and directories are mode `0700`. A database restore names a database that already exists. Kopi loads the dump into it. The dump user needs to be allowed to write there.

## Failure mail and the digest

One SMTP channel, on the Mail screen. An admin edits it. A viewer can read it. The password is shown as "Set. Not shown."

A failed run sends one message. It names the source, the target, the time, the reason, and either the object name or that nothing was uploaded. A send error is tried again on a later pass. The SMTP password is not in the message. Sending the message is an audit row. The run's own failure row stays as it is.

The digest is the heartbeat. The default is monthly, on the 1st at 08:00, in the channel's timezone. Weekly is Monday at the chosen hour. Daily is every day at that hour. The Mail screen is what changes it. Several missed periods send one digest. The body lists every link: last success, last failure, and the size of the last success. No links still sends. A quiet installation with only successes still sends. If the digest stops arriving, Kopi is down, and something outside Kopi has to notice. A digest is not an audit row.

Port 587 uses STARTTLS. Port 465 uses implicit TLS. Any other port sends without TLS.

## Users, roles, and the audit log

An admin manages sources, targets, links, users, and the mail channel, and can restore and replace the key. A viewer can read the history, the configuration, the mail channel, and the audit log, and cannot change them.

An admin can disable another user. A disabled user cannot sign in, and a session they already have ends. You cannot disable yourself, and the last active admin stays active. Enabling the user lets them sign in again.

Repeated login failures pause that account for a short time.

The audit log is the meaningful events: login, setup, the key being created or replaced, a source or target or link change, a link removed, retired, or resumed, a run queued, started, and finished, a restore, a user created, disabled, or enabled, notification settings changed, failure mail sent, and a check that reached the network. Job stdout is the run log, not the audit log.

## If secret.key is lost

Objects written with that key stay readable only with that key. Kopi cannot decrypt them, and it cannot decrypt the database passwords and S3 secrets stored in SQLite. The session key on the data volume is a different file. It signs cookies. It does not open a backup.

Keep a copy of `secret.key` outside the container and outside every folder source. Losing the data volume and every copy of the key means the bucket is still full of ciphertext and none of it can be read.
