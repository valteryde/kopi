-- A few rows so a dump of this database is not empty.

CREATE TABLE kopi_notes (
    id integer PRIMARY KEY,
    body text NOT NULL
);

INSERT INTO kopi_notes (id, body) VALUES
    (1, 'alpha'),
    (2, 'beta'),
    (3, 'gamma');
