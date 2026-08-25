-- Seeds the production `query` and `selectors` JSON for the four HTTP
-- sources. Before this migration, migrations/001_init.sql left both columns
-- NULL, so `git clone && jobsearch init-db && jobsearch harvest` failed on
-- every HTTP source with "has no selectors configured" -- the only copy of
-- this configuration lived in one un-backed-up MySQL row.
--
-- `... AND selectors IS NULL` makes this a no-op wherever a source has
-- already been configured (by this migration or by an owner's later tuning)
-- and only seeds a genuinely fresh clone.
--
-- Keep tests/fixtures/<source>/selectors.json in step with these values --
-- .claude/skills/harvest/repair.md already instructs the owner to update
-- both together when a selector repair is approved.

UPDATE sources SET query = '{"remote": true, "keyword": "PHP"}', selectors = '{"item": "div.job-item", "fields": {"url": {"attr": "href", "absolute": true, "selector": "a.job_item__header-link"}, "title": {"attr": "text", "selector": "h2.job-item__position"}, "company": {"attr": "text", "selector": ".text-gray-800"}, "location": {"attr": "text", "selector": ".location-text"}, "description": {"attr": "text", "selector": ".js-truncated-text"}, "external_id": {"attr": "href", "regex": "/jobs/(\\\\d+)", "selector": "a.job_item__header-link"}, "arrangement_hint": {"attr": "text", "selector": "span.text-nowrap:-soup-contains-own(\\"Remote\\"), span.text-nowrap:-soup-contains-own(\\"Office\\")"}}, "container": "#jobs_main", "empty_state": "#jobs_main p.text-secondary", "minimum_items": 1}'
WHERE name = 'djinni' AND selectors IS NULL;

UPDATE sources SET query = '{"remote": true, "category": "PHP"}', selectors = '{"item": "li.l-vacancy", "fields": {"url": {"attr": "href", "absolute": true, "selector": "a.vt"}, "title": {"attr": "text", "selector": "a.vt"}, "company": {"attr": "text", "selector": "a.company"}, "location": {"attr": "text", "selector": ".cities"}, "salary_raw": {"attr": "text", "selector": ".salary"}, "description": {"attr": "text", "selector": ".sh-info"}, "external_id": {"attr": "href", "regex": "/vacancies/(\\\\d+)/", "selector": "a.vt"}, "arrangement_hint": {"attr": "text", "regex": "(remote|віддалено)", "selector": ".cities"}}, "container": "#vacancyListId", "empty_state": ".b-inner-page-header h1:-soup-contains(\\"Немає\\")", "minimum_items": 1}'
WHERE name = 'dou' AND selectors IS NULL;

UPDATE sources SET query = '{"tag": "php"}', selectors = '{"root": "", "fields": {"url": "url", "title": "position", "company": "company", "location": "location", "description": "description", "external_id": "id"}, "skip_first": true}'
WHERE name = 'remoteok' AND selectors IS NULL;

UPDATE sources SET query = '{"category": "remote-programming-jobs"}', selectors = '{"root": "", "fields": {"url": "link", "title": "title", "company": "title", "location": "region", "description": "description", "external_id": "guid"}, "skip_first": false}'
WHERE name = 'weworkremotely' AND selectors IS NULL;
