-- Preserve legacy command rows and identifiers; add a source-qualified lookup.
CREATE TABLE command_identities(account_id TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('web','telegram')), external_request_id TEXT NOT NULL, storage_key TEXT NOT NULL, PRIMARY KEY(account_id,source,external_request_id), UNIQUE(account_id,storage_key)) STRICT;
INSERT INTO command_identities SELECT account_id,source,request_id,request_id FROM commands;
