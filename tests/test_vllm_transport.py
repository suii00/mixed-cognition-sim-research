import copy
import json
import unittest
from unittest import mock

import requests

from engine.llm_client import (
    LLMTransportError,
    build_vllm_chat_payload,
    call_vllm,
)
from engine.parallel_transport import LLMRequest
from engine.sim import Simulation


class FakeResponse:
    def __init__(
        self,
        envelope,
        *,
        status_code=200,
        body=None,
        http_error=None,
        json_error=None,
    ):
        self.envelope = envelope
        self.status_code = status_code
        self.http_error = http_error
        self.json_error = json_error
        self.content = (
            body
            if body is not None
            else json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        )

    def raise_for_status(self):
        if self.http_error is not None:
            raise self.http_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.envelope


def envelope(content):
    return {
        "id": "chatcmpl-test",
        "model": "llama-3.1-8b-instruct",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }


def request(provider="vllm"):
    return LLMRequest(
        request_id="step-000001:phase1:agent-000000",
        step=1,
        phase="phase1",
        agent_id=0,
        model="llama-3.1-8b-instruct",
        base_url="http://127.0.0.1:8001",
        prompt="exact prompt",
        temperature=0.0,
        max_tokens=64,
        timeout_s=9,
        provider=provider,
        llm_overrides={"top_p": 0.9, "seed": 7},
    )


class VllmTransportTests(unittest.TestCase):
    def test_payload_is_fixed_openai_compatible_contract(self):
        overrides = {
            "top_p": 0.9,
            "seed": 7,
            "response_format": {"type": "json_object"},
        }
        payload = build_vllm_chat_payload(
            prompt="exact prompt",
            model="llama-3.1-8b-instruct",
            temperature=0.0,
            max_tokens=64,
            llm_overrides=overrides,
        )
        self.assertEqual(payload, {
            "model": "llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "exact prompt"}],
            "temperature": 0.0,
            "max_tokens": 64,
            "stream": False,
            "top_p": 0.9,
            "seed": 7,
            "response_format": {"type": "json_object"},
        })
        payload["stop"] = ["mutation"]
        self.assertEqual(overrides, {
            "top_p": 0.9,
            "seed": 7,
            "response_format": {"type": "json_object"},
        })

    def test_response_format_is_bounded_to_json_object_mode(self):
        invalid_values = (
            "json_object",
            {"type": "text"},
            {"type": "json_schema", "json_schema": {}},
            {"type": "json_object", "extra": True},
        )
        for value in invalid_values:
            with self.subTest(value=value), mock.patch(
                "engine.llm_client.requests.post"
            ) as post:
                with self.assertRaisesRegex(ValueError, "response_format"):
                    call_vllm(
                        prompt="prompt",
                        model="model",
                        base_url="http://127.0.0.1:8001",
                        llm_overrides={"response_format": value},
                    )
                post.assert_not_called()

    def test_unknown_or_reserved_overrides_fail_before_http(self):
        for key in ("num_ctx", "num_predict", "messages", "temperature"):
            with self.subTest(key=key), mock.patch(
                "engine.llm_client.requests.post"
            ) as post:
                with self.assertRaisesRegex(ValueError, key):
                    call_vllm(
                        prompt="prompt",
                        model="model",
                        base_url="http://127.0.0.1:8001",
                        llm_overrides={key: 1},
                    )
                post.assert_not_called()

    def test_success_maps_choices_content_and_preserves_observers(self):
        response = FakeResponse(envelope('{"message":"ok"}'))
        observed = []
        exchanges = []
        events = []
        with mock.patch(
            "engine.llm_client.requests.post", return_value=response
        ) as post:
            parsed, raw = call_vllm(
                prompt="exact prompt",
                model="llama-3.1-8b-instruct",
                base_url="http://127.0.0.1:8001",
                temperature=0.0,
                max_tokens=64,
                timeout_s=9,
                llm_overrides={"top_p": 0.9, "seed": 7},
                telemetry=lambda event, amount: events.extend([event] * amount),
                response_observer=observed.append,
                http_response_observer=lambda status, body: exchanges.append(
                    (status, body)
                ),
            )

        self.assertEqual(parsed, {"message": "ok"})
        self.assertEqual(raw, '{"message":"ok"}')
        self.assertEqual(events, ["http_attempt"])
        self.assertEqual(exchanges, [(200, response.content)])
        self.assertEqual(observed, [response.envelope])
        self.assertIsNot(observed[0], response.envelope)
        post.assert_called_once_with(
            "http://127.0.0.1:8001/v1/chat/completions",
            json={
                "model": "llama-3.1-8b-instruct",
                "messages": [{"role": "user", "content": "exact prompt"}],
                "temperature": 0.0,
                "max_tokens": 64,
                "stream": False,
                "top_p": 0.9,
                "seed": 7,
            },
            timeout=9,
        )

    def test_invalid_json_is_not_retried(self):
        events = []
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=[
                FakeResponse(envelope("not json")),
                FakeResponse(envelope('{"message":"recovered"}')),
            ],
        ):
            parsed, raw = call_vllm(
                prompt="prompt",
                model="model",
                base_url="http://127.0.0.1:8001",
                telemetry=lambda event, amount: events.extend([event] * amount),
            )
        self.assertIsNone(parsed)
        self.assertEqual(raw, "not json")
        self.assertEqual(events.count("http_attempt"), 1)
        self.assertEqual(events.count("generation_retry"), 0)
        self.assertEqual(events.count("syntax_parse_attempt_failure"), 1)

    def test_malformed_envelope_is_terminal_transport_failure(self):
        for malformed in ([], {}, {"choices": []}, {"choices": [{}]}):
            with self.subTest(malformed=malformed):
                events = []
                with mock.patch(
                    "engine.llm_client.requests.post",
                    return_value=FakeResponse(malformed),
                ):
                    with self.assertRaises(LLMTransportError):
                        call_vllm(
                            prompt="prompt",
                            model="model",
                            base_url="http://127.0.0.1:8001",
                            telemetry=lambda event, amount: events.extend(
                                [event] * amount
                            ),
                        )
                self.assertEqual(events, ["http_attempt", "transport_failure"])

    def test_invalid_json_envelope_is_terminal_transport_failure(self):
        events = []
        response = FakeResponse(
            None,
            body=b"not json",
            json_error=ValueError("decode details"),
        )
        with mock.patch(
            "engine.llm_client.requests.post",
            return_value=response,
        ):
            with self.assertRaisesRegex(LLMTransportError, "invalid JSON"):
                call_vllm(
                    prompt="prompt",
                    model="model",
                    base_url="http://127.0.0.1:8001",
                    telemetry=lambda event, amount: events.extend(
                        [event] * amount
                    ),
                )
        self.assertEqual(events, ["http_attempt", "transport_failure"])

    def test_http_error_is_observed_and_terminal(self):
        response = FakeResponse(
            {"error": "unavailable"},
            status_code=503,
            body=b'{"error":"unavailable"}',
            http_error=requests.HTTPError("sensitive response"),
        )
        exchanges = []
        events = []
        with mock.patch(
            "engine.llm_client.requests.post", return_value=response
        ):
            with self.assertRaises(LLMTransportError):
                call_vllm(
                    prompt="prompt",
                    model="model",
                    base_url="http://127.0.0.1:8001",
                    telemetry=lambda event, amount: events.extend([event] * amount),
                    http_response_observer=lambda status, body: exchanges.append(
                        (status, body)
                    ),
                )
        self.assertEqual(exchanges, [(503, response.content)])
        self.assertEqual(events, ["http_attempt", "transport_failure"])

    def test_default_dispatch_selects_vllm_and_owns_overrides(self):
        item = request()
        expected_overrides = copy.deepcopy(item.llm_overrides)
        with mock.patch(
            "engine.sim.call_vllm", return_value=({"message": "ok"}, "{}")
        ) as transport, mock.patch("engine.sim.call_ollama") as ollama:
            result = Simulation._default_transport(item, lambda *_: None)
        self.assertEqual(result[0], {"message": "ok"})
        ollama.assert_not_called()
        self.assertEqual(transport.call_args.kwargs["llm_overrides"], expected_overrides)
        self.assertIsNot(transport.call_args.kwargs["llm_overrides"], item.llm_overrides)

    def test_request_rejects_unknown_provider(self):
        with self.assertRaisesRegex(ValueError, "provider"):
            request("unknown")


if __name__ == "__main__":
    unittest.main()
