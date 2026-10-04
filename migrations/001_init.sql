-- Regulatory agent schema. Idempotent: safe to run on every boot.

CREATE TABLE IF NOT EXISTS requests (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      text NOT NULL UNIQUE,            -- inbound Message-ID: the dedup key
    track_token     text NOT NULL UNIQUE,            -- unguessable id for the public progress page
    imap_uid        bigint,
    imap_uidvalidity bigint,
    raw_sha256      text NOT NULL,                   -- raw MIME stored under data/raw/<sha>
    from_addr       text NOT NULL,
    subject         text NOT NULL DEFAULT '',
    thread_root     text,                            -- first Message-ID of the thread
    state           text NOT NULL DEFAULT 'received'
                    CHECK (state IN ('received','rejected','clarify','accepted','fetching',
                                     'packaging','replying','done','failed')),
    reject_reason   text,
    auth            jsonb,                           -- SenderAuth
    parsed          jsonb,                           -- ParsedRequest
    provider        text,
    matter          text,
    doc_type        text,
    progress        jsonb NOT NULL DEFAULT '{}',     -- {step, done, total} for the progress page
    result          jsonb,                           -- summary, counts, links, citations ids
    error           text,
    attempts        int NOT NULL DEFAULT 0,
    ack_message_id  text,                            -- outbound ids are derived from request id
    reply_message_id text,
    ack_sent_at     timestamptz,
    reply_sent_at   timestamptz,
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS requests_state_idx ON requests (state, updated_at);
CREATE INDEX IF NOT EXISTS requests_from_idx ON requests (from_addr, received_at DESC);
CREATE INDEX IF NOT EXISTS requests_thread_idx ON requests (thread_root);

CREATE TABLE IF NOT EXISTS matters (
    provider        text NOT NULL,
    matter          text NOT NULL,
    info            jsonb NOT NULL,                  -- MatterInfo
    listings        jsonb NOT NULL DEFAULT '{}',     -- {doc_type: [external_id, ...]} newest first
    fetched_at      timestamptz NOT NULL,
    PRIMARY KEY (provider, matter)
);

CREATE TABLE IF NOT EXISTS documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        text NOT NULL,
    matter          text NOT NULL,
    doc_type        text NOT NULL,
    external_id     text NOT NULL,                   -- regulator's file id
    title           text NOT NULL,
    filed_on        date,
    sha256          text,                            -- null until downloaded
    size_bytes      bigint,
    filename        text,
    page_count      int,
    listed_at       timestamptz NOT NULL DEFAULT now(),
    downloaded_at   timestamptz,
    UNIQUE (provider, external_id)
);
CREATE INDEX IF NOT EXISTS documents_matter_idx ON documents (provider, matter, doc_type);

-- Text per page, extracted once per content hash (identical PDFs in two matters share rows).
CREATE TABLE IF NOT EXISTS pages (
    sha256          text NOT NULL,
    page            int NOT NULL,                    -- 1-based
    text            text NOT NULL,
    PRIMARY KEY (sha256, page)
);

CREATE TABLE IF NOT EXISTS citations (
    id              text PRIMARY KEY,                -- short unguessable id used in links
    request_id      uuid REFERENCES requests(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id),
    sha256          text,                            -- the file version the offsets refer to
    page            int NOT NULL,
    quote           text NOT NULL,                   -- verbatim span from pages.text
    char_start      int NOT NULL,
    char_end        int NOT NULL,
    claim           text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit trail of every state change and outbound email.
CREATE TABLE IF NOT EXISTS events (
    id              bigserial PRIMARY KEY,
    request_id      uuid REFERENCES requests(id) ON DELETE CASCADE,
    kind            text NOT NULL,
    data            jsonb,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_request_idx ON events (request_id, id);
