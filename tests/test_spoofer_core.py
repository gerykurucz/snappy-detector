"""Unit tests for the flat spoofer core modules."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spoofer as audit
import spoofer as driver
import spoofer as priv
import spoofer as ext
import spoofer as verify
def test_audit_event_type_values():
    assert audit.AuditEventType.SPOOF_SUCCESS.value == "spoof_success"
    assert audit.AuditEventType.SPOOF_FAILURE.value == "spoof_failure"


def test_driver_status_shape():
    status = driver.get_loading_status()
    assert "driver_active" in status
    assert "driver_file" in status
    assert isinstance(status["driver_active"], bool)


def test_privilege_is_admin_bool():
    assert isinstance(priv.is_admin(), bool)


def test_external_tools_detect_shape():
    found = ext.detect()
    assert isinstance(found, dict)
    assert "volumeid" in found


def test_verification_result_shape():
    res = verify.run_verification()
    assert "os" in res
    assert "admin" in res
    assert "driver" in res
