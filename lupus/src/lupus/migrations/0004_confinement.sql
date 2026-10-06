-- Schema version 4: explicit consent for drivers that cannot be kept inside the project.
-- Some CLIs let a worker READ any file the user can (measured by `lupus probe --live` as
-- read_confinement = unsupported). Such a driver may work on a project only after the user said
-- so for that project.
ALTER TABLE project ADD COLUMN allow_unconfined_reads INTEGER NOT NULL DEFAULT 0;
