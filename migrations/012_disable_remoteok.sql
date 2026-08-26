-- RemoteOK ignores the `tag` query parameter. https://remoteok.com/api?tag=php
-- and https://remoteok.com/api return the same unfiltered firehose, and the
-- `tags` on each item bear no relation to its content: one PHP-tagged page held
-- a mail carrier, a butcher, a barman, a handyman and several retail shift
-- leads. Across 59 stored postings none stated a salary and none were PHP.
--
-- The source is disabled rather than deleted so the row, its selectors and its
-- history survive. Re-enable it if the API ever filters again. Its postings are
-- retired here too, because they are being dropped deliberately rather than
-- because they stopped being listed.
UPDATE sources SET enabled = FALSE WHERE name = 'remoteok';

UPDATE job_sources js
  JOIN sources s ON s.id = js.source_id
   SET js.inactive_at = NOW()
 WHERE s.name = 'remoteok' AND js.inactive_at IS NULL;

UPDATE jobs j
   SET j.inactive_at = NOW()
 WHERE j.inactive_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM job_sources js2
                    WHERE js2.job_id = j.id AND js2.inactive_at IS NULL);
