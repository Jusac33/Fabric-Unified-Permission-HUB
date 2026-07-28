"""Resolve Databricks Unity Catalog ABAC policies to concrete table/column targets.

ABAC policies are tag-driven and dynamic: a single policy attached at a catalog,
schema, or table applies a row filter or column mask to every object whose
governed tags satisfy the policy conditions. Microsoft Fabric OneLake security
has no tag-driven equivalent — its Data Access Security (DAS) constraints bind to
specific table paths. To mirror ABAC into Fabric we therefore *materialize* each
policy: evaluate its tag conditions against the current governed-tag assignments
and emit one concrete target per matching (table[, column]).

This module is pure: it reads no I/O itself. Callers supply the policy list and a
``GovernedTags`` snapshot (built from ``information_schema`` tag tables).

IMPORTANT: materialization is a point-in-time snapshot. Tables tagged after this
runs will NOT automatically receive the Fabric constraint; re-run to refresh.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# --- condition grammar -------------------------------------------------------
# Supported expressions (case-insensitive function names):
#   has_tag('key')                 / hasTag('key')
#   has_tag_value('key','value')   / hasTagValue('key','value')
# combined with AND / OR and optional parentheses.

_FUNC_RE = re.compile(
    r"""(?P<fn>has_tag_value|hastagvalue|has_tag|hastag)\s*\(\s*
        '(?P<k>[^']*)'\s*
        (?:,\s*'(?P<v>[^']*)'\s*)?
        \)""",
    re.IGNORECASE | re.VERBOSE,
)


class TagCondition:
    """A parsed, evaluable tag condition (``has_tag`` / ``has_tag_value`` + AND/OR)."""

    def __init__(self, predicate: Callable[[Dict[str, str]], bool], source: str):
        self._predicate = predicate
        self.source = source

    def evaluate(self, tags: Dict[str, str]) -> bool:
        """Return True if ``tags`` ({tag_name_lower: tag_value}) satisfies the condition."""
        return self._predicate(tags)


def _always_true(_tags: Dict[str, str]) -> bool:
    return True


def parse_tag_condition(expression: Optional[str]) -> TagCondition:
    """Parse an ABAC tag condition into an evaluable :class:`TagCondition`.

    An empty/missing expression defaults to TRUE (matches Databricks ``WHEN``
    semantics). Anything not expressible with the supported grammar raises
    ``ValueError`` so callers can skip the policy safely rather than mis-apply it.
    """
    text = (expression or "").strip()
    if not text:
        return TagCondition(_always_true, "TRUE")

    # Replace each function call with a placeholder token mapped to a predicate.
    predicates: List[Callable[[Dict[str, str]], bool]] = []

    def _sub(match: re.Match) -> str:
        fn = match.group("fn").lower().replace("_", "")
        key = (match.group("k") or "").strip().lower()
        val = match.group("v")
        if fn == "hastag":
            predicates.append(lambda tags, k=key: k in tags)
        elif fn == "hastagvalue":
            wanted = "" if val is None else val
            predicates.append(lambda tags, k=key, w=wanted: tags.get(k) == w)
        else:  # pragma: no cover - regex restricts fn set
            raise ValueError(f"unsupported tag function '{fn}'")
        return f"\x00{len(predicates) - 1}\x00"

    tokenized = _FUNC_RE.sub(_sub, text)

    # After substitution only placeholders, AND, OR, parens, whitespace remain.
    leftover = re.sub(r"[\x00\d\x00()\s]|and|or", "", tokenized, flags=re.IGNORECASE)
    if leftover:
        raise ValueError(f"unsupported tag condition syntax: {expression!r}")

    # Build a Python boolean expression that indexes into ``predicates``.
    py_expr = tokenized
    py_expr = re.sub(r"\x00(\d+)\x00", r"P[\1](T)", py_expr)
    py_expr = re.sub(r"\bAND\b", " and ", py_expr, flags=re.IGNORECASE)
    py_expr = re.sub(r"\bOR\b", " or ", py_expr, flags=re.IGNORECASE)

    try:
        code = compile(py_expr, "<abac-condition>", "eval")
    except SyntaxError as exc:
        raise ValueError(f"unsupported tag condition syntax: {expression!r}") from exc

    def _predicate(tags: Dict[str, str], _code=code, _preds=predicates) -> bool:
        return bool(eval(_code, {"__builtins__": {}}, {"P": _preds, "T": tags}))  # noqa: S307

    return TagCondition(_predicate, text)


# --- governed tag snapshot ---------------------------------------------------
@dataclass
class GovernedTags:
    """Governed-tag assignments for a single catalog, read from information_schema."""

    catalog: Dict[str, str] = field(default_factory=dict)
    schema: Dict[str, Dict[str, str]] = field(default_factory=dict)
    table: Dict[Tuple[str, str], Dict[str, str]] = field(default_factory=dict)
    column: Dict[Tuple[str, str, str], Dict[str, str]] = field(default_factory=dict)

    def effective_table_tags(self, schema: str, table: str) -> Dict[str, str]:
        """Table tags with catalog→schema→table inheritance (child overrides parent)."""
        merged: Dict[str, str] = {}
        merged.update(self.catalog)
        merged.update(self.schema.get(schema, {}))
        merged.update(self.table.get((schema, table), {}))
        return merged

    def column_tags(self, schema: str, table: str, column: str) -> Dict[str, str]:
        """Direct column tags only (column tags do not inherit, per Databricks)."""
        return self.column.get((schema, table, column), {})


# --- resolution --------------------------------------------------------------
@dataclass
class ResolvedTarget:
    """One concrete (table[, column]) an ABAC policy materializes to."""

    constraint_kind: str          # "row_filter" | "column_mask"
    schema: str
    table: str
    function_name: str
    column: str = ""              # masked column for column_mask
    input_columns: List[str] = field(default_factory=list)
    policy_id: str = ""
    policy_name: str = ""
    when_condition: str = ""
    to_principals: List[str] = field(default_factory=list)
    except_principals: List[str] = field(default_factory=list)

    @property
    def table_fqn_suffix(self) -> str:
        return f"{self.schema}.{self.table}"


def _policy_kind(policy: dict) -> str:
    column_mask = policy.get("column_mask") or policy.get("columnMask")
    row_filter = policy.get("row_filter") or policy.get("rowFilter")
    ptype = str(policy.get("policy_type") or policy.get("policyType") or "").upper()
    if column_mask or "COLUMN_MASK" in ptype:
        return "column_mask"
    if row_filter or "ROW_FILTER" in ptype:
        return "row_filter"
    return ""


def _policy_spec(policy: dict, kind: str) -> dict:
    if kind == "column_mask":
        spec = policy.get("column_mask") or policy.get("columnMask")
    else:
        spec = policy.get("row_filter") or policy.get("rowFilter")
    return spec if isinstance(spec, dict) else {}


def _match_columns(policy: dict) -> List[dict]:
    raw = policy.get("match_columns") or policy.get("matchColumns") or []
    return [m for m in raw if isinstance(m, dict)]


def _scope_schema_table(policy: dict) -> Tuple[str, str, str]:
    """Return (on_type_lower, schema, table) parsed from the policy attachment."""
    on_type = str(policy.get("on_securable_type") or policy.get("onSecurableType") or "").upper()
    fullname = str(policy.get("on_securable_fullname") or policy.get("onSecurableFullname") or "")
    parts = fullname.split(".")
    if on_type == "SCHEMA" and len(parts) >= 2:
        return "schema", parts[1], ""
    if on_type == "TABLE" and len(parts) >= 3:
        return "table", parts[1], parts[2]
    return "catalog", "", ""


def _candidate_tables(
    policy: dict,
    tables_by_schema: Dict[str, List[str]],
) -> List[Tuple[str, str]]:
    """All (schema, table) the policy's attachment scope covers."""
    on_type, schema, table = _scope_schema_table(policy)
    out: List[Tuple[str, str]] = []
    if on_type == "table":
        out.append((schema, table))
    elif on_type == "schema":
        out.extend((schema, t) for t in tables_by_schema.get(schema, []))
    else:  # catalog
        for sname, tnames in tables_by_schema.items():
            out.extend((sname, t) for t in tnames)
    return out


def resolve_policy(
    policy: dict,
    tags: GovernedTags,
    tables_by_schema: Dict[str, List[str]],
    columns_by_table: Dict[Tuple[str, str], List[str]],
) -> List[ResolvedTarget]:
    """Materialize a single ABAC policy into concrete targets.

    ``tables_by_schema``: {schema: [table, ...]} for the catalog.
    ``columns_by_table``: {(schema, table): [column, ...]}.
    """
    kind = _policy_kind(policy)
    if kind not in {"row_filter", "column_mask"}:
        return []

    spec = _policy_spec(policy, kind)
    function_name = str(
        spec.get("function_name") or spec.get("functionName") or ""
    )
    match_cols = _match_columns(policy)
    try:
        when = parse_tag_condition(
            policy.get("when_condition") or policy.get("whenCondition")
        )
        alias_conditions = {
            str(m.get("alias") or ""): parse_tag_condition(m.get("condition"))
            for m in match_cols
        }
    except ValueError:
        # Untranslatable condition syntax — caller records as skipped.
        return []

    policy_id = str(policy.get("id") or policy.get("name") or "")
    policy_name = str(policy.get("name") or "")
    to_principals = [str(p) for p in (policy.get("to_principals") or policy.get("toPrincipals") or []) if p]
    except_principals = [
        str(p) for p in (policy.get("except_principals") or policy.get("exceptPrincipals") or []) if p
    ]

    out: List[ResolvedTarget] = []
    for schema, table in _candidate_tables(policy, tables_by_schema):
        # Table-level WHEN condition (uses inherited table tags).
        if not when.evaluate(_lower_keys(tags.effective_table_tags(schema, table))):
            continue

        columns = columns_by_table.get((schema, table), [])
        # Evaluate each alias condition against each column's direct tags.
        alias_matches: Dict[str, List[str]] = {}
        for alias, cond in alias_conditions.items():
            matched = [
                col
                for col in columns
                if cond.evaluate(_lower_keys(tags.column_tags(schema, table, col)))
            ]
            alias_matches[alias] = matched

        # All match_columns conditions must match ≥1 column for the policy to apply.
        if alias_conditions and any(not cols for cols in alias_matches.values()):
            continue

        common = dict(
            function_name=function_name,
            policy_id=policy_id,
            policy_name=policy_name,
            when_condition=when.source,
            to_principals=to_principals,
            except_principals=except_principals,
        )

        if kind == "column_mask":
            on_alias = str(spec.get("on_column") or spec.get("onColumn") or "")
            masked_columns = alias_matches.get(on_alias) or []
            # No alias bound (rare) — fall back to any matched column.
            if not masked_columns and alias_matches:
                masked_columns = next(iter(alias_matches.values()))
            for masked in masked_columns:
                out.append(ResolvedTarget(
                    constraint_kind="column_mask",
                    schema=schema,
                    table=table,
                    column=masked,
                    input_columns=[masked],
                    **common,
                ))
        else:  # row_filter — applies to the whole table
            using_cols = _resolve_using_columns(spec, alias_matches)
            out.append(ResolvedTarget(
                constraint_kind="row_filter",
                schema=schema,
                table=table,
                input_columns=using_cols,
                **common,
            ))

    return out


def resolve_policies(
    policies: List[dict],
    tags: GovernedTags,
    tables_by_schema: Dict[str, List[str]],
    columns_by_table: Dict[Tuple[str, str], List[str]],
) -> List[ResolvedTarget]:
    out: List[ResolvedTarget] = []
    for policy in policies or []:
        if isinstance(policy, dict):
            out.extend(resolve_policy(policy, tags, tables_by_schema, columns_by_table))
    return out


def _resolve_using_columns(spec: dict, alias_matches: Dict[str, List[str]]) -> List[str]:
    """Map a row-filter ``using`` clause to concrete column names (constants dropped)."""
    cols: List[str] = []
    for entry in spec.get("using") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("alias")
        if alias and alias_matches.get(alias):
            cols.append(alias_matches[alias][0])
    return cols


def _lower_keys(tags: Dict[str, str]) -> Dict[str, str]:
    return {str(k).lower(): v for k, v in (tags or {}).items()}


# --- information_schema tag reader -------------------------------------------
def read_governed_tags(client, warehouse_id: str, catalog: str) -> GovernedTags:
    """Read governed-tag assignments for ``catalog`` from information_schema.

    ``client`` must expose ``ensure_sql_warehouse_running`` and ``execute_sql``
    (i.e. a DatabricksUCClient). Returns an empty snapshot if the tag tables are
    unavailable.
    """
    if not warehouse_id:
        raise ValueError("a SQL warehouse id is required to read governed tags")

    client.ensure_sql_warehouse_running(warehouse_id)
    gov = GovernedTags()

    def _rows(sql: str) -> List[List[str]]:
        resp = client.execute_sql(warehouse_id, sql)
        return ((resp.get("result") or {}).get("data_array")) or []

    base = f"{catalog}.information_schema"
    for tag_name, tag_value in _rows(
        f"SELECT tag_name, tag_value FROM {base}.catalog_tags"
    ):
        gov.catalog[str(tag_name)] = str(tag_value)

    for schema, tag_name, tag_value in _rows(
        f"SELECT schema_name, tag_name, tag_value FROM {base}.schema_tags"
    ):
        gov.schema.setdefault(str(schema), {})[str(tag_name)] = str(tag_value)

    for schema, table, tag_name, tag_value in _rows(
        f"SELECT schema_name, table_name, tag_name, tag_value FROM {base}.table_tags"
    ):
        gov.table.setdefault((str(schema), str(table)), {})[str(tag_name)] = str(tag_value)

    for schema, table, column, tag_name, tag_value in _rows(
        f"SELECT schema_name, table_name, column_name, tag_name, tag_value "
        f"FROM {base}.column_tags"
    ):
        gov.column.setdefault(
            (str(schema), str(table), str(column)), {}
        )[str(tag_name)] = str(tag_value)

    return gov
