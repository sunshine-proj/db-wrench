# ADR-0009: Sensitive-field masking

Status: Accepted

## Decision

Columns (relational) and field paths (MongoDB) can be **marked sensitive** per connection
(`PUT /connections/{id}/sensitive`, needs `connection.edit`). Users without
`data.read_sensitive` (not part of the Viewer/Editor roles; Database Admin and Super Admin hold
it) get:

- **Masked values** (`{"$masked": true}`; NULL stays NULL) in the grid, in cell fetches, in bulk
  previews, in write results and in exports (`[masked]` in CSV).
- **No probing.** Filtering, sorting, searching, comparing (`expected`) or writing a masked field
  is refused (403). Otherwise a binary search through `starts_with` filters, or a sort, would leak
  the value one character at a time.
- **No whole-document overwrite** of MongoDB documents in collections that have masked fields
  (the editor would write the marker back), and no inserts that set a masked path.
- **No free-form queries** on connections that have *any* marker. SQL and aggregation results
  cannot be masked reliably (aliases, expressions, joins, `$project`), so closing the door is the
  only safe default. Granting `data.read_sensitive` reopens it.
- Primary-key columns and `_id` cannot be marked: row keys and pagination cursors expose them.

Every marker is validated against live metadata when saved.

## Consequences

- Marking is per object, not per value: it is a coarse, auditable control, not row-level security.
- Exports and reads record which fields were masked in the audit detail.
- A user with query access *and* `data.read_sensitive` can read everything; the permission is the
  boundary, so grant it deliberately.
