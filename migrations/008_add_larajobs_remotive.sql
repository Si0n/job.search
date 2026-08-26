-- Two sources added for salary coverage. The existing four state a figure on
-- roughly 4% of postings, which leaves the owner's salary floor with almost
-- nothing to act on. LaraJobs publishes one on about half its listings and is
-- Laravel-specific, Remotive on about two thirds of its software-dev listings.
--
-- Keep tests/fixtures/<source>/selectors.json in step with these rows.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('larajobs', TRUE, 'http-json', 15, 'https://larajobs.com'),
  ('remotive', TRUE, 'http-json', 35, 'https://remotive.com');
