import json
from types import SimpleNamespace

import groq
import httpx
import pytest

import llm_utils
from llm_utils import call_llm_json


def fake_response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def make_client(create_fn):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_fn)))


def make_rate_limit_error(retry_after: str | None = None) -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(429, headers=headers, request=request)
    return groq.RateLimitError("rate limited", response=response, body=None)


# -- happy path ---------------------------------------------------------------

def test_call_llm_json_parses_valid_response():
    client = make_client(lambda **_: fake_response(json.dumps({"score": 9})))
    data = call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")
    assert data == {"score": 9}


# -- malformed JSON retry -------------------------------------------------------

def test_call_llm_json_retries_once_on_bad_json_then_succeeds():
    calls = {"n": 0}

    def create(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            return fake_response("not json")
        return fake_response(json.dumps({"score": 7}))

    client = make_client(create)
    data = call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")
    assert data == {"score": 7}
    assert calls["n"] == 2


def test_call_llm_json_raises_json_decode_error_after_exhausting_retries():
    client = make_client(lambda **_: fake_response("still not json"))
    with pytest.raises(json.JSONDecodeError):
        call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")


# -- rate limit retry -----------------------------------------------------------

def test_call_llm_json_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(llm_utils.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def create(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error()
        return fake_response(json.dumps({"score": 5}))

    client = make_client(create)
    data = call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")
    assert data == {"score": 5}
    assert calls["n"] == 2


def test_call_llm_json_honors_retry_after_header(monkeypatch):
    waits = []
    monkeypatch.setattr(llm_utils.time, "sleep", lambda s: waits.append(s))
    calls = {"n": 0}

    def create(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error(retry_after="5")
        return fake_response(json.dumps({"score": 1}))

    client = make_client(create)
    call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")
    assert waits == [5.0]


def test_call_llm_json_gives_up_after_max_rate_limit_retries(monkeypatch):
    monkeypatch.setattr(llm_utils.time, "sleep", lambda _: None)
    client = make_client(lambda **_: (_ for _ in ()).throw(make_rate_limit_error()))
    with pytest.raises(groq.RateLimitError):
        call_llm_json(client, model="m", system_prompt="sys", user_prompt="usr")
