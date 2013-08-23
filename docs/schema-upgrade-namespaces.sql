-- schema updates for namespaces



CREATE TABLE namespace (
        id SERIAL NOT NULL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
) WITHOUT OIDS;

INSERT INTO namespace (id, name) VALUES (0, 'DEFAULT');



ALTER TABLE build ADD COLUMN namespace_id INTEGER DEFAULT 0;
-- can be null


ALTER TABLE build DROP CONSTRAINT build_pkg_ver_rel;
ALTER TABLE build ADD CONSTRAINT build_namespace_sanity UNIQUE (namespace_id, pkg_id, version, release);
--      note that namespace_id can be null, which allows arbitrary nvr overlap


ALTER TABLE rpminfo ADD COLUMN namespace_id INTEGER DEFAULT 0;
-- can be null


ALTER TABLE rpminfo DROP CONSTRAINT rpminfo_unique_nvra;
ALTER TABLE rpminfo ADD CONSTRAINT rpminfo_namespace_sanity UNIQUE (namespace_id,name,version,release,arch,external_repo_id);
--      note that namespace_id can be null, which allows arbitrary nvr overlap



