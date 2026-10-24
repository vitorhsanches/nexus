import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
STORAGE_DIR = ROOT_DIR / "nexus" / "storage"
DATABASE_PATH = STORAGE_DIR / "nexus.db"


def get_connection() -> sqlite3.Connection:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    # Phase 2 Multi-Agent MVP: multiple Worker threads can now issue
    # concurrent checkpoint/agent/run writes for the same Run. Each thread
    # still gets its own independent sqlite3.Connection (never shared
    # across threads) -- record_checkpoint()'s BEGIN IMMEDIATE already
    # serializes concurrent writers correctly at the SQLite level. The
    # only change needed is a longer busy-wait timeout (default is 5s)
    # so a writer that is briefly blocked behind another thread's short
    # transaction retries instead of failing with "database is locked"
    # under legitimate, small-scale (a handful of Workers) concurrency.
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
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
        cols = [r[1] for r in connection.execute("PRAGMA table_info(runs)").fetchall()]
        if "owner_host" not in cols:
            connection.execute("ALTER TABLE runs ADD COLUMN owner_host TEXT")
        if "owner_pid" not in cols:
            connection.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
        if "owner_acquired_at" not in cols:
            connection.execute("ALTER TABLE runs ADD COLUMN owner_acquired_at TEXT")

        # Full, untruncated agent deliverable -- separate from the bounded
        # `result` preview column so existing readers of `result` (dependency
        # context, CLI reports, legacy UI) are unaffected. NULL for every
        # agent row written before this migration: there is no way to
        # recover a complete result for a run whose full text was never
        # captured, so historical rows honestly report has_full_result=False
        # rather than pretending the bounded preview was the whole thing.
        agent_cols = [r[1] for r in connection.execute("PRAGMA table_info(agents)").fetchall()]
        if "full_result" not in agent_cols:
            connection.execute("ALTER TABLE agents ADD COLUMN full_result TEXT")
        if "full_result_truncated" not in agent_cols:
            connection.execute(
                "ALTER TABLE agents ADD COLUMN full_result_truncated INTEGER NOT NULL DEFAULT 0"
            )

        # The Manager's planned capability requirement for this Worker
        # (mechanical/standard-coding/complex-coding/review-critical/
        # security-critical) -- never overwritten by adaptive routing, so
        # unlike `provider`/`model`/`effort` (which the router always
        # replaces with the actual selected route; see
        # resolve_initial_worker_route) this one planned field genuinely is
        # authoritative. NULL for agent rows created before this migration
        # and for non-Worker roles (Manager/ManagerReview).
        if "route_class" not in agent_cols:
            connection.execute("ALTER TABLE agents ADD COLUMN route_class TEXT")

        # Manager model selection (NEXUS_MANAGER_MODEL_SELECTION): which mode
        # chose the Manager model for this Run, and the model the caller
        # explicitly requested when mode="manual". The *actual* executed
        # model/provider/effort continue to live on the Manager's own
        # agents row, same as before this migration -- these two columns
        # only capture user intent. Historical rows predating this migration
        # read back as manager_mode="auto", manager_requested_model=NULL,
        # which is honest: they were all auto-selected (no manual mode
        # existed yet).
        cols = [r[1] for r in connection.execute("PRAGMA table_info(runs)").fetchall()]
        if "manager_mode" not in cols:
            connection.execute(
                "ALTER TABLE runs ADD COLUMN manager_mode TEXT NOT NULL DEFAULT 'auto'"
            )
        if "manager_requested_model" not in cols:
            connection.execute("ALTER TABLE runs ADD COLUMN manager_requested_model TEXT")

        # Nexus Chat: minimal session/turn metadata. Codex App-Server's own
        # non-ephemeral thread (nexus/chat/appserver_client.py) is the
        # source of truth for conversation *content* -- these tables only
        # map a Nexus session id to the Codex thread/CODEX_HOME needed to
        # resume it, plus durable turn status for idempotency and for a
        # client that reconnects long after a turn finished.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                codex_thread_id TEXT,
                codex_home TEXT,
                model TEXT,
                provider TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE INDEX IF NOT EXISTS idx_chat_sessions_project
                ON chat_sessions(project_id);

            CREATE TABLE IF NOT EXISTS chat_turns (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                response_text TEXT,
                error_code TEXT,
                error_message TEXT,
                model TEXT,
                provider TEXT,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chat_turns_session
                ON chat_turns(session_id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_session_idem
                ON chat_turns(session_id, idempotency_key);
            """
        )

        # Existing Nexus databases predate durable Chat process ownership.
        # Keep active-turn recovery safe across service restarts without
        # replaying a turn whose provider outcome may be uncertain.
        chat_turn_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(chat_turns)")
        }
        if "owner_pid" not in chat_turn_columns:
            connection.execute("ALTER TABLE chat_turns ADD COLUMN owner_pid INTEGER")
        if "owner_host" not in chat_turn_columns:
            connection.execute("ALTER TABLE chat_turns ADD COLUMN owner_host TEXT")
        if "owner_acquired_at" not in chat_turn_columns:
            connection.execute("ALTER TABLE chat_turns ADD COLUMN owner_acquired_at TEXT")
        chat_session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(chat_sessions)")}
        if "requested_model" not in chat_session_columns:
            connection.execute("ALTER TABLE chat_sessions ADD COLUMN requested_model TEXT")
        if "requested_effort" not in chat_session_columns:
            connection.execute("ALTER TABLE chat_sessions ADD COLUMN requested_effort TEXT")

        # Development Mode (F1.2): per-project authorization flag. Disabled
        # by default for every project -- new AND pre-existing rows alike,
        # since a plain ADD COLUMN with DEFAULT 0 backfills every existing
        # row as disabled. nexus.registry.projects.sync_projects() never
        # touches this column (see its own INSERT/UPDATE column lists), so
        # re-syncing config/projects.json can never silently re-enable or
        # disable it -- only the explicit internal setter in
        # nexus/registry/projects.py does.
        project_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
        if "dev_enabled" not in project_columns:
            connection.execute(
                "ALTER TABLE projects ADD COLUMN dev_enabled INTEGER NOT NULL DEFAULT 0"
            )

        # Development Mode (F1.2): durable session metadata only -- no
        # agent execution, no turns/messages yet (see nexus/dev/service.py).
        # One row per development session: which project, which isolated
        # worktree (nexus/dev/worktree.py), which branch, and the exact
        # base commit it was created from. Mirrors chat_sessions'
        # shape/intent closely (and reuses its owner_host/owner_pid/
        # owner_acquired_at + BEGIN IMMEDIATE pattern for restart recovery
        # and concurrency-safe creation) but is a fully separate table:
        # Development Mode's safety requirements (write-capable sandbox,
        # explicit per-project authorization) are different enough from
        # read-only Chat's that keeping the schemas independent avoids any
        # risk of a Dev Mode change ever affecting Chat's already-hardened
        # code path.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dev_sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                worktree_path TEXT,
                branch TEXT NOT NULL,
                base_sha TEXT,
                requested_model TEXT,
                -- Codex App-Server's own non-ephemeral thread for this
                -- session (F2.1), mirroring chat_sessions.codex_thread_id/
                -- codex_home exactly: lets a second and later turn in the
                -- same development session resume the same conversation
                -- instead of starting a fresh, unrelated one each time.
                codex_thread_id TEXT,
                codex_home TEXT,
                status TEXT NOT NULL DEFAULT 'CREATING',
                error_code TEXT,
                error_message TEXT,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE INDEX IF NOT EXISTS idx_dev_sessions_project
                ON dev_sessions(project_id);

            CREATE INDEX IF NOT EXISTS idx_dev_sessions_status
                ON dev_sessions(status);

            -- One branch per project at a time: also the DB-level backstop
            -- against two concurrent requests racing to reserve the same
            -- session's branch name (Git's own branch-collision check in
            -- nexus/dev/worktree.py is the first line of defense; this is
            -- the second, for the metadata row itself).
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dev_sessions_project_branch
                ON dev_sessions(project_id, branch);

            -- Development Mode (F2.1): one row per executed turn inside a
            -- development session. Deliberately a separate table from
            -- chat_turns -- same reasoning as dev_sessions vs chat_sessions
            -- above. owner_host/owner_pid/owner_acquired_at +
            -- started_at/finished_at mirror chat_turns exactly, for the
            -- same orphan-recovery and duplicate-completion-prevention
            -- guarantees.
            CREATE TABLE IF NOT EXISTS dev_turns (
                id TEXT PRIMARY KEY,
                dev_session_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                requested_model TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                response_text TEXT,
                error_code TEXT,
                error_message TEXT,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (dev_session_id) REFERENCES dev_sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_dev_turns_session
                ON dev_turns(dev_session_id);

            CREATE INDEX IF NOT EXISTS idx_dev_turns_status
                ON dev_turns(status);

            -- Human-approved Development Mode integration audit trail.
            -- Review snapshots and Apply results are session-linked and
            -- survive reopening the session/browser.
            CREATE TABLE IF NOT EXISTS dev_integration_reviews (
                review_id TEXT PRIMARY KEY,
                dev_session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                target_head TEXT NOT NULL,
                target_branch TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_sha TEXT,
                result_sha TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (dev_session_id) REFERENCES dev_sessions(id),
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_dev_integration_reviews_session
                ON dev_integration_reviews(dev_session_id, created_at);

            -- Cross-process serialization for the final branch update.
            -- Orphaned locks are intentionally fail-closed and require
            -- operator inspection instead of being silently reclaimed.
            CREATE TABLE IF NOT EXISTS dev_integration_locks (
                project_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                owner_host TEXT NOT NULL,
                owner_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                FOREIGN KEY (review_id) REFERENCES dev_integration_reviews(review_id)
            );

            -- Development Mode autonomous mission loop (F3.6B). A mission
            -- owns at most one active execution for a development session;
            -- turns remain in dev_turns and verification evidence is kept
            -- separately so model claims can never stand in for test results.
            CREATE TABLE IF NOT EXISTS dev_autonomous_missions (
                id TEXT PRIMARY KEY,
                dev_session_id TEXT NOT NULL,
                description TEXT NOT NULL,
                requested_model TEXT,
                allowed_models_json TEXT NOT NULL,
                test_profile TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                current_turn_id TEXT,
                interrupt_requested INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (dev_session_id) REFERENCES dev_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_dev_autonomous_missions_session
                ON dev_autonomous_missions(dev_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_dev_autonomous_missions_status
                ON dev_autonomous_missions(status);

            CREATE TABLE IF NOT EXISTS dev_mission_test_runs (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                profile TEXT NOT NULL,
                argv_json TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                stdout_text TEXT,
                stderr_text TEXT,
                error_code TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY (mission_id) REFERENCES dev_autonomous_missions(id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_dev_mission_test_runs_mission
                ON dev_mission_test_runs(mission_id, started_at);

            -- Multi-pane workspace (P1). A plan is one auditable Maestro
            -- proposal for a task; a pane is one agent identity bound to
            -- exactly one Development session (and therefore one isolated
            -- worktree). Hiding a pane is layout state only: the session,
            -- its turns and missions are never deleted with it.
            CREATE TABLE IF NOT EXISTS workspace_plans (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_text TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                complexity TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_plans_project
                ON workspace_plans(project_id, created_at);

            CREATE TABLE IF NOT EXISTS workspace_panes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                dev_session_id TEXT NOT NULL UNIQUE,
                plan_id TEXT,
                origin TEXT NOT NULL,
                role TEXT NOT NULL,
                title TEXT NOT NULL,
                model TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0,
                expanded INTEGER NOT NULL DEFAULT 0,
                hidden INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                FOREIGN KEY (dev_session_id) REFERENCES dev_sessions(id),
                FOREIGN KEY (plan_id) REFERENCES workspace_plans(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_panes_project
                ON workspace_panes(project_id, hidden, position);

            CREATE TABLE IF NOT EXISTS workspace_pane_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pane_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pane_id) REFERENCES workspace_panes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_pane_events_pane
                ON workspace_pane_events(pane_id, id);
            """
        )

        # Intelligent Maestro (P2A). A conversation is backed by exactly one
        # read-only Nexus Chat session (the Codex thread holds the content);
        # these rows keep the user's own words, the Maestro's parsed plan and
        # the per-message state so a restart can resume reconciliation.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_maestro_conversations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                chat_session_id TEXT UNIQUE,
                model TEXT,
                planning_authorized_at TEXT,
                cost_confirmed_model TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_maestro_conversations_project
                ON workspace_maestro_conversations(project_id, created_at);

            CREATE TABLE IF NOT EXISTS workspace_maestro_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                chat_turn_id TEXT UNIQUE,
                status TEXT NOT NULL,
                plan_id TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES workspace_maestro_conversations(id),
                FOREIGN KEY (plan_id) REFERENCES workspace_plans(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_maestro_messages_conversation
                ON workspace_maestro_messages(conversation_id, created_at);

            -- Direct user -> agent messages. Each one is a real Development
            -- turn in the pane's own worktree; changed-file snapshots taken
            -- before and after classify it as conversation-only or as code
            -- changes that still need verification.
            CREATE TABLE IF NOT EXISTS workspace_pane_messages (
                id TEXT PRIMARY KEY,
                pane_id TEXT NOT NULL,
                dev_turn_id TEXT UNIQUE,
                text TEXT NOT NULL,
                model TEXT,
                changes_before_json TEXT NOT NULL,
                changes_after_json TEXT,
                outcome TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pane_id) REFERENCES workspace_panes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_pane_messages_pane
                ON workspace_pane_messages(pane_id, created_at);
            """
        )
        conversation_cols = {row["name"] for row in connection.execute(
            "PRAGMA table_info(workspace_maestro_conversations)"
        )}
        if "cost_confirmed_model" not in conversation_cols:
            connection.execute(
                "ALTER TABLE workspace_maestro_conversations ADD COLUMN cost_confirmed_model TEXT"
            )
        plan_cols = [r[1] for r in connection.execute("PRAGMA table_info(workspace_plans)").fetchall()]
        for column, ddl in (
            ("source", "TEXT NOT NULL DEFAULT 'deterministic'"),
            ("conversation_id", "TEXT"),
            ("message_id", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'PROPOSED'"),
            ("approval_json", "TEXT"),
            ("approved_at", "TEXT"),
        ):
            if column not in plan_cols:
                connection.execute(f"ALTER TABLE workspace_plans ADD COLUMN {column} {ddl}")
        # One plan per Maestro user message: the database refuses a duplicate
        # even if two reconciliations race.
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_plans_message
               ON workspace_plans(message_id) WHERE message_id IS NOT NULL"""
        )
        pane_cols = [r[1] for r in connection.execute("PRAGMA table_info(workspace_panes)").fetchall()]
        for column in ("plan_agent_key", "brief_json"):
            if column not in pane_cols:
                connection.execute(f"ALTER TABLE workspace_panes ADD COLUMN {column} TEXT")

        # P2B.1 durable sequential plan execution. The coordinator is a
        # controller over existing Development missions; it is not another
        # executor. A single execution row is the cross-process claim, while
        # steps preserve the exact order, mission id and outcome for restart
        # recovery without replaying an uncertain mission.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_plan_executions (
                plan_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                status TEXT NOT NULL,
                agent_keys_json TEXT NOT NULL,
                test_profiles_json TEXT NOT NULL,
                max_turns_per_agent INTEGER NOT NULL,
                mission_timeout_seconds INTEGER NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                interrupt_requested INTEGER NOT NULL DEFAULT 0,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                error_code TEXT,
                error_message TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES workspace_plans(id),
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_plan_executions_project
                ON workspace_plan_executions(project_id, created_at);

            CREATE TABLE IF NOT EXISTS workspace_plan_execution_steps (
                plan_id TEXT NOT NULL,
                agent_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                pane_id TEXT NOT NULL,
                test_profile TEXT NOT NULL,
                status TEXT NOT NULL,
                mission_id TEXT,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, agent_key),
                UNIQUE (plan_id, ordinal),
                FOREIGN KEY (plan_id) REFERENCES workspace_plan_executions(plan_id),
                FOREIGN KEY (pane_id) REFERENCES workspace_panes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_plan_execution_steps_status
                ON workspace_plan_execution_steps(plan_id, status, ordinal);
            """
        )
        execution_cols = {row[1] for row in connection.execute("PRAGMA table_info(workspace_plan_executions)")}
        for column in ("owner_token", "lease_renewed_at", "resume_history_json"):
            if column not in execution_cols:
                connection.execute(f"ALTER TABLE workspace_plan_executions ADD COLUMN {column} TEXT")

        # P2B.2A: an approved, immutable snapshot and a durable write barrier.
        # A TRANSFERRING row is deliberately never reclaimed automatically.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_handoffs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                source_agent_key TEXT NOT NULL,
                target_agent_key TEXT NOT NULL,
                source_pane_id TEXT NOT NULL,
                target_pane_id TEXT NOT NULL,
                source_mission_id TEXT NOT NULL,
                source_session_id TEXT NOT NULL,
                target_session_id TEXT NOT NULL,
                source_base_sha TEXT NOT NULL,
                source_head_sha TEXT NOT NULL,
                target_base_sha TEXT NOT NULL,
                target_head_sha TEXT NOT NULL,
                snapshot_fingerprint TEXT NOT NULL,
                files_json TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_token TEXT,
                owner_host TEXT,
                owner_pid INTEGER,
                owner_acquired_at TEXT,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                error_code TEXT,
                error_message TEXT,
                result_json TEXT,
                FOREIGN KEY (plan_id) REFERENCES workspace_plans(id),
                FOREIGN KEY (source_pane_id) REFERENCES workspace_panes(id),
                FOREIGN KEY (target_pane_id) REFERENCES workspace_panes(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_handoffs_target
                ON workspace_handoffs(target_pane_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_handoffs_target_transferring
                ON workspace_handoffs(target_session_id) WHERE status = 'TRANSFERRING';

            -- Persistent cost consent is separate from Development Mode and
            -- Maestro plan approvals. No rows are seeded or activated by a
            -- migration. Uses are reserved under BEGIN IMMEDIATE and leases
            -- remain durable across process restarts.
            CREATE TABLE IF NOT EXISTS workspace_cost_authorizations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                cost_status TEXT NOT NULL,
                max_uses INTEGER NOT NULL,
                uses_reserved INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                max_concurrent INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_cost_authorizations_project
                ON workspace_cost_authorizations(project_id, status, expires_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_cost_authorizations_active_model
                ON workspace_cost_authorizations(project_id, model_id) WHERE status = 'ACTIVE';

            CREATE TABLE IF NOT EXISTS workspace_cost_authorization_leases (
                id TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL,
                execution_kind TEXT,
                execution_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                FOREIGN KEY (authorization_id) REFERENCES workspace_cost_authorizations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_cost_authorization_leases_active
                ON workspace_cost_authorization_leases(authorization_id, released_at, expires_at);
            """
        )
