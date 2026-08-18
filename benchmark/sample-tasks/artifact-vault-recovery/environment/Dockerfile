FROM public.ecr.aws/docker/library/golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    asciinema \
    ca-certificates \
    curl \
    jq \
    postgresql-client \
    python3 \
    supervisor \
    tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/

RUN mkdir -p \
      /app/bin \
      /var/lib/artifact-vault/blobs/legacy \
      /var/lib/artifact-vault/staging \
      /srv/artifact-vault/blobs \
      /logs/artifacts \
    && cp -a /app/seed/blobs/. /var/lib/artifact-vault/blobs/ \
    && go build -o /app/bin/artifact-vault ./cmd/vault \
    && chmod +x /app/scripts/*.sh \
    && ln -s /app/scripts/vaultctl.sh /usr/local/bin/vaultctl

ENV PGHOST=postgres \
    PGPORT=5432 \
    PGDATABASE=vault \
    PGUSER=vault \
    PGPASSWORD=vault \
    VAULT_API_CONFIG=/app/config/api.json \
    VAULT_WORKER_CONFIG=/app/config/worker.json

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["sleep", "infinity"]
