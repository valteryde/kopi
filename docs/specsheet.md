# Base idea

Kopi backs up Postgres databases and folders to S3. The Kopi host encrypts every backup with a single key before upload, so the bucket stores ciphertext.

The key protects data at rest in S3. Anyone who can read the Kopi host can read the key, database passwords, and S3 credentials. Kopi does not protect against compromise of its own host.

# Concepts

* A **source** is a Postgres database or a folder tree.
* A **target** is an S3 bucket (in the future may be more).
* A **link** connects a source to a target. Links are many-to-many: one source can ship to several buckets, and one bucket can receive several sources. The link is the backup job. Schedule, retention, and optional pre and post scripts live on the link.
* A **run** is one execution of a link: run the pre script if one is set, then dump or archive, encrypt, upload, and record, then run the post script if one is set, then apply retention. While it is in progress the run stores its phase and how many bytes that phase has moved.
* A **channel** is an SMTP destination (host, from-address, recipients). v1 has email only.

# Feature specifications

* Store a single key in `secret.key` for encryption.
* Back up Postgres databases as logical dumps.
* Back up folders as one archive per run.
* Send backups to S3 on a schedule.
* Record each upload (time, source, size, checksum) and list what has been sent.
* Restore one backup to a folder path or a database.
* Apply a retention rule per link.
* Send mail on failure, and a periodic status digest.
* Stream each backup straight into `age` and store only ciphertext on the work volume. The volume has to fit the largest backup. A finished ciphertext file is kept and uploaded again if the upload fails, without taking another dump. An unfinished file is deleted. The ciphertext is deleted after a successful upload.
* On each link, optionally run a bash script from disk before the backup and another after it.

`secret.key` is never part of a backup. A folder source that would include the key file skips that file.

# Encryption

* One host-wide key file, `secret.key`.
* The first-run screen generates the key and offers a one-time download. Replacing the key is an audited admin action. Objects written with the previous key stay readable only with that key, so the previous key has to be kept until those objects age out.
* Postgres passwords and S3 credentials are stored in the app database, encrypted with `secret.key`.
* Object names are not encrypted. Each object name carries the source, the timestamp, and a checksum so backups can be listed without the app database. The payload is encrypted.

# Sources

* **Postgres.** Host, database name, user, and password. One database is one source. The backup is a logical dump (`pg_dump`), not a physical base backup. The host names the server itself. A connection pooler is refused, because the dump leaves an empty search path on whatever session it borrowed.
* **Folders.** A root path plus include and exclude rules. The archive is a full copy of the matched files for that run, including permissions, compressed with zstd. Symlinks are stored as symlinks, not followed. Incremental folder backup is out of scope for v1.

# Pre and post scripts

A link can run a script before the backup and a script after it. The pre script puts the source in a consistent state so the copy is not corrupted. The post script releases that state. Both are bash scripts that already exist on disk. Kopi does not ship a built-in quiesce, and it does not accept a script pasted into the UI.

* The link stores a filesystem path for each script, or nothing. An empty path skips that script. Kopi does not store, edit, or upload the script body.
* Kopi runs each as `bash <path>` on the Kopi host, as the Kopi user.
* The pre script runs before the dump or archive. The backup starts only when it exits 0. A missing file or a non-zero exit fails the run, sends the failure mail, and does not upload. The post script does not run in that case.
* The post script runs after the backup attempt, whether the upload succeeded or failed, as long as the pre script was skipped or exited 0. A successful upload is kept if the post script then fails. The run is still marked failed, and the failure mail is sent.
* Each script has its own timeout. If a script is still running when its timeout hits, Kopi stops it and fails the run. A pre-script timeout skips the backup and the post script. A post-script timeout leaves a successful upload in place.
* Stdout and stderr from both scripts go to the run log, capped so a noisy script cannot fill the disk.
* A post script must be safe to run again. If the worker stops after the pre script succeeded and before the post script finishes, the next start runs the post script again.
* Setting or clearing either path is an admin action, audited with other link changes.
* Passwords and the age identity are not placed on a script's command line or in its environment.

# Schedule and retention

* Each link has a cadence and a timezone.
* Each link has a retention rule: keep the last N successful backups, or delete objects older than a set age.
* A run that overlaps the previous run for the same link waits. It does not start a second dump.
* The worker runs one backup at a time by default. Further links wait for a free slot. There is no maximum runtime. A backup that moves no bytes for the stall interval fails.

# Notifications

* One SMTP channel: host, from-address, and recipients.
* A failed run sends a failure mail for that job.
* A periodic digest lists, per link, the last success, the last failure, and the last backup size. The digest is the heartbeat. If it stops arriving, Kopi is down, and something outside Kopi has to notice.

# User interface specifications

* Set multiple targets.
* Set multiple sources.
* Connect a target to a source (create a link) and set that link's schedule, retention, and optional pre and post script paths.
* Set the notification channel.
* List backup runs and restore one backup.
* While a run is in progress, show its phase and a live count of megabytes moved in that phase.
* Full audit log of meaningful events: login, setup complete, source or target or link changed, run started and finished, restore, key replaced, notification settings changed. Job stdout is a run log, not the audit log.
* Multiple user logins with two roles. An admin manages sources, targets, links, the key, and SMTP. A viewer can see backup history, restores, and the audit log.
* A single setup screen on first install. It creates the first admin and generates `secret.key`. Sources, targets, and SMTP are configured after login.

# Deployment

Kopi ships as one Docker image. One container runs the web UI and the worker.

* The image contains the app, `bash`, GNU `tar`, `zstd`, `age`, and more than one major version of `pg_dump`. Each Postgres source names the binary it uses.
* The image itself stays stateless. A data volume holds the SQLite database, `secret.key`, and the separate key Django uses to sign sessions. A work volume holds staged ciphertext and must have room for the largest backup.
* The web and worker services run as an unprivileged user. Key files and the database are readable only by that user.
* Folder sources and pre/post scripts are bind-mounted from the host. Paths set in the UI are paths inside the container.
* The container serves HTTPS with a mounted certificate. Plain HTTP is off unless it is explicitly enabled.

# Documentation

Operator documentation lives in `docs/` and matches this spec. The readme is the entry point and links to it.

* Run the image: HTTPS, the data and work volumes, how large the work volume has to be, and the bind mounts for folders and scripts
* First-run setup, and where to keep `secret.key` outside any folder source
* Sources, targets, links, schedules, and retention
* Pre and post scripts: both files stay on disk, Kopi invokes each with bash, a pre-script failure skips the backup, and the post script still runs when the backup itself fails
* Restore
* SMTP failure mail and the status digest
* Users, roles, and the audit log
* What is lost if `secret.key` is lost

# Out of scope for v1

* Incremental backups
* Physical Postgres base backups
* Notification channels other than SMTP
* Key rotation that re-encrypts existing objects
* Hook steps other than a pre and a post bash script on disk
