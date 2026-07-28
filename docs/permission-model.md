# Permission Model Notes

Fabric Unified Permission Hub compares and applies permissions across two different models:

- **Microsoft Fabric / OneLake**: workspace roles plus OneLake Data Access Roles.
- **Databricks Unity Catalog**: a three-level namespace of `catalog.schema.table` with grants on catalog, schema, and table securables.

## Databricks Unity Catalog assumptions

- Catalog grants apply at the catalog level and can inherit down to schemas and tables.
- Schema grants use full two-level names such as `main.gold`.
- Table grants use full three-level names such as `main.gold.orders`.
- Object-use privileges are scope-specific:
  - catalog: `USE_CATALOG`
  - schema: `USE_SCHEMA`
  - table: no direct object-use grant is emitted by this app
- Data privileges are scope-specific:
  - catalog read/write: `USE_CATALOG + SELECT` / `USE_CATALOG + MODIFY`
  - schema read/write: `USE_SCHEMA + SELECT` / `USE_SCHEMA + MODIFY`
  - table read/write: `SELECT` / `MODIFY`
- Schema and table grants also emit required parent grants:
  - schema/table: `USE_CATALOG` on the parent catalog
  - table: `USE_SCHEMA` on the parent schema

## Fabric to Databricks apply behavior

The apply engine uses the diff row scope to pick the Unity Catalog target:

| Diff scope | UC target |
| --- | --- |
| `workspace:<workspace-id>` | paired catalog |
| `catalog:<catalog>` | catalog |
| `schema:<catalog>.<schema>` | schema |
| `table:<catalog>.<schema>.<table>` | table |

Dry-run remains the default. Real applies are audited to `audits/permission-applies.jsonl`.

## Databricks to Fabric apply behavior

- Catalog and workspace scope map to Fabric workspace roles.
- Table-scope read access maps to OneLake Data Access Roles only when an exact `MirroredAzureDatabricksCatalog` display-name match is found for the paired UC catalog.
- Schema-only scope does not have an equivalent OneLake DAR path and is skipped rather than broadened to a workspace role.
- Mirrored Databricks catalog items are read-only from Fabric; write/admin classes are skipped rather than reported as successful read-only downgrades.

## Row and column security

- Databricks Unity Catalog supports row filters and column masks through SQL functions and table metadata.
- Fabric OneLake Data Access Security supports row and column constraints inside `decisionRules[].constraints`; `permission` itself must contain exactly `Path` and `Action`.
- Fabric column constraints use `constraints.columns` with `tablePath`, case-sensitive `columnNames`, `columnEffect: Permit`, and `columnAction: ["Read"]`.
- Fabric row constraints use `constraints.rows` with `tablePath` and a T-SQL predicate query. In the mirrored Databricks catalog tested here, the row query needed the schema-qualified table name, for example `select * from uph_test.table_name where country = 'US'`.
- The current app discovers and compares Unity Catalog row filters/column masks and Fabric OneLake row/column constraints as fine-grained security rows.
- Simple Unity Catalog BOOLEAN row-filter functions can be translated to Fabric OneLake DAS row predicates, and Fabric DAS row predicates can be represented as Unity Catalog row-filter functions when they map cleanly.
- Unity Catalog column masks can be represented as Fabric DAS column visibility constraints by excluding the masked column; Fabric DAS does not reproduce the original mask expression.
- Fabric DAS column constraints can be represented as Unity Catalog column masks when the hidden-column semantics map cleanly. Unsupported rows are skipped safely instead of being broadened.

## Attribute-based access control (ABAC)

- Unity Catalog ABAC policies are tag-driven: a single policy attached at a catalog, schema, or table applies a row filter or column mask to every object whose governed tags satisfy the policy conditions (`has_tag` / `has_tag_value`).
- The app discovers ABAC policies via `GET /api/2.1/unity-catalog/policies/{type}/{fullname}` at catalog, schema, and table scope. Workspaces without ABAC respond 4xx/501 and degrade to an empty result.
- Fabric OneLake DAS has no tag-driven equivalent; constraints bind to specific table paths. To mirror ABAC into Fabric the app **materializes** each policy: when a SQL warehouse is configured (`DBX_WAREHOUSE_ID`) it reads governed-tag assignments from `<catalog>.information_schema.{catalog,schema,table,column}_tags`, evaluates each policy's `when_condition` (table tags, with catalog→schema→table inheritance) and `match_columns` conditions (direct column tags only), and emits one concrete row-filter / column-mask row per matching table/column.
- Materialized ABAC rows (`source: databricks_abac_resolved`) flow through the normal Fabric DAS apply path. Unresolved or warehouse-unavailable policies (`source: databricks_abac`) are surfaced for review but skipped on apply.
- An active ABAC policy also surfaces through the table's effective masks/row filters (reported by `get_table`), so column masks are discovered even without a warehouse. When a resolved ABAC target produces the same Fabric constraint as an effective mask already recorded, the rows are deduped and the surviving row is annotated with `abac_driven: true` and the `abac_policy` name so provenance is preserved.
- The diff view flags ABAC-driven row/column constraints with an **ABAC** badge; expanding the constraint shows the policy name and tag `when` condition.
- Materialization is a point-in-time snapshot. Tables tagged after a run do not automatically receive the Fabric constraint; re-run to refresh. Policies whose UDF or tag condition cannot be translated are skipped safely with a `translation_error` rather than broadened.

## Known limitations

- Unity Catalog object names containing literal periods are not safely represented by the current dotted scope string format.
- Schema-level Fabric grants require careful interpretation because OneLake DAR operates on paths, not pure schema securables.
- DBX-to-Fabric table applies require an exact mirrored Fabric item match. Store or name the mirrored item consistently with the UC catalog before applying.
- Audit logs can contain sensitive principal and scope data; keep `audits/` private and define a retention policy before production use.
