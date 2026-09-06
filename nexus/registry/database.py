import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = ROOT_DIR / "nexus" / "storage"
DATABASE_PATH = STORAGE_DIR / "nexus.db"


def get_connection() -> sqlite3.Connection:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def initialize_database() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                input TEXT NOT NULL,
                intent TEXT NOT NULL,
                status TEXT NOT NULL,
                risk TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                result TEXT,
                commit_sha TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                parent_agent_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                result TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(id),
                FOREIGN KEY (parent_agent_id) REFERENCES agents(id)
            );

            CREATE INDEX IF NOT EXISTS idx_runs_project
                ON runs(project_id);

            CREATE INDEX IF NOT EXISTS idx_runs_status
                ON runs(status);

            CREATE INDEX IF NOT EXISTS idx_agents_run
                ON agents(run_id);

            CREATE INDEX IF NOT EXISTS idx_agents_status
                ON agents(status);

            CREATE TABLE IF NOT EXISTS approved_plans (
                run_id TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                boundary TEXT NOT NULL,
                worker_ordinal INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoints_run
                ON checkpoints(run_id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_checkpoints_run_sequence
                ON checkpoints(run_id, sequence);
            """
        )
