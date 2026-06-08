"""LLM provider abstractions."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from .actions import Action, ActionParser, StopAction


@dataclass(slots=True)
class ProviderResponse:
    """Normalized response from a model provider."""

    content: str
    raw: Any | None = None


class BaseProvider:
    """Base class for chat-completion style providers."""

    provider_name = "base"
    base_url: str | None = None
    api_key_env: str | None = None
    model_aliases: dict[str, str] = {}

    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None) -> None:
        self.model = self.model_aliases.get(model, model)
        self.api_key = api_key or (os.getenv(self.api_key_env) if self.api_key_env else None)
        self.base_url = base_url or self.base_url

    async def get_action(self, messages: list[dict[str, Any]], *, temperature: float = 0.2) -> Action:
        """Return the next action for the agent."""

        response = await self.chat(messages, temperature=temperature)
        return ActionParser.parse(response.content)

    async def is_complete(self, messages: list[dict[str, Any]]) -> bool:
        """Heuristic completion check with an LLM-aware fallback."""

        response = await self.chat(messages, temperature=0.0)
        text = response.content.lower()
        return "complete" in text or "done" in text or "stop" in text

    async def chat(self, messages: list[dict[str, Any]], *, temperature: float = 0.2) -> ProviderResponse:
        """Perform a provider request or return a deterministic offline fallback."""

        if not self.base_url or not self.api_key:
            return ProviderResponse(content=self._offline_response(messages))

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url.rstrip('/')}/chat/completions", json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        return ProviderResponse(content=content, raw=data)

    def _offline_response(self, messages: list[dict[str, Any]]) -> str:
        """Generate a deterministic answer when no remote model is configured."""

        content = " ".join(str(message.get("content", "")) for message in messages)
        if "stop" in content.lower() or "complete" in content.lower():
            return json.dumps({"kind": "stop", "reason": "objective marked complete"})
        return json.dumps({"kind": "wait", "seconds": 0.1})


class OpenAIProvider(BaseProvider):
    """OpenAI-compatible provider."""

    provider_name = "openai"
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"
    model_aliases = {"gpt-4o": "gpt-4o", "gpt-4o-mini": "gpt-4o-mini"}


class AnthropicProvider(BaseProvider):
    """Anthropic provider."""

    provider_name = "anthropic"
    base_url = "https://api.anthropic.com/v1"
    api_key_env = "ANTHROPIC_API_KEY"
    model_aliases = {"claude-3.5-sonnet": "claude-3-5-sonnet-20241022"}

    async def chat(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.2
    ) -> Any:
        """Send messages to Anthropic API with screenshot as image content."""

        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self.api_key)

        anthropic_messages = []
        for msg in messages:
            if isinstance(msg.get("content"), list):
                anthropic_messages.append(msg)
            elif msg.get("screenshot_b64"):
                anthropic_messages.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": msg["screenshot_b64"],
                                },
                            },
                            {
                                "type": "text",
                                "text": msg.get("text", "What action should I take?"),
                            },
                        ],
                    }
                )
            else:
                anthropic_messages.append(msg)

        response = await client.messages.create(
            model=self.model,
            max_tokens=1024,
            temperature=temperature,
            messages=anthropic_messages,
        )

        class _Resp:
            def __init__(self, text):
                self.content = text

        return _Resp(response.content[0].text)


class GroqProvider(BaseProvider):
    """Groq OpenAI-compatible provider."""

    provider_name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    api_key_env = "GROQ_API_KEY"
    model_aliases = {"llama-3.3": "llama-3.3-70b-versatile"}


class MistralProvider(BaseProvider):
    """Mistral provider."""

    provider_name = "mistral"
    base_url = "https://api.mistral.ai/v1"
    api_key_env = "MISTRAL_API_KEY"
    model_aliases = {"mistral-large": "mistral-large-latest"}


class GeminiProvider(BaseProvider):
    """Gemini provider using the OpenAI-compatible endpoint."""

    provider_name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    api_key_env = "GEMINI_API_KEY"
    model_aliases = {"gemini-2.0-flash": "gemini-2.0-flash"}


class DeepSeekProvider(BaseProvider):
    """DeepSeek provider."""

    provider_name = "deepseek"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"


class OpenRouterProvider(BaseProvider):
    """OpenRouter provider."""

    provider_name = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"


class OllamaProvider(BaseProvider):
    """Ollama OpenAI-compatible provider with vision support."""

    provider_name = "ollama"
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    api_key_env = None
    model_aliases = {"llava": "llava"}

    def __init__(
        self,
        model: str = "llava",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        default_model = os.getenv("OLLAMA_MODEL", model)
        super().__init__(model=default_model, api_key=api_key or "", base_url=base_url or self.base_url)

    async def chat(self, messages: list[dict[str, Any]], *, temperature: float = 0.2) -> ProviderResponse:
        converted: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                converted.append(msg)
                continue
            if msg.get("screenshot_b64"):
                converted.append(
                    {
                        "role": msg.get("role", "user"),
                        "content": [
                            {"type": "text", "text": msg.get("text", "What should I do?")},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{msg['screenshot_b64']}",
                                },
                            },
                        ],
                    }
                )
            else:
                converted.append(msg)
        return await super().chat(converted, temperature=temperature)


class FireworksProvider(OpenAIProvider):
    """Fireworks OpenAI-compatible provider."""

    provider_name = "fireworks"
    base_url = "https://api.fireworks.ai/inference/v1"
    api_key_env = "FIREWORKS_API_KEY"


class MoonshotProvider(OpenAIProvider):
    """Moonshot OpenAI-compatible provider."""

    provider_name = "moonshot"
    base_url = "https://api.moonshot.cn/v1"
    api_key_env = "MOONSHOT_API_KEY"


class HuggingFaceProvider(OpenAIProvider):
    """Hugging Face hosted OpenAI-compatible provider."""

    provider_name = "huggingface"
    base_url = "https://router.huggingface.co/v1"
    api_key_env = "HUGGINGFACE_API_KEY"


class QwenProvider(OpenAIProvider):
    """Qwen OpenAI-compatible provider."""

    provider_name = "qwen"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env = "QWEN_API_KEY"


class OSAtlasProvider(BaseProvider):
    """
    Free grounding provider using HuggingFace Spaces OS-Atlas.
    No API key required. Converts natural language to screen coordinates.
    """

    provider_name = "osatlas"
    SPACES_URL = "https://hf.space/embed/OS-Copilot/OS-Atlas-Base-7B/api/predict"

    def __init__(self) -> None:
        super().__init__(model="os-atlas", api_key="")

    async def get_action(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.2
    ) -> "Action":
        raise NotImplementedError("OSAtlasProvider is a grounding model, use locate()")

    async def locate(self, screenshot_b64: str, description: str) -> tuple[int, int]:
        """
        Find screen coordinates of a described UI element.
        Calls HuggingFace Spaces OS-Atlas free API.
        Returns (x, y) pixel coordinates.
        """
        payload = {"data": [screenshot_b64, description]}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.SPACES_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bbox = data["data"][0][0]
                    x = int((bbox[0] + bbox[2]) / 2)
                    y = int((bbox[1] + bbox[3]) / 2)
                    return (x, y)
                raise RuntimeError(f"OS-Atlas API error: {resp.status}")

    async def judge_completion(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.0
    ) -> bool:
        return False

    async def chat(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.2
    ) -> Any:
        raise NotImplementedError


class LocalProvider(BaseProvider):
    """Deterministic provider used for tests and offline operation."""

    provider_name = "local"

    async def chat(self, messages: list[dict[str, Any]], *, temperature: float = 0.2) -> ProviderResponse:
        return ProviderResponse(content=self._offline_response(messages))


PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    provider.provider_name: provider
    for provider in [
        OpenAIProvider,
        AnthropicProvider,
        GroqProvider,
        MistralProvider,
        GeminiProvider,
        DeepSeekProvider,
        OpenRouterProvider,
        FireworksProvider,
        MoonshotProvider,
        HuggingFaceProvider,
        QwenProvider,
        OllamaProvider,
        OSAtlasProvider,
        LocalProvider,
    ]
}


def create_provider(name: str, model: str, *, api_key: str | None = None, base_url: str | None = None) -> BaseProvider:
    """Build a provider by name."""

    provider_cls = PROVIDER_REGISTRY.get(name.lower(), LocalProvider)
    return provider_cls(model=model, api_key=api_key, base_url=base_url)
