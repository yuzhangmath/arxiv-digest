CREATE TABLE application_generation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL CHECK (generation > 0)
) STRICT;
INSERT INTO application_generation(singleton, generation) VALUES (1, 2);

DROP TABLE review_date_state;
DROP TABLE event_evidence;
DROP TABLE enrichment_days;
DROP TABLE review_events;

ALTER TABLE state_meta
ADD COLUMN projection_revision INTEGER NOT NULL DEFAULT 0
    CHECK (projection_revision >= 0);

CREATE TABLE source_observations (
    observation_id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('atom', 'catchup', 'oai')),
    category TEXT,
    announce_type TEXT CHECK (
        announce_type IS NULL OR
        announce_type IN ('new', 'cross', 'replace', 'replace-cross')
    ),
    daily_list_date TEXT,
    announced_version INTEGER CHECK (
        announced_version IS NULL OR announced_version > 0
    ),
    list_position INTEGER CHECK (list_position IS NULL OR list_position >= 0),
    oai_datestamp TEXT,
    response_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE catchup_days (
    category TEXT NOT NULL,
    daily_list_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'complete', 'empty', 'failed')
    ),
    attempted_at TEXT,
    response_sha256 TEXT,
    error_code TEXT,
    PRIMARY KEY (category, daily_list_date)
) STRICT;

CREATE TABLE canonical_events (
    event_id INTEGER PRIMARY KEY,
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    daily_list_date TEXT NOT NULL,
    announced_version INTEGER CHECK (
        announced_version IS NULL OR announced_version > 0
    ),
    version_resolution TEXT NOT NULL CHECK (
        version_resolution IN (
            'atom_confirmed', 'chronology_matched', 'unconfirmed'
        )
    ),
    queue_revision INTEGER NOT NULL CHECK (queue_revision > 0),
    reviewed_at TEXT,
    recovered_after_finish INTEGER NOT NULL DEFAULT 0 CHECK (
        recovered_after_finish IN (0, 1)
    ),
    conflict_code TEXT,
    UNIQUE (arxiv_id, daily_list_date),
    FOREIGN KEY (arxiv_id, announced_version)
        REFERENCES article_versions(arxiv_id, version)
) STRICT;

CREATE TABLE canonical_event_observations (
    event_id INTEGER NOT NULL
        REFERENCES canonical_events(event_id) ON DELETE CASCADE,
    observation_id INTEGER NOT NULL
        REFERENCES source_observations(observation_id) ON DELETE RESTRICT,
    PRIMARY KEY (event_id, observation_id)
) STRICT;

CREATE TABLE review_date_state (
    daily_list_date TEXT PRIMARY KEY,
    anchor_event_id INTEGER REFERENCES canonical_events(event_id),
    profile_revision INTEGER NOT NULL DEFAULT 0 CHECK (profile_revision >= 0),
    last_finished_at TEXT,
    last_finished_revision INTEGER CHECK (
        last_finished_revision IS NULL OR last_finished_revision >= 0
    )
) STRICT;

CREATE TABLE reconciliation_diagnostics (
    diagnostic_code TEXT PRIMARY KEY CHECK (
        diagnostic_code IN ('version_evidence_conflict')
    ),
    occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK (occurrence_count >= 0),
    last_observed_at TEXT
) STRICT;
