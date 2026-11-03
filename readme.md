# Kopi

Kopi backs up Postgres databases and folders to S3. The host encrypts every backup before upload, so the bucket stores ciphertext.

- [Spec](docs/specsheet.md)
- [Stack](docs/techstack.md)
- [Roadmap](docs/roadmap.md)

## Development harness

Postgres 18 and Garage run in Docker Compose. The Kopi application comes in a later phase. `./devdata` is bind-mounted so a worker on the host can see data and work files. Postgres and Garage keep named volumes.

Dev credentials are in `.env.example`. The image build leaves them out.

```sh
docker compose up -d
```

Postgres is published on `127.0.0.1:5432`. Garage's S3 API is published on `127.0.0.1:3900`. boto3 uses that endpoint, path-style addressing, and the `garage` region. The `garage-init` container assigns the layout and creates the bucket and access key. It is safe to run again.

`POSTGRES_PORT` changes the published Postgres port when 5432 is already taken. The default is 5432. Compose reads that override from `.env`. The host `pg_dump` has to be Postgres 18 or newer.

`garage-init` exits 0 once the bucket and access key exist. `docker compose up --wait` can still return non-zero, because that container is meant to exit.

Ward tests tagged `integration` talk to the harness and skip when Compose is not up.

```sh
ward
```
