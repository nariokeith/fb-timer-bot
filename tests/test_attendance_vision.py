import base64
import json

import pytest

from attendance_vision import MODEL, VisionError, extract_names
from conftest import FakeGeminiClient

IMAGE = b"\x89PNG\r\n\x1a\n fake image bytes"


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
