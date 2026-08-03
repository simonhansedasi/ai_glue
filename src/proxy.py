import time

from .logger import log_call
from .governance import check
from .store import init_db

init_db()


def _extract_prompt(kwargs: dict) -> str:
    """Only the newest message, not the whole growing conversation — see routes/proxy.py."""
    messages = kwargs.get("messages", [])
    if not messages:
        return ""
    content = messages[-1].get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " | ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class _MessagesProxy:
    def __init__(self, glued_client):
        self._gc = glued_client

    def create(self, **kwargs):
        return self._gc._call_anthropic(**kwargs)


class _ChatCompletionsProxy:
    def __init__(self, glued_client):
        self._gc = glued_client

    def create(self, **kwargs):
        return self._gc._call_openai(**kwargs)


class _ChatProxy:
    def __init__(self, glued_client):
        self.completions = _ChatCompletionsProxy(glued_client)


class GluedClient:
    """
    Drop-in wrapper around Anthropic and OpenAI SDK clients.
    Every call is governance-checked and logged to audit.db.

    Usage:
        client = GluedClient("anthropic", session_id="user-abc", project="hr-bot")
        response = client.messages.create(model="claude-sonnet-4-6", ...)

        client = GluedClient("openai", session_id="user-abc", project="support-bot")
        response = client.chat.completions.create(model="gpt-4o", ...)
    """

    def __init__(self, provider: str, session_id: str = "default", project: str = "default"):
        self.provider = provider.lower()
        self.session_id = session_id
        self.project = project
        self._client = self._init_client()

        if self.provider == "anthropic":
            self.messages = _MessagesProxy(self)
        elif self.provider == "openai":
            self.chat = _ChatProxy(self)

    def _init_client(self):
        if self.provider == "anthropic":
            import anthropic
            return anthropic.Anthropic()
        elif self.provider == "openai":
            import openai
            return openai.OpenAI()
        raise ValueError(f"Unsupported provider: '{self.provider}'. Use 'anthropic' or 'openai'.")

    def _call_anthropic(self, **kwargs):
        model = kwargs.get("model", "")
        prompt = _extract_prompt(kwargs)
        gov_flags = check(self.provider, model, self.project, self.session_id, prompt)

        t0 = time.time()
        response = None
        error = None
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as e:
            error = str(e)
            raise
        finally:
            latency_ms = int((time.time() - t0) * 1000)
            input_tokens = response.usage.input_tokens if response else 0
            output_tokens = response.usage.output_tokens if response else 0
            raw_response = (
                response.content[0].text
                if response and response.content else ""
            )
            log_call(
                provider=self.provider, model=model,
                session_id=self.session_id, project=self.project,
                input_tokens=input_tokens, output_tokens=output_tokens,
                latency_ms=latency_ms, raw_prompt=prompt,
                raw_response=raw_response, gov_flags=gov_flags, error=error,
            )
        return response

    def _call_openai(self, **kwargs):
        model = kwargs.get("model", "")
        prompt = _extract_prompt(kwargs)
        gov_flags = check(self.provider, model, self.project, self.session_id, prompt)

        t0 = time.time()
        response = None
        error = None
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as e:
            error = str(e)
            raise
        finally:
            latency_ms = int((time.time() - t0) * 1000)
            usage = getattr(response, "usage", None)
            input_tokens = usage.prompt_tokens if usage else 0
            output_tokens = usage.completion_tokens if usage else 0
            raw_response = (
                response.choices[0].message.content
                if response and response.choices else ""
            )
            log_call(
                provider=self.provider, model=model,
                session_id=self.session_id, project=self.project,
                input_tokens=input_tokens, output_tokens=output_tokens,
                latency_ms=latency_ms, raw_prompt=prompt,
                raw_response=raw_response, gov_flags=gov_flags, error=error,
            )
        return response
