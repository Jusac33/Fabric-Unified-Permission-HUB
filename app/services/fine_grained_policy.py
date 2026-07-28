from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Iterable


@dataclass(frozen=True)
class FabricConstraintTranslation:
    table_path: str
    predicate: str = ""
    column_names: tuple[str, ...] = ()
    note: str = ""


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_FUNCTION = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SELECT_WHERE = re.compile(
    r"^\s*select\s+\*\s+from\s+.+?\s+where\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_GROUP_MEMBER_PREFIX = re.compile(
    r"^\s*is_(?:account_)?group_member\s*\(\s*'[^']+'\s*\)\s+AND\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CURRENT_USER_WRAPPER = re.compile(
    r"^\s*CASE\s+WHEN\s+current_user\s*\(\s*\)\s+IN\s*\([^)]*\)\s+"
    r"THEN\s*\((.+)\)\s+ELSE\s+true\s+END\s*$",
    re.IGNORECASE | re.DOTALL,
)


def table_parts_from_scope(scope: str, default_catalog: str) -> tuple[str, str, str] | None:
    scope_type, _, raw_name = (scope or "").partition(":")
    if scope_type != "table":
        return None
    parts = [part for part in raw_name.split(".") if part]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2 and default_catalog:
        return default_catalog, parts[0], parts[1]
    return None


def fabric_table_path(scope: str, default_catalog: str) -> str:
    parts = table_parts_from_scope(scope, default_catalog)
    if not parts:
        raise ValueError("fine-grained Fabric DAS constraints require table scope")
    _, schema, table = parts
    _require_safe_identifier(schema, "schema")
    _require_safe_identifier(table, "table")
    return f"/Tables/{schema}/{table}"


def function_param_names(function_info: dict) -> list[str]:
    params = ((function_info or {}).get("input_params") or {}).get("parameters") or []
    names: list[str] = []
    for param in params:
        if not isinstance(param, dict):
            continue
        if (param.get("parameter_type") or "PARAM") != "PARAM":
            continue
        name = str(param.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def build_fabric_row_constraint(
    scope: str,
    default_catalog: str,
    function_info: dict,
    input_columns: Iterable[str],
    allow_test_degraded: bool,
) -> FabricConstraintTranslation:
    table = table_parts_from_scope(scope, default_catalog)
    if not table:
        raise ValueError("row filters can only be translated for table scope")
    _, schema, table_name = table
    _require_safe_identifier(schema, "schema")
    _require_safe_identifier(table_name, "table")

    if (function_info.get("data_type") or "").upper() != "BOOLEAN":
        raise ValueError("UC row-filter function must return BOOLEAN")
    expression = str(function_info.get("routine_definition") or "").strip()
    if not expression:
        raise ValueError("UC row-filter function definition is unavailable")

    expression = _map_function_params(
        expression,
        function_param_names(function_info),
        [str(column) for column in input_columns or []],
    )
    expression, note = _remove_databricks_current_user_wrapper(expression)
    expression, group_note = _remove_databricks_group_predicate(
        expression,
        allow_test_degraded=allow_test_degraded,
    )
    note = "; ".join(part for part in (note, group_note) if part)
    _validate_safe_row_predicate(expression)

    return FabricConstraintTranslation(
        table_path=f"/Tables/{schema}/{table_name}",
        predicate=f"select * from {schema}.{table_name} where {expression}",
        note=note,
    )


def build_fabric_column_constraint(
    scope: str,
    default_catalog: str,
    masked_column: str,
    table_columns: Iterable[str],
) -> FabricConstraintTranslation:
    table_path = fabric_table_path(scope, default_catalog)
    masked = str(masked_column or "").strip()
    if not masked:
        raise ValueError("UC column mask metadata did not identify the masked column")
    _require_safe_identifier(masked, "column")

    visible_columns = [
        str(column).strip()
        for column in table_columns
        if str(column or "").strip() and str(column).strip() != masked
    ]
    if not visible_columns:
        raise ValueError("cannot create Fabric column constraint without visible columns")
    for column in visible_columns:
        _require_safe_identifier(column, "column")

    return FabricConstraintTranslation(
        table_path=table_path,
        column_names=tuple(visible_columns),
        note=(
            f"Fabric DAS cannot mask values; UC mask on '{masked}' maps "
            "to column-level visibility by excluding that column"
        ),
    )


def canonical_row_filter_key(scope: str, predicate_or_query: str) -> str:
    return _constraint_hash(
        {
            "kind": "row_filter",
            "scope": scope,
            "predicate": normalize_row_predicate(predicate_or_query),
        }
    )


def canonical_column_constraint_key(scope: str, column_names: Iterable[str]) -> str:
    return _constraint_hash(
        {
            "kind": "column_mask",
            "scope": scope,
            "columns": sorted(str(column) for column in column_names),
        }
    )


def normalize_row_predicate(predicate_or_query: str) -> str:
    raw = str(predicate_or_query or "").strip()
    match = _SELECT_WHERE.match(raw)
    if match:
        raw = match.group(1)
    return " ".join(raw.split())


def referenced_columns(expression: str, table_columns: Iterable[str]) -> list[str]:
    """Return table columns referenced by a safe row predicate, preserving table order."""
    raw = normalize_row_predicate(expression)
    _validate_safe_row_predicate(raw)
    referenced: list[str] = []
    for column in table_columns:
        name = str(column or "").strip()
        if not name:
            continue
        _require_safe_identifier(name, "column")
        if re.search(rf"\b{re.escape(name)}\b", raw):
            referenced.append(name)
    return referenced


def _constraint_hash(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _map_function_params(
    expression: str,
    param_names: list[str],
    input_columns: list[str],
) -> str:
    if len(param_names) != len(input_columns):
        return expression
    mapped = expression
    for param, column in zip(param_names, input_columns):
        if not param or not column or param == column:
            continue
        _require_safe_identifier(param, "function parameter")
        _require_safe_identifier(column, "input column")
        mapped = re.sub(rf"\b{re.escape(param)}\b", column, mapped)
    return mapped


def _remove_databricks_group_predicate(
    expression: str,
    allow_test_degraded: bool,
) -> tuple[str, str]:
    match = _GROUP_MEMBER_PREFIX.match(expression)
    if not match:
        return expression, ""
    if not allow_test_degraded:
        raise ValueError("Databricks group-membership predicates need explicit production mapping")
    return (
        match.group(1).strip(),
        "removed Databricks-only group-membership predicate before applying Fabric DAS row constraint",
    )


def _remove_databricks_current_user_wrapper(expression: str) -> tuple[str, str]:
    match = _CURRENT_USER_WRAPPER.match(expression)
    if not match:
        return expression, ""
    return (
        match.group(1).strip(),
        "removed Databricks current_user wrapper before comparing Fabric row constraint",
    )


def _validate_safe_row_predicate(expression: str) -> None:
    raw = expression.strip()
    if not raw:
        raise ValueError("empty row predicate")
    blocked_tokens = (";", "--", "/*", "*/")
    if any(token in raw for token in blocked_tokens):
        raise ValueError("row predicate contains blocked SQL token")
    if re.search(
        r"\b(select|insert|update|delete|merge|drop|alter|create|exec|execute|grant|revoke)\b",
        raw,
        re.IGNORECASE,
    ):
        raise ValueError("row predicate contains unsupported SQL statement keyword")
    for function_name in _SQL_FUNCTION.findall(raw):
        if function_name.lower() != "in":
            raise ValueError(f"row predicate contains unsupported function '{function_name}'")
    if not re.fullmatch(r"[A-Za-z0-9_\s'\"=<>!,().-]+", raw):
        raise ValueError("row predicate contains unsupported characters")


def _require_safe_identifier(value: str, label: str) -> None:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsupported {label} identifier '{value}'")
