"""`lium workspaces`, `--workspace`, `lium keys`, `Lium.workspaces`, `PodInfo.workspace_id` (lium-platform
DAH-2975 / DAH-2986 / DAH-3030 / DAH-3031).

HTTP is answered from the recorded fixtures in test/fixtures/workspaces with `responses`, so the real SDK
and CLI code paths run. The switch-off cases pin today's behaviour: a server whose GET /users/me has no
`workspace` gets exactly the same requests and output as before.
"""

import json
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ps import display as ps_display
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import Config, Lium
from lium.sdk.exceptions import LiumAuthError, LiumError
from lium.sdk.workspaces import NEEDS_SESSION, NOT_ENABLED

FIXTURES = Path(__file__).parent / "fixtures" / "workspaces"
API = "https://lium.io/api"
RESEARCH = "9d8c7b6a-5f4e-4d3c-8b2a-190807060504"
PERSONAL = "11111111-2222-4333-8444-555555555555"
BEN = "7a9e2b1c-3d4f-4e5a-8b6c-1d2e3f4a5b6c"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty ~/.lium with one API key and an ssh key path, so ensure_config() never prompts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_WORKSPACE", raising=False)
    monkeypatch.delenv("LIUM_SESSION_TOKEN", raising=False)
    (tmp_path / ".lium").mkdir()
    (tmp_path / ".lium" / "config.ini").write_text("[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n")
    # the CLI's settings singleton read the real file at import; point it at this one
    from lium.cli import settings

    settings.config.config_dir = tmp_path / ".lium"
    settings.config.config_file = tmp_path / ".lium" / "config.ini"
    settings.config._config = settings.config._load_config()
    return tmp_path


def config_text(home) -> str:
    return (home / ".lium" / "config.ini").read_text()


def me(name="users_me_research"):
    responses.add(responses.GET, f"{API}/users/me", json=fixture(name))


def run(*args, **kwargs):
    return CliRunner().invoke(cli, list(args), **kwargs)


# ------------------------------------------------------------------------------------------------- SDK
@responses.activate
def test_sdk_reads_the_workspace_a_key_acts_in_from_users_me():
    me()
    lium = Lium(Config(api_key="k"))

    current = lium.workspaces.current()

    assert lium.workspaces.enabled() is True
    assert (current.id, current.name, current.role, current.is_personal) == (RESEARCH, "Research", "owner", False)
    assert len(responses.calls) == 1  # cached: the capability check does not call twice


@responses.activate
def test_sdk_sees_no_workspaces_on_a_server_without_them():
    me("users_me_off")
    lium = Lium(Config(api_key="k"))

    assert lium.workspaces.enabled() is False and lium.workspaces.current() is None
    with pytest.raises(LiumError, match=NOT_ENABLED):
        lium.workspaces.require_enabled()


@responses.activate
def test_sdk_reads_with_the_key_and_writes_with_a_session():
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.POST, f"{API}/users/login", json=fixture("login"))
    responses.add(responses.POST, f"{API}/workspaces", json=fixture("workspaces_session")[1])
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))
    lium = Lium(Config(api_key="k"))

    listed = lium.workspaces.list()
    members = lium.workspaces.members(RESEARCH)
    with pytest.raises(LiumAuthError, match="browser session"):
        lium.workspaces.create("Nope")
    token = lium.workspaces.login("ana@example.com", "pw")
    created = lium.workspaces.create("Research")
    key = lium.workspaces.create_key("ci", RESEARCH)

    assert [w.name for w in listed] == ["Research"] and [m.role for m in members] == ["owner", "admin", "member"]
    key_calls = [c for c in responses.calls if c.request.url.endswith("/workspaces") and c.request.method == "GET"]
    assert key_calls[0].request.headers["X-API-KEY"] == "k" and "Authorization" not in key_calls[0].request.headers
    login = next(c for c in responses.calls if c.request.url.endswith("/users/login"))
    assert "X-API-KEY" not in login.request.headers and json.loads(login.request.body)["email"] == "ana@example.com"
    assert token == "eyJ.fixture.session" and created.name == "Research"
    post_key = next(c for c in responses.calls if c.request.url.endswith("/keys"))
    # the key is minted with the session, in the named workspace, and never with the API key header
    assert post_key.request.headers["Authorization"] == "Bearer eyJ.fixture.session"
    assert post_key.request.headers["X-Lium-Workspace-Id"] == RESEARCH and "X-API-KEY" not in post_key.request.headers
    assert key["workspace_id"] == RESEARCH


@responses.activate
def test_sdk_pod_info_carries_workspace_id_and_none_without_workspaces():
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_off"))
    lium = Lium(Config(api_key="k"))

    with_workspace = lium.ps()[0]
    without = lium.ps()[0]

    assert with_workspace.workspace_id == RESEARCH and without.workspace_id is None
    assert "workspace_id" in ps_display.compact_pod(with_workspace)
    assert "workspace_id" not in ps_display.compact_pod(without)  # today's JSON, byte for byte


def test_config_picks_the_key_of_the_requested_or_active_workspace(home, monkeypatch):
    (home / ".lium" / "config.ini").write_text(
        "[api]\napi_key = sk_test_default\n[workspaces]\nactive = research\n"
        "[workspace.research]\nid = 9d8c\napi_key = sk_test_research\n"
    )

    default = Config.load()
    explicit = Config.load(workspace="research")
    env_key = None
    monkeypatch.setenv("LIUM_API_KEY", "sk_test_env")
    env_key = Config.load()
    explicit_over_env = Config.load(workspace="research")
    unknown = Config.load(workspace="nowhere")

    assert (default.api_key, default.workspace) == ("sk_test_research", "research")  # `lium workspaces use`
    assert explicit.api_key == "sk_test_research"
    assert (env_key.api_key, env_key.workspace) == ("sk_test_env", "research")  # env beats the stored default
    assert explicit_over_env.api_key == "sk_test_research"  # an explicit --workspace beats env
    assert (unknown.api_key, unknown.workspace) == ("sk_test_env", "nowhere")  # no key for it: fall through


def test_config_without_any_workspace_section_is_todays(home):
    config = Config.load()

    assert (config.api_key, config.workspace, config.session_token) == ("sk_test_default", None, None)


# ------------------------------------------------------------------------------------------------- CLI
@responses.activate
def test_workspaces_list_with_a_key_shows_its_workspace_and_how_to_see_the_rest(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    result = run("workspaces")

    assert result.exit_code == 0, result.output
    assert "Research" in result.output and "owner" in result.output and "*" in result.output
    assert "lium workspaces login" in result.output


@responses.activate
def test_workspaces_list_json_with_a_session_lists_every_workspace(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    result = run("workspaces", "list", "--json")

    assert result.exit_code == 0, result.output
    assert [w["name"] for w in json.loads(result.output)] == ["Ana's workspace", "Research"]
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer eyJ.fixture.session"


@responses.activate
def test_workspaces_say_when_the_server_has_none_and_everything_else_is_unchanged(home):
    me("users_me_off")
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_off"))

    workspaces = run("workspaces")
    ps = run("ps", "--format", "json")

    assert workspaces.exit_code == EXIT_API_ERROR and NOT_ENABLED in workspaces.output
    assert ps.exit_code == 0, ps.output
    assert "workspace" not in ps.output.lower()
    # `ps --format json` makes its one request and no more: the context line is table-only
    assert [c.request.url.rsplit("/", 1)[-1] for c in responses.calls] == ["me", "pods"]


@responses.activate
def test_ps_names_the_workspace_under_the_table_and_in_json(home):
    me()
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    table = run("ps")
    as_json = run("ps", "--format", "json")

    assert table.exit_code == 0, table.output
    assert "Workspace: Research (owner)" in table.output
    assert json.loads(as_json.output)[0]["workspace_id"] == RESEARCH


@responses.activate
def test_workspace_flag_picks_the_saved_key_and_warns_when_the_key_acts_elsewhere(home):
    (home / ".lium" / "config.ini").write_text(
        "[api]\napi_key = sk_test_default\n[ssh]\nkey_path = /dev/null\n"
        f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_research\n"
    )
    me()
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))
    me("users_me_personal")
    responses.add(responses.GET, f"{API}/pods", json=fixture("pods_research"))

    saved = run("--workspace", "research", "ps")
    elsewhere = run("--workspace", "ops", "ps")

    assert saved.exit_code == 0, saved.output
    assert responses.calls[1].request.headers["X-API-KEY"] == "sk_test_research"
    assert "X-Lium-Workspace-Id" not in responses.calls[1].request.headers  # the key selects; no header with a key
    assert elsewhere.exit_code == 0
    assert responses.calls[3].request.headers["X-API-KEY"] == "sk_test_default"  # no key saved for 'ops'
    assert "not in 'ops'" in elsewhere.output and "lium keys create --workspace ops --save" in elsewhere.output


@responses.activate
def test_workspaces_use_stores_the_default_and_the_key_when_it_acts_there(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    result = run("workspaces", "use", "research")

    assert result.exit_code == 0, result.output
    text = config_text(home)
    assert "[workspaces]\nactive = Research" in text
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_default" in text


@responses.activate
def test_workspaces_members_invite_remove_transfer_delete_go_through_the_session(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.POST, f"{API}/workspaces/{RESEARCH}/invitations", json=fixture("invitation"))
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}/members/{BEN}", json={"message": "Member removed"})
    responses.add(
        responses.POST,
        f"{API}/workspaces/{RESEARCH}/billing-owner/transfer",
        json={**fixture("workspaces_key")[0], "billing_owner_user_id": BEN},
    )
    responses.add(responses.DELETE, f"{API}/workspaces/{RESEARCH}", json={"message": "Workspace deleted"})

    members = run("workspaces", "members", "Research")
    invite = run("workspaces", "invite", "dana@example.com", "Research", "--role", "member")
    remove = run("workspaces", "remove", "ben@example.com", "Research", "--yes")
    transfer = run("workspaces", "transfer-billing", BEN, "Research")
    delete = run("workspaces", "delete", "Research", "--yes")

    assert members.exit_code == 0 and "ben@example.com" in members.output and "admin" in members.output
    assert invite.exit_code == 0 and "Invited dana@example.com to Research as member" in invite.output
    sent = next(c for c in responses.calls if c.request.url.endswith("/invitations"))
    assert json.loads(sent.request.body) == {"email": "dana@example.com", "role": "member"}
    assert remove.exit_code == 0 and "Member removed" in remove.output
    assert transfer.exit_code == 0 and f"{BEN} now pays for Research" in transfer.output
    assert delete.exit_code == 0 and "Workspace deleted" in delete.output
    for call in responses.calls:
        if call.request.method != "GET" or call.request.url.endswith("/members"):
            assert call.request.headers.get("Authorization") == "Bearer eyJ.fixture.session"


@responses.activate
def test_the_servers_guard_errors_are_shown_as_they_are(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(
        responses.DELETE,
        f"{API}/workspaces/{RESEARCH}",
        status=409,
        json={"message": "The workspace still has 1 running pod(s); delete them first"},
    )

    result = run("workspaces", "delete", "Research", "--yes")

    assert result.exit_code == EXIT_API_ERROR
    assert "API error 409: The workspace still has 1 running pod(s)" in result.output


@responses.activate
def test_writes_without_a_session_explain_how_to_get_one(home):
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_key"))

    result = run("workspaces", "invite", "dana@example.com")

    assert result.exit_code == EXIT_API_ERROR
    assert NEEDS_SESSION.split(":")[0] in result.output and "lium workspaces login" in result.output
    assert not any(c.request.url.endswith("/invitations") for c in responses.calls)


@responses.activate
def test_login_keeps_the_session_token_in_config(home):
    responses.add(responses.POST, f"{API}/users/login", json=fixture("login"))

    result = run("workspaces", "login", "--email", "ana@example.com", "--password-stdin", input="pw\n")

    assert result.exit_code == 0, result.output
    assert "[session]\ntoken = eyJ.fixture.session" in config_text(home)
    assert json.loads(responses.calls[0].request.body) == {"email": "ana@example.com", "password": "pw"}


@responses.activate
def test_keys_create_binds_the_key_to_the_workspace_and_saves_it_for_the_flag(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created"))

    result = run("keys", "create", "ci", "--workspace", "Research", "--save")

    assert result.exit_code == 0, result.output
    post = responses.calls[-1].request
    assert post.headers["X-Lium-Workspace-Id"] == RESEARCH and json.loads(post.body) == {"name": "ci"}
    assert "sk_test_fixture_key_not_a_secret_0000000000" in result.output
    assert f"[workspace.research]\nid = {RESEARCH}\napi_key = sk_test_fixture_key_not_a_secret_0000000000" in config_text(home)


@responses.activate
def test_unknown_member_or_workspace_is_a_configuration_error(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", "eyJ.fixture.session")
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))
    responses.add(responses.GET, f"{API}/workspaces/{RESEARCH}/members", json=fixture("members"))
    responses.add(responses.GET, f"{API}/workspaces", json=fixture("workspaces_session"))

    member = run("workspaces", "remove", "nobody@example.com", "Research", "--yes")
    workspace = run("workspaces", "members", "Nowhere")

    assert member.exit_code == EXIT_CONFIGURATION_ERROR and "No member 'nobody@example.com'" in member.output
    assert workspace.exit_code == EXIT_API_ERROR and "No workspace named 'Nowhere'" in workspace.output
