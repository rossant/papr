from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir


DEFAULT_TEMPLATE = '{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title truncate="100" }}'


@dataclass
class Config:
    download_dir: Path = field(default_factory=lambda: Path.home() / "Downloads")
    formats: list[str] = field(default_factory=lambda: ["pdf"])
    filename_template: str = DEFAULT_TEMPLATE
    filename_max_length: int = 180
    filename_ascii: bool = False
    filename_space: str = "_"
    email: str | None = None
    unpaywall_email: str | None = None
    openalex_api_key: str | None = None
    md_backend: str = "auto"
    mistral_model: str = "mistral-ocr-latest"
    auto_accept_score: float = 0.78
    auto_accept_margin: float = 0.08
    max_candidates: int = 5

    @property
    def config_dir(self) -> Path:
        return Path(user_config_dir("papr"))

    @property
    def cache_dir(self) -> Path:
        return Path(user_cache_dir("papr"))

    @property
    def data_dir(self) -> Path:
        return Path(user_data_dir("papr"))

    @property
    def ucl_profile_dir(self) -> Path:
        return self.data_dir / "browser" / "ucl"

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        cfg = cls()
        secrets = Path(user_config_dir("papr")) / "secrets.env"
        if secrets.exists():
            for raw in secrets.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        path = path or (Path(user_config_dir("papr")) / "config.toml")
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "download_dir" in data:
                cfg.download_dir = Path(data["download_dir"]).expanduser()
            if "formats" in data:
                cfg.formats = list(data["formats"])
            cfg.email = data.get("email", cfg.email)
            cfg.unpaywall_email = data.get("unpaywall_email", cfg.unpaywall_email)
            cfg.openalex_api_key = data.get("openalex_api_key", cfg.openalex_api_key)
            fn = data.get("filename", {})
            cfg.filename_template = fn.get("template", cfg.filename_template)
            cfg.filename_max_length = int(fn.get("max_length", cfg.filename_max_length))
            cfg.filename_ascii = bool(fn.get("ascii", cfg.filename_ascii))
            cfg.filename_space = fn.get("space", cfg.filename_space)
            proc = data.get("processors", {}).get("md", {})
            cfg.md_backend = proc.get("backend", cfg.md_backend)
            cfg.mistral_model = proc.get("model", cfg.mistral_model)
            match = data.get("matching", {})
            cfg.auto_accept_score = float(match.get("auto_accept_score", cfg.auto_accept_score))
            cfg.auto_accept_margin = float(match.get("auto_accept_margin", cfg.auto_accept_margin))
            cfg.max_candidates = int(match.get("max_candidates", cfg.max_candidates))
        cfg.email = os.getenv("PAPR_EMAIL", cfg.email)
        cfg.unpaywall_email = os.getenv("UNPAYWALL_EMAIL", cfg.unpaywall_email or cfg.email)
        cfg.openalex_api_key = os.getenv("OPENALEX_API_KEY", cfg.openalex_api_key)
        return cfg
