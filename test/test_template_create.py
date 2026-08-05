from lium.cli.up.actions import CreateEphemeralTemplateAction
from lium.sdk import Config, Lium


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _stub_request(captured: list, response_payload: dict):
    def fake_request(method, endpoint, **kwargs):
        captured.append((method, endpoint, kwargs))
        return _Response(response_payload)

    return fake_request


def test_create_template_defaults_one_time_template_to_false(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    captured: list = []
    monkeypatch.setattr(
        client,
        "_request",
        _stub_request(captured, {"id": "t1", "name": "ephemeral-abc"}),
    )

    client.create_template(name="ephemeral-abc", docker_image="alpine")

    _, _, kwargs = captured[0]
    assert kwargs["json"]["one_time_template"] is False


def test_create_template_passes_one_time_template_true(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    captured: list = []
    monkeypatch.setattr(
        client,
        "_request",
        _stub_request(captured, {"id": "t2", "name": "ephemeral-abc"}),
    )

    client.create_template(
        name="ephemeral-abc",
        docker_image="alpine",
        one_time_template=True,
    )

    _, _, kwargs = captured[0]
    assert kwargs["json"]["one_time_template"] is True


def test_ephemeral_action_marks_template_one_time(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    captured: list = []
    monkeypatch.setattr(
        client,
        "_request",
        _stub_request(captured, {"id": "t3", "name": "ephemeral-xyz"}),
    )

    action = CreateEphemeralTemplateAction()
    result = action.execute(
        {
            "lium": client,
            "image": "alpine:latest",
            "env": {},
            "entrypoint": "",
            "cmd": "",
            "ports": [22],
        }
    )

    assert result.ok is True
    _, endpoint, kwargs = captured[0]
    assert endpoint == "/templates"
    assert kwargs["json"]["one_time_template"] is True
    assert kwargs["json"]["is_private"] is True
    assert kwargs["json"]["name"].startswith("ephemeral-")
