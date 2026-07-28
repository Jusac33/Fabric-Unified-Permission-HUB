from __future__ import annotations

import pytest

from app.services.abac_resolver import (
    GovernedTags,
    parse_tag_condition,
    resolve_policies,
    resolve_policy,
)


# --- condition parser --------------------------------------------------------
def test_empty_condition_defaults_to_true() -> None:
    cond = parse_tag_condition("")
    assert cond.evaluate({}) is True
    assert parse_tag_condition(None).evaluate({"x": "y"}) is True


def test_has_tag_matches_presence() -> None:
    cond = parse_tag_condition("has_tag('pii')")
    assert cond.evaluate({"pii": "ssn"}) is True
    assert cond.evaluate({"other": "v"}) is False


def test_has_tag_value_matches_exact_value() -> None:
    cond = parse_tag_condition("has_tag_value('pii', 'ssn')")
    assert cond.evaluate({"pii": "ssn"}) is True
    assert cond.evaluate({"pii": "email"}) is False


def test_camelcase_functions_supported() -> None:
    assert parse_tag_condition("hasTag('x')").evaluate({"x": "1"}) is True
    assert parse_tag_condition("hasTagValue('x','1')").evaluate({"x": "1"}) is True


def test_and_or_combination() -> None:
    cond = parse_tag_condition(
        "has_tag_value('pii','email') AND has_tag('consent')"
    )
    assert cond.evaluate({"pii": "email", "consent": "yes"}) is True
    assert cond.evaluate({"pii": "email"}) is False

    cond_or = parse_tag_condition("has_tag('a') OR has_tag('b')")
    assert cond_or.evaluate({"b": "1"}) is True
    assert cond_or.evaluate({"c": "1"}) is False


def test_tag_name_is_case_insensitive() -> None:
    cond = parse_tag_condition("has_tag('PII')")
    assert cond.evaluate({"pii": "ssn"}) is True


def test_unsupported_syntax_raises() -> None:
    with pytest.raises(ValueError):
        parse_tag_condition("region = 'US'")


# --- governed tag inheritance ------------------------------------------------
def test_effective_table_tags_inherit_catalog_schema_table() -> None:
    gov = GovernedTags(
        catalog={"domain": "sales"},
        schema={"gold": {"tier": "curated"}},
        table={("gold", "orders"): {"tier": "raw"}},
    )
    eff = gov.effective_table_tags("gold", "orders")
    assert eff == {"domain": "sales", "tier": "raw"}  # table overrides schema


def test_column_tags_do_not_inherit() -> None:
    gov = GovernedTags(
        table={("gold", "orders"): {"pii": "yes"}},
        column={("gold", "orders", "ssn"): {"pii": "ssn"}},
    )
    assert gov.column_tags("gold", "orders", "ssn") == {"pii": "ssn"}
    assert gov.column_tags("gold", "orders", "amount") == {}


# --- resolution --------------------------------------------------------------
def _column_mask_policy(**over) -> dict:
    base = {
        "id": "p1",
        "name": "mask_pii",
        "policy_type": "POLICY_TYPE_COLUMN_MASK",
        "on_securable_type": "CATALOG",
        "on_securable_fullname": "main",
        "column_mask": {"function_name": "main.gov.mask", "on_column": "c"},
        "match_columns": [{"alias": "c", "condition": "has_tag_value('pii','ssn')"}],
        "to_principals": ["analysts"],
    }
    base.update(over)
    return base


def test_resolve_column_mask_targets_tagged_column() -> None:
    gov = GovernedTags(column={("gold", "orders", "ssn"): {"pii": "ssn"}})
    targets = resolve_policy(
        _column_mask_policy(),
        gov,
        {"gold": ["orders"]},
        {("gold", "orders"): ["id", "ssn", "amount"]},
    )
    assert len(targets) == 1
    t = targets[0]
    assert t.constraint_kind == "column_mask"
    assert (t.schema, t.table, t.column) == ("gold", "orders", "ssn")
    assert t.function_name == "main.gov.mask"


def test_resolve_column_mask_skips_table_without_tag() -> None:
    gov = GovernedTags()  # no tags
    targets = resolve_policy(
        _column_mask_policy(),
        gov,
        {"gold": ["orders"]},
        {("gold", "orders"): ["id", "ssn"]},
    )
    assert targets == []


def test_resolve_respects_when_condition_on_table_tags() -> None:
    gov = GovernedTags(
        table={("gold", "orders"): {"hr": "true"}},
        column={("gold", "orders", "ssn"): {"pii": "ssn"}},
    )
    policy = _column_mask_policy(when_condition="has_tag('hr')")
    # table has hr tag -> applies
    targets = resolve_policy(
        policy, gov, {"gold": ["orders"]}, {("gold", "orders"): ["ssn"]}
    )
    assert len(targets) == 1

    # different table lacking hr tag -> skipped
    gov2 = GovernedTags(column={("gold", "p2"): {"pii": "ssn"}})
    targets2 = resolve_policy(
        policy, gov2, {"gold": ["p2"]}, {("gold", "p2"): ["ssn"]}
    )
    assert targets2 == []


def test_resolve_row_filter_applies_to_table_with_matching_column() -> None:
    gov = GovernedTags(column={("sales", "emea", "region"): {"region": ""}})
    policy = {
        "id": "rf1",
        "name": "regional",
        "policy_type": "POLICY_TYPE_ROW_FILTER",
        "on_securable_type": "SCHEMA",
        "on_securable_fullname": "main.sales",
        "row_filter": {
            "function_name": "main.gov.by_region",
            "using": [{"alias": "rgn"}, {"constant": "EMEA"}],
        },
        "match_columns": [{"alias": "rgn", "condition": "has_tag('region')"}],
        "to_principals": ["emea"],
    }
    targets = resolve_policy(
        policy,
        gov,
        {"sales": ["emea"]},
        {("sales", "emea"): ["region", "amount"]},
    )
    assert len(targets) == 1
    t = targets[0]
    assert t.constraint_kind == "row_filter"
    assert (t.schema, t.table) == ("sales", "emea")
    # `using` alias resolved to the tagged column; constant dropped from column list.
    assert t.input_columns == ["region"]


def test_resolve_table_scope_only_targets_that_table() -> None:
    gov = GovernedTags(column={("gold", "orders", "ssn"): {"pii": "ssn"}})
    policy = _column_mask_policy(
        on_securable_type="TABLE", on_securable_fullname="main.gold.orders"
    )
    targets = resolve_policy(
        policy,
        gov,
        {"gold": ["orders", "other"]},
        {("gold", "orders"): ["ssn"], ("gold", "other"): ["ssn"]},
    )
    assert {(t.schema, t.table) for t in targets} == {("gold", "orders")}


def test_resolve_policies_skips_untranslatable_condition() -> None:
    gov = GovernedTags()
    policy = _column_mask_policy(
        match_columns=[{"alias": "c", "condition": "region = 'US'"}]
    )
    assert resolve_policies([policy], gov, {"gold": ["orders"]},
                            {("gold", "orders"): ["ssn"]}) == []
