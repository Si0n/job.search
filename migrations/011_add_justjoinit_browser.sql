-- justjoin.it answers 503 to a plain HTTP client but serves normally in a real
-- browser, so it is collected through the owner's own Chrome session by
-- /harvest-browser rather than by an adapter. Poland requires salary
-- disclosure, which is why it is worth the manual step.
--
-- `query.search_url` is where the skill navigates. `query.notes` is shown to
-- the operator before collection. Browser sources carry no selectors: the
-- agent reads the rendered page instead of matching markup.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('justjoinit', TRUE, 'browser', 18, 'https://justjoin.it');
