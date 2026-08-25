CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) NOT NULL PRIMARY KEY,
  applied_at DATETIME     NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sources (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  name              VARCHAR(64)  NOT NULL UNIQUE,
  enabled           BOOLEAN      NOT NULL DEFAULT TRUE,
  fetch_mode        ENUM('http-json','http-html','browser') NOT NULL,
  priority          TINYINT      NOT NULL DEFAULT 50,
  base_url          VARCHAR(512) NOT NULL,
  query             JSON         NULL,
  selectors         JSON         NULL,
  status            ENUM('ok','degraded') NOT NULL DEFAULT 'ok',
  consecutive_empty INT          NOT NULL DEFAULT 0,
  last_ok_at        DATETIME     NULL,
  last_run_at       DATETIME     NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS runs (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  kind        ENUM('harvest','score') NOT NULL,
  source_id   INT      NULL,
  started_at  DATETIME NOT NULL,
  finished_at DATETIME NULL,
  fetched     INT      NULL,
  new         INT      NULL,
  error       TEXT     NULL,
  CONSTRAINT fk_runs_source FOREIGN KEY (source_id) REFERENCES sources(id),
  INDEX idx_runs_kind_started (kind, started_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS raw_fetches (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  source_id     INT          NOT NULL,
  run_id        INT          NOT NULL,
  path          VARCHAR(512) NOT NULL,
  content_hash  CHAR(64)     NOT NULL,
  http_status   SMALLINT     NOT NULL,
  etag          VARCHAR(255) NULL,
  last_modified VARCHAR(255) NULL,
  fetched_at    DATETIME     NOT NULL,
  CONSTRAINT fk_raw_source FOREIGN KEY (source_id) REFERENCES sources(id),
  CONSTRAINT fk_raw_run    FOREIGN KEY (run_id)    REFERENCES runs(id),
  INDEX idx_raw_source_fetched (source_id, fetched_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS jobs (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  fingerprint        CHAR(64)     NOT NULL,
  title              VARCHAR(255) NOT NULL,
  company            VARCHAR(255) NOT NULL,
  location           VARCHAR(255) NULL,
  arrangement        ENUM('remote','hybrid','onsite','unknown') NOT NULL DEFAULT 'unknown',
  employment_type    ENUM('full-time','part-time','contract','internship','unknown') NOT NULL DEFAULT 'unknown',
  salary_min         INT          NULL,
  salary_max         INT          NULL,
  salary_currency    CHAR(3)      NULL,
  salary_period      ENUM('hour','day','month','year') NULL,
  salary_type        ENUM('employee','contractor','unknown') NULL,
  salary_source      ENUM('posting','inferred','absent') NOT NULL DEFAULT 'absent',
  salary_monthly_eur INT          NULL,
  first_seen_at      DATETIME     NOT NULL,
  last_seen_at       DATETIME     NOT NULL,
  inactive_at        DATETIME     NULL,
  filtered_at        DATETIME     NULL,
  filter_reason      VARCHAR(255) NULL,
  latest_score_id    INT          NULL,
  INDEX idx_jobs_fingerprint (fingerprint),
  INDEX idx_jobs_latest_score (latest_score_id),
  INDEX idx_jobs_triage (inactive_at, filtered_at, last_seen_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS job_sources (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  job_id           INT          NOT NULL,
  source_id        INT          NOT NULL,
  external_id      VARCHAR(255) NOT NULL,
  url              VARCHAR(1024) NOT NULL,
  raw_fetch_id     INT          NULL,
  description      MEDIUMTEXT   NULL,
  description_hash CHAR(64)     NULL,
  salary_raw       VARCHAR(255) NULL,
  posted_at        DATETIME     NULL,
  first_seen_at    DATETIME     NOT NULL,
  last_seen_at     DATETIME     NOT NULL,
  inactive_at      DATETIME     NULL,
  missed_runs      INT          NOT NULL DEFAULT 0,
  source_meta      JSON         NULL,
  UNIQUE KEY uq_source_external (source_id, external_id),
  CONSTRAINT fk_js_job    FOREIGN KEY (job_id)       REFERENCES jobs(id),
  CONSTRAINT fk_js_source FOREIGN KEY (source_id)    REFERENCES sources(id),
  CONSTRAINT fk_js_raw    FOREIGN KEY (raw_fetch_id) REFERENCES raw_fetches(id),
  INDEX idx_js_job (job_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS scores (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  job_id          INT      NOT NULL,
  run_id          INT      NOT NULL,
  `pass`          TINYINT  NOT NULL,
  score           TINYINT  NOT NULL,
  dimensions      JSON     NULL,
  red_flag_penalty TINYINT NOT NULL DEFAULT 0,
  hard_concerns   JSON     NULL,
  strengths       JSON     NULL,
  weaknesses      JSON     NULL,
  verdict         TEXT     NULL,
  profile_version INT      NOT NULL,
  profile_hash    CHAR(64) NOT NULL,
  scored_at       DATETIME NOT NULL,
  CONSTRAINT fk_scores_job FOREIGN KEY (job_id) REFERENCES jobs(id),
  CONSTRAINT fk_scores_run FOREIGN KEY (run_id) REFERENCES runs(id),
  INDEX idx_scores_job_pass (job_id, `pass`, profile_hash)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS notifications (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  job_id              INT         NOT NULL,
  channel             VARCHAR(32) NOT NULL,
  chat_id             VARCHAR(64) NOT NULL,
  telegram_message_id BIGINT      NULL,
  score_id            INT         NULL,
  queued_at           DATETIME    NOT NULL,
  sent_at             DATETIME    NULL,
  UNIQUE KEY uq_job_channel (job_id, channel),
  CONSTRAINT fk_notif_job   FOREIGN KEY (job_id)   REFERENCES jobs(id),
  CONSTRAINT fk_notif_score FOREIGN KEY (score_id) REFERENCES scores(id),
  INDEX idx_notif_message (telegram_message_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS applications (
  job_id     INT NOT NULL PRIMARY KEY,
  status     ENUM('interested','skipped','applied','replied','rejected','interviewing','offer') NOT NULL,
  note       TEXT     NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT fk_app_job FOREIGN KEY (job_id) REFERENCES jobs(id)
) ENGINE=InnoDB;

INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('djinni',         TRUE, 'http-html', 10, 'https://djinni.co'),
  ('dou',            TRUE, 'http-html', 20, 'https://jobs.dou.ua'),
  ('remoteok',       TRUE, 'http-json', 30, 'https://remoteok.com'),
  ('weworkremotely', TRUE, 'http-json', 40, 'https://weworkremotely.com'),
  ('linkedin',       TRUE, 'browser',    5, 'https://www.linkedin.com');
