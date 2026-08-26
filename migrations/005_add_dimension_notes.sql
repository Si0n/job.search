-- Per-dimension reasoning from the scorer. Separate from `dimensions` so the
-- existing numeric shape is untouched and older scores simply have NULL here
-- rather than an empty object, which would read as "explained, nothing to say".
ALTER TABLE scores ADD COLUMN dimension_notes JSON NULL AFTER dimensions;
