-- Application drafts: written on demand for one job, never in bulk.
-- profile_hash mirrors the contract on `scores`: editing profile.yaml marks a
-- draft stale rather than silently leaving text written against an older idea
-- of what the owner wants.
CREATE TABLE IF NOT EXISTS drafts (
  job_id       INT NOT NULL PRIMARY KEY,
  cover_letter TEXT     NULL,
  email        TEXT     NULL,
  why_fit      TEXT     NULL,
  profile_hash CHAR(64) NOT NULL,
  created_at   DATETIME NOT NULL,
  updated_at   DATETIME NOT NULL,
  CONSTRAINT fk_drafts_job FOREIGN KEY (job_id) REFERENCES jobs(id)
) ENGINE=InnoDB;
