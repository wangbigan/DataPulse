from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    app_name: str = "DataPulse MVP"
    metadata_path: Path = ROOT_DIR / "data" / "datapulse.duckdb"
    report_dir: Path = ROOT_DIR / "reports"
    sensitive_config_path: Path = ROOT_DIR / "config" / "sensitive.yaml"
    metric_def_version: str = "mvp-1.0"
    sample_size: int = 8
    low_cardinality_limit: int = 50
    distinct_exact_row_limit: int = 1_000_000
    llm_auto_enrich: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_timeout_seconds: int = 60
    llm_max_tables_per_request: int = 50
    placeholder_values: tuple[str, ...] = (
        "无",
        "未知",
        "不详",
        "-",
        "--",
        "/",
        "\\",
        "N/A",
        "NULL",
        "null",
        "0",
        ".",
    )


def get_settings() -> Settings:
    metadata_path = Path(os.getenv("DATAPULSE_DB", ROOT_DIR / "data" / "datapulse.duckdb"))
    report_dir = Path(os.getenv("DATAPULSE_REPORT_DIR", ROOT_DIR / "reports"))
    sensitive_config_path = Path(
        os.getenv("DATAPULSE_SENSITIVE_CONFIG", ROOT_DIR / "config" / "sensitive.yaml")
    )
    return Settings(
        metadata_path=metadata_path,
        report_dir=report_dir,
        sensitive_config_path=sensitive_config_path,
        llm_auto_enrich=_env_bool("DATAPULSE_LLM_AUTO_ENRICH", False),
        llm_base_url=os.getenv("DATAPULSE_LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=os.getenv("DATAPULSE_LLM_API_KEY"),
        llm_model=os.getenv("DATAPULSE_LLM_MODEL", "gpt-4.1-mini"),
        llm_timeout_seconds=int(os.getenv("DATAPULSE_LLM_TIMEOUT_SECONDS", "60")),
        llm_max_tables_per_request=int(os.getenv("DATAPULSE_LLM_MAX_TABLES", "50")),
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
