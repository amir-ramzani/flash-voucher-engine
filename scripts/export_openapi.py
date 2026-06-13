"""Export the OpenAPI spec from the FastAPI app (no Redis/DynamoDB needed).

    python -m scripts.export_openapi            # writes docs/openapi.json + docs/openapi.yaml
    python -m scripts.export_openapi --check    # exit 1 if the committed files are stale

tests/test_openapi.py runs the --check logic, so CI fails if the spec drifts from the code.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

from api.app import create_app
from common.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = ROOT / "docs" / "openapi.json"
YAML_PATH = ROOT / "docs" / "openapi.yaml"

SERVERS = [
    {"url": "http://localhost:8000", "description": "Local (docker compose)"},
    {
        "url": "{apiUrl}",
        "description": "AWS (ApiUrl output of infra/stack.yaml)",
        "variables": {"apiUrl": {"default": "http://your-alb.example.com"}},
    },
]


def build_spec() -> dict:
    spec = create_app(Settings(_env_file=None)).openapi()
    spec["servers"] = SERVERS
    return spec


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def render(spec: dict) -> tuple[str, str]:
    as_json = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    as_yaml = yaml.dump(spec, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    return as_json, as_yaml


def is_stale() -> bool:
    as_json, as_yaml = render(build_spec())
    return not (
        JSON_PATH.exists()
        and YAML_PATH.exists()
        and JSON_PATH.read_text() == as_json
        and YAML_PATH.read_text() == as_yaml
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true", help="fail if committed spec is stale")
    args = p.parse_args()
    if args.check:
        if is_stale():
            print("docs/openapi.{json,yaml} are stale: run `make openapi`")
            return 1
        print("openapi spec up to date")
        return 0
    as_json, as_yaml = render(build_spec())
    JSON_PATH.write_text(as_json)
    YAML_PATH.write_text(as_yaml)
    print(f"wrote {JSON_PATH.relative_to(ROOT)} and {YAML_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
