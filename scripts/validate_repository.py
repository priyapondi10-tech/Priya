#!/usr/bin/env python3
"""Strict JSON/YAML/TOML and Markdown structure/local-link validation (no network)."""

import copy
import re
import sys
import tomllib
from itertools import pairwise
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from api_model.config import load_config, load_json
from jsonschema import Draft202012Validator
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "results", "data", "artifacts", "build", "dist"}
PARSER = MarkdownIt("commonmark").enable("table")


class UniqueLoader(yaml.SafeLoader):
    pass


UniqueLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for key, rules in UniqueLoader.yaml_implicit_resolvers.items():
    UniqueLoader.yaml_implicit_resolvers[key] = [(tag, expression) for tag, expression in rules if tag != "tag:yaml.org,2002:bool"]
UniqueLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF"))


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def headings(text):
    tokens, slugs, seen = PARSER.parse(text), set(), {}
    levels = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            levels.append(int(token.tag[1:]))
            title = tokens[index + 1].content
            slug = re.sub(r"[^\w -]", "", title.lower()).replace(" ", "-")
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            slugs.add(slug if not count else f"{slug}-{count}")
    return tokens, slugs, levels


def markdown(path):
    text = path.read_text()
    if not text.endswith("\n"):
        raise ValueError("Markdown must end with a newline")
    fence = None
    for line in text.splitlines():
        if line.rstrip() != line:
            raise ValueError("Markdown contains trailing whitespace")
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if match:
            marker, suffix = match.groups()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = None
    if fence:
        raise ValueError("Unclosed Markdown code fence")
    tokens, _, levels = headings(text)
    if not levels or levels[0] != 1 or levels.count(1) != 1 or any(b > a + 1 for a, b in pairwise(levels)):
        raise ValueError("Markdown requires one H1 and non-skipping heading hierarchy")
    for token in tokens:
        for child in token.children or []:
            if child.type not in {"link_open", "image"}:
                continue
            target = child.attrGet("href") if child.type == "link_open" else child.attrGet("src")
            parsed = urlsplit(target or "")
            if parsed.scheme or parsed.netloc:
                continue
            destination = (ROOT / unquote(parsed.path).lstrip("/")) if parsed.path.startswith("/") else (path.parent / unquote(parsed.path)) if parsed.path else path
            if not destination.exists():
                raise ValueError(f"Broken local Markdown link: {target}")
            if parsed.fragment and destination.suffix == ".md" and unquote(parsed.fragment) not in headings(destination.read_text())[1]:
                raise ValueError(f"Broken Markdown heading link: {target}")


def main():
    counts = {"json": 0, "yaml": 0, "markdown": 0, "toml": 0}
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in SKIP or part.endswith(".egg-info") for part in relative.parts):
            continue
        try:
            if path.suffix == ".json":
                data = load_json(path)
                if "schemas" in relative.parts:
                    Draft202012Validator.check_schema(data)
                counts["json"] += 1
            elif path.suffix in {".yaml", ".yml"}:
                workflow = yaml.load(path.read_text(), Loader=UniqueLoader)
                if ".github/workflows" in str(relative):
                    if not isinstance(workflow, dict) or not {"name", "on", "jobs", "permissions"}.issubset(workflow):
                        raise ValueError("Invalid GitHub workflow structure")
                    if workflow["permissions"].get("contents") != "read":
                        raise ValueError("Workflows require read-only repository content permissions")
                counts["yaml"] += 1
            elif path.suffix == ".md":
                markdown(path)
                counts["markdown"] += 1
            elif path.suffix == ".toml":
                tomllib.loads(path.read_text())
                counts["toml"] += 1
        except Exception as error:
            raise ValueError(f"{relative}: {error}") from error
    load_config(ROOT / "config/trading.example.json")
    load_config(ROOT / "api-model/config/training.json", "training")
    print(f"Repository validation passed: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
