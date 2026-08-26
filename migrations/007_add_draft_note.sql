-- The instruction the owner gave when asking for a rewrite ("shorter, lead with
-- the KYC work"). `note` is pending: set from the dashboard, consumed and cleared
-- by the next /draft run. `applied_note` is what actually produced the current
-- text, kept so a draft reopened weeks later explains its own shape.
ALTER TABLE drafts ADD COLUMN note         VARCHAR(1000) NULL AFTER why_fit;
ALTER TABLE drafts ADD COLUMN applied_note VARCHAR(1000) NULL AFTER note;
