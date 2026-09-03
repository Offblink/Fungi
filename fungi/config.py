"""Configuration loading: config.json first, environment variables override."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_API_KEY = "sk-your-key-here"
DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-pro"


@dataclass
class Config:
    api_key: str = DEFAULT_API_KEY
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    # Per-layer model override: layer number (1/2/3) -> model name.
    # Layers without an entry fall back to `model` (see model_for).
    layer_models: dict[int, str] = field(default_factory=dict)
    system_prompt: str | None = None
    # MCP servers (stdio): name -> {command, args?, env?}
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    # File transfer: per-file size cap and the local landing directory.
    max_file_mb: int = 200
    inbox_dir: str = ""  # empty -> PROJECT_ROOT / "inbox"
    # Presentation nickname shown to friends; never used on the wire.
    display: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != DEFAULT_API_KEY

    def model_for(self, layer: int) -> str:
        """Model for one TriLayer layer: per-layer override if set, else `model`."""
        return self.layer_models.get(layer) or self.model


def load_config(path: Path | None = None) -> Config:
    """Load config from JSON file, then apply env overrides."""
    cfg = Config()
    source = path if path is not None else CONFIG_PATH
    if source.is_file():
        try:
            data = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if data.get("api_key"):
            cfg.api_key = data["api_key"]
        if data.get("endpoint"):
            cfg.endpoint = data["endpoint"]
        if data.get("model"):
            cfg.model = data["model"]
        models = data.get("models")
        if isinstance(models, dict):
            cfg.layer_models = {
                int(k): str(v) for k, v in models.items() if str(k) in ("1", "2", "3") and v
            }
        if isinstance(data.get("mcp_servers"), dict):
            cfg.mcp_servers = {
                str(k): v for k, v in data["mcp_servers"].items() if isinstance(v, dict)
            }
        if data.get("max_file_mb"):
            cfg.max_file_mb = int(data["max_file_mb"])
        if data.get("inbox_dir"):
            cfg.inbox_dir = str(data["inbox_dir"])
        if data.get("display"):
            cfg.display = str(data["display"])
    cfg.api_key = os.environ.get("OPENAI_API_KEY") or cfg.api_key
    cfg.endpoint = os.environ.get("OPENAI_ENDPOINT") or cfg.endpoint
    cfg.model = os.environ.get("OPENAI_MODEL") or cfg.model
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Persist api_key/endpoint/model (and system_prompt when set) to JSON."""
    target = path if path is not None else CONFIG_PATH
    data: dict[str, str] = {
        "api_key": cfg.api_key,
        "endpoint": cfg.endpoint,
        "model": cfg.model,
    }
    if cfg.layer_models:
        data["models"] = {str(k): cfg.layer_models[k] for k in sorted(cfg.layer_models)}
    if cfg.system_prompt:
        data["system_prompt"] = cfg.system_prompt
    if cfg.mcp_servers:
        data["mcp_servers"] = cfg.mcp_servers
    if cfg.max_file_mb != 200:
        data["max_file_mb"] = cfg.max_file_mb
    if cfg.inbox_dir:
        data["inbox_dir"] = cfg.inbox_dir
    if cfg.display:
        data["display"] = cfg.display
    target.write_text(json.dumps(data, indent=4), encoding="utf-8")
