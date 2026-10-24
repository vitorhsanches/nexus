"""P2B.1C native Windows checks and fail-closed ownership regressions."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from datetime import UTC, datetime, timedelta

import pytest

from nexus.orchestration import ownership
from nexus.program import state
from tests import test_program_resume_safety as program_tests

_minimal_program = program_tests._minimal_program
live_other_pid = program_tests.live_other_pid
program_store = program_tests.program_store


class WinAPI:
    def __init__(self, *, handle=0x1234567887654321, exit_code=259, query_ok=True, times_ok=True):
        self.handle, self.exit_code = handle, exit_code
        self.query_ok, self.times_ok = query_ok, times_ok
        self.closed = []
        self.created = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)

    def OpenProcess(self, rights, inherit, pid):
        assert rights == 0x1000 and inherit is False
        return self.handle

    def GetExitCodeProcess(self, handle, output):
        assert handle == self.handle
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents.value = self.exit_code
        return self.query_ok

    def GetProcessTimes(self, handle, creation, *_rest):
        assert handle == self.handle
        delta = self.created - datetime(1970, 1, 1, tzinfo=UTC)
        ticks = 116444736000000000 + (delta.days * 86400 * 1000000 + delta.seconds * 1000000 + delta.microseconds) * 10
        output = ctypes.cast(creation, ctypes.POINTER(wintypes.FILETIME)).contents
        output.dwLowDateTime, output.dwHighDateTime = ticks & 0xFFFFFFFF, ticks >> 32
        return self.times_ok

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


def test_pointer_sized_handle_and_exact_filetime(monkeypatch):
    api = WinAPI()
    monkeypatch.setattr(ownership, "_windows_api", lambda: api)
    assert ownership._windows_process_info(100) == ("alive", api.created)
    assert api.closed == [0x1234567887654321]


@pytest.mark.parametrize(("win_error", "expected"), [(5, "unknown"), (87, "dead"), (6, "unknown"), (0, "unknown")])
def test_failed_open_distinguishes_absent_pid_from_unavailable_query(monkeypatch, win_error, expected):
    api = WinAPI(handle=0)
    monkeypatch.setattr(ownership, "_windows_api", lambda: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: win_error, raising=False)
    assert ownership._windows_process_info(100) == (expected, None)
    assert not api.closed


@pytest.mark.parametrize(("query_ok", "times_ok", "exit_code", "expected"), [
    (False, True, 259, "unknown"), (True, False, 259, "alive"), (True, True, 0, "dead"),
])
def test_query_failures_and_confirmed_exit_close_handle(monkeypatch, query_ok, times_ok, exit_code, expected):
    api = WinAPI(query_ok=query_ok, times_ok=times_ok, exit_code=exit_code)
    monkeypatch.setattr(ownership, "_windows_api", lambda: api)
    assert ownership._windows_process_info(100)[0] == expected
    assert api.closed == [api.handle]


def test_unavailable_native_api_is_unknown(monkeypatch):
    def unavailable():
        raise OSError("query unavailable")
    monkeypatch.setattr(ownership, "_windows_api", unavailable)
    assert ownership._windows_process_info(100) == ("unknown", None)


@pytest.mark.parametrize("pid", [0, -1, "invalid", None, True, 1.5, 2**32])
def test_invalid_pid_is_not_evidence_of_dead_owner(pid):
    assert ownership.is_pid_alive(pid) is None
    assert ownership.classify_ownership(ownership.current_host(), pid) == "unknown"


def test_partial_owner_record_is_not_unowned():
    assert ownership.classify_ownership(None, 123) == "unknown"
    assert ownership.classify_ownership("other-host", None) == "foreign_host"
    assert ownership.classify_ownership(None, None) == "unowned"


@pytest.mark.parametrize(("delta_ms", "expected"), [(-1, "live_same_host"), (0, "live_same_host"), (1, "unknown"), (500, "unknown"), (2000, "dead_same_host")])
def test_creation_time_needs_evidence_beyond_clock_resolution(monkeypatch, delta_ms, expected):
    acquired = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(ownership, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(ownership, "get_process_created_at", lambda _pid: acquired + timedelta(milliseconds=delta_ms))
    assert ownership.classify_ownership(ownership.current_host(), 12345678, acquired.isoformat()) == expected


def test_unknown_liveness_blocks_program_without_mutation(program_store, monkeypatch):
    program = _minimal_program(owner_host=ownership.current_host(), owner_pid=12345678)
    state.save_program(program)
    monkeypatch.setattr(ownership, "is_pid_alive", lambda _pid: None)
    with pytest.raises(state.ProgramOwnershipError) as caught:
        state.acquire_ownership(program["program_id"])
    assert caught.value.code == "PROGRAM_OWNER_FOREIGN"
    assert state.load_program(program["program_id"])["owner_pid"] == 12345678


def test_known_live_process_blocks_program(program_store, live_other_pid):
    assert ownership.is_pid_alive(live_other_pid) is True
    acquired = state.now_iso()
    classification = ownership.classify_ownership(ownership.current_host(), live_other_pid, acquired)
    assert classification in {"live_same_host", "unknown"}, {
        "classification": classification, "created": str(ownership.get_process_created_at(live_other_pid)), "acquired": acquired,
    }
    program = _minimal_program(owner_host=ownership.current_host(), owner_pid=live_other_pid, owner_acquired_at=acquired)
    state.save_program(program)
    with pytest.raises(state.ProgramOwnershipError):
        state.acquire_ownership(program["program_id"])
    assert state.load_program(program["program_id"])["owner_pid"] == live_other_pid


def test_native_windows_prototypes_preserve_handle_width():
    if os.name != "nt":
        pytest.skip("native Windows API only")
    api = ownership._windows_api()
    assert api.OpenProcess.restype is wintypes.HANDLE
    assert api.CloseHandle.argtypes == [wintypes.HANDLE]
    assert api.GetExitCodeProcess.argtypes[0] is wintypes.HANDLE
    assert ctypes.sizeof(api.OpenProcess.restype) == ctypes.sizeof(ctypes.c_void_p)
    assert ownership.is_pid_alive(os.getpid()) is True
    assert ownership.get_process_created_at(os.getpid()).tzinfo is not None


def test_unknown_query_never_authorizes_run_recovery_or_resume(monkeypatch):
    from nexus.orchestration.resume import ResumeError
    from tests.test_f3_recovery import F3RecoveryTestCase

    fixture = F3RecoveryTestCase()
    fixture.setUp()
    try:
        run_id = fixture._make_run()
        fixture._set_owner(run_id, ownership.current_host(), 12345678)
        monkeypatch.setattr(ownership, "is_pid_alive", lambda _pid: None)
        assert ownership.try_acquire_for_recovery(run_id) is False
        with pytest.raises(ResumeError) as caught:
            ownership.try_acquire_for_resume(run_id)
        assert caught.value.code == "RESUME_OWNER_FOREIGN"
        assert fixture._owner(run_id) == (ownership.current_host(), 12345678)
    finally:
        fixture.tearDown()
