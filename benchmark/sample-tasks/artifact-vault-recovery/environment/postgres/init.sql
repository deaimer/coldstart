CREATE TABLE schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    status text NOT NULL CHECK (status IN ('pending', 'ready', 'error')),
    legacy_path text,
    object_key text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE upload_requests (
    idempotency_key text PRIMARY KEY,
    artifact_id uuid NOT NULL REFERENCES artifacts(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations(version) VALUES (1), (2);

-- Version 2 added object_key, but the historical backfill was interrupted even
-- though the migration ledger was marked complete.
INSERT INTO artifacts(id, name, sha256, size_bytes, status, legacy_path, object_key)
VALUES
    ('11111111-1111-4111-8111-111111111111', 'compiler-linux-amd64', '578a54725b43ba85b67852a03c56a5f2acc2d7d707492a1162d7228f9b6b00a8', 46, 'ready', 'legacy/compiler-linux-amd64.bin', NULL),
    ('22222222-2222-4222-8222-222222222222', 'release-manifest-2026-08', 'e43d2d05548643a55a2023ba830ed34abeabf56cae369f510dac5bdb8cd50c5e', 48, 'ready', 'legacy/release-manifest-2026-08.txt', NULL);
