-- Refocus the source list on remote roles in the US, EU and Ukraine.
--
-- Two sources are switched off rather than deleted, so their harvested history
-- and the jobs they already contributed stay intact:
--
--   nofluffjobs    Polish-market board. Two live postings, none scored above 7.
--                  Its listings are Poland-domestic roles that expect Polish in
--                  the workplace, which is the market being stepped away from.
--   weworkremotely 25 live postings and not one above 7 across the whole run
--                  history -- the worst ratio of any enabled source. Its feed is
--                  a category dump with no keyword filter, so it costs a fetch
--                  and a page of triage to return nothing.
--
-- justjoin.it stays enabled deliberately: it is a browser source that costs
-- nothing unless the owner runs /harvest-browser, and it has surfaced English
-- remote roles alongside the Polish-language ones.
UPDATE sources SET enabled = FALSE WHERE name IN ('nofluffjobs', 'weworkremotely');

-- The monthly "Ask HN: Who is hiring?" thread. Postings here appear on no job
-- board, skew US and EU remote, and routinely state a salary in the body.
--
-- The keyword list leans on the domain rather than the language on purpose.
-- Measured against the August 2026 thread: 240 comments, of which exactly one
-- mentions PHP, fifteen mention payments or fintech, and sixty-nine mention
-- backend work of any kind. Keying on PHP alone would make the source almost
-- silent, and keying on "backend" would flood pass 1 with Python and Rust.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('hnwhoishiring', TRUE, 'http-json', 22, 'https://news.ycombinator.com');

UPDATE sources SET
  query = '{"keywords": ["PHP", "Laravel", "Symfony", "payments", "fintech"]}',
  selectors = '{"root": "", "skip_first": false, "fields": {"external_id": "external_id", "url": "url", "title": "title", "company": "company", "location": "location", "description": "description", "arrangement_hint": "arrangement_hint", "posted_by": "posted_by"}}'
WHERE name = 'hnwhoishiring';
