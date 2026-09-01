-- The old `applications` table has always been triage: one row per job, no
-- history, and the inbox is defined by the absence of a row. The tracker needs
-- that name for the thing that actually tracks applications, so triage takes
-- the name it means. Guarded rather than a bare RENAME because db.migrate
-- re-runs a whole file after a partial failure.
SET @sql := IF(
  (SELECT COUNT(*) FROM information_schema.tables
    WHERE table_schema = DATABASE() AND table_name = 'triage') = 0,
  'RENAME TABLE applications TO triage', 'DO 0')
;
PREPARE stmt FROM @sql
;
EXECUTE stmt
;
DEALLOCATE PREPARE stmt
;

CREATE TABLE IF NOT EXISTS stages (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  slug       VARCHAR(64) NOT NULL UNIQUE,
  label      VARCHAR(64) NOT NULL,
  weight     SMALLINT    NOT NULL,
  kind       ENUM('active','won','lost') NOT NULL DEFAULT 'active',
  builtin    BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at DATETIME    NOT NULL,
  INDEX idx_stages_sort (kind, weight)
) ENGINE=InnoDB
;

INSERT IGNORE INTO stages (slug, label, weight, kind, builtin, created_at) VALUES
  ('applied',          'Applied',                     10, 'active', TRUE, NOW()),
  ('recruiter_screen', 'Recruiter screen',            20, 'active', TRUE, NOW()),
  ('waiting_feedback', 'Waiting for feedback',        25, 'active', TRUE, NOW()),
  ('test_task',        'Test task / take-home',       30, 'active', TRUE, NOW()),
  ('tech_interview',   'Technical interview',         40, 'active', TRUE, NOW()),
  ('team_interview',   'Team interview',              50, 'active', TRUE, NOW()),
  ('cto_interview',    'Interview with CTO / founder',60, 'active', TRUE, NOW()),
  ('final_interview',  'Final / culture interview',   70, 'active', TRUE, NOW()),
  ('reference_check',  'Reference check',             80, 'active', TRUE, NOW()),
  ('offer',            'Offer',                       90, 'won',    TRUE, NOW()),
  ('accepted',         'Accepted',                   100, 'won',    TRUE, NOW()),
  ('declined',         'Declined by them',             0, 'lost',   TRUE, NOW()),
  ('withdrawn',        'Withdrawn by me',              0, 'lost',   TRUE, NOW()),
  ('ghosted',          'Ghosted / no answer',          0, 'lost',   TRUE, NOW())
;

CREATE TABLE IF NOT EXISTS cv_files (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  sha256       CHAR(64)     NOT NULL UNIQUE,
  filename     VARCHAR(255) NOT NULL,
  content_type VARCHAR(100) NOT NULL,
  size_bytes   INT          NOT NULL,
  path         VARCHAR(512) NOT NULL,
  uploaded_at  DATETIME     NOT NULL
) ENGINE=InnoDB
;

CREATE TABLE IF NOT EXISTS applications (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  job_id             INT NOT NULL,
  applied_at         DATETIME NOT NULL,
  stage_id           INT NOT NULL,
  stage_at           DATETIME NOT NULL,
  next_action        VARCHAR(255) NULL,
  next_action_at     DATETIME NULL,
  cv_file_id         INT NULL,
  cover_letter       TEXT NULL,
  why_company        TEXT NULL,
  salary_expectation VARCHAR(120) NULL,
  notice_period      VARCHAR(120) NULL,
  answers            JSON NULL,
  created_at         DATETIME NOT NULL,
  updated_at         DATETIME NOT NULL,
  UNIQUE KEY uq_application_job (job_id),
  CONSTRAINT fk_application_job   FOREIGN KEY (job_id)     REFERENCES jobs(id),
  CONSTRAINT fk_application_stage FOREIGN KEY (stage_id)   REFERENCES stages(id),
  CONSTRAINT fk_application_cv    FOREIGN KEY (cv_file_id) REFERENCES cv_files(id),
  INDEX idx_application_next (next_action_at)
) ENGINE=InnoDB
;

CREATE TABLE IF NOT EXISTS application_events (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  application_id INT NOT NULL,
  kind           ENUM('applied','stage','note') NOT NULL,
  stage_id       INT NULL,
  occurred_at    DATETIME NOT NULL,
  note           TEXT NULL,
  next_action    VARCHAR(255) NULL,
  next_action_at DATETIME NULL,
  created_at     DATETIME NOT NULL,
  CONSTRAINT fk_event_application FOREIGN KEY (application_id) REFERENCES applications(id),
  CONSTRAINT fk_event_stage       FOREIGN KEY (stage_id)       REFERENCES stages(id),
  INDEX idx_event_app (application_id, occurred_at)
) ENGINE=InnoDB
;

-- enabled = FALSE is load-bearing twice over: harvest never tries to crawl a
-- source with no listing page, and sweep's two UPDATEs both require
-- s.enabled = TRUE, so a posting the owner has applied to is never aged out
-- when the company takes the ad down. Priority 90 keeps a real board's title
-- and company winning over a hand-pasted page when the two merge.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('manual', FALSE, 'http-html', 90, 'https://example.invalid')
;
