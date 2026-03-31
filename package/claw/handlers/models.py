"""
OpenAI chat-completions adapter for the Claw ``Loop`` (:class:`Models`).

Maps :class:`~claw.handlers.class_prototypes.ContextBundle` and tool definitions to the
OpenAI API and returns ``choices`` shaped for :meth:`claw.handlers.loop.Loop.interpret_model_output`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from openai import OpenAI

from .class_prototypes import ContextBundle, PromptMessage, ToolDefinition


class Models:
    """
    OpenAI chat-completions adapter for the Claw ``Loop``.

    Accepts a ``ContextBundle`` from ``Context.build_context`` and returns a dict with
    ``choices`` for ``Loop.interpret_model_output``. Ad-hoc dict payloads with ``model``,
    ``messages``, ``temperature``, and optional ``tools`` / ``tool_choice`` / ``response_format``
    are also supported.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.AI_2_MODEL = "gpt-4o-mini"
        try:
            openai_key = self.config.get("OPENAI_API_KEY", "")
            self.AI_2 = OpenAI(api_key=openai_key) if openai_key else None
        except Exception as e:
            print(f"Error initializing OpenAI client: {e}")
            self.AI_2 = None

    def complete(self, context: ContextBundle | Dict[str, Any]) -> Dict[str, Any]:
        """
        Run chat completions. Primary input is ``ContextBundle`` from the loop.

        For ad-hoc use, a plain dict with keys ``model``, ``messages``, ``temperature``,
        and optional ``tools`` / ``tool_choice`` / ``response_format`` is still accepted.
        """
        if self.AI_2 is None:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "OpenAI client is not configured (missing OPENAI_API_KEY).",
                        }
                    }
                ]
            }

        try:
            if isinstance(context, dict):
                params: Dict[str, Any] = {
                    "model": context.get("model") or self.AI_2_MODEL,
                    "messages": context.get("messages") or [],
                    "temperature": float(context.get("temperature", 0.0)),
                }
                if "tools" in context and context["tools"]:
                    params["tools"] = context["tools"]
                if "tool_choice" in context:
                    params["tool_choice"] = context["tool_choice"]
                if "response_format" in context:
                    params["response_format"] = context["response_format"]
                response = self.AI_2.chat.completions.create(**params)
            else:
                bundle = context
                params = {
                    "model": self.AI_2_MODEL,
                    "messages": Models.prompt_messages_to_openai(bundle.messages),
                    "temperature": 0.0,
                }
                oa_tools = Models.tool_definitions_to_openai(bundle.tools)
                if oa_tools:
                    params["tools"] = oa_tools
                    params["tool_choice"] = "auto"
                response = self.AI_2.chat.completions.create(**params)

            msg = response.choices[0].message
            return {"choices": [{"message": Models.completion_message_to_choice_dict(msg)}]}

        except Exception as e:
            print(f"Error running LLM call: {e}")
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"LLM error: {e}",
                        }
                    }
                ]
            }

    @staticmethod
    def openai_role(role: str) -> str:
        """Map Claw PromptMessage roles to OpenAI chat roles."""
        if role in ("system", "user", "assistant", "tool"):
            return role
        if role == "internal":
            return "system"
        return "system"

    @staticmethod
    def prompt_messages_to_openai(messages: list[PromptMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            out.append({"role": Models.openai_role(m.role), "content": m.content})
        return out

    @staticmethod
    def tool_definitions_to_openai(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        api_tools: list[dict[str, Any]] = []
        for td in tools:
            params = td.input_schema if isinstance(td.input_schema, dict) else {}
            if not params:
                params = {"type": "object", "properties": {}}
            api_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": td.tool_name,
                        "description": (td.description or "")[:8000],
                        "parameters": params,
                    },
                }
            )
        return api_tools

    @staticmethod
    def completion_message_to_choice_dict(msg: Any) -> dict[str, Any]:
        """Shape expected by ``Loop.interpret_model_output`` (OpenAI-style message + tool_calls)."""
        row: dict[str, Any] = {
            "role": getattr(msg, "role", "assistant"),
            "content": getattr(msg, "content", None) or "",
        }
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return row
        serialized = []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn is not None else ""
            arguments = getattr(fn, "arguments", "{}") if fn is not None else "{}"
            serialized.append(
                {
                    "id": getattr(tc, "id", ""),
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        row["tool_calls"] = serialized
        return row
