# Phase 4 — Operate

The phase is described in `docs/roadmap.md`. The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`. The mail screen is `designs/layout.html`.

This is the working split. The child still archives and uploads. This phase applies the retention rule, sends mail from the worker, and sets the flags a deployed process needs. The image, s6, and the operator guide stay in phase 5. Replacing `secret.key` stays out.

Done when a failed run sends mail, a quiet installation still sends the digest, and old backups are deleted by the link's rule.

## Mail screen

One SMTP channel. `/mail/` is the screen. An admin gets the form. A viewer gets the key-value list from the mock. The password renders as "Set. Not shown." Both see the last-digest panel. `mail.css` is the mock's mail grid, linked only from this template. The restore dialog stays the only shadow.

Nav gains Mail, icon `envelope`, between Links and Audit. The admin role card gains the mail channel. `icons.css` gains the glyphs this screen uses.

## Goal 1 — Retention

The link form already stores the rule. This goal deletes objects.

1. Add `retain` to the run phases. The phase strip gains Retain after Post, caption "Applying the retention rule". `poll.js` already reads the phase keys from the page. A migration adds `removed_by_retention`, default false. History, size, and checksum stay. Restore is hidden when the flag is set, and the inspect column says the rule removed the object.
2. Retention runs in the child, after the post script and before the run leaves `running`, on the same path `execute` and `reconcile` already share. The stall watcher is already stopped, as it is for the post script. The child reads the link row at that moment.
3. A run with no object does not list or delete. A run that stored an object lists that target with the source prefix (`name/`). Keep-last-N keeps the newest N objects that `list` returns, timestamp in the object name, key as the tiebreak. Delete-older-than deletes objects whose timestamp is earlier than now minus the link's days. An object left by a failed post script counts, because it is in the bucket. Keys `list` skips are left alone.
4. Each deleted key sets `removed_by_retention` on the run that stored that name. A delete error marks the run failed and keeps the new object. The next successful run of the link applies the rule again.
5. Reconcile of a running row whose upload is already recorded skips the archive, runs the post script when the pre script had succeeded, then applies retention. It does not upload again.
6. Ward tests, with the fake storage: keep-last-2 deletes the older object and hides its restore, delete-older-than uses the timestamp in the name, a failed upload deletes nothing, a post-script failure still prunes, a delete error fails the run and leaves the new object, and a marked file reconciled after a crash is not archived again.

## Goal 2 — Failure mail

The worker sends it. The child does not open an SMTP connection.

1. One `Channel` row, updated in place. Host, port (default 587), from-address, recipients, optional username, and optional password. The password is age ciphertext. Digest columns live on the same row: kind (`monthly`, `weekly`, or `daily`), hour, timezone, and `last_digest_at`. Defaults are monthly, hour 8, timezone UTC.
2. A failed run gains `failure_notified_at`. The tick sends one message for each failed run that finished at or after the channel was created and has not been notified. No channel means nothing is sent. A send error leaves the timestamp empty, and the next tick tries again. A mail error does not stop claims. The SMTP password is decrypted for the connection and stays out of the run child, the log, and the message.
3. Port 587 uses STARTTLS. Port 465 uses implicit TLS. Any other port sends without TLS. The message names the source, the target, the time, the reason, and either the object name or that nothing was uploaded.
4. The worker writes an audit row when the message goes out. The child's failure row stays as it is. The run inspect column shows that failure mail was sent once `failure_notified_at` is set.
5. Ward tests: a failed run produces one message and a second tick does not repeat it, a send error retries, a failure from before the channel existed is skipped, a succeeded run sends nothing, and the message contains no password and no `secret.key`.

## Goal 3 — Digest

The digest is the heartbeat. The mock draws a daily one. The stored default is monthly, on the 1st at 08:00, in the channel timezone. Weekly is Monday at the chosen hour. Daily is every day at that hour. The Mail screen is what changes it.

1. Turn the stored choice into a cron expression and decide it is due with the same rule as a link: the latest instant is at or before now, and `last_digest_at` is empty or earlier than that instant. Several missed months send one digest.
2. The tick sends it after the failure sweep. The body lists every link: last success, last failure, and the size of the last success, times in the channel timezone. No links still sends, and the body says there are no links. A quiet installation with only successes still sends.
3. `last_digest_at` is set after a send. A send error leaves it unchanged. No channel sends nothing. A digest is not an audit row.
4. Ward tests: a fixed clock in a non-UTC zone, the monthly default is due once across three missed months, a daily channel is not due again the same day, an empty link table still produces a message, and a send error stays due.

## Goal 4 — Mail screen

The screen from above, on the channel from goal 2.

1. The form saves host, port, from-address, recipients, username, password, digest kind, hour, and timezone. A blank password keeps the stored one. A bad port, an empty recipient list, or an unknown timezone is a form error. Saving writes "Notification settings changed".
2. The digest panel shows when the last one went out and when the next one is due. Nothing sent yet says so. The table is the current per-link status, in the same date style as runs.
3. The admin role card lists the mail channel. A viewer GET shows the key-value list. A viewer POST is refused.
4. Ward tests: the saved row matches the form, the password is ciphertext and absent from the HTML, a monthly channel can be switched to daily, and a viewer POST is refused.

## Goal 5 — Process flags

Settings only. The image in phase 5 is what listens with a certificate. Gunicorn sets the WSGI scheme from that TLS socket, so these flags do not use a proxy header.

1. `security_settings` reads the environment. `KOPI_ALLOWED_HOSTS` is a comma-separated list. Unset means `localhost` and `127.0.0.1`, which is the host default. A set value replaces it. `DEBUG` stays off.
2. Cookies are `HttpOnly` and `SameSite=Lax`. Unless `KOPI_ALLOW_HTTP=1`, the session and CSRF cookies are `Secure` and HTTP requests redirect to HTTPS. The opt-in turns those three off.
3. `tests/bootstrap.py` sets `KOPI_ALLOW_HTTP=1` before Django starts, so the existing client tests stay on HTTP. The readme's development section says the host exports the same variable. The image does not set it.
4. Ward tests: the function with the opt-in, the function without it, `DEBUG` is off, and a request whose `Host` is outside the configured list is refused.
