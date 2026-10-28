CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    acceptance_pct REAL NOT NULL DEFAULT 0,
    share_level TEXT NOT NULL DEFAULT 'private'
        CHECK (share_level IN ('private', 'summary', 'full')),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('increase', 'decrease')),
    aggregation TEXT NOT NULL CHECK (aggregation IN ('sum', 'latest')),
    start_value REAL NOT NULL DEFAULT 0,
    final_target REAL NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);
-- Exactly one primary metric per goal, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_per_goal
    ON metrics(goal_id) WHERE is_primary = 1;

CREATE TABLE IF NOT EXISTS plan_versions (
    id INTEGER PRIMARY KEY,
    metric_id INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    end_date TEXT NOT NULL,
    final_target REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (metric_id, version_no)
);

CREATE TABLE IF NOT EXISTS weekly_targets (
    id INTEGER PRIMARY KEY,
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,
    target_value REAL NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    metric_id INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    entry_date TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (metric_id, entry_date)
);

CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY,
    requester_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'declined', 'revoked')),
    UNIQUE (requester_id, addressee_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
