from researcher import llm


def test_extract_json_fenced_last_block():
    text = "thinking aloud...\n```json\n{\"a\": 1}\n```\nmore prose"
    assert llm.extract_json(text) == {"a": 1}


def test_extract_json_prefers_last_fence():
    text = "```json\n{\"a\": 1}\n```\n```json\n{\"b\": 2}\n```"
    assert llm.extract_json(text) == {"b": 2}


def test_extract_json_bare_object():
    assert llm.extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_object_in_prose():
    assert llm.extract_json('here is the answer: {"a": 1} thanks')
    assert llm.extract_json("no json here at all") is None


def test_extract_json_empty():
    assert llm.extract_json("") is None
    assert llm.extract_json("   ") is None


def test_same_model_prefix_matches():
    assert llm._same_model("claude-haiku-4-5", "claude-haiku-4-5")
    assert llm._same_model("anthropic/claude-haiku-4-5", "claude-haiku-4-5")
    assert not llm._same_model("claude-haiku-4-5", "gemini-3.1-pro-preview")


def test_api_key_fallback(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("Litellm", "sk-fallback")
    assert llm.api_key() == "sk-fallback"
    monkeypatch.setenv("LITELLM_API_KEY", "sk-primary")
    assert llm.api_key() == "sk-primary"
