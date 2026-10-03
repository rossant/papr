from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir

DEFAULT_TEMPLATE = '{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title truncate="100" }}'
VALID_FORMATS = {"pdf", "md", "txt", "bib", "bibtex", "csl", "json"}
VALID_MD_BACKENDS = {"auto", "mistral", "native"}


def _path(value: object, key: str) -> Path | None:
    if value is None or value == "":
        return None
    return _config_path(value, key)


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
    local_sources: bool = True
    zotero_api_url: str = "http://127.0.0.1:23119/api"
    zotero_data_dir: Path | None = None
    zolit_repo: Path | None = None
    zolit_db: Path | None = None
    zolit_sbs_repo: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.formats, list) or not self.formats:
            raise ValueError("formats must be a non-empty list of supported format names")
        for value in self.formats:
            if not isinstance(value, str) or value not in VALID_FORMATS:
                supported = ", ".join(sorted(VALID_FORMATS))
                raise ValueError(
                    f"formats contains unsupported format {value!r}; choose from {supported}"
                )
        if not isinstance(self.md_backend, str) or self.md_backend not in VALID_MD_BACKENDS:
            supported = ", ".join(sorted(VALID_MD_BACKENDS))
            raise ValueError(f"processors.md.backend must be one of: {supported}")
        for name in ("auto_accept_score", "auto_accept_margin"):
            value = getattr(self, name)
            invalid = isinstance(value, bool) or not isinstance(value, (int, float))
            if invalid or not 0 <= value <= 1:
                raise ValueError(f"matching.{name} must be a number between 0 and 1")
        for name in ("max_candidates", "filename_max_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                key = (
                    "matching.max_candidates" if name == "max_candidates" else "filename.max_length"
                )
                raise ValueError(f"{key} must be a positive integer")
        string_fields = ("filename_template", "filename_space", "mistral_model", "zotero_api_url")
        for name in string_fields:
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        for name in ("email", "unpaywall_email", "openalex_api_key"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        for name in ("filename_ascii", "local_sources"):
            if type(getattr(self, name)) is not bool:
                key = "filename.ascii" if name == "filename_ascii" else "local.enabled"
                raise ValueError(f"{key} must be true or false")
        for name in ("download_dir", "zotero_data_dir", "zolit_repo", "zolit_db", "zolit_sbs_repo"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                raise ValueError(f"{name} must be a path")

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
        key_file = Path(os.getenv("MISTRAL_API_KEY_FILE") or (cfg.config_dir / "mistral.key"))
        if not os.getenv("MISTRAL_API_KEY") and key_file.expanduser().is_file():
            os.environ["MISTRAL_API_KEY"] = (
                key_file.expanduser().read_text(encoding="utf-8").strip()
            )
        path = path or (Path(user_config_dir("papr")) / "config.toml")
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("configuration must contain a TOML table")

            def table(name: str, parent: dict = data) -> dict:
                value = parent.get(name, {})
                if not isinstance(value, dict):
                    raise ValueError(f"{name} must be a TOML table")
                return value

            filename = table("filename")
            processors = table("processors")
            md = table("md", processors)
            matching = table("matching")
            local = table("local")
            if "download_dir" in data:
                cfg.download_dir = _config_path(data["download_dir"], "download_dir")
            if "formats" in data:
                cfg.formats = data["formats"]
            cfg.email = data.get("email", cfg.email)
            cfg.unpaywall_email = data.get("unpaywall_email", cfg.unpaywall_email)
            cfg.openalex_api_key = data.get("openalex_api_key", cfg.openalex_api_key)
            cfg.filename_template = filename.get("template", cfg.filename_template)
            cfg.filename_max_length = filename.get("max_length", cfg.filename_max_length)
            cfg.filename_ascii = filename.get("ascii", cfg.filename_ascii)
            cfg.filename_space = filename.get("space", cfg.filename_space)
            cfg.md_backend = md.get("backend", cfg.md_backend)
            cfg.mistral_model = md.get("model", cfg.mistral_model)
            cfg.auto_accept_score = matching.get("auto_accept_score", cfg.auto_accept_score)
            cfg.auto_accept_margin = matching.get("auto_accept_margin", cfg.auto_accept_margin)
            cfg.max_candidates = matching.get("max_candidates", cfg.max_candidates)
            cfg.local_sources = local.get("enabled", cfg.local_sources)
            cfg.zotero_api_url = local.get("zotero_api_url", cfg.zotero_api_url)
            cfg.zotero_data_dir = _path(local.get("zotero_data_dir"), "local.zotero_data_dir")
            cfg.zolit_repo = _path(local.get("zolit_repo"), "local.zolit_repo")
            cfg.zolit_db = _path(local.get("zolit_db"), "local.zolit_db")
            cfg.zolit_sbs_repo = _path(local.get("zolit_sbs_repo"), "local.zolit_sbs_repo")
        cfg.email = os.getenv("PAPR_EMAIL", cfg.email)
        cfg.unpaywall_email = os.getenv("UNPAYWALL_EMAIL", cfg.unpaywall_email or cfg.email)
        cfg.openalex_api_key = os.getenv("OPENALEX_API_KEY", cfg.openalex_api_key)
        cfg.zotero_api_url = os.getenv("PAPR_ZOTERO_API_URL", cfg.zotero_api_url)
        cfg.zotero_data_dir = (
            _path(os.getenv("PAPR_ZOTERO_DATA_DIR"), "PAPR_ZOTERO_DATA_DIR") or cfg.zotero_data_dir
        )
        cfg.zolit_repo = _path(os.getenv("PAPR_ZOLIT_REPO"), "PAPR_ZOLIT_REPO") or cfg.zolit_repo
        cfg.zolit_db = _path(os.getenv("PAPR_ZOLIT_DB"), "PAPR_ZOLIT_DB") or cfg.zolit_db
        cfg.zolit_sbs_repo = (
            _path(os.getenv("PAPR_ZOLIT_SBS_REPO"), "PAPR_ZOLIT_SBS_REPO") or cfg.zolit_sbs_repo
        )
        cfg.__post_init__()
        return cfg


def _config_path(value: object, key: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a path string")
    return Path(value).expanduser()
