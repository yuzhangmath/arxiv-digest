PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE state_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    queue_revision INTEGER NOT NULL DEFAULT 0
) STRICT;
INSERT INTO state_meta(singleton, queue_revision) VALUES (1, 0);

CREATE TABLE articles (
    arxiv_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    primary_category TEXT,
    comments TEXT NOT NULL DEFAULT '',
    journal_ref TEXT NOT NULL DEFAULT '',
    doi TEXT,
    metadata_hash TEXT NOT NULL,
    last_oai_datestamp TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    deleted_at TEXT
) STRICT;

CREATE TABLE oai_tombstones (
    oai_identifier TEXT PRIMARY KEY,
    arxiv_id TEXT,
    oai_datestamp TEXT NOT NULL,
    set_specs_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE article_versions (
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    submitted_at TEXT NOT NULL,
    size TEXT,
    source_type TEXT,
    PRIMARY KEY (arxiv_id, version)
) STRICT;

CREATE TABLE article_authors (
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    PRIMARY KEY (arxiv_id, position)
) STRICT;

CREATE TABLE article_categories (
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (arxiv_id, category)
) STRICT;

CREATE TABLE category_sync_state (
    category TEXT PRIMARY KEY,
    set_spec TEXT NOT NULL,
    coverage_start TEXT NOT NULL,
    completed_through_utc TEXT,
    pending_backfill_start TEXT,
    pending_backfill_until TEXT,
    status TEXT NOT NULL DEFAULT 'idle' CHECK (
        status IN ('idle', 'syncing', 'failed')
    ),
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    CHECK (
        (pending_backfill_start IS NULL AND pending_backfill_until IS NULL) OR
        (
            pending_backfill_start IS NOT NULL AND
            pending_backfill_until IS NOT NULL AND
            pending_backfill_start <= pending_backfill_until
        )
    )
) STRICT;

CREATE TABLE sync_runs (
    run_id INTEGER PRIMARY KEY,
    category TEXT NOT NULL REFERENCES category_sync_state(category),
    run_kind TEXT NOT NULL CHECK (
        run_kind IN ('incremental', 'coverage_backfill')
    ),
    requested_from TEXT NOT NULL,
    requested_until TEXT,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'failed', 'interrupted')
    ),
    pages_applied INTEGER NOT NULL DEFAULT 0,
    records_applied INTEGER NOT NULL DEFAULT 0,
    final_response_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    CHECK (
        (run_kind = 'incremental' AND requested_until IS NULL) OR
        (run_kind = 'coverage_backfill' AND requested_until IS NOT NULL)
    )
) STRICT;

CREATE TABLE review_events (
    event_id INTEGER PRIMARY KEY,
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id),
    announced_version INTEGER CHECK (
        announced_version IS NULL OR announced_version > 0
    ),
    effective_date TEXT NOT NULL,
    date_basis TEXT NOT NULL CHECK (
        date_basis IN ('feed_mailing', 'catchup_mailing', 'version_history_utc')
    ),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('current', 'recovered', 'inferred')
    ),
    queue_revision INTEGER NOT NULL CHECK (queue_revision > 0),
    reviewed_at TEXT,
    FOREIGN KEY (arxiv_id, announced_version)
        REFERENCES article_versions(arxiv_id, version)
) STRICT;

CREATE UNIQUE INDEX review_event_identity
ON review_events(arxiv_id, COALESCE(announced_version, 0), effective_date);

CREATE TABLE event_evidence (
    evidence_id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES review_events(event_id) ON DELETE CASCADE,
    source_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK (source IN ('atom', 'catchup', 'oai')),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('current', 'recovered', 'inferred')
    ),
    category TEXT NOT NULL,
    announce_type TEXT CHECK (
        announce_type IS NULL OR
        announce_type IN ('new', 'cross', 'replace', 'replace-cross')
    ),
    mailing_date TEXT,
    announced_version INTEGER CHECK (
        announced_version IS NULL OR announced_version > 0
    ),
    list_position INTEGER CHECK (
        list_position IS NULL OR list_position >= 0
    ),
    oai_datestamp TEXT,
    raw_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    CHECK (
        (source = 'atom' AND confidence = 'current') OR
        (source = 'catchup' AND confidence = 'recovered') OR
        (source = 'oai' AND confidence = 'inferred')
    )
) STRICT;

CREATE TABLE review_date_state (
    effective_date TEXT PRIMARY KEY,
    anchor_event_id INTEGER REFERENCES review_events(event_id),
    profile_revision INTEGER NOT NULL DEFAULT 0,
    last_finished_at TEXT,
    last_finished_revision INTEGER CHECK (
        last_finished_revision IS NULL OR last_finished_revision >= 0
    )
) STRICT;

CREATE TABLE enrichment_days (
    category TEXT NOT NULL,
    mailing_date TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'empty', 'failed')),
    fetched_at TEXT NOT NULL,
    raw_sha256 TEXT,
    error_code TEXT,
    error_message TEXT,
    PRIMARY KEY (category, mailing_date, source)
) STRICT;

CREATE TABLE saved_papers (
    arxiv_id TEXT PRIMARY KEY REFERENCES articles(arxiv_id),
    saved_version INTEGER CHECK (
        saved_version IS NULL OR saved_version > 0
    ),
    FOREIGN KEY (arxiv_id, saved_version)
        REFERENCES article_versions(arxiv_id, version)
) STRICT;

CREATE VIRTUAL TABLE papers_fts USING fts5(
    arxiv_id, title, authors, abstract
);

CREATE TABLE category_article_state (
    category TEXT NOT NULL,
    arxiv_id TEXT NOT NULL,
    last_oai_datestamp TEXT NOT NULL,
    category_set_hash TEXT NOT NULL,
    observed_categories_json TEXT NOT NULL,
    last_raw_sha256 TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (category, arxiv_id)
) STRICT;

CREATE TRIGGER articles_fts_insert AFTER INSERT ON articles BEGIN
    INSERT INTO papers_fts(rowid, arxiv_id, title, authors, abstract)
    VALUES (new.rowid, new.arxiv_id, new.title, '', new.abstract);
END;

CREATE TRIGGER articles_fts_update AFTER UPDATE OF arxiv_id, title, abstract
ON articles BEGIN
    UPDATE papers_fts
    SET arxiv_id = new.arxiv_id,
        title = new.title,
        authors = COALESCE((
            SELECT group_concat(name, ' ')
            FROM (
                SELECT name FROM article_authors
                WHERE arxiv_id = new.arxiv_id ORDER BY position
            )
        ), ''),
        abstract = new.abstract
    WHERE rowid = old.rowid;
END;

CREATE TRIGGER articles_fts_delete AFTER DELETE ON articles BEGIN
    DELETE FROM papers_fts WHERE rowid = old.rowid;
END;

CREATE TRIGGER article_authors_fts_insert AFTER INSERT ON article_authors BEGIN
    UPDATE papers_fts
    SET authors = COALESCE((
        SELECT group_concat(name, ' ')
        FROM (
            SELECT name FROM article_authors
            WHERE arxiv_id = new.arxiv_id ORDER BY position
        )
    ), '')
    WHERE rowid = (SELECT rowid FROM articles WHERE arxiv_id = new.arxiv_id);
END;

CREATE TRIGGER article_authors_fts_update AFTER UPDATE ON article_authors BEGIN
    UPDATE papers_fts
    SET authors = COALESCE((
        SELECT group_concat(name, ' ')
        FROM (
            SELECT name FROM article_authors
            WHERE arxiv_id = new.arxiv_id ORDER BY position
        )
    ), '')
    WHERE rowid = (SELECT rowid FROM articles WHERE arxiv_id = new.arxiv_id);
END;

CREATE TRIGGER article_authors_fts_delete AFTER DELETE ON article_authors BEGIN
    UPDATE papers_fts
    SET authors = COALESCE((
        SELECT group_concat(name, ' ')
        FROM (
            SELECT name FROM article_authors
            WHERE arxiv_id = old.arxiv_id ORDER BY position
        )
    ), '')
    WHERE rowid = (SELECT rowid FROM articles WHERE arxiv_id = old.arxiv_id);
END;
