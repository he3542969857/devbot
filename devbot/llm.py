"""LLM client — mock + OpenAI-compatible (SiliconFlow / DeepSeek / etc.)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import LlmCfg, get_settings


@dataclass
class LlmResponse:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str


class LlmClient:
    def __init__(self, cfg: LlmCfg | None = None):
        self.cfg = cfg or get_settings().llm

    def chat(self, messages: list[dict[str, str]], *,
             model_key: str = "default", max_tokens: int = 1024,
             temperature: float = 0.2) -> LlmResponse:
        model = self.cfg.models.get(model_key, self.cfg.models["default"])
        if self.cfg.provider == "mock":
            return self._mock(messages, model)
        return self._openai(messages, model, max_tokens, temperature)

    def _openai(self, messages, model, max_tokens, temperature) -> LlmResponse:
        from openai import OpenAI
        client = OpenAI(base_url=self.cfg.base_url, api_key=self.cfg.api_key)
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        latency = int((time.time() - t0) * 1000)
        choice = resp.choices[0]
        usage = resp.usage
        return LlmResponse(
            text=(choice.message.content or "").strip(),
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            latency_ms=latency,
            model=model,
        )

    def _mock(self, messages, model) -> LlmResponse:
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")

        if "risk_score" in system_msg and "findings" in system_msg:
            text = json.dumps({
                "risk_score": 45,
                "confidence": 0.8,
                "findings": [{"file": "Main.java", "line": 10,
                              "severity": "warn", "message": "mock finding"}],
                "suggestion": "mock suggestion",
            })
        elif "subtask" in system_msg or "requirement" in system_msg.lower():
            text = json.dumps({
                "subtasks": [
                    {"id": "T1", "title": "Design API schema", "effort": "S"},
                    {"id": "T2", "title": "Implement backend logic", "effort": "M"},
                    {"id": "T3", "title": "Add unit tests", "effort": "S"},
                ],
                "impact_modules": ["api", "service", "repository"],
                "acceptance_criteria": [
                    "API returns 200 on valid input",
                    "Invalid input returns 400 with error details",
                    "Unit test coverage >= 80%",
                ],
                "risk_notes": [
                    "Cross-module dependency on auth service",
                    "Missing error handling specification",
                ],
                "estimated_effort": "M",
            })
        elif "generate code" in system_msg.lower() or "code generation" in system_msg.lower():
            lang = "python"
            if "java" in user_msg.lower() or "java" in system_msg.lower():
                lang = "java"
            if lang == "java":
                text = json.dumps({
                    "generated_code": "public class Handler {\n    public String handle(String input) {\n        return input.trim();\n    }\n}",
                    "language": "java",
                    "explanation": "Mock generated code for the requested task.",
                })
            else:
                text = json.dumps({
                    "generated_code": "def handle(input_data: str) -> str:\n    return input_data.strip()",
                    "language": "python",
                    "explanation": "Mock generated code for the requested task.",
                })
        elif "validate" in system_msg.lower() or "review" in system_msg.lower() and "code" in system_msg.lower():
            text = json.dumps({
                "is_valid": True,
                "validation_notes": "Code follows best practices. No issues found.",
                "suggestions": [],
            })
        elif "test" in system_msg.lower() and ("generate" in system_msg.lower() or "skeleton" in system_msg.lower()):
            text = json.dumps({
                "generated_tests": "import pytest\n\ndef test_handle_valid_input():\n    assert handle('  hello  ') == 'hello'\n\ndef test_handle_empty_input():\n    assert handle('') == ''\n\ndef test_handle_none():\n    with pytest.raises(AttributeError):\n        handle(None)\n",
                "test_count": 3,
                "target_methods": ["handle"],
                "language": "python",
            })
        elif "analyze" in system_msg.lower() and "diff" in system_msg.lower():
            text = json.dumps({
                "target_methods": ["handle", "process"],
                "boundary_scenarios": [
                    "empty input",
                    "null input",
                    "very long input",
                ],
            })
        else:
            text = f"[mock-llm] {len(user_msg)} chars"

        return LlmResponse(text=text, tokens_in=len(user_msg) // 4,
                           tokens_out=len(text) // 4, latency_ms=50, model=model)
