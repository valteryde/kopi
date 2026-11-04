# Kopi

Kopi backs up Postgres databases and folders to S3. The host encrypts every backup before upload, so the bucket stores ciphertext.

## Operate

[Operate](docs/operate.md) is the guide for running the image.

- [Spec](docs/specsheet.md)
- [Stack](docs/techstack.md)
- [Roadmap](docs/roadmap.md)
- [Phase 1](docs/phase-1.md)
- [Phase 2](docs/phase-2.md)
- [Phase 3](docs/phase-3.md)
- [Phase 4](docs/phase-4.md)
- [Phase 5](docs/phase-5.md)

## Development harness

Postgres 18 and Garage run in Docker Compose. The Django project stores SQLite in `./devdata/data`. `./devdata` is bind-mounted so a worker on the host can see data and work files. Postgres and Garage keep named volumes.

Dev credentials are in `.env.example`. The image build leaves them out.

Host development exports `KOPI_ALLOW_HTTP=1` so the development server accepts plain HTTP. The image does not.

```sh
docker compose up -d
```

Postgres is published on `127.0.0.1:5432`. Garage's S3 API is published on `127.0.0.1:3900`. boto3 uses that endpoint, path-style addressing, and the `garage` region. The `garage-init` container assigns the layout and creates the bucket and access key. It is safe to run again.

`POSTGRES_PORT` changes the published Postgres port when 5432 is already taken. The default is 5432. Compose reads that override from `.env`. The host `pg_dump` has to be Postgres 18 or newer. Encryption tests need `age` and `age-keygen` on the host.

`garage-init` exits 0 once the bucket and access key exist. `docker compose up --wait` can still return non-zero, because that container is meant to exit.

The image joins this network behind the `app` profile. `docker compose up` without that profile still starts only Postgres and Garage. `harness/image-cert.sh` writes a throwaway certificate under `devdata/cert`. That key is not committed.

```sh
harness/image-cert.sh
docker compose --profile app up -d --build
```

The image listens on `https://127.0.0.1:8443/`. Ward tests tagged `integration` skip when Compose is not up. Tests tagged `image` skip unless that port answers. On a fresh data volume the image test runs setup. After that it signs in as `admin` with password `kopi-image-pass`.

```sh
ward
```
