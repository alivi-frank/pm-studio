"""The Claude model allow-list for PM sessions - shared by the session registry
(validation, persistence), the live PMAgent/TaskRegistry runtime, and the API/UI
(GET /models) so there is exactly one place that defines what's selectable.

A deployment can replace the whole list (and the default) via the [models] table in
pm_studio_local/config.toml - e.g. an enterprise restricted to specific model ids -
otherwise these package defaults apply.
"""

from .config import CONFIG

_DEFAULT_MODELS: dict[str, str] = {
    "claude-opus-4-8": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
}

MODELS: dict[str, str] = CONFIG.models or _DEFAULT_MODELS

DEFAULT_MODEL = CONFIG.default_model or (
    "claude-opus-4-8" if "claude-opus-4-8" in MODELS else next(iter(MODELS))
)


def validate_model(model: str) -> str:
    if model not in MODELS:
        raise ValueError(f"Unknown model: {model!r}. Must be one of: {', '.join(MODELS)}")
    return model


def list_models() -> list[dict]:
    return [{"id": model_id, "label": label} for model_id, label in MODELS.items()]
