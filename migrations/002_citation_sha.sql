-- Citations pin the file version their char offsets refer to.
ALTER TABLE citations ADD COLUMN IF NOT EXISTS sha256 text;
