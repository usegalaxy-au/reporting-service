-- Minimal subset of the Galaxy schema for integration testing:
-- only the columns the `tools` report's JOB_QUERY reads.

CREATE TABLE galaxy_user (
    id      bigint PRIMARY KEY,
    email   text
);

CREATE TABLE job (
    id          bigint PRIMARY KEY,
    tool_id     text NOT NULL,
    create_time timestamp without time zone NOT NULL,
    user_id     bigint REFERENCES galaxy_user(id)
);

CREATE INDEX job_create_time_idx ON job (create_time);
CREATE INDEX job_tool_id_idx ON job (tool_id);

CREATE TABLE workflow (
    id              integer PRIMARY KEY,
    uuid            character(32),
    source_metadata bytea
);

CREATE TABLE stored_workflow (
    id                 integer PRIMARY KEY,
    name               text,
    user_id            integer NOT NULL REFERENCES galaxy_user(id),
    latest_workflow_id integer REFERENCES workflow(id)
);

CREATE INDEX stored_workflow_user_id_idx ON stored_workflow (user_id);
