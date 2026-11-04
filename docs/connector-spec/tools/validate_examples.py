"""Validates every file in examples/ against its schema. Exit code 1 on any failure.

Run:  pip install jsonschema referencing && python tools/validate_examples.py
Naming: examples/<schema-name>/*.json  ->  schemas/<schema-name>.schema.json
        examples/config/<type>*.json   ->  schemas/protocols/<type>.config.schema.json
        examples/invalid/<schema>__*.json must FAIL validation (negative tests)
"""
import json
import pathlib
import sys

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parent.parent
schemas = {}
for p in (ROOT / "schemas").rglob("*.schema.json"):
    doc = json.loads(p.read_text())
    schemas[doc["$id"]] = doc
registry = Registry().with_resources((k, Resource.from_contents(v)) for k, v in schemas.items())
BASE = "https://schemas.opcaresystems.com/daq/connector/1.0/"


def validator(schema_rel):
    schema = schemas[BASE + schema_rel]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=registry)


def schema_for(kind, name):
    if kind == "config":
        return f"protocols/{name.split('-')[0]}.config.schema.json"
    return f"{kind}.schema.json"


failures = 0
count = 0
for p in sorted((ROOT / "examples").rglob("*.json")):
    kind = p.parent.name
    doc = json.loads(p.read_text())
    if kind == "invalid":
        target, _ = p.stem.split("__", 1)
        rel = schema_for(*target.split("~")) if "~" in target else f"{target}.schema.json"
        errors = list(validator(rel).iter_errors(doc))
        ok = bool(errors)
        msg = f"rejected ({errors[0].message[:70]})" if ok else "WRONGLY ACCEPTED"
    else:
        errors = list(validator(schema_for(kind, p.stem)).iter_errors(doc))
        ok = not errors
        msg = "valid" if ok else "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
    count += 1
    failures += not ok
    print(("PASS " if ok else "FAIL ") + str(p.relative_to(ROOT)) + " - " + msg)

for p in (ROOT / "schemas").rglob("*.schema.json"):
    Draft202012Validator.check_schema(json.loads(p.read_text()))
print(f"\n{count} examples checked, {failures} failure(s); all {len(schemas)} schemas are valid JSON Schema 2020-12")
sys.exit(1 if failures else 0)
