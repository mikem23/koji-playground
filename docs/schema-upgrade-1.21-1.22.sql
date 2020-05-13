-- upgrade script to migrate the Koji database schema
-- from version 1.20 to 1.21


BEGIN;

ALTER TABLE events ALTER COLUMN time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), time::timestamptz);
ALTER TABLE sessions ALTER COLUMN start_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), start_time::timestamptz);
ALTER TABLE sessions ALTER COLUMN update_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), update_time::timestamptz);
ALTER TABLE task ALTER COLUMN create_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), create_time::timestamptz);
ALTER TABLE task ALTER COLUMN start_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), start_time::timestamptz);
ALTER TABLE task ALTER COLUMN completion_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), completion_time::timestamptz);
ALTER TABLE build ALTER COLUMN start_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), start_time::timestamptz);
ALTER TABLE build ALTER COLUMN completion_time TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), completion_time::timestamptz);
ALTER TABLE build_reservations ALTER COLUMN created TYPE TIMESTAMPTZ USING
    timezone(current_setting('TIMEZONE'), created::timestamptz);

DROP FUNCTION IF EXISTS get_event_time;
CREATE FUNCTION get_event_time(INTEGER) RETURNS TIMESTAMPTZ AS '
	SELECT time FROM events WHERE id=$1;
' LANGUAGE SQL;

COMMIT;
