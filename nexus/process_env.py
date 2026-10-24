"""Environment helpers for launching Codex-backed subprocesses."""

from __future__ import annotations

import os
import shutil
import tempfile
import tomllib
import uuid

from nexus.runtime_assets import MANAGER_SKILL_SOURCE


_WORKER_RULES = '''# Defense in depth; workspace sandboxing is the primary boundary.
prefix_rule(
    pattern = [["cmd", "cmd.exe"], "/c", ["rmdir", "rd"]],
    decision = "forbidden",
    justification = "Nexus workers must not use cmd-based recursive deletion.",
)

prefix_rule(
    pattern = [["rmdir", "rd"], "/s"],
    decision = "forbidden",
    justification = "Nexus workers must use guarded Python cleanup.",
)

prefix_rule(
    pattern = [["Remove-Item", "remove-item"]],
    decision = "forbidden",
    justification = "Nexus workers must use guarded Python cleanup.",
)
'''


def _safe_omniroute_config(path: str) -> str:
    """Return a Worker config with non-overridable safe defaults."""
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            source = config_file.read()
        config = tomllib.loads(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError("OmniRoute Worker configuration is unavailable or invalid") from exc

    if any(key in config for key in ("sandbox_mode", "approval_policy")):
        raise RuntimeError("OmniRoute source config must not override Worker safety settings")
    if "danger-full-access" in source:
        raise RuntimeError("OmniRoute Worker configuration requests forbidden full access")

    safe = (
        'sandbox_mode = "workspace-write"\n'
        'approval_policy = "never"\n'
        # Without this, the installed Windows Codex CLI silently downgrades
        # every workspace-write request to read-only (reproduced directly:
        # omitting it forces `sandbox: read-only` in the exec banner even
        # with --sandbox workspace-write on the command line and
        # sandbox_mode="workspace-write" in config). This is the Windows
        # sandbox-elevation mode, not an approval/permission escalation --
        # "unelevated" is the least-privileged option, required for the
        # workspace-write boundary to actually take effect at all on Windows.
        'windows.sandbox = "unelevated"\n'
        + source.rstrip()
        + "\n\n"
        "[sandbox_workspace_write]\n"
        "exclude_tmpdir_env_var = true\n"
        "exclude_slash_tmp = true\n"
    )
    _validate_omniroute_data(tomllib.loads(safe))
    return safe


def _validate_omniroute_data(config: dict) -> None:
    """Fail closed unless provider and Worker safety controls are exact."""
    provider = config.get("model_providers", {}).get("omniroute")
    if config.get("model_provider") != "omniroute" or not isinstance(provider, dict):
        raise RuntimeError(
            "OmniRoute Worker configuration must select model_provider='omniroute'"
        )
    if not provider.get("base_url") or not provider.get("env_key"):
        raise RuntimeError("OmniRoute Worker provider configuration is incomplete")
    if config.get("sandbox_mode") != "workspace-write":
        raise RuntimeError("OmniRoute Worker must use workspace-write sandboxing")
    if config.get("approval_policy") != "never":
        raise RuntimeError("OmniRoute Worker must not auto-escalate permissions")


def _validate_omniroute_config(path: str) -> None:
    """Fail closed unless *path* selects a usable OmniRoute provider."""
    try:
        with open(path, "rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError("OmniRoute Worker configuration is unavailable or invalid") from exc

    _validate_omniroute_data(config)
    rules_path = os.path.join(os.path.dirname(path), "rules", "nexus-worker.rules")
    try:
        with open(rules_path, "r", encoding="utf-8") as rules_file:
            rules = rules_file.read()
    except OSError as exc:
        raise RuntimeError("OmniRoute Worker destructive-command rules are unavailable") from exc
    if rules != _WORKER_RULES:
        raise RuntimeError("OmniRoute Worker destructive-command rules are invalid")


def codex_subprocess_env(
    profile: str = "default", *, worker_home: str | None = None, include_auth: bool = True
) -> dict[str, str]:
    """Return an environment dict for launching the Codex CLI as a subprocess.

    On Windows the standard Codex data directory (~/.codex or the path named
    by CODEX_HOME) is shared with the user's interactive Codex session.  That
    session holds an exclusive write lock on state_5.sqlite and on the IPC
    socket used by the in-process app-server client.  Launching a second
    Codex process against the same data directory therefore fails immediately
    with "attempt to write a readonly database" or "Access denied" -- even
    when the --ephemeral flag is used.

    A single shared Nexus-dedicated directory fixes the conflict with the
    user's interactive session, but reintroduces the identical problem
    between two *concurrently running Nexus workers*: state_5.sqlite and the
    IPC socket are still single-writer, so a second worker launched in the
    same round blocks silently waiting for the first worker's lock instead
    of failing fast -- it never even reaches its "Execution started"
    checkpoint.

    Fix: every call gets its own fresh, uniquely-named CODEX_HOME directory
    (so every concurrently-spawned Codex subprocess gets a private
    state_5.sqlite / IPC socket -- no cross-worker lock contention).
    config.toml, optional auth.json, and the multi-agent-development-manager
    skill are copied once into a shared *template* directory and then copied
    (not shared) into each per-call directory. OmniRoute-only callers such as
    Chat can set ``include_auth=False`` to prevent direct OAuth fallback.
    ``profile="native"`` uses a distinct template, requires auth inclusion,
    and is selected only by the Codex Native Chat adapter.

    HOME is set from USERPROFILE on Windows when it is missing, preserving
    the behaviour that was already in place.  On non-Windows this function
    only ensures HOME is set.

    ``worker_home``: an explicit, caller-owned directory to use instead of
    a freshly generated one. Every existing caller (Manager/Worker/
    Reviewer, via this function's default) omits it and keeps getting a
    brand-new uuid-named directory every call, exactly as before --
    required for their one-shot, concurrent-safe isolation. It exists only
    for a caller that owns one *stable* identity across multiple calls
    (e.g. a Nexus Chat conversation that must `thread/resume` after an
    app-server process restart, where a fresh CODEX_HOME would mean no
    rollout file to resume). Config/auth/rules/skill population below is
    unchanged either way: files are only copied in if not already present,
    so resuming an existing ``worker_home`` never overwrites it.
    """
    env = os.environ.copy()

    # --- resolve the canonical home directory -----------------------------
    userprofile = env.get("USERPROFILE")
    if not userprofile and env.get("HOMEDRIVE") and env.get("HOMEPATH"):
        userprofile = env["HOMEDRIVE"] + env["HOMEPATH"]

    # Ensure HOME is set (needed by Codex on Windows when HOME is absent).
    if not env.get("HOME") and userprofile:
        env["HOME"] = userprofile

    # --- resolve the Nexus-dedicated *base* directory ----------------------
    # An explicit override is useful for managed deployments and tests.
    # Otherwise prefer LocalAppData, then a per-user Nexus directory, and
    # finally the OS temp directory. Never fall back to the shared ~/.codex:
    # that recreates the SQLite lock conflict this isolation is meant to fix.
    localappdata = env.get("LOCALAPPDATA")
    nexus_base = env.get("NEXUS_CODEX_HOME")
    if not nexus_base:
        if localappdata:
            nexus_base = os.path.join(localappdata, "Nexus", "CodexHome")
        elif userprofile:
            nexus_base = os.path.join(userprofile, ".nexus_codex_home")
        else:
            nexus_base = os.path.join(tempfile.gettempdir(), "Nexus", "CodexHome")

    if profile not in {"default", "native", "omniroute"}:
        raise ValueError(f"Unsupported Codex subprocess profile: {profile!r}")
    if profile == "native" and not include_auth:
        raise ValueError("Codex Native profile requires local CLI authentication.")

    template_name = (
        "_template_omniroute_noauth"
        if profile == "omniroute" and not include_auth
        else "_template_omniroute" if profile == "omniroute"
        else "_template_native" if profile == "native"
        else "_template"
    )
    template_dir = os.path.join(nexus_base, template_name)
    workers_root = os.path.join(nexus_base, "workers")

    try:
        os.makedirs(template_dir, exist_ok=True)
        os.makedirs(workers_root, exist_ok=True)

        # Populate the template once. CODEX_HOME is the config directory
        # itself, so config.toml/auth.json belong directly inside it (not
        # in a nested .codex directory).
        if userprofile:
            source_codex_home = os.path.join(userprofile, ".codex")
            source_config = os.path.join(source_codex_home, "config.toml")
            if profile == "omniroute":
                source_omniroute_home = env.get("NEXUS_OMNIROUTE_CODEX_HOME") or os.path.join(
                    userprofile, ".codex-omniroute-worker"
                )
                source_config = os.path.join(source_omniroute_home, "config.toml")
                safe_config = _safe_omniroute_config(source_config)
                with open(
                    os.path.join(template_dir, "config.toml"), "w", encoding="utf-8"
                ) as config_file:
                    config_file.write(safe_config)
                rules_dir = os.path.join(template_dir, "rules")
                os.makedirs(rules_dir, exist_ok=True)
                with open(
                    os.path.join(rules_dir, "nexus-worker.rules"),
                    "w",
                    encoding="utf-8",
                ) as rules_file:
                    rules_file.write(_WORKER_RULES)
                _validate_omniroute_config(os.path.join(template_dir, "config.toml"))
            elif os.path.isfile(source_config):
                shutil.copy2(source_config, os.path.join(template_dir, "config.toml"))

            source_auth = os.path.join(source_codex_home, "auth.json")
            if include_auth and os.path.isfile(source_auth):
                shutil.copy2(source_auth, os.path.join(template_dir, "auth.json"))

            # The manager prompt invokes this skill by name. Keep its source
            # available in the isolated profile without copying the entire
            # interactive Codex cache or its locked state database.
            dst_skill = os.path.join(
                template_dir, "skills", "multi-agent-development-manager"
            )
            if not MANAGER_SKILL_SOURCE.is_dir():
                raise RuntimeError("Repository-owned Nexus Worker skill is unavailable")
            os.makedirs(os.path.dirname(dst_skill), exist_ok=True)
            shutil.copytree(MANAGER_SKILL_SOURCE, dst_skill, dirs_exist_ok=True)

        # Each call gets a fresh, private CODEX_HOME so concurrent Codex
        # subprocesses never contend for the same state_5.sqlite / IPC
        # socket lock -- unless the caller passed its own stable
        # worker_home (see the docstring above).
        if worker_home is None:
            worker_home = os.path.join(workers_root, uuid.uuid4().hex)
        os.makedirs(worker_home, exist_ok=True)

        worker_files = ("config.toml", "auth.json") if include_auth else ("config.toml",)
        for filename in worker_files:
            src_file = os.path.join(template_dir, filename)
            dst_file = os.path.join(worker_home, filename)
            if os.path.isfile(src_file) and not os.path.exists(dst_file):
                shutil.copy2(src_file, dst_file)
        if profile == "omniroute" and not include_auth and os.path.isfile(os.path.join(worker_home, "auth.json")):
            raise RuntimeError(
                "Refusing OmniRoute launch with a CODEX_HOME that contains an OAuth auth.json."
            )

        template_rules = os.path.join(template_dir, "rules")
        worker_rules = os.path.join(worker_home, "rules")
        if os.path.isdir(template_rules):
            shutil.copytree(template_rules, worker_rules, dirs_exist_ok=True)

        template_skill = os.path.join(
            template_dir, "skills", "multi-agent-development-manager"
        )
        worker_skill = os.path.join(
            worker_home, "skills", "multi-agent-development-manager"
        )
        if os.path.isdir(template_skill) and not os.path.exists(worker_skill):
            os.makedirs(os.path.dirname(worker_skill), exist_ok=True)
            shutil.copytree(template_skill, worker_skill)

        env["CODEX_HOME"] = worker_home
    except OSError as exc:
        raise RuntimeError(
            f"Could not create isolated Nexus Codex home under {nexus_base!r}"
        ) from exc

    return env


GIT_SAFE_DIRECTORY_KEY = "safe.directory"


def with_scoped_git_trust(env: dict[str, str], repo_path: str) -> dict[str, str]:
    """Return a copy of ``env`` that trusts exactly ``repo_path`` for Git.

    Git refuses a repository owned by another Windows user ("dubious
    ownership"), and sandboxed agents run under a different identity than the
    host user that owns the assigned repository or worktree. This appends one
    ``safe.directory`` entry as *command-scope* configuration
    (``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``).

    That scope exists only in the launched process and its children. It writes
    no global, system, or repository config and changes no filesystem
    permissions. Existing command-scope entries are preserved and the new entry
    is appended after them.

    Wildcards, relative paths and a malformed existing ``GIT_CONFIG_COUNT`` are
    rejected, so the helper can never widen trust beyond the one path given.
    """

    if not repo_path or "*" in repo_path or not os.path.isabs(repo_path):
        raise ValueError(
            "Git trust requires one absolute repository path without wildcards."
        )

    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError as exc:
        raise ValueError(
            "GIT_CONFIG_COUNT is not an integer; refusing to append Git trust."
        ) from exc
    if count < 0:
        raise ValueError("GIT_CONFIG_COUNT must not be negative.")

    trusted = os.path.normpath(repo_path).replace(os.sep, "/")

    scoped = dict(env)
    scoped["GIT_CONFIG_COUNT"] = str(count + 1)
    scoped[f"GIT_CONFIG_KEY_{count}"] = GIT_SAFE_DIRECTORY_KEY
    scoped[f"GIT_CONFIG_VALUE_{count}"] = trusted
    return scoped


def command_argument(command: list[str], flag: str) -> str:
    """Return the value that follows ``flag`` in an argument vector."""

    return command[command.index(flag) + 1]
