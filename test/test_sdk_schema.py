"""`lium schema`: the shape of what `--format json` and `to_dict()` produce, derived from the dataclasses.

An agent parsing `lium ps --format json` needs the field names and types
without reading the source; generating the schema from the models means it
cannot drift from them.
"""

import json

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.sdk import ExecutorInfo, PodInfo, Template
from lium.sdk.schema import PUBLIC_MODELS, dataclass_schema, schemas


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="exec-1", huid="brave-otter-11", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
        gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={"country": "US"},
        specs={"gpu": {"driver": "550.0", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active", docker_in_docker=False, ip="1.2.3.4",
    )


def _pod() -> PodInfo:
    return PodInfo(
        id="pod-1", name="job", huid="swift-fox-c8", status="RUNNING", ssh_cmd="ssh root@1.2.3.4 -p 20299",
        ports={"22": 20299}, created_at="2026-01-01T00:00:00Z", updated_at="",
        executor=_executor(), template={"id": "tpl-1"}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


def test_schema_marks_optional_fields_as_nullable_and_lists_required_ones():
    schema = dataclass_schema(PodInfo)

    assert schema["properties"]["ssh_cmd"] == {"type": ["string", "null"]}
    assert "id" in schema["required"] and "enable_volume_encryption" not in schema["required"]
    assert schema["properties"]["executor"]["title"] == "ExecutorInfo"
    assert schema["properties"]["ssh_port"] == {"type": "integer"}


def test_schema_matches_to_dict_keys():
    """What the schema promises is exactly what to_dict() produces."""
    for instance in (_pod(), _executor(), Template("t", "n", "h", "img", "tag", "cat", "ok")):
        schema = dataclass_schema(type(instance))
        assert set(schema["properties"]) == set(instance.to_dict())


def test_schema_command_prints_the_public_models_by_default():
    result = CliRunner().invoke(cli, ["schema"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == set(PUBLIC_MODELS)
    assert payload == schemas()


def test_schema_command_selects_models_and_lists_them():
    one = CliRunner().invoke(cli, ["schema", "Template", "--compact"])
    assert one.exit_code == 0 and list(json.loads(one.output)) == ["Template"]
    assert "\n" not in one.output.strip()

    listing = CliRunner().invoke(cli, ["schema", "--list"])
    assert "PodInfo" in listing.output.split()


def test_schema_command_rejects_an_unknown_model_with_the_known_names():
    result = CliRunner().invoke(cli, ["schema", "Podinfo"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "PodInfo" in result.output
