from pathlib import Path
import os
import tomllib
import pytest

from nexus.process_env import codex_subprocess_env
from nexus.runtime_assets import MANAGER_SKILL_SOURCE


def test_codex_profile_is_isolated_and_receives_config(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    config = user_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "test-model"\n', encoding="utf-8")
    auth = config.parent / "auth.json"
    auth.write_text('{"access_token":"test-token"}', encoding="utf-8")
    isolated_home = tmp_path / "local" / "Nexus" / "CodexHome"
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("NEXUS_CODEX_HOME", "")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    env = codex_subprocess_env()

    assert env["HOME"] == str(user_home)
    assert Path(env["CODEX_HOME"]).parent == isolated_home / "workers"
    assert Path(env["CODEX_HOME"]).is_dir()
    worker_home = Path(env["CODEX_HOME"])
    assert (worker_home / "config.toml").read_text(encoding="utf-8") == config.read_text(encoding="utf-8")
    assert (worker_home / "auth.json").read_text(encoding="utf-8") == auth.read_text(encoding="utf-8")
    assert (
        worker_home / "skills" / "multi-agent-development-manager" / "SKILL.md"
    ).read_text(encoding="utf-8") == (MANAGER_SKILL_SOURCE / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert not (isolated_home / ".codex" / "config.toml").exists()


def test_codex_profile_override_is_honored(tmp_path, monkeypatch):
    isolated_home = tmp_path / "override"
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(isolated_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)

    env = codex_subprocess_env()

    assert Path(env["CODEX_HOME"]).parent == isolated_home / "workers"
    assert Path(env["CODEX_HOME"]).is_dir()


def test_codex_profile_refreshes_config_and_auth_for_new_workers(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    config = codex_home / "config.toml"
    auth = codex_home / "auth.json"
    config.write_text('model = "config-a"\n', encoding="utf-8")
    auth.write_text('{"access_token":"token-a"}', encoding="utf-8")

    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("NEXUS_CODEX_HOME", "")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    first = codex_subprocess_env()
    assert (Path(first["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8") == 'model = "config-a"\n'
    assert (Path(first["CODEX_HOME"]) / "auth.json").read_text(encoding="utf-8") == '{"access_token":"token-a"}'

    config.write_text('model = "config-b"\n', encoding="utf-8")
    auth.write_text('{"access_token":"token-b"}', encoding="utf-8")
    second = codex_subprocess_env()

    assert (Path(second["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8") == 'model = "config-b"\n'
    assert (Path(second["CODEX_HOME"]) / "auth.json").read_text(encoding="utf-8") == '{"access_token":"token-b"}'
    assert first["CODEX_HOME"] != second["CODEX_HOME"]


def _write_omniroute_config(path: Path, suffix: str = "") -> str:
    content = (
        'model_provider = "omniroute"\n'
        f'model = "cc/test{suffix}"\n'
        '[model_providers.omniroute]\n'
        'name = "OmniRoute"\n'
        'base_url = "http://127.0.0.1:20128/v1"\n'
        'env_key = "OMNIROUTE_API_KEY"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def test_omniroute_profile_uses_dedicated_config_and_current_auth(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    generic_home = user_home / ".codex"
    generic_home.mkdir(parents=True)
    (generic_home / "config.toml").write_text('model = "openai-model"\n', encoding="utf-8")
    (generic_home / "auth.json").write_text('{"token":"current"}', encoding="utf-8")
    _write_omniroute_config(user_home / ".codex-omniroute-worker" / "config.toml")

    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    env = codex_subprocess_env(profile="omniroute")
    worker_home = Path(env["CODEX_HOME"])

    with (worker_home / "config.toml").open("rb") as stream:
        worker_config = tomllib.load(stream)
    assert worker_config["model_provider"] == "omniroute"
    assert worker_config["sandbox_mode"] == "workspace-write"
    assert worker_config["approval_policy"] == "never"
    # Without this, the installed Windows Codex CLI silently downgrades every
    # workspace-write request to read-only -- reproduced directly against the
    # real binary: omitting this key forces `sandbox: read-only` in the exec
    # banner even with --sandbox workspace-write and sandbox_mode set.
    # "unelevated" is the least-privileged Windows sandbox-elevation mode, not
    # a permission escalation -- it is required for workspace-write to take
    # effect at all on Windows.
    assert worker_config["windows"]["sandbox"] == "unelevated"
    assert worker_config["sandbox_workspace_write"]["exclude_tmpdir_env_var"] is True
    assert worker_config["sandbox_workspace_write"]["exclude_slash_tmp"] is True
    rules = (worker_home / "rules" / "nexus-worker.rules").read_text(encoding="utf-8")
    assert 'decision = "forbidden"' in rules
    assert '["cmd", "cmd.exe"], "/c", ["rmdir", "rd"]' in rules
    assert "openai-model" not in (worker_home / "config.toml").read_text(encoding="utf-8")
    assert (worker_home / "auth.json").read_text(encoding="utf-8") == '{"token":"current"}'


def test_omniroute_chat_home_does_not_copy_oauth_auth(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    generic_home = user_home / ".codex"
    generic_home.mkdir(parents=True)
    (generic_home / "auth.json").write_text('{"token":"must-not-copy"}', encoding="utf-8")
    _write_omniroute_config(user_home / ".codex-omniroute-worker" / "config.toml")
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    env = codex_subprocess_env(profile="omniroute", include_auth=False)

    worker_home = Path(env["CODEX_HOME"])
    assert (worker_home / "config.toml").is_file()
    assert not (worker_home / "auth.json").exists()
    assert not (tmp_path / "isolated" / "_template_omniroute_noauth" / "auth.json").exists()


def test_omniroute_chat_home_fails_closed_on_existing_oauth_auth(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    user_home.mkdir()
    _write_omniroute_config(user_home / ".codex-omniroute-worker" / "config.toml")
    worker_home = tmp_path / "isolated" / "workers" / "existing"
    worker_home.mkdir(parents=True)
    (worker_home / "auth.json").write_text('{"token":"legacy-token"}', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    with pytest.raises(RuntimeError, match="contains an OAuth auth.json"):
        codex_subprocess_env(profile="omniroute", worker_home=str(worker_home), include_auth=False)


def test_omniroute_profiles_are_unique_and_refreshed(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    generic_home = user_home / ".codex"
    generic_home.mkdir(parents=True)
    (generic_home / "auth.json").write_text("{}", encoding="utf-8")
    config = user_home / ".codex-omniroute-worker" / "config.toml"
    _write_omniroute_config(config, "-a")

    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    first = codex_subprocess_env(profile="omniroute")
    _write_omniroute_config(config, "-b")
    second = codex_subprocess_env(profile="omniroute")

    assert first["CODEX_HOME"] != second["CODEX_HOME"]
    assert 'model = "cc/test-a"' in (Path(first["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8")
    assert 'model = "cc/test-b"' in (Path(second["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8")


def test_omniroute_profile_fails_closed_without_selected_provider(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    dedicated = user_home / ".codex-omniroute-worker" / "config.toml"
    dedicated.parent.mkdir(parents=True)
    dedicated.write_text('model = "generic"\n', encoding="utf-8")

    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    import pytest

    with pytest.raises(RuntimeError, match="must select model_provider"):
        codex_subprocess_env(profile="omniroute")


def test_omniroute_profile_rejects_source_sandbox_override(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    dedicated = user_home / ".codex-omniroute-worker" / "config.toml"
    content = _write_omniroute_config(dedicated)
    dedicated.write_text(
        'sandbox_mode = "danger-full-access"\n' + content,
        encoding="utf-8",
    )
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))

    import pytest

    with pytest.raises(RuntimeError, match="must not override|forbidden full access"):
        codex_subprocess_env(profile="omniroute")


def _native_user(tmp_path, monkeypatch, *, with_auth=True):
    user_home = tmp_path / "user"
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model_provider = "omniroute"\n[mcp_servers.dangerous]\ncommand = "evil"\n', encoding="utf-8"
    )
    if with_auth:
        (codex_home / "auth.json").write_text('{"tokens":{"access_token":"native-login"}}', encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(tmp_path / "isolated"))
    monkeypatch.delenv("NEXUS_NATIVE_CODEX_AUTH_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return user_home


def test_native_profile_needs_no_omniroute_config_and_copies_only_local_auth(tmp_path, monkeypatch):
    _native_user(tmp_path, monkeypatch)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "must-not-leak")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    env = codex_subprocess_env(profile="native")
    home = Path(env["CODEX_HOME"])

    source_auth = tmp_path / "user" / ".codex" / "auth.json"
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"tokens":{"access_token":"native-login"}}'
    assert os.path.samefile(home / "auth.json", source_auth), "the login must be shared, not duplicated"
    assert os.stat(source_auth).st_nlink == 2
    config = (home / "config.toml").read_text(encoding="utf-8")
    with (home / "config.toml").open("rb") as stream:
        parsed = tomllib.load(stream)
    assert "omniroute" not in config and "mcp_servers" not in config and "evil" not in config
    assert parsed["cli_auth_credentials_store"] == "file"
    assert parsed["windows"]["sandbox"] == "unelevated"
    assert "sandbox_mode" not in parsed and "approval_policy" not in parsed
    assert 'decision = "forbidden"' in (home / "rules" / "nexus-worker.rules").read_text(encoding="utf-8")
    assert not any(key.upper().startswith("OMNIROUTE_") for key in env)
    assert str(home).startswith(str(tmp_path / "isolated"))


def test_native_profile_fails_closed_without_a_local_login(tmp_path, monkeypatch):
    _native_user(tmp_path, monkeypatch, with_auth=False)
    with pytest.raises(RuntimeError, match="codex login"):
        codex_subprocess_env(profile="native")


def test_native_profile_reuses_a_stable_home_and_keeps_one_shared_credential(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    source_auth = user_home / ".codex" / "auth.json"
    session_home = tmp_path / "session-home"
    first = codex_subprocess_env(profile="native", worker_home=str(session_home))
    # Codex refreshing its token writes the file in place: the one credential stays one file.
    (session_home / "auth.json").write_text('{"refreshed":"by-codex"}', encoding="utf-8")
    second = codex_subprocess_env(profile="native", worker_home=str(session_home))

    assert first["CODEX_HOME"] == second["CODEX_HOME"] == str(session_home)
    assert source_auth.read_text(encoding="utf-8") == '{"refreshed":"by-codex"}'
    assert os.path.samefile(session_home / "auth.json", source_auth)


def test_native_profile_does_not_keep_a_credential_alive_after_logout(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    codex_subprocess_env(profile="native", worker_home=str(session_home))
    (user_home / ".codex" / "auth.json").unlink()

    with pytest.raises(RuntimeError, match="codex login"):
        codex_subprocess_env(profile="native", worker_home=str(session_home))
    assert not (session_home / "auth.json").exists(), "a stale link must not outlive 'codex logout'"


def test_native_profile_follows_a_new_login_but_refuses_a_diverged_credential(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    source_auth = user_home / ".codex" / "auth.json"
    session_home = tmp_path / "session-home"
    codex_subprocess_env(profile="native", worker_home=str(session_home))

    # The user logs in again: a brand-new file replaces the old one.
    source_auth.unlink()
    source_auth.write_text('{"new":"login"}', encoding="utf-8")
    codex_subprocess_env(profile="native", worker_home=str(session_home))
    assert os.path.samefile(session_home / "auth.json", source_auth)

    # A session-local file that is newer than the login is never silently discarded.
    (session_home / "auth.json").unlink()
    (session_home / "auth.json").write_text('{"diverged":"newer"}', encoding="utf-8")
    os.utime(source_auth, (1, 1))
    with pytest.raises(RuntimeError, match="diverged"):
        codex_subprocess_env(profile="native", worker_home=str(session_home))


def test_native_profile_refuses_to_copy_credentials_unless_explicitly_allowed(tmp_path, monkeypatch):
    _native_user(tmp_path, monkeypatch)

    def no_links(*_args, **_kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    with pytest.raises(RuntimeError, match="NEXUS_NATIVE_ALLOW_AUTH_COPY"):
        codex_subprocess_env(profile="native", worker_home=str(tmp_path / "home-a"))

    monkeypatch.setenv("NEXUS_NATIVE_ALLOW_AUTH_COPY", "1")
    env = codex_subprocess_env(profile="native", worker_home=str(tmp_path / "home-b"))
    assert (Path(env["CODEX_HOME"]) / "auth.json").is_file()


def test_native_profile_refuses_a_codex_home_that_belongs_to_another_backend(tmp_path, monkeypatch):
    _native_user(tmp_path, monkeypatch)
    foreign = tmp_path / "omniroute-session-home"
    foreign.mkdir()
    foreign_config = 'model_provider = "omniroute"\n'
    (foreign / "config.toml").write_text(foreign_config, encoding="utf-8")
    with pytest.raises(RuntimeError, match="another backend"):
        codex_subprocess_env(profile="native", worker_home=str(foreign))
    assert (foreign / "config.toml").read_text(encoding="utf-8") == foreign_config
    assert not (foreign / "auth.json").exists()


def test_omniroute_profile_refuses_a_codex_home_holding_a_native_login(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    _write_omniroute_config(user_home / ".codex-omniroute-worker" / "config.toml")
    native_home = tmp_path / "native-session-home"
    codex_subprocess_env(profile="native", worker_home=str(native_home))
    with pytest.raises(RuntimeError, match="auth.json"):
        codex_subprocess_env(profile="omniroute", worker_home=str(native_home), include_auth=False)


def test_native_profile_never_writes_into_the_users_codex_home(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    before = sorted(path.name for path in (user_home / ".codex").iterdir())
    codex_subprocess_env(profile="native")
    assert sorted(path.name for path in (user_home / ".codex").iterdir()) == before


def test_native_profile_uses_the_users_own_codex_home_when_one_is_set(tmp_path, monkeypatch):
    _native_user(tmp_path, monkeypatch, with_auth=False)
    custom = tmp_path / "custom-codex-home"
    custom.mkdir()
    (custom / "auth.json").write_text('{"tokens":{"access_token":"custom-home-login"}}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(custom))

    env = codex_subprocess_env(profile="native", worker_home=str(tmp_path / "session-home"))

    assert os.path.samefile(Path(env["CODEX_HOME"]) / "auth.json", custom / "auth.json")
    assert Path(env["CODEX_HOME"]) != custom, "the session never runs inside the user's own Codex home"


def test_native_profile_shares_one_credential_across_many_sessions(tmp_path, monkeypatch):
    user_home = _native_user(tmp_path, monkeypatch)
    homes = [tmp_path / f"session-{index}" for index in range(5)]
    for home in homes:
        codex_subprocess_env(profile="native", worker_home=str(home))
    source = user_home / ".codex" / "auth.json"
    assert all(os.path.samefile(home / "auth.json", source) for home in homes)
    assert os.stat(source).st_nlink == 1 + len(homes)
    # One session ending (its name removed) never affects the login or its siblings.
    (homes[0] / "auth.json").unlink()
    assert source.is_file() and all(os.path.samefile(home / "auth.json", source) for home in homes[1:])
