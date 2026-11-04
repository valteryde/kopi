FROM debian:stable-slim

# s6-overlay 3.2.3.2. The noarch tarball and the arch tarball are pinned by
# sha256 below. A shell script is not PID 1.
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        python3 \
        tar \
        xz-utils \
        zstd \
        age \
        procps \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' \
        "$VERSION_CODENAME" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client-17 \
        postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu \
    && case "$TARGETARCH" in \
        amd64) arch=x86_64; hash=e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 ;; \
        arm64) arch=aarch64; hash=b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/s6-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
    && curl -fsSL -o /tmp/s6-arch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${arch}.tar.xz" \
    && echo "5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12  /tmp/s6-noarch.tar.xz" | sha256sum -c - \
    && echo "${hash}  /tmp/s6-arch.tar.xz" | sha256sum -c - \
    && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-arch.tar.xz \
    && rm /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz

RUN groupadd --gid 10001 kopi \
    && useradd --uid 10001 --gid 10001 --home-dir /home/kopi --create-home --shell /bin/bash kopi

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

WORKDIR /opt/kopi
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python python3
COPY manage.py ./
COPY kopi ./kopi

COPY image/s6/prepare /etc/s6-overlay/s6-rc.d/prepare
COPY image/s6/migrate /etc/s6-overlay/s6-rc.d/migrate
COPY image/s6/web /etc/s6-overlay/s6-rc.d/web
COPY image/s6/worker /etc/s6-overlay/s6-rc.d/worker
RUN chmod 755 \
        /etc/s6-overlay/s6-rc.d/prepare/up \
        /etc/s6-overlay/s6-rc.d/migrate/up \
        /etc/s6-overlay/s6-rc.d/web/run \
        /etc/s6-overlay/s6-rc.d/worker/run \
    && touch \
        /etc/s6-overlay/user-bundles.d/user/contents.d/prepare \
        /etc/s6-overlay/user-bundles.d/user/contents.d/migrate \
        /etc/s6-overlay/user-bundles.d/user/contents.d/web \
        /etc/s6-overlay/user-bundles.d/user/contents.d/worker

ENV LANG=C.UTF-8 \
    HOME=/home/kopi \
    KOPI_DATA_DIR=/data \
    KOPI_WORK_DIR=/work \
    TMPDIR=/work/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/kopi \
    UV_PYTHON_DOWNLOADS=never \
    S6_KEEP_ENV=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_KILL_GRACETIME=5000 \
    PATH="/usr/lib/postgresql/18/bin:/usr/lib/postgresql/17/bin:/opt/kopi/.venv/bin:/command:/usr/local/bin:/usr/bin:/bin"

EXPOSE 8443
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD curl --fail --silent --show-error --output /dev/null --cacert /cert/tls.crt https://127.0.0.1:8443/
ENTRYPOINT ["/init"]
