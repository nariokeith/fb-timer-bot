import base64
import inspect
import json
import re

import pytest

from attendance_vision import MODEL, RosterRead, VisionError, read_roster
from conftest import FakeGeminiClient

IMAGE = b"\x89PNG\r\n\x1a\n fake image bytes"


def test_installed_sdk_actually_has_the_interactions_api_the_module_calls():
    """Construction-only check against the REAL google-genai package.

    Every other test in this file injects FakeGeminiClient, which always
    has `.interactions` by construction -- so a fully green mocked suite
    proves nothing about whether the *installed* SDK version actually
    supports the Interactions API. google-genai==1.44.0 has no
    `client.interactions` attribute at all; this test is the one place
    that would catch a bad pin like that.

    `genai.Client(api_key=...)` only builds a client object locally and
    makes no network call, so this runs safely offline and without
    GEMINI_API_KEY set.
    """
    from google import genai

    # Derive the attribute chain from the module's own source, rather than
    # hardcoding "interactions"/"create" as strings, so this test stays
    # coupled to whatever attendance_vision.read_roster actually calls.
    source = inspect.getsource(read_roster)
    match = re.search(r"client\.(\w+)\.(\w+)\(", source)
    assert match, "could not find a client.<attr>.<method>(...) call in read_roster"
    attr_name, method_name = match.groups()

    installed_version = getattr(genai, "__version__", "unknown")
    client = genai.Client(api_key="dummy-key-construction-only-no-network-call")

    outer = getattr(client, attr_name, None)
    assert outer is not None, (
        f"installed google-genai=={installed_version} has no `{attr_name}` "
        f"attribute on Client -- the pinned SDK is too old for the "
        f"Interactions API that attendance_vision.read_roster() calls"
    )

    method = getattr(outer, method_name, None)
    assert callable(method), (
        f"installed google-genai=={installed_version}: `{attr_name}` exists "
        f"but has no callable `{method_name}` -- the pinned SDK is too old "
        f"for the Interactions API that attendance_vision.read_roster() calls"
    )


def test_returns_the_active_and_dimmed_names_the_model_reported():
    client = FakeGeminiClient(
        output_text=json.dumps({
            "players": [
                {"name": "Kobe", "status": "active"},
                {"name": "Talong", "status": "dimmed"},
                {"name": "fLuffy", "status": "active"},
            ]
        })
    )
    assert read_roster(IMAGE, "image/png", client=client) == RosterRead(
        active=["Kobe", "fLuffy"], dimmed=["Talong"]
    )


def test_sends_the_image_base64_encoded_with_its_mime_type():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": "active"}
    ]}))
    read_roster(IMAGE, "image/png", client=client)

    call = client.calls[0]
    assert call["model"] == MODEL

    image_part = next(p for p in call["input"] if p["type"] == "image")
    assert image_part["mime_type"] == "image/png"
    assert base64.b64decode(image_part["data"]) == IMAGE


def test_requests_json_constrained_to_the_schema():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": "active"}
    ]}))
    read_roster(IMAGE, "image/png", client=client)

    fmt = client.calls[0]["response_format"]
    assert fmt["mime_type"] == "application/json"
    players = fmt["schema"]["properties"]["players"]
    assert players["type"] == "array"
    assert players["items"]["properties"]["status"]["enum"] == ["active", "dimmed"]


def test_empty_result_is_an_error_not_a_silent_no_op():
    client = FakeGeminiClient(output_text=json.dumps({"players": []}))
    with pytest.raises(VisionError, match="no names"):
        read_roster(IMAGE, "image/png", client=client)


def test_unparseable_response_raises():
    client = FakeGeminiClient(output_text="I'm sorry, I can't read that image.")
    with pytest.raises(VisionError, match="not valid JSON"):
        read_roster(IMAGE, "image/png", client=client)


def test_response_missing_the_names_key_raises():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe"]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        read_roster(IMAGE, "image/png", client=client)


def test_non_string_entries_are_rejected():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": "active"},
        {"name": 42, "status": "active"},
    ]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        read_roster(IMAGE, "image/png", client=client)


def test_api_failure_is_wrapped_in_vision_error():
    client = FakeGeminiClient(error=RuntimeError("429 quota exceeded"))
    with pytest.raises(VisionError, match="quota exceeded"):
        read_roster(IMAGE, "image/png", client=client)


def test_blank_names_are_dropped():
    client = FakeGeminiClient(
        output_text=json.dumps({"players": [
            {"name": "Kobe", "status": "active"},
            {"name": "  ", "status": "dimmed"},
            {"name": "", "status": "active"},
            {"name": "Talong", "status": "dimmed"},
        ]})
    )
    assert read_roster(IMAGE, "image/png", client=client) == RosterRead(
        active=["Kobe"], dimmed=["Talong"]
    )


def test_status_casing_from_the_model_does_not_block_the_whole_command():
    """"Active" must read as active rather than aborting the command.

    An unrecognised status raises, which aborts the entire !attendance run
    -- correct for a status nobody can interpret, but far too harsh for a
    reply that differs only in casing. The schema's enum should prevent
    this, yet structured-output enforcement varies across model versions,
    and GEMINI_MODEL is deliberately overridable. Refusing to log anything
    at all because the model capitalised a word it plainly meant would be
    a self-inflicted outage, so casing is normalised before the check.
    """
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": "Active"},
        {"name": "LOOKatLOOK", "status": "DIMMED"},
    ]}))
    assert read_roster(IMAGE, "image/png", client=client) == RosterRead(
        active=["Kobe"], dimmed=["LOOKatLOOK"]
    )


def test_surrounding_whitespace_in_a_status_is_tolerated():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": " active "},
    ]}))
    assert read_roster(IMAGE, "image/png", client=client) == RosterRead(
        active=["Kobe"], dimmed=[]
    )


def test_unknown_status_is_rejected_instead_of_treated_as_active():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "Kobe", "status": "unclear"}
    ]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        read_roster(IMAGE, "image/png", client=client)


def test_every_name_dimmed_explains_why_nothing_was_read():
    client = FakeGeminiClient(output_text=json.dumps({"players": [
        {"name": "LOOKatLOOK", "status": "dimmed"}
    ]}))
    with pytest.raises(VisionError, match="dimmed"):
        read_roster(IMAGE, "image/png", client=client)
