"""Aggregates principals/objects/grants across all configured connectors."""
from __future__ import annotations
import re
from pathlib import Path
from typing import List, Dict
from app.config import settings
from app.connectors import get_connector, SourceType


SOURCE_BY_PREFIX = {
    "databricks": SourceType.DATABRICKS,
    "dbx": SourceType.DATABRICKS,
    "databricks_uc": SourceType.DATABRICKS_UC,
    "snowflake": SourceType.SNOWFLAKE,
    "snow": SourceType.SNOWFLAKE,
    "snowflake_horizon": SourceType.SNOWFLAKE_HORIZON,
    "aws_lakeformation": SourceType.AWS_LAKE_FORMATION_S3,
    "aws_lake_formation": SourceType.AWS_LAKE_FORMATION_S3,
    "s3": SourceType.AWS_LAKE_FORMATION_S3,
    "fabric_onelake": SourceType.FABRIC_ONELAKE,
    "fabric_shortcut": SourceType.FABRIC_SHORTCUT,
    "dataverse": SourceType.DATAVERSE,
    "dv": SourceType.DATAVERSE,
}


def discover_configs() -> List[Dict[str, str]]:
    """Find YAML configs in CONFIGS_DIR; infer source type from filename prefix.

    Sample/template configs (``*_sample.yaml`` or files still containing
    ``<placeholder>`` tokens) are flagged ``is_template`` so the UI can keep them
    out of the active sources list without deleting the on-disk templates.
    """
    out = []
    for f in sorted(settings.configs_path.glob("*.y*ml")):
        st = None
        for prefix, source in SOURCE_BY_PREFIX.items():
            if f.stem.lower().startswith(prefix):
                st = source
                break
        out.append({
            "name": f.stem,
            "path": str(f),
            "type": st.value if st else "unknown",
            "is_template": _is_template_config(f),
        })
    return out


def _is_template_config(path: Path) -> bool:
    """A config is a template if named ``*_sample`` or has unresolved placeholders."""
    if path.stem.lower().endswith("_sample") or "sample" in path.stem.lower():
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    # Unresolved angle-bracket placeholders like <azure-tenant-id> mark a template.
    return bool(re.search(r"<[a-z0-9][a-z0-9 _-]*>", text, re.IGNORECASE))


def summarize_all() -> List[dict]:
    summaries = []
    for cfg in discover_configs():
        if cfg.get("is_template"):
            # Sample/template configs hold placeholders, not real endpoints — skip.
            continue
        if cfg["type"] == "unknown":
            summaries.append({**cfg, "summary": {"error": "type unknown (rename file with databricks_/snowflake_/dataverse_ prefix)"}})
            continue
        try:
            c = get_connector(cfg["type"], cfg["path"])
            summaries.append({**cfg, "summary": c.summary()})
        except Exception as e:
            summaries.append({**cfg, "summary": {"error": str(e)}})
    return summaries
