"""
Shared helper to build an LLM client from config + env.

Supported providers:
  gemini   - Google Gemini via google-genai SDK (supports AQ keys, free tier)
  groq     - Groq (free tier, low latency)
  deepseek - DeepSeek (low cost)
  openai   - OpenAI

All providers expose an OpenAI-compatible interface:
  client.chat.completions.create(model=..., messages=[...], ...)
  → response with .choices[0].message.content and .usage
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


# ── Gemini native SDK wrapper ─────────────────────────────────────────────────
# google-genai SDK uses AQ keys; the OpenAI-compatible endpoint does not.
# This wrapper makes google-genai look like an OpenAI client so the rest of
# the codebase (extract_product_attributes.py, extract_review_mentions.py, etc.)
# needs no changes.

class _GeminiMessage:
    def __init__(self, text: str) -> None:
        self.content = text


class _GeminiChoice:
    def __init__(self, text: str) -> None:
        self.message = _GeminiMessage(text)


class _GeminiUsage:
    def __init__(self, meta: Any) -> None:
        self.input_tokens  = int(getattr(meta, "prompt_token_count",     0) or 0)
        self.output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
        self.total_tokens  = int(getattr(meta, "total_token_count",      0) or 0)

    def model_dump(self) -> dict[str, int]:
        return {
            "input_tokens":  self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens":  self.total_tokens,
        }


class _GeminiResponse:
    def __init__(self, resp: Any) -> None:
        self.choices = [_GeminiChoice(getattr(resp, "text", "") or "")]
        self.usage   = _GeminiUsage(getattr(resp, "usage_metadata", None))


class _GeminiCompletions:
    def __init__(self, client: Any, types_mod: Any) -> None:
        self._client = client
        self._types  = types_mod

    def create(
        self,
        model: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0,
        max_tokens: int | None = None,
        **_: Any,
    ) -> _GeminiResponse:
        types = self._types

        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        system_instruction = system_parts[0] if system_parts else None

        contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = "model" if m["role"] == "assistant" else "user"
            contents.append(
                types.Content(role=role, parts=[types.Part(text=m["content"])])
            )

        mime = (
            "application/json"
            if (response_format or {}).get("type") == "json_object"
            else None
        )
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type=mime,
        )

        resp = self._client.models.generate_content(
            model=model, contents=contents, config=config
        )
        return _GeminiResponse(resp)


class _GeminiChat:
    def __init__(self, completions: _GeminiCompletions) -> None:
        self.completions = completions


class _GeminiCompat:
    """OpenAI-compatible façade around google-genai."""

    def __init__(self, api_key: str) -> None:
        try:
            from google import genai as _genai
            from google.genai import types as _types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai not installed. Run: pip install google-genai"
            ) from exc

        _client = _genai.Client(api_key=api_key)
        self.chat = _GeminiChat(_GeminiCompletions(_client, _types))


# ── provider table ────────────────────────────────────────────────────────────

# gemini base_url is None because it uses the native SDK, not OpenAI SDK
_PROVIDER_CONFIG: dict[str, tuple[str, str | None, str]] = {
    "gemini": (
        "GEMINI_API_KEY",
        None,
        "gemini-2.0-flash",
    ),
    "groq": (
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
    ),
    "deepseek": (
        "DEEPSEEK_API_KEY",
        os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "deepseek-chat",
    ),
    "openai": (
        "OPENAI_API_KEY",
        None,
        "gpt-4o-mini",
    ),
    # Ollama: local server, OpenAI-compatible, no auth required
    "ollama": (
        "OLLAMA_API_KEY",
        os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "llama3.2",
    ),
}


# ── helpers ───────────────────────────────────────────────────────────────────

def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key   = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def build_client(
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[Any, str]:
    """Return (client, resolved_model). Client exposes OpenAI-compatible interface."""
    load_env_file()

    provider = provider.lower().strip()
    if provider not in _PROVIDER_CONFIG:
        print(
            f"Unknown provider '{provider}'. Choose from: {', '.join(_PROVIDER_CONFIG)}",
            file=sys.stderr,
        )
        sys.exit(2)

    env_key, default_base_url, default_model = _PROVIDER_CONFIG[provider]
    resolved_model = model or default_model

    api_key = os.environ.get(env_key)
    if not api_key:
        if provider == "ollama":
            api_key = "ollama"  # Ollama ignores the key; OpenAI SDK requires a non-empty string
        else:
            print(
                f"API key not found. Set {env_key} in your .env file.\n"
                f"  cp .env.example .env  # then fill in {env_key}",
                file=sys.stderr,
            )
            sys.exit(2)

    if provider == "gemini":
        return _GeminiCompat(api_key), resolved_model

    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed. Run: pip install openai", file=sys.stderr)
        sys.exit(2)

    resolved_base_url = base_url or default_base_url
    kwargs: dict[str, Any] = {"api_key": api_key}
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url

    return OpenAI(**kwargs), resolved_model


def provider_from_config(llm_cfg: dict) -> tuple[str, str | None, str | None]:
    """Extract LLM settings, with environment variables taking precedence.

    ``config.yaml`` holds portable defaults.  A deployment can override only the
    values it needs through ``LLM_PROVIDER``, ``LLM_MODEL``, and
    ``LLM_BASE_URL`` (for example, a Kubera vLLM endpoint) without changing a
    tracked file.
    """
    load_env_file()
    provider = os.environ.get("LLM_PROVIDER") or str(llm_cfg.get("provider", "gemini"))
    model = os.environ.get("LLM_MODEL") or llm_cfg.get("model") or None
    base_url = os.environ.get("LLM_BASE_URL") or llm_cfg.get("base_url") or None
    return provider, model, base_url
