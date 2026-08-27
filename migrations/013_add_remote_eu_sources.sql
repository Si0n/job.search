-- Three keyword-searchable remote boards covering the EU and US markets.
--
-- What separates these from the aggregators already here is that their feeds
-- answer a keyword: `search_keywords=php` and `q=php` return PHP roles rather
-- than a catalogue to sift, so a run adds a handful of relevant postings
-- instead of fifty to triage. Volume is low by design, and precision is the point.
--
-- Priority orders which source's description becomes canonical when the same
-- job arrives from several boards. euremotejobs sits high because it carries
-- the employer's full posting body, including for roles that reach the other
-- sources as a stub.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('euremotejobs', TRUE, 'http-json', 15, 'https://euremotejobs.com'),
  ('landingjobs',  TRUE, 'http-json', 18, 'https://landing.jobs'),
  ('jobspresso',   TRUE, 'http-json', 35, 'https://jobspresso.co');

-- WordPress Job Manager boards. rss_to_items flattens the `job_listing:`
-- namespace, so the prefixed fields are addressable as plain keys.
UPDATE sources SET
  query = '{"search_keywords": "php"}',
  selectors = '{"root": "", "skip_first": false, "fields": {"external_id": "guid", "url": "link", "title": "title", "company": "company", "location": "location", "salary_raw": "salary", "description": "encoded", "employment_hint": "job_type"}}'
WHERE name = 'euremotejobs';

-- Same shape, minus employment_hint: Jobspresso files roles under marketing
-- categories regardless of the work (its first PHP backend listing is typed
-- "Marketing"), so job_type travels in meta instead of reaching the employment
-- filter as a claim the posting never made.
UPDATE sources SET
  query = '{"search_keywords": "php"}',
  selectors = '{"root": "", "skip_first": false, "fields": {"external_id": "guid", "url": "link", "title": "title", "company": "company", "location": "location", "description": "encoded", "job_type": "job_type"}}'
WHERE name = 'jobspresso';

-- landing.jobs exposes neither a company field nor a salary string. The adapter
-- assembles both onto each record during fetch, which is why plain keys appear
-- here for fields the raw API does not publish.
UPDATE sources SET
  query = '{"q": "php", "limit": 50}',
  selectors = '{"root": "", "skip_first": false, "fields": {"external_id": "id", "url": "url", "title": "title", "company": "company", "location": "location", "salary_raw": "salary_raw", "description": "description", "arrangement_hint": "arrangement_hint", "employment_hint": "type"}}'
WHERE name = 'landingjobs';
