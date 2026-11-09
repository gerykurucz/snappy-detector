"""Unit tests for macro / recording spec parsing in input_utils."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import input as iu

def test_parse_macro_spec_basic():
    assert iu._parse_macro_spec("ctrl+c") == (0x0002, (0x43,))
    assert iu._parse_macro_spec("f5") == (0, (0x74,))
    assert iu._parse_macro_spec("ctrl+shift+s") == (0x0006, (0x53,))
    assert iu._parse_macro_spec("space") == (0, (0x20,))


def test_parse_macro_spec_invalid():
    assert iu._parse_macro_spec("") is None
    assert iu._parse_macro_spec(None) is None
    # more than one key is invalid
    assert iu._parse_macro_spec("ctrl+c+v") is None
    # unknown key
    assert iu._parse_macro_spec("ctrl+notakey") is None


def test_parse_spec_controller():
    assert iu.parse_spec("a") is not None
    assert iu.parse_spec("rb+a") is not None
    assert iu.parse_spec("") is None
    assert iu.parse_spec("notabutton") is None


def test_validate_steps_key():
    steps = iu.validate_steps([[0, 2, 0x41, 100]])
    assert steps == [(iu.STEP_KEY, 2, 0x41, 100)]


def test_validate_steps_moves_and_buttons():
    steps = iu.validate_steps([
        [1, 100, 200, 50],          # STEP_MOVE, x, y, delay
        [2, 0, 0, 100, 100, 0],     # STEP_BUTTON, btn, phase, x, y, delay
    ])
    assert steps[0][0] == iu.STEP_MOVE
    assert steps[1][0] == iu.STEP_BUTTON


def test_validate_steps_rejects_bad():
    assert iu.validate_steps([]) is None
    assert iu.validate_steps("notalist") is None
    assert iu.validate_steps([[99]]) is None  # bad tag


def test_parse_macro_sequence_ordered_repeat():
    # Ordered taps, repeats allowed.
    assert iu._parse_macro_sequence("S+M+Shift") == (0x53, 0x4D, 0x10)
    assert iu._parse_macro_sequence("Shift+Ctrl+Shift") == (0x10, 0x11, 0x10)
    assert iu._parse_macro_sequence("a") == (0x41,)
    assert iu._parse_macro_sequence("ctrl") == (0x11,)
    # Empty / too long / unknown -> None.
    assert iu._parse_macro_sequence("") is None
    assert iu._parse_macro_sequence(None) is None
    assert iu._parse_macro_sequence("+".join(["a"] * 9)) is None
    assert iu._parse_macro_sequence("notakey") is None


def test_parse_macro_action():
    assert iu.parse_macro_action("S+M+Shift") == ("key", (0x53, 0x4D, 0x10))
    assert iu.parse_macro_action("ctrl+c") == ("key", (0x11, 0x43))
    act = iu.parse_macro_action("pad:a+b")
    assert act is None or (isinstance(act, tuple) and act[0] == "pad")
    assert iu.parse_macro_action("") is None
    assert iu.parse_macro_action(None) is None


def test_build_hotkey_table_assist_off():
    cfg = {"key_assist_off": "ctrl+alt+o, end"}
    always, local = iu.build_hotkey_table(cfg)
    ids = {hid for hid, _, _ in always} | {hid for hid, _ in local}
    assert 40 in ids  # ASSIST_OFF_KEY_IDS[0]


def test_build_action_bindings_pad_assist_off():
    cfg = {"pad_assist_off": "back, lb+rb"}
    bindings = iu.build_action_bindings(cfg)
    specs = {b.spec for b in bindings}
    assert "back" in specs
    assert "lb+rb" in specs
