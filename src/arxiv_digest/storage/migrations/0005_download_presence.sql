ALTER TABLE download_files
ADD COLUMN is_present INTEGER NOT NULL DEFAULT 1
    CHECK (is_present IN (0, 1));
