"""Named defaults for PDF compression quality and resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CompressionPolicy:
    profile: str
    dpi: int
    quality: int
    downsample_threshold: float = 1.4
    min_savings_bytes: int = 4096
    min_savings_ratio: float = 0.01

    def to_dict(self) -> dict:
        return asdict(self)


_PROFILES = {
    "balanced": (180, 80),
    "compact": (120, 65),
    "lossless": (0, 0),
}


def resolve_policy(config) -> CompressionPolicy:
    profile = getattr(config, "pdf_compression_profile", "balanced")
    if profile not in _PROFILES:
        raise ValueError("pdf.profile must be one of: balanced, compact, lossless")
    dpi, quality = _PROFILES[profile]
    if profile != "lossless":
        dpi = getattr(config, "pdf_compression_dpi", None) or dpi
        quality = getattr(config, "pdf_compression_quality", None) or quality
    return CompressionPolicy(profile=profile, dpi=dpi, quality=quality)
