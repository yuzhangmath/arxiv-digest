CREATE TABLE download_files (
    arxiv_id TEXT NOT NULL REFERENCES articles(arxiv_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    filename TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count > 0),
    sha256 TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    PRIMARY KEY (arxiv_id, version),
    FOREIGN KEY (arxiv_id, version)
        REFERENCES article_versions(arxiv_id, version) ON DELETE CASCADE,
    UNIQUE (filename),
    CHECK (
        filename NOT LIKE '%/%' AND
        filename NOT LIKE '%\%' AND
        filename NOT IN ('.', '..')
    )
) STRICT;
