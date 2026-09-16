"""Adapter for OpenAI-compatible chat servers.

Works with llama.cpp `llama-server`, vLLM, LM Studio, TabbyAPI, and anything
that exposes `/v1/chat/completions` + `/v1/models`. Accepts either a bare
host URL or a URL that already ends in /v1.
"""
import json

import httpx

from .base import AdapterError, ServerAdapter

# Tiny 1x1 PNG, only used for the best-effort vision probe.
_ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAD1fKIgAAAADElEQVR42mNkYGBgAAEAAAqAApUAAQBXjSJJAAAAAElFTkSuQmCC"
)


class OpenAICompatAdapter(ServerAdapter):
    kind = "openai"

    def __init__(self, cfg: dict, timeout: float = 600.0):
        super().__init__(cfg)
        if not self.url:
            raise AdapterError("server url is empty")
        base = self.url
        if not base.endswith("/v1"):
            base = base + "/v1"
        self.base = base
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def list_models(self) -> list:
        try:
            r = await self.client.get(f"{self.base}/models", headers=self.headers, timeout=10)
        except Exception as e:
            raise AdapterError(f"cannot reach {self.base}/models: {e}") from e
        if r.status_code != 200:
            raise AdapterError(f"GET {self.base}/models -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json().get("data", [])
        except json.JSONDecodeError:
            raise AdapterError(f"GET {self.base}/models returned non-JSON")
        out = []
        for m in data:
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            out.append({"id": mid, "meta": {k: v for k, v in m.items() if k != "id"}})
        return out

    async def chat_stream(self, messages: list, params: dict, tools: list | None = None):
        payload = {
            "model": params["model"],
            "messages": messages,
            "stream": True,
            "temperature": params.get("temperature", 0.7),
            "top_p": params.get("top_p", 1.0),
            "max_tokens": params.get("max_tokens", 1024),
            # Ask OpenAI-compatible servers (llama.cpp, vLLM, ...) to include
            # token usage in the final stream chunk. Ignored by servers
            # without support.
            "stream_options": {"include_usage": True},
        }
        if params.get("repeat_penalty") is not None:
            payload["repeat_penalty"] = params["repeat_penalty"]
        if params.get("seed") is not None:
            payload["seed"] = params["seed"]
        ctk = params.get("chat_template_kwargs")
        if ctk:
            # llama.cpp (--jinja) and vLLM accept per-request chat template
            # kwargs, e.g. {"reasoning_effort": "xhigh", "enable_thinking": True}
            payload["chat_template_kwargs"] = ctk
        if tools:
            payload["tools"] = tools

        try:
            async with self.client.stream(
                "POST",
                f"{self.base}/chat/completions",
                json=payload,
                headers=self.headers,
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    # Some servers reject the tools param: caller may retry without tools.
                    yield {
                        "type": "error",
                        "message": f"HTTP {r.status_code}: {body[:400]}",
                        "status": r.status_code,
                    }
                    return
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        yield {"type": "usage", "usage": obj["usage"]}
                    for ch in obj.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            yield {"type": "delta", "content": delta["content"]}
                        tcs = delta.get("tool_calls")
                        if tcs:
                            yield {"type": "tool_calls", "tool_calls": tcs}
                        fr = ch.get("finish_reason")
                        if fr:
                            yield {"type": "finish", "finish_reason": fr}
        except Exception as e:
            yield {"type": "error", "message": f"{type(e).__name__}: {e}", "status": None}

    async def complete(self, messages: list, params: dict, timeout: float = 60.0) -> dict:
        """Non-streaming completion. Returns {"ok", "status", "error", "usage"}.

        Used for cheap capability probes (e.g. thinking-level checks) where
        the answer content does not matter, only whether the server accepted
        the request (200) and what it reported in `usage`.
        """
        payload = {
            "model": params["model"],
            "messages": messages,
            "stream": False,
            "temperature": params.get("temperature", 0.0),
            "max_tokens": params.get("max_tokens", 16),
        }
        ctk = params.get("chat_template_kwargs")
        if ctk:
            payload["chat_template_kwargs"] = ctk
        try:
            r = await self.client.post(
                f"{self.base}/chat/completions",
                json=payload,
                headers=self.headers,
                timeout=timeout,
            )
        except Exception as e:
            return {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}", "usage": None}
        usage = None
        if r.status_code == 200:
            try:
                data = r.json()
                usage = data.get("usage")
            except json.JSONDecodeError:
                pass
        else:
            try:
                data = r.json()
                usage = data.get("usage")
            except Exception:
                pass
        return {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "error": None if r.status_code == 200 else (r.text or "")[:300],
            "usage": usage,
        }

    async def probe_vision(self, model: str, timeout: float = 30.0) -> bool:
        """Best-effort vision capability check for one model.

        Sends a 1x1 image with a trivial prompt. Servers whose model cannot
        take image content answer with an error (non-200), so a clean 200
        means the model accepted the image.
        """
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in the image? Reply with the single word: ok"},
                        {"type": "image_url", "image_url": {"url": _ONE_PIXEL_PNG}},
                    ],
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        }
        try:
            r = await self.client.post(
                f"{self.base}/chat/completions",
                json=payload,
                headers=self.headers,
                timeout=timeout,
            )
        except Exception:
            return False
        if r.status_code != 200:
            return False
        try:
            data = r.json()
        except json.JSONDecodeError:
            return False
        choices = data.get("choices") or []
        if not choices:
            return False
        finish = choices[0].get("finish_reason")
        return finish in (None, "stop", "length")

    async def health(self) -> bool:
        try:
            r = await self.client.get(f"{self.base}/models", headers=self.headers, timeout=5)
            return r.status_code == 200
        except Exception:
            return False
