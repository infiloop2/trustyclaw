"""The operator-selectable model and effort combinations for agent sessions."""

from __future__ import annotations


SESSION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "codex": {
        "gpt-5.6-terra": ("high", "max", "ultra"),
        "gpt-5.6-sol": ("high", "max", "ultra"),
        "gpt-5.6-luna": ("high", "max"),
    },
    "claude_code": {
        "opus": ("high", "max", "ultracode"),
        "fable": ("high", "max", "ultracode"),
        "sonnet": ("high", "max", "ultracode"),
    },
    # Hermes's headless CLI has no effort flag.
    "hermes": {
        "deepseek.v3.2": ("high",),
        "qwen.qwen3-coder-next": ("high",),
        "moonshotai.kimi-k2.5": ("high",),
    },
}


def public_session_options() -> dict[str, dict[str, list[str]]]:
    """Return the JSON-facing option matrix as a fresh mutable payload."""
    return {
        runtime: {model: list(efforts) for model, efforts in models.items()}
        for runtime, models in SESSION_OPTIONS.items()
    }


def session_config_error(runtime: str, model: object, effort: object) -> str | None:
    models = SESSION_OPTIONS.get(runtime)
    if models is None:
        return "agent_runtime must be one of " + ", ".join(f"'{name}'" for name in SESSION_OPTIONS)
    if not isinstance(model, str) or model not in models:
        return f"model must be one of {', '.join(models)} for {runtime}"
    efforts = models[model]
    if not isinstance(effort, str) or effort not in efforts:
        return f"effort must be one of {', '.join(efforts)} for {model}"
    return None
