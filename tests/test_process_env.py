from pathlib import Path
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


def test_native_profile_uses_separate_template_and_local_cli_auth(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model_provider = "custom-user-value"\n', encoding="utf-8")
    (codex_home / "auth.json").write_text('{"token":"backend-only"}', encoding="utf-8")
    isolated = tmp_path / "isolated"
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("NEXUS_CODEX_HOME", str(isolated))

    env = codex_subprocess_env(profile="native", include_auth=True)
    worker = Path(env["CODEX_HOME"])

    assert worker.parent == isolated / "workers"
    assert (worker / "auth.json").read_text(encoding="utf-8") == '{"token":"backend-only"}'
    assert (isolated / "_template_native" / "auth.json").is_file()
    assert not (isolated / "_template_omniroute_noauth").exists()

    with pytest.raises(ValueError, match="requires local CLI authentication"):
        codex_subprocess_env(profile="native", include_auth=False)


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
