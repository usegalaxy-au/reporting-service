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
