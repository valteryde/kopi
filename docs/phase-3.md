# Phase 3 — Web

The phase is described in `docs/roadmap.md`. The spec is `docs/specsheet.md`. The stack is `docs/techstack.md`. The look is `designs/layout.html` and `designs/rigid.css`.

This is the working split. Each screen is its own URL. The sidebar, the black bar, the lime current item, the pick list beside the inspect column, and the Phosphor icons stay. The mock stays in `designs/` as the reference.

Mail, the digest, retention deletes, HTTPS, and the plain-HTTP opt-in stay in phase 4. The link form still stores the retention rule, because the column already exists. The Mail item arrives with that phase. Replacing `secret.key` stays out of this phase. Setup generates it once.

Done when an admin can configure a folder and a database, watch the megabytes move, and restore both. Ward tests are the gate, using Django's test client.

## How the pages are split

Jinja through Django's Jinja2 backend, with the environment in `kopi/jinja.py`. Templates live in `kopi/jinja/`. A screen template includes `macros.jinja` and the partials it needs. `base.jinja` is the shell: brand, nav, key line, signed-in user, and the main block. Setup and login use the login card from the mock, on their own templates.

Icons go through one `icon` macro, so a screen asks for `folder` and the class string lives in one place. Repeated rows are macros: page head, search field, key-value row, empty line, pick, and stacked field.

CSS is extracted from `designs/rigid.css` into files under `kopi/static/css/`, linked from the shell:

* `tokens.css` holds the green, lime, sand, ink, and line variables, the type, and focus.
* `shell.css` holds the sidebar, brand, nav, and the main column.
* `controls.css` holds buttons, search, inputs, the segment control, and radios.
* `panel.css` holds panels, the two-column layout, tables, picks, key-value rows, and hints.
* `live.css` holds the running-run card, the phase strip, and the log.
* `modal.css` holds the restore dialog. That dialog is the only place a shadow is used.
* `login.css` holds the setup and sign-in card.

`tokens.css`, `shell.css`, `controls.css`, and `panel.css` ship with the shell. A screen's template adds the file that is only its own. WhiteNoise serves them. `django.contrib.staticfiles` is installed. `django.contrib.admin` stays out.

Instrument Sans (400, 500, 600, 700) and IBM Plex Mono (400, 500) are vendored under `kopi/static/fonts/`. Pages leave Google alone. Phosphor stays the icon font from `designs/vendor/phosphor/`. `icons.css` keeps the font-face and only the glyphs the templates use.

Scripts are plain modules under `kopi/static/js/`, one behavior each, included by the screen that uses them:

* `filter.js` hides rows that miss the search text, and shows the empty line from the mock.
* `poll.js` reads one running run once a second and writes the phase and the megabytes.
* `restore.js` opens the dialog from the row's data and submits its form.

Picks, the folder and Postgres switch, sign-in, and Run now are links and forms.

Views, forms, and URL routes are one module per screen under `kopi/views/` and `kopi/forms/`. A goal edits its own module.

The phase strip shows the phases the run row actually stores: Pre, Archive, Upload, Post. Archive is the pipe into `age`, so encryption stays inside that step. Retain appears when phase 4 applies it. Credential fields render as "Set. Not shown." A schedule is shown as its cron expression and its timezone.

## Goal 1 — Shell

The frame, with no accounts and no data.

1. Add the Jinja backend, WhiteNoise, and the static files above. Copy the icon to `kopi/static/icon.png`.
2. `base.jinja` renders the sidebar and an empty Runs screen: the head, the lede, and the table with no rows. Nav items that have no view yet are omitted.
3. Ward test: the runs URL returns the shell, the Kopi brand, and the Runs heading.

## Goal 2 — Accounts

First request with an empty user table is setup. After that, the same URL is login.

1. Install `django.contrib.auth`, `contenttypes`, and `sessions`. Roles are the groups `admin` and `viewer`.
2. Write `session.key` on the data volume, mode `0600`, and use it as Django's secret key. It is a different file from `secret.key`.
3. Setup, in the login card: name, password, create the admin, generate `secret.key`, and offer that file once. Login and logout are forms. Repeated failures are rows in SQLite and pause that account for a short time.
4. A viewer can read runs and the audit log. Sources, targets, links, Run now, and restore accept an admin only.
5. Ward tests: setup creates the admin and a mode `0600` identity, a second setup request is login, a paused account is refused, and a viewer POST to create a source is refused.

## Goal 3 — Audit

The log exists before the configuration forms, so those forms can write it.

1. An audit row stores when, who (a user or the worker), and the event. Job stdout stays on the run.
2. The audit screen is the mock's table. `filter.js` searches it. Viewers can open it.
3. The child writes run started and run finished, including a run the worker started on cadence. The web writes login, setup complete, and key generated.
4. Ward test: a login and a finished run each add a row, and the page lists them.

## Goal 4 — Sources

The pick list and the inspect column, for folder and Postgres.

1. `/sources/` selects the first source. `/sources/<id>/` selects that one. `/sources/new/` puts the empty form in the inspect column. The segment control is two links, folder and Postgres.
2. An admin gets the form in the inspect column. A viewer gets the key-value list. The password is stored with the age helper and shown as set.
3. The dump binary select lists `pg_dump` binaries found on `PATH`.
4. Creating or changing a source writes an audit row.
5. Ward tests: a folder and a Postgres source round-trip, the password is ciphertext, and a viewer gets the key-value list.

## Goal 5 — Targets

The same list and inspect pattern as sources.

1. Name, endpoint, region, bucket, path-style or virtual-hosted, access key, and secret. Both credential columns are ciphertext and render as set.
2. Creating or changing a target writes an audit row.
3. Ward test: a path-style target round-trips, and the secret is not in the HTML.

## Goal 6 — Links

A link is the job. Retention is stored and not applied.

1. Source, target, cron, timezone, keep-last-N or delete-older-than, and the pre and post paths with their timeouts. An empty path skips that script.
2. Run now is a POST that inserts a pending row. The button is disabled while that link is pending or running. The re-rendered page shows the queued line.
3. A bad cron or an unknown timezone is a form error. Creating, changing, or queuing a link writes an audit row.
4. Ward tests: the row matches the form, a bad cadence is refused, and Run now inserts one pending run.

## Goal 7 — Runs

The history, the live card, and the one-second poll.

1. The table lists runs. Selecting one shows the inspect column: result, size, checksum, object, and the log. A running run also shows the live card and the phase strip. The nav count is the number of running runs.
2. `GET` of that run's status returns phase, status, and `bytes_moved`. `poll.js` is on the page only while a run is running. It paints the megabytes and the phase, then reloads when the run leaves `running`.
3. Ward test: the status URL returns the bytes on the row, and the runs page includes the live card for a running run.

## Goal 8 — Restore

The dialog from the mock, on a finished run that has an object.

1. `restore.js` fills the dialog from the row. A folder asks for a path. A database asks for a database name. Confirm is a POST.
2. The view calls the existing folder and database restore. Plaintext is written only at that destination.
3. The runs page then shows the action note. The audit row records the restore.
4. Ward test: a marked folder object is restored to the named directory, and a viewer POST is refused.

## Goal 9 — Users

The table and the two role cards from the mock.

1. An admin adds a user with a name, a login, a password, and a role. The page lists users. The cards state what each role can do in this phase.
2. Creating a user writes an audit row.
3. Ward test: an admin creates a viewer, and that viewer can open runs and cannot open the new-source form.
