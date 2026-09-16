"""Base class for server adapters.

An adapter knows how to talk to one server protocol:
- list_models():     auto-discover what models the server hosts
- chat_stream():     one streaming chat completion, as a stream of events
- health():          cheap reachability check

Event vocabulary used by chat_stream():
    {"type": "delta", "content": str}
    {"type": "tool_calls", "tool_calls": [ {index, id, type, function:{name, arguments}} ... ]}
         # may be partial fragments; the caller accumulates by index
    {"type": "finish", "finish_reason": str}
    {"type": "usage", "usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}
    {"type": "error", "message": str}
"""
import abc


class AdapterError(Exception):
    pass


class ServerAdapter(abc.ABC):
    kind = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = (cfg.get("url") or "").strip().rstrip("/")
        self.api_key = (cfg.get("api_key") or "").strip()

    @property
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @abc.abstractmethod
    async def list_models(self) -> list:
        """Return [{"id": str, "meta": dict}, ...]."""
        raise NotImplementedError

    @abc.abstractmethod
    async def chat_stream(self, messages: list, params: dict, tools: list | None = None):
        """Async iterator of events (see module docstring)."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def health(self) -> bool:
        return False

    async def close(self) -> None:
        pass
