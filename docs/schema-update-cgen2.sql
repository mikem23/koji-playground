BEGIN;

-- New tables

SELECT statement_timestamp(), 'Creating new tables' as msg;

CREATE TABLE btype (
        id SERIAL NOT NULL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
) WITHOUT OIDS;

CREATE TABLE build_types (
        build_id INTEGER NOT NULL REFERENCES build(id),
        btype_id INTEGER NOT NULL REFERENCES btype(id),
        PRIMARY KEY (build_id, btype_id)
) WITHOUT OIDS;

-- predefined build types

SELECT statement_timestamp(), 'Adding predefined build types' as msg;
INSERT INTO btype(name) VALUES ('rpm');
INSERT INTO btype(name) VALUES ('maven');
INSERT INTO btype(name) VALUES ('win');
INSERT INTO btype(name) VALUES ('image');

-- new column for archiveinfo

SELECT statement_timestamp(), 'Altering archiveinfo table' as msg;
ALTER TABLE archiveinfo ADD COLUMN btype_id INTEGER REFERENCES btype(id);

-- fill in legacy types
SELECT statement_timestamp(), 'Adding legacy btypes to builds' as msg;
INSERT INTO build_types(btype_id, build_id)
    SELECT btype.id, maven_builds.build_id FROM btype JOIN maven_builds ON btype.name='maven';
INSERT INTO build_types(btype_id, build_id)
    SELECT btype.id, win_builds.build_id FROM btype JOIN win_builds ON btype.name='win';
INSERT INTO build_types(btype_id, build_id)
    SELECT btype.id, image_builds.build_id FROM btype JOIN image_builds ON btype.name='image';
-- not sure if this is the best way to select rpm builds...
INSERT INTO build_types(btype_id, build_id)
    SELECT DISTINCT btype.id, build_id FROM btype JOIN rpminfo ON btype.name='rpm'
        WHERE build_id IS NOT NULL;

SELECT statement_timestamp(), 'Adding legacy btypes to archiveinfo' as msg;
UPDATE archiveinfo SET btype_id=(SELECT id FROM btype WHERE name='maven' LIMIT 1)
    WHERE (SELECT archive_id FROM maven_archives WHERE archive_id=archiveinfo.id) IS NOT NULL;
UPDATE archiveinfo SET btype_id=(SELECT id FROM btype WHERE name='win' LIMIT 1)
    WHERE (SELECT archive_id FROM win_archives WHERE archive_id=archiveinfo.id) IS NOT NULL;
UPDATE archiveinfo SET btype_id=(SELECT id FROM btype WHERE name='image' LIMIT 1)
    WHERE (SELECT archive_id FROM image_archives WHERE archive_id=archiveinfo.id) IS NOT NULL;

-- new component tables
SELECT statement_timestamp(), 'Creating new component tables' as msg;
CREATE TABLE archive_rpm_components (
        archive_id INTEGER NOT NULL REFERENCES archiveinfo(id),
        rpm_id INTEGER NOT NULL REFERENCES rpminfo(id),
        UNIQUE (archive_id, rpm_id)
) WITHOUT OIDS;
INSERT into archive_rpm_components (archive_id, rpm_id) SELECT image_id, rpm_id from image_listing;
CREATE TABLE archive_components (
        archive_id INTEGER NOT NULL REFERENCES archiveinfo(id),
        component_id INTEGER NOT NULL REFERENCES archiveinfo(id),
        UNIQUE (archive_id, component_id)
) WITHOUT OIDS;
INSERT into archive_components (archive_id, component_id) SELECT image_id, archive_id from image_archive_listing;

SELECT statement_timestamp(), 'Adding component table indexes' as msg;
CREATE INDEX rpm_components_idx on archive_rpm_components(rpm_id);
CREATE INDEX archive_components_idx on archive_components(component_id);


-- image_listing and image_archive_listing are no longer used


COMMIT;

