import json
import os
import re
from typing import Any

from utils import settings

PROVIDERS: dict[str, tuple[str | None, str | None, str | None]] = {
    "gemini": ("GEMINI_API_KEY", None, "gemini-2.0-flash"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat"),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "openai": ("OPENAI_API_KEY", None, "gpt-4o-mini"),
    "local": (None, "http://localhost:11434/v1", None),
}


def parse_json_response(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class LlmClient:
    def __init__(self, purpose: str) -> None:
        config = settings.llm_settings(purpose)
        provider = config["provider"]
        key_variable, default_base_url, default_model = PROVIDERS[provider]
        api_key = os.environ[key_variable] if key_variable else "not-needed"
        self.provider = provider
        self.model = config["model"] or default_model
        if self.model is None:
            raise RuntimeError(f"{purpose.upper()}_LLM_MODEL is required for the local provider")
        if provider == "gemini":
            from google import genai

            self.sdk_client = genai.Client(api_key=api_key)
        else:
            from openai import OpenAI

            self.sdk_client = OpenAI(api_key=api_key, base_url=config["base_url"] or default_base_url)

    def complete_json(self, messages: list[dict[str, str]], temperature: float = 0.0) -> dict[str, Any]:
        if self.provider == "gemini":
            from google.genai import types

            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            contents = [
                types.Content(
                    role="model" if m["role"] == "assistant" else "user",
                    parts=[types.Part(text=m["content"])],
                )
                for m in messages
                if m["role"] != "system"
            ]
            response = self.sdk_client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system or None,
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            return parse_json_response(response.text or "{}")
        response = self.sdk_client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=temperature,
        )
        return parse_json_response(response.choices[0].message.content or "{}")
