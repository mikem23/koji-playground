
BEGIN;

DROP TABLE build_notifications;

DROP TABLE log_messages;

DROP TABLE buildroot_listing;
DROP TABLE image_listing;

DROP TABLE rpminfo;
DROP TABLE image_builds;
DROP TABLE image_archives;

DROP TABLE group_package_listing;
DROP TABLE group_req_listing;
DROP TABLE group_config;
DROP TABLE groups;

DROP TABLE tag_listing;
DROP TABLE tag_packages;

DROP TABLE buildroot;
DROP TABLE repo;

DROP TABLE build_target_config;
DROP TABLE build_target;

DROP TABLE tag_config;
DROP TABLE tag_inheritance;
DROP TABLE tag;

DROP TABLE build;

DROP TABLE task;

DROP TABLE host_channels;
DROP TABLE host;

DROP TABLE channels;
DROP TABLE package;

DROP TABLE user_groups;
DROP TABLE user_perms;
DROP TABLE permissions;

DROP TABLE sessions;
DROP TABLE users;

DROP TABLE event_labels;
DROP TABLE events;
DROP FUNCTION get_event();
DROP FUNCTION get_event_time(INTEGER);

-- uncomment if you want to clear your tables
-- COMMIT;
