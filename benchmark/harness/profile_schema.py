"""Comprehensive profile of the live OMOP_DEID schema -> JSON for downstream design."""
import json, sys, time
sys.path.insert(0, ".")
from db import query, connect

out = {"server": "QCDIDDWDB001", "database": "OMOP_DEID", "tables": {}, "vocabularies": [],
       "concept_domains": [], "notes": []}

# 1. All base tables + columns + types
cols, rows, _ = query("""
SELECT t.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, c.IS_NULLABLE
FROM INFORMATION_SCHEMA.TABLES t
JOIN INFORMATION_SCHEMA.COLUMNS c ON t.TABLE_NAME=c.TABLE_NAME AND t.TABLE_SCHEMA=c.TABLE_SCHEMA
WHERE t.TABLE_TYPE='BASE TABLE'
ORDER BY t.TABLE_NAME, c.ORDINAL_POSITION
""")
for tname, col, dtype, maxlen, nullable in rows:
    t = out["tables"].setdefault(tname, {"columns": [], "row_count": None})
    t["columns"].append({"name": col, "type": dtype, "len": maxlen, "nullable": nullable})

print(f"{len(out['tables'])} tables, profiling row counts...", flush=True)

# 2. Row counts (fast: sys.partitions estimate, exact for most)
cols, rows, _ = query("""
SELECT t.name AS tbl, SUM(p.rows) AS cnt
FROM sys.tables t
JOIN sys.partitions p ON t.object_id=p.object_id AND p.index_id IN (0,1)
GROUP BY t.name
""")
counts = {t: int(c) for t, c in rows}
for tname in out["tables"]:
    out["tables"][tname]["row_count"] = counts.get(tname)

# 3. Vocabularies present (if vocabulary table exists)
try:
    cols, rows, _ = query("SELECT vocabulary_id, COUNT(*) FROM concept GROUP BY vocabulary_id ORDER BY COUNT(*) DESC")
    out["vocabularies"] = [{"vocabulary_id": v, "concept_count": int(n)} for v, n in rows]
except Exception as e:
    out["notes"].append(f"vocabulary profiling failed: {e}")

# 4. Domains in concept
try:
    cols, rows, _ = query("SELECT domain_id, COUNT(*) FROM concept GROUP BY domain_id ORDER BY COUNT(*) DESC")
    out["concept_domains"] = [{"domain_id": d, "concept_count": int(n)} for d, n in rows]
except Exception as e:
    out["notes"].append(f"domain profiling failed: {e}")

with open("../schema/schema_profile.json", "w") as f:
    json.dump(out, f, indent=2)

# human summary
print("\n=== TABLES (rows) ===")
for tname in sorted(out["tables"], key=lambda x: -(out["tables"][x]["row_count"] or 0)):
    rc = out["tables"][tname]["row_count"]
    print(f"  {tname:40s} {rc:>14,}" if rc is not None else f"  {tname:40s}  ?")
print("\n=== TOP VOCABULARIES ===")
for v in out["vocabularies"][:20]:
    print(f"  {v['vocabulary_id']:25s} {v['concept_count']:>12,}")
print("\n=== DOMAINS ===")
for d in out["concept_domains"][:25]:
    print(f"  {d['domain_id']:25s} {d['concept_count']:>12,}")
print("\nsaved -> schema/schema_profile.json")
