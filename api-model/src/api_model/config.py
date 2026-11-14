"""Strict configuration, canonical hashes, and atomic audit serialization."""

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def load_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=_unique, parse_constant=_invalid)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Expected a finite monetary number")
    try:
        number = Decimal(str(value))
    except Exception:
        raise ValueError("Expected a finite monetary number") from None
    if not number.is_finite():
        raise ValueError("Expected a finite monetary number")
    return number


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def validate(value, name):
    schema = json.loads(files("api_model").joinpath(f"schemas/{name}.json").read_text())
    Draft202012Validator.check_schema(schema)

    def finite(node, spec):
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError("Non-finite configuration number")
        if spec.get("type") in ("integer", "number") and isinstance(node, bool):
            raise ValueError("Boolean supplied for a numeric field")
        if isinstance(node, dict):
            for key, child in node.items():
                finite(child, spec.get("properties", {}).get(key, {}))
        if isinstance(node, list):
            for child in node:
                finite(child, spec.get("items", {}))
    finite(value, schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise ValueError(f"Invalid {name} configuration at {list(errors[0].path)}: {errors[0].message}")
    if name == "trading":
        capital, limits = value["capital"], value["position_limits"]
        initial = money(capital["initial_capital"])
        if money(capital["max_deployed_value"]) != initial * money(capital["max_deployed_pct"]) / 100:
            raise ValueError("Deployment value must equal starting balance times deployment percent")
        if money(limits["max_position_value"]) != initial * money(limits["max_position_pct"]) / 100:
            raise ValueError("Position value must equal starting balance times position percent")
        if limits["max_position_value"] > capital["max_deployed_value"]:
            raise ValueError("Position ceiling exceeds portfolio ceiling")
    elif value["train_fraction"] + value["validation_fraction"] >= 0.95:
        raise ValueError("Chronological splits must leave an untouched test partition")
    return json.loads(canonical(value))


def load_config(path, name="trading"):
    return validate(load_json(path), name)
