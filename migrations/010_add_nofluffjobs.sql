-- Poland requires salary disclosure in job adverts, so this source states a
-- figure on effectively every posting -- the best coverage available anywhere,
-- against roughly 4% on the boards this project started with. Warsaw-based
-- remote and hybrid roles also need no relocation.
INSERT IGNORE INTO sources (name, enabled, fetch_mode, priority, base_url) VALUES
  ('nofluffjobs', TRUE, 'http-json', 12, 'https://nofluffjobs.com');
