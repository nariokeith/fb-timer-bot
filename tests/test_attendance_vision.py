import base64
import inspect
import json
import re

import pytest

from attendance_vision import MODEL, VisionError, extract_names
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
    # coupled to whatever attendance_vision.extract_names actually calls.
    source = inspect.getsource(extract_names)
    match = re.search(r"client\.(\w+)\.(\w+)\(", source)
    assert match, "could not find a client.<attr>.<method>(...) call in extract_names"
    attr_name, method_name = match.groups()

    installed_version = getattr(genai, "__version__", "unknown")
    client = genai.Client(api_key="dummy-key-construction-only-no-network-call")

    outer = getattr(client, attr_name, None)
    assert outer is not None, (
        f"installed google-genai=={installed_version} has no `{attr_name}` "
        f"attribute on Client -- the pinned SDK is too old for the "
        f"Interactions API that attendance_vision.extract_names() calls"
    )

    method = getattr(outer, method_name, None)
    assert callable(method), (
        f"installed google-genai=={installed_version}: `{attr_name}` exists "
        f"but has no callable `{method_name}` -- the pinned SDK is too old "
        f"for the Interactions API that attendance_vision.extract_names() calls"
    )


def test_returns_the_names_the_model_reported():
    client = FakeGeminiClient(
        output_text=json.dumps({"names": ["Kobe", "Talong", "fLuffy"]})
    )
    assert extract_names(IMAGE, "image/png", client=client) == [
        "Kobe", "Talong", "fLuffy",
    ]


def test_sends_the_image_base64_encoded_with_its_mime_type():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe"]}))
    extract_names(IMAGE, "image/png", client=client)

    call = client.calls[0]
    assert call["model"] == MODEL

    image_part = next(p for p in call["input"] if p["type"] == "image")
    assert image_part["mime_type"] == "image/png"
    assert base64.b64decode(image_part["data"]) == IMAGE


def test_requests_json_constrained_to_the_schema():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe"]}))
    extract_names(IMAGE, "image/png", client=client)

    fmt = client.calls[0]["response_format"]
    assert fmt["mime_type"] == "application/json"
    assert fmt["schema"]["properties"]["names"]["type"] == "array"


def test_empty_result_is_an_error_not_a_silent_no_op():
    client = FakeGeminiClient(output_text=json.dumps({"names": []}))
    with pytest.raises(VisionError, match="no names"):
        extract_names(IMAGE, "image/png", client=client)


def test_unparseable_response_raises():
    client = FakeGeminiClient(output_text="I'm sorry, I can't read that image.")
    with pytest.raises(VisionError, match="not valid JSON"):
        extract_names(IMAGE, "image/png", client=client)


def test_response_missing_the_names_key_raises():
    client = FakeGeminiClient(output_text=json.dumps({"players": ["Kobe"]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        extract_names(IMAGE, "image/png", client=client)


def test_non_string_entries_are_rejected():
    client = FakeGeminiClient(output_text=json.dumps({"names": ["Kobe", 42]}))
    with pytest.raises(VisionError, match="unexpected shape"):
        extract_names(IMAGE, "image/png", client=client)


def test_api_failure_is_wrapped_in_vision_error():
    client = FakeGeminiClient(error=RuntimeError("429 quota exceeded"))
    with pytest.raises(VisionError, match="quota exceeded"):
        extract_names(IMAGE, "image/png", client=client)


def test_blank_names_are_dropped():
    client = FakeGeminiClient(
        output_text=json.dumps({"names": ["Kobe", "  ", "", "Talong"]})
    )
    assert extract_names(IMAGE, "image/png", client=client) == ["Kobe", "Talong"]
