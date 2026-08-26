-- Jobicy states a salary on roughly three quarters of its listings, the best
-- coverage of any source here, and publishes it as structured min/max with a
-- currency and a period rather than as free text. It spans USD, EUR, GBP and
-- CAD, which is what the owner asked for: remote roles outside the PL market.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('jobicy', TRUE, 'http-json', 25, 'https://jobicy.com');
