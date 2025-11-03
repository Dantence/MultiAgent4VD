#!/usr/bin/env python3
"""
infer_json_schema.py

Quickly infer a human-readable "schema" from a large/complex JSON file.

Features:
- Works with a single JSON object, a JSON array, or NDJSON (one JSON per line).
- Merges heterogeneous structures (e.g., arrays with mixed element types).
- Tracks field presence %, data types, and collects small example values.
- Emits: (1) readable tree summary, (2) optional JSON Schema (draft-like) approximation.

Usage:
  python infer_json_schema.py path/to/file.json [--ndjson] [--sample 10000] [--jsonschema out.json]

Notes:
- For big files, use --sample to limit processed records (random reservoir sampling).
- NDJSON: add --ndjson if your file is "one JSON object per line".
"""

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict, Counter
from typing import Any, Dict, List, Tuple, Union, Optional

Json = Any

PRIMITIVES = ("null", "boolean", "integer", "number", "string")
COMPLEX = ("object", "array")

def jtype(x: Any) -> str:
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "boolean"
    if isinstance(x, int) and not isinstance(x, bool):
        return "integer"
    if isinstance(x, float):
        return "number"
    if isinstance(x, str):
        return "string"
    if isinstance(x, dict):
        return "object"
    if isinstance(x, list):
        return "array"
    return type(x).__name__

def ensure_schema() -> Dict[str, Any]:
    return {
        "types": Counter(),         # type -> count
        "examples": [],             # small list of example values
        "object": {                 # object-specific
            "properties": dict(),   # name -> schema
            "presence": Counter()   # name -> #objects where present
        },
        "array": {                  # array-specific
            "items": None,          # merged schema of all elements
            "lengths": Counter()    # length -> count
        },
        "_observations": 0          # how many values merged into this node
    }

def merge_examples(dst: List[Any], value: Any, max_examples: int = 3) -> None:
    # Lightly deduplicate stringified examples
    s = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if s not in {json.dumps(v, ensure_ascii=False, sort_keys=True) for v in dst}:
        if len(dst) < max_examples:
            dst.append(value)

def merge_schema(dst: Dict[str, Any], value: Any) -> Dict[str, Any]:
    dst["_observations"] += 1
    t = jtype(value)
    dst["types"][t] += 1
    merge_examples(dst["examples"], value)

    if t == "object":
        props = dst["object"]["properties"]
        dst["object"]["presence"]["__objects__"] += 1
        for k, v in value.items():
            dst["object"]["presence"][k] += 1
            if k not in props:
                props[k] = ensure_schema()
            props[k] = merge_schema(props[k], v)
    elif t == "array":
        arr = dst["array"]
        arr["lengths"][len(value)] += 1
        # Merge each element into a single "items" schema
        if value:
            if arr["items"] is None:
                arr["items"] = ensure_schema()
            for el in value:
                arr["items"] = merge_schema(arr["items"], el)
        else:
            # empty array: record that items is unknown but array exists
            if arr["items"] is None:
                arr["items"] = ensure_schema()
    else:
        # primitives handled via types/examples above
        pass
    return dst

def merge_nodes(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    # Merge two schema nodes
    out = ensure_schema()
    # Merge counters and obs
    out["_observations"] = a["_observations"] + b["_observations"]
    out["types"] = a["types"] + b["types"]
    # Merge examples
    for ex in a["examples"]:
        merge_examples(out["examples"], ex)
    for ex in b["examples"]:
        merge_examples(out["examples"], ex)
    # Merge object
    out["object"]["presence"] = a["object"]["presence"] + b["object"]["presence"]
    props = {}
    keys = set(a["object"]["properties"]) | set(b["object"]["properties"])
    for k in keys:
        if k in a["object"]["properties"] and k in b["object"]["properties"]:
            props[k] = merge_nodes(a["object"]["properties"][k], b["object"]["properties"][k])
        elif k in a["object"]["properties"]:
            props[k] = a["object"]["properties"][k]
        else:
            props[k] = b["object"]["properties"][k]
    out["object"]["properties"] = props
    # Merge array
    out["array"]["lengths"] = a["array"]["lengths"] + b["array"]["lengths"]
    if a["array"]["items"] and b["array"]["items"]:
        out["array"]["items"] = merge_nodes(a["array"]["items"], b["array"]["items"])
    else:
        out["array"]["items"] = a["array"]["items"] or b["array"]["items"]
    return out

def observations(n: Dict[str, Any]) -> int:
    return max(1, n.get("_observations", 1))

def pct(part: int, whole: int) -> float:
    if whole <= 0: return 0.0
    return (100.0 * part) / float(whole)

def format_types(types: Counter, total: int) -> str:
    items = []
    for t, c in types.most_common():
        items.append(f"{t} ({pct(c, total):.1f}%)")
    return ", ".join(items) if items else "unknown"

def indent(s: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in s.splitlines())

def render_summary(node: Dict[str, Any], name: Optional[str], depth: int = 0) -> str:
    total = observations(node)
    header = f"{name if name is not None else '$'}: types=[{format_types(node['types'], total)}], observed={total}"
    lines = [header]
    # Examples
    if node["examples"]:
        # Keep examples brief
        try:
            ex_str = json.dumps(node["examples"], ensure_ascii=False)[:200]
        except Exception:
            ex_str = str(node["examples"])[:200]
        lines.append(f"  examples: {ex_str}")
    # Object details
    if node["types"].get("object", 0) > 0 or node["object"]["properties"]:
        pres = node["object"]["presence"]
        obj_count = max(1, pres.get("__objects__", 0))
        if node["object"]["properties"]:
            lines.append("  object.properties:")
            for k in sorted(node["object"]["properties"]):
                child = node["object"]["properties"][k]
                presence = pres.get(k, 0)
                req = pct(presence, obj_count)
                req_label = "required" if math.isclose(req, 100.0, abs_tol=1e-6) else f"present {req:.1f}%"
                child_str = render_summary(child, k, depth + 2)
                # inject presence label below the child header
                child_lines = child_str.splitlines()
                if child_lines:
                    child_lines[0] += f" [{req_label}]"
                lines.append(indent("\n".join(child_lines), 4))
    # Array details
    if node["types"].get("array", 0) > 0 or node["array"]["lengths"]:
        lens = ", ".join(f"{ln}×{ct}" for ln, ct in sorted(node["array"]["lengths"].items()))
        lines.append(f"  array.lengths: {lens if lens else 'unknown'}")
        if node["array"]["items"]:
            lines.append("  array.items:")
            lines.append(indent(render_summary(node["array"]["items"], None, depth + 2), 4))
    return "\n".join(lines)

def to_jsonschema(node: Dict[str, Any]) -> Dict[str, Any]:
    # Approximate JSON Schema (draft-07 like)
    total = observations(node)
    types = [t for t, c in node["types"].items() if c > 0]
    if not types:
        types = ["null", "object", "array", "string", "number", "integer", "boolean"]

    schema: Dict[str, Any] = {}
    if len(types) == 1:
        schema["type"] = types[0]
    else:
        schema["type"] = types

    if "object" in types or node["object"]["properties"]:
        props_schema = {}
        required = []
        pres = node["object"]["presence"]
        obj_count = max(1, pres.get("__objects__", 0))
        for k, child in sorted(node["object"]["properties"].items()):
            props_schema[k] = to_jsonschema(child)
            if pct(pres.get(k, 0), obj_count) >= 99.9:
                required.append(k)
        schema["properties"] = props_schema
        if required:
            schema["required"] = required

    if "array" in types or node["array"]["items"]:
        schema["items"] = to_jsonschema(node["array"]["items"] or ensure_schema())

    # Attach examples if primitive or small
    if node["examples"]:
        try:
            schema["examples"] = node["examples"]
        except Exception:
            pass
    return schema

def reservoir_sample_lines(path: str, k: int) -> List[str]:
    sample: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i <= k:
                sample.append(line)
            else:
                j = random.randint(1, i)
                if j <= k:
                    sample[j - 1] = line
    return sample

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Infer JSON schema-like summary from a file.")
    p.add_argument("path", help="Path to JSON file (object, array, or NDJSON).")
    p.add_argument("--ndjson", action="store_true", help="Treat input as NDJSON (one JSON value per line).")
    p.add_argument("--sample", type=int, default=0, help="If >0, sample up to N records (arrays or NDJSON).")
    p.add_argument("--jsonschema", type=str, default=None, help="Optional path to write JSON Schema to (e.g., schema.json).")
    return p.parse_args()

def load_data(path: str, ndjson: bool, sample_n: int) -> Tuple[List[Json], str]:
    if ndjson:
        lines = reservoir_sample_lines(path, sample_n) if sample_n > 0 else open(path, "r", encoding="utf-8").read().splitlines()
        data = [json.loads(ln) for ln in lines if ln.strip()]
        shape = f"NDJSON with {len(data)} lines"
        return data, shape
    else:
        with open(path, "r", encoding="utf-8") as f:
            root = json.load(f)
        if isinstance(root, list):
            data = root if sample_n <= 0 else random.sample(root, min(sample_n, len(root)))
            shape = f"JSON array with {len(root)} elements (processed {len(data)})"
            return data, shape
        else:
            shape = "single JSON object"
            return [root], shape

def main() -> None:
    args = parse_args()
    path = args.path
    if not os.path.exists(path):
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(2)

    data, shape = load_data(path, args.ndjson, args.sample)
    root_schema = ensure_schema()
    for idx, item in enumerate(data):
        root_schema = merge_schema(root_schema, item)

    print(f"# Inferred schema summary ({shape})\n")
    print(render_summary(root_schema, name="$"))
    print("\n# Tips")
    print("- 'required' means a field appeared in ~100% of observed objects at that level; 'present X%' shows partial presence.")
    print("- For very large data, re-run with --sample N or use --ndjson for line-delimited logs.")
    print("- Use --jsonschema schema.json to write an approximate JSON Schema.")

    if args.jsonschema:
        schema_obj = to_jsonschema(root_schema)
        with open(args.jsonschema, "w", encoding="utf-8") as f:
            json.dump(schema_obj, f, ensure_ascii=False, indent=2)
        print(f"\nWrote JSON Schema to: {args.jsonschema}")

if __name__ == "__main__":
    main()
