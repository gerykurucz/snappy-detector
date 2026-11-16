"""Regression tests for Windows utility contracts and safe failure paths."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine
import input as input_mod
import maintenance
import measurement
import reticle
import scanner
import spoofer
import system
import utils


def test_ed25519_vector_and_identity_forgery_rejection():
    seed = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    public = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert system.sign(seed, b"") == signature
    assert system.verify(public, b"", signature)
    identity = bytes.fromhex("01" + "00" * 31)
    assert not system.verify(identity, b"message", identity + b"\0" * 32)


def test_timer_clamp_uses_semantic_bounds_and_invalid_default():
    bounds = system.TimerResolution(min_us=500.0, max_us=15625.0)
    assert system.clamp_timer_target(100.0, bounds) == 5000
    assert system.clamp_timer_target(20000.0, bounds) == 156250
    assert system.clamp_timer_target("invalid", bounds) == 5000
    reversed_bounds = system.TimerResolution(min_us=15625.0, max_us=500.0)
    assert system.clamp_timer_target(1000.0, reversed_bounds) == 10000


def test_hsv_invalid_values_are_safe():
    assert system.hsv_to_rgb(float("nan"), 1, 1) == (0, 0, 0)
    assert system.hsv_to_rgb(0, float("inf"), 1) == (0, 0, 0)


def test_input_role_dedup_and_interception_packet(monkeypatch):
    messages = []
    monkeypatch.setattr(input_mod, "log", messages.append)
    input_mod._ROLE_NOTED.clear()
    input_mod.note_mouse_role("test", 1)
    input_mod.note_mouse_role("test", 1)
    assert len(messages) == 1

    backend = input_mod.InterceptionBackend(ctx=object())
    packet = backend._button_stroke(True)
    assert len(packet) == backend._STROKE_LEN
    unit, flags, buttons, data, raw, x, y, info = struct.unpack(
        backend._STROKE_FMT, packet)
    assert (unit, buttons, data, raw, x, y, info) == (
        0, 1, 0, 0, 0, 0, input_mod.INTERCEPTION_EXTRA_INFO)
    assert flags == backend._MOUSE_MOVE_RELATIVE


def test_input_falls_back_and_closes_unusable_interception(monkeypatch):
    closed = []

    class FakeInterception:
        name = "interception"
        _reason = "no mouse device"

        def ok_for_button(self):
            return False

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        input_mod, "InterceptionBackend", FakeInterception)
    monkeypatch.setattr(input_mod, "Win32Backend", lambda: "win32")
    monkeypatch.setattr(input_mod, "log", lambda *_args: None)
    assert input_mod.make_input_backend(
        {"input_backend": "driver"}) == "win32"
    assert closed == [True]


def test_input_invalid_measurement_arguments_do_not_raise():
    assert not input_mod.run_click_test(count="bad").ok
    result = input_mod.measure_sleep_overshoot(n="bad")
    assert result["error"]


def test_click_receiver_handles_negative_virtual_desktop():
    assert input_mod._receiver_origin(
        -1200, 400, -1920, 0, 3840, 1080, 244, 64) == (-1322, 368)


def test_temporary_mouse_accel_release_requires_successful_acquire(monkeypatch):
    calls = []
    monkeypatch.setattr(
        input_mod, "_set_mouse_accel",
        lambda enabled: calls.append(enabled) or (None if not enabled else True))
    input_mod._accel_nesting.count = 0
    input_mod._accel_nesting.acquired = False
    with input_mod._disable_mouse_accel():
        pass
    assert calls == [False]


def test_driver_strokes_carry_private_extra_info_tag():
    backend = input_mod.InterceptionBackend(ctx=object())
    stroke = backend._button_stroke(True)
    assert stroke[-4:] == struct.pack(
        "<I", input_mod.INTERCEPTION_EXTRA_INFO)


def test_probe_auto_selects_one_transport(monkeypatch):
    calls = []

    def ping(*_args):
        calls.append("icmp")
        return None

    def tcp(*_args):
        calls.append("tcp")
        return 2.5

    monkeypatch.setattr(measurement, "_ping_once", ping)
    monkeypatch.setattr(measurement, "_tcp_once", tcp)
    result = measurement.probe("example.test", count=3, interval=0)
    assert result.transport == "tcp"
    assert calls == ["icmp", "tcp", "tcp", "tcp"]
    assert [s.rtt_ms for s in result.samples] == [2.5, 2.5, 2.5]


def test_probe_validation_and_percentile_bounds():
    with pytest.raises(ValueError):
        measurement.probe("host", transport="udp")
    with pytest.raises(ValueError):
        measurement.probe("host", port=70000)
    assert measurement._pct([1.0], float("nan")) is None
    assert measurement._pct([1.0, 2.0], 101) is None


def test_trace_cgnat_ignores_malformed_addresses():
    trace = measurement.TraceResult(
        target="x",
        hops=[
            measurement.Hop(1, "100.bad.1.1"),
            measurement.Hop(2, "100.64.2.3"),
        ],
    )
    assert trace.cgnat


def test_power_generation_defaults_to_read_only_survey():
    steps = maintenance.build_power_steps()
    assert len(steps) == 1
    assert "survey" in steps[0].description.lower()
    assert "/set" not in (steps[0].command or "").lower()


def test_memory_process_trim_uses_supported_win32_api():
    steps = maintenance.build_memory_steps(
        ("memory.trim_process",), process_names=("game",))
    command = "\n".join(step.command or "" for step in steps)
    assert "EmptyWorkingSet" in command
    assert "TrimWorkingSet" not in command


def test_tweak_rejects_out_of_range_percentile():
    with pytest.raises(system.TweakValidationError):
        dataclasses.replace(maintenance.all_tweaks()[0], measure_metric="p101")


def test_nic_parser_uses_locale_independent_registry_value():
    payload = {
        "a": [{"Name": "Ethernet", "Status": "Up", "MtuSize": 1500}],
        "p": [
            {"Name": "Ethernet", "AllowComputerToTurnOffDevice": False},
        ],
        "m": [{"Name": "Ethernet", "DisplayValue": "Engedélyezve",
               "RegistryValue": ["1"]}],
    }
    nic = engine.parse_nics(payload)[0]
    assert nic.interrupt_moderation is True
    assert nic.allow_turn_off is False


def test_generated_preview_does_not_prompt_for_irreversible_step():
    step = maintenance.Step(
        kind=maintenance.StepKind.FILE_DELETE,
        description="delete test file",
        command="Remove-Item test",
        reversible=False,
        safety="test only",
    )
    text = engine.render_maintenance_script(
        [step], title="test", generated_at="now")
    assert "if (-not $Force -and -not $WhatIf2)" in text
    assert "[preview] no change made" in text


def test_generated_undo_uses_embedded_allowlist_not_snapshot_code():
    step = maintenance.reg_set(
        "HKCU", r"Software\Test", "Enabled", 1)
    change = engine.PlannedChange(
        tweak=maintenance.all_tweaks()[0], steps=[step])
    bundle = engine.render_bundle([change], generated_at="now")
    undo_id = bundle.manifest["changes"][0]["steps"][0]["undo_id"]
    assert undo_id
    assert undo_id in bundle.apply_ps1
    assert undo_id in bundle.undo_ps1
    assert "[scriptblock]::Create" not in bundle.undo_ps1
    assert "undoScript" not in bundle.apply_ps1
    # PS 5.1 returns a JSON array as one pipeline object; a direct @(...)
    # around ConvertFrom-Json would nest the records and break multi-record
    # snapshots. Both readers must assign first, then wrap.
    assert "$old = @()" in bundle.apply_ps1
    assert "if ($null -ne $parsed) { $old = @($parsed) }" in bundle.apply_ps1
    assert "$changes = @()" in bundle.undo_ps1
    assert "if ($null -ne $parsed) { $changes = @($parsed) }" in bundle.undo_ps1
    assert "applyCompleted = $false" in bundle.apply_ps1
    assert "$record.applyCompleted = $true" in bundle.apply_ps1
    assert "Archived the completed snapshot" in bundle.undo_ps1
    # A preview must stop before the archive/move step: the snapshot has to
    # survive so the real undo can still consume it.
    preview = bundle.undo_ps1.index("Undo preview complete")
    assert preview < bundle.undo_ps1.index("Archived the completed snapshot")
    assert "active snapshot was left untouched" in bundle.undo_ps1

    again = engine.render_bundle([change], generated_at="later")
    assert again.manifest["changes"][0]["steps"][0]["undo_id"] == undo_id

    maintenance_text = engine.render_maintenance_script(
        [step], title="test", generated_at="now")
    assert "[scriptblock]::Create" not in maintenance_text
    assert "undoScript" not in maintenance_text
    assert "$old = @()" in maintenance_text
    assert "if ($null -ne $parsed) { $changes = @($parsed) }" in maintenance_text
    assert "applyCompleted = $false" in maintenance_text
    assert "Archived the completed snapshot" in maintenance_text
    maintenance_preview = maintenance_text.index("Undo preview complete")
    assert maintenance_preview < maintenance_text.index(
        "Archived the completed snapshot")


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="requires Windows PowerShell",
)
def test_generated_undo_handles_multiple_records_and_preview_keeps_snapshot(
        tmp_path):
    steps = [
        maintenance.reg_set("HKCU", r"Software\SnappyAITest", "A", 1),
        maintenance.reg_set("HKCU", r"Software\SnappyAITest", "B", 2),
    ]
    change = engine.PlannedChange(
        tweak=maintenance.all_tweaks()[0], steps=steps)
    bundle = engine.render_bundle([change], generated_at="now")
    (tmp_path / "undo.ps1").write_text(bundle.undo_ps1, encoding="utf-8")
    ids = [s["undo_id"] for s in bundle.manifest["changes"][0]["steps"]]
    assert len(ids) == 2
    snapshot = [
        {
            "tweak": change.tweak.id,
            "description": "preview probe",
            "kind": "registry_set",
            "prior": {"keyExists": False, "valueExists": False},
            "undoId": undo_id,
            "reversible": True,
            "requiresReboot": False,
        }
        for undo_id in ids
    ]
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    original = snapshot_path.read_bytes()

    proc = subprocess.run(
        [
            shutil.which("powershell.exe"),
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", str(tmp_path / "undo.ps1"),
            "-WhatIf2", "-NoPause", "-SkipManual",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Restoring 2 recorded change(s)" in proc.stdout
    assert "Undo preview complete" in proc.stdout
    assert snapshot_path.read_bytes() == original


def test_scanner_explicit_root_and_scan_output_are_self_contained(tmp_path):
    root = tmp_path / "controller"
    export = tmp_path / "export"
    ctl = scanner.GuiController(backend_name="fake", out_root=root)
    assert ctl._reports_root == root
    assert ctl._scripts_exec_root == root / "exec"
    ctl._scan(
        experimental=False, with_maintenance=False,
        min_confidence="low", out_dir=export,
    )
    assert (export / "report.md").is_file()
    assert (export / "manifest.json").is_file()
    assert (export / "exec" / "apply.ps1").is_file()
    assert (export / "undo" / "undo.ps1").is_file()
    assert ctl.last_scan.apply_dir == export / "exec"
    assert ctl._apply_dir() == export / "exec"


def test_scanner_rejects_invalid_monitor_parameters(tmp_path):
    ctl = scanner.GuiController(backend_name="fake", out_root=tmp_path)
    assert not ctl.start_monitor("")
    assert not ctl.start_monitor("localhost", port=70000)


def test_scanner_does_not_overwrite_active_transaction(tmp_path):
    ctl = scanner.GuiController(backend_name="fake", out_root=tmp_path)
    apply = tmp_path / "exec" / "apply.ps1"
    undo = tmp_path / "undo" / "undo.ps1"
    snapshot = tmp_path / "exec" / "snapshot.json"
    apply.write_text("old apply", encoding="utf-8")
    undo.write_text("old undo", encoding="utf-8")
    snapshot.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="active rollback snapshot"):
        ctl._scan(
            experimental=False, with_maintenance=False,
            min_confidence="low", out_dir=None,
        )
    assert apply.read_text(encoding="utf-8") == "old apply"
    assert undo.read_text(encoding="utf-8") == "old undo"


def test_dll_pin_requires_file_and_valid_hash(tmp_path):
    target = tmp_path / "library.dll"
    assert not utils.dll_pin_ok(str(target))
    target.write_bytes(b"payload")
    target.with_suffix(".dll.sha256").write_text(
        "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
        encoding="ascii",
    )
    assert utils.dll_pin_ok(str(target))
    target.write_bytes(b"changed")
    assert not utils.dll_pin_ok(str(target))


def test_run_ps1_missing_and_invalid_timeout(tmp_path):
    missing = utils.run_ps1(tmp_path / "missing.ps1")
    assert missing.code == 2
    script = tmp_path / "test.ps1"
    script.write_text("exit 0", encoding="utf-8")
    invalid = utils.run_ps1(script, timeout="bad")
    assert invalid.code == 2


def test_cleaner_only_removes_owned_temp_entries(tmp_path, monkeypatch):
    temp_root = tmp_path / "temp"
    app_root = tmp_path / "app"
    temp_root.mkdir()
    (temp_root / "unrelated.tmp").write_text("keep", encoding="utf-8")
    (temp_root / "snappyai-probe.tmp").write_text("remove", encoding="utf-8")
    monkeypatch.setattr(spoofer.tempfile, "gettempdir", lambda: str(temp_root))
    cleaner = spoofer.CleanerEngine()
    cleaner.app_dir = str(app_root)
    removed = cleaner._clean_temp_files()
    assert str(temp_root / "snappyai-probe.tmp") in removed
    assert (temp_root / "unrelated.tmp").exists()


class _FakeRegistryKey:
    def __init__(self, registry, hive, path):
        self.registry = registry
        self.hive = hive
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    HKEY_CLASSES_ROOT = 3
    HKEY_USERS = 4
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self):
        self.keys = {}

    @staticmethod
    def _parts(path):
        return [part for part in str(path).split("\\") if part]

    def _ensure(self, hive, path):
        parts = self._parts(path)
        for index in range(1, len(parts) + 1):
            self.keys.setdefault((hive, "\\".join(parts[:index])), {})

    def OpenKey(self, hive, path, *_args):
        if (hive, path) not in self.keys:
            raise FileNotFoundError(path)
        return _FakeRegistryKey(self, hive, path)

    def CreateKeyEx(self, hive, path, *_args):
        self._ensure(hive, path)
        return _FakeRegistryKey(self, hive, path)

    def EnumValue(self, key, index):
        values = list(self.keys[(key.hive, key.path)].items())
        if index >= len(values):
            raise OSError("no more values")
        name, (value, value_type) = values[index]
        return name, value, value_type

    def EnumKey(self, key, index):
        prefix = key.path + "\\"
        children = sorted({
            path[len(prefix):].split("\\", 1)[0]
            for hive, path in self.keys
            if hive == key.hive and path.startswith(prefix)
        })
        if index >= len(children):
            raise OSError("no more keys")
        return children[index]

    def DeleteKey(self, hive, path):
        if any(h == hive and p.startswith(path + "\\") for h, p in self.keys):
            raise OSError("key has children")
        try:
            del self.keys[(hive, path)]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    def SetValueEx(self, key, name, _reserved, value_type, value):
        self.keys[(key.hive, key.path)][name] = (value, value_type)

    def DeleteValue(self, key, name):
        try:
            del self.keys[(key.hive, key.path)][name]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc


def test_registry_cleanup_snapshot_restores_values_and_empty_keys(
        tmp_path, monkeypatch):
    fake = _FakeWinreg()
    root = r"Software\SnappyAI"
    child = root + r"\EmptyChild"
    nested = root + r"\Nested"
    fake._ensure(fake.HKEY_CURRENT_USER, child)
    fake._ensure(fake.HKEY_CURRENT_USER, nested)
    fake.keys[(fake.HKEY_CURRENT_USER, root)]["Name"] = ("old", fake.REG_SZ)
    fake.keys[(fake.HKEY_CURRENT_USER, nested)]["Blob"] = (
        b"\x00\xff", fake.REG_BINARY)

    backup = tmp_path / "backups"
    registry = spoofer.RegistrySpoofingEngine(str(backup))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(spoofer, "winreg", fake)
    monkeypatch.setattr(spoofer, "get_registry_engine", lambda: registry)

    result = spoofer.CleanerEngine()._clean_registry()
    assert result["success"]
    assert result["cleaned"] == [root]
    assert not any(
        hive == fake.HKEY_CURRENT_USER and
        (path == root or path.startswith(root + "\\"))
        for hive, path in fake.keys)

    snapshot_name = Path(result["snapshot"]).stem
    assert registry.restore_snapshot(snapshot_name)
    assert (fake.HKEY_CURRENT_USER, child) in fake.keys
    assert fake.keys[(fake.HKEY_CURRENT_USER, root)]["Name"] == (
        "old", fake.REG_SZ)
    assert fake.keys[(fake.HKEY_CURRENT_USER, nested)]["Blob"] == (
        b"\x00\xff", fake.REG_BINARY)


def test_full_clean_reports_registry_failure(monkeypatch):
    cleaner = spoofer.CleanerEngine()
    monkeypatch.setattr(cleaner, "_clean_files", lambda: [])
    monkeypatch.setattr(cleaner, "_clean_temp_files", lambda: [])
    monkeypatch.setattr(cleaner, "_clean_cache", lambda: [])
    monkeypatch.setattr(
        cleaner, "_clean_registry",
        lambda: {"success": False, "cleaned": [], "error": "denied"})
    assert not cleaner.full_clean()["success"]


def test_registry_snapshot_names_and_manual_listing(tmp_path):
    registry = spoofer.RegistrySpoofingEngine(str(tmp_path))
    registry.track("HKCU", r"Software\Test", "Value", b"old", 3)
    path = Path(registry.create_snapshot("manual_20260101"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["changes"][0]["previous"]["__registry_bytes__"]
    assert registry.list_snapshots() == ["manual_20260101"]
    with pytest.raises(FileExistsError):
        registry.create_snapshot("manual_20260101")
    with pytest.raises(ValueError):
        registry.create_snapshot("../escape")
    assert not registry.restore_snapshot("../escape")


def test_transaction_unknown_undo_is_reported(tmp_path):
    tx = spoofer.Transaction(str(tmp_path / "tx.json"))
    tx.add("change", "target", None, {"kind": "unknown"})
    tx.commit()
    loaded = spoofer.Transaction(str(tmp_path / "tx.json"))
    result = loaded.undo()
    assert not result["success"]
    assert result["failed"][0]["target"] == "target"


def test_driver_file_is_not_equated_with_active_service(tmp_path, monkeypatch):
    (tmp_path / "Kernel.sys").write_bytes(b"driver")
    monkeypatch.setattr(spoofer, "driver_dir", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(
        spoofer, "_service_status", lambda _name: (False, "not_installed", ""))
    status = spoofer.get_loading_status()
    assert status["driver_file"]
    assert not status["driver_active"]
    assert not status["driver_installed"]


def test_driver_service_must_match_bundled_binary(tmp_path, monkeypatch):
    bundled = tmp_path / "Kernel.sys"
    installed = tmp_path / "installed.sys"
    bundled.write_bytes(b"trusted driver")
    installed.write_bytes(b"different driver")
    monkeypatch.setattr(
        spoofer, "driver_dir", lambda *parts: str(tmp_path.joinpath(*parts)))
    monkeypatch.setattr(
        spoofer, "_service_status", lambda _name: (True, "running", ""))
    monkeypatch.setattr(
        spoofer, "_service_image_path",
        lambda _name: (str(installed), ""))
    status = spoofer.get_loading_status()
    assert status["driver_installed"]
    assert status["binary_matches"] is False
    assert not status["driver_active"]

    installed.write_bytes(b"trusted driver")
    status = spoofer.get_loading_status()
    assert status["binary_matches"] is True
    assert status["driver_active"]


def test_reload_refuses_unverified_installed_service(tmp_path, monkeypatch):
    bundled = tmp_path / "Kernel.sys"
    bundled.write_bytes(b"driver")
    iface = spoofer.DriverInterface(
        path=str(bundled), installed=True, service_state="stopped",
        service_path=str(tmp_path / "other.sys"), binary_matches=False,
        error="binary mismatch")
    monkeypatch.setattr(spoofer, "get_driver", lambda: iface)
    monkeypatch.setattr(spoofer, "is_admin", lambda: True)
    result = spoofer.reload_driver()
    assert not result.loaded
    assert result.error == "binary mismatch"


def test_spoofers_do_not_claim_simulated_success(monkeypatch):
    inactive = spoofer.DriverInterface(
        loaded=False, path="missing", installed=False,
        error="driver unavailable")
    monkeypatch.setattr(spoofer, "get_driver", lambda: inactive)
    monkeypatch.setattr(spoofer, "reload_driver", lambda: inactive)
    monkeypatch.setattr(spoofer, "audit_log", lambda *_a, **_k: None)
    monkeypatch.setattr(spoofer, "audit_log_no_create_task", lambda *_a, **_k: None)
    assert not spoofer.get_gpu_spoofer().spoof()["success"]
    smbios = spoofer.get_smbios_spoofer().spoof(1)
    assert not smbios["success"]
    assert "registry fallback" in smbios["error"]
    assert not spoofer.get_disk_spoofer().spoof("é")["success"]


def test_reticle_invalid_numbers_and_nested_preset_directory(tmp_path):
    geo = reticle.shape_geometry("bad", float("nan"), float("inf"), 2, "bad")
    assert geo["lines"]
    nested = tmp_path / "nested" / "configs"
    assert reticle.save_preset(
        "test", {"reticle_size": 20, "unrelated": 1}, nested)
    assert reticle.load_preset("test", nested) == {"reticle_size": 20}
