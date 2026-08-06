CREATE TABLE IF NOT EXISTS fault_rule_configs (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    parameters TEXT NOT NULL DEFAULT '{}',
    persistence_seconds REAL,
    clear_seconds REAL,
    severity TEXT,
    UNIQUE(device_id, rule_id)
);

CREATE TABLE IF NOT EXISTS fault_events (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    rule_id TEXT NOT NULL,
    state TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    timestamp REAL NOT NULL,
    activated_at REAL,
    cleared_at REAL
);

CREATE INDEX IF NOT EXISTS idx_fault_events_device_id ON fault_events(device_id);
CREATE INDEX IF NOT EXISTS idx_fault_events_rule_id ON fault_events(rule_id);
