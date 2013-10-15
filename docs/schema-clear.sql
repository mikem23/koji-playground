
BEGIN;

-- to generate these drops:
-- grep -i '^create table' schema.sql | awk '{printf("DROP TABLE %s;\n", $3)}' | tac

DROP TABLE win_archives;
DROP TABLE buildroot_archives;
DROP TABLE image_listing;
DROP TABLE image_archives;
DROP TABLE maven_archives;
DROP TABLE archiveinfo;
DROP TABLE archivetypes;
DROP TABLE win_builds;
DROP TABLE maven_builds;
DROP TABLE build_notifications;
DROP TABLE log_messages;
DROP TABLE buildroot_listing;
DROP TABLE rpmsigs;
DROP TABLE rpminfo;
DROP TABLE group_package_listing;
DROP TABLE group_req_listing;
DROP TABLE group_config;
DROP TABLE groups;
DROP TABLE tag_packages;
DROP TABLE tag_listing;
DROP TABLE image_builds;
DROP TABLE buildroot;
DROP TABLE tag_external_repos;
DROP TABLE external_repo_config;
DROP TABLE external_repo;
DROP TABLE repo;
DROP TABLE build_target_config;
DROP TABLE build_target;
DROP TABLE tag_updates;
DROP TABLE tag_config;
DROP TABLE tag_inheritance;
DROP TABLE tag;
DROP TABLE build;
DROP TABLE namespace;
DROP TABLE volume;
DROP TABLE package;
DROP TABLE task;
DROP TABLE host_channels;
DROP TABLE host;
DROP TABLE channels;
DROP TABLE sessions;
DROP TABLE user_groups;
DROP TABLE user_perms;
DROP TABLE permissions;
DROP TABLE users;
DROP TABLE event_labels;
DROP TABLE events;
DROP FUNCTION get_event();
DROP FUNCTION get_event_time(INTEGER);


-- uncomment if you actually want to clear your data
-- COMMIT;
