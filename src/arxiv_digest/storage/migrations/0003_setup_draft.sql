CREATE TABLE setup_draft (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    current_step TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE profile_publication (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    pending_revision INTEGER,
    pending_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('none', 'pending', 'published'))
) STRICT;
INSERT INTO profile_publication(singleton, status) VALUES (1, 'none');

CREATE TABLE application_settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    launcher_operation TEXT NOT NULL DEFAULT 'none' CHECK (
        launcher_operation IN ('none', 'create_pending', 'create_failed')
    ),
    launcher_last_error_code TEXT
) STRICT;
INSERT INTO application_settings(singleton) VALUES (1);
