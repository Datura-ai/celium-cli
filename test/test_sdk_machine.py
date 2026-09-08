"""`@lium.machine` against a fake client: node selection, TTL, timeout and cleanup."""

import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from lium.sdk import ExecutorInfo, LiumError, PodInfo
from lium.sdk import decorators as D


def _executor(gpu_type, count, price, huid="node", machine_name=None, country="US"):
    return ExecutorInfo(
        id=f"{huid}-id", huid=huid,
        machine_name=machine_name or f"NVIDIA {gpu_type}",
        gpu_type=gpu_type, gpu_count=count,
        price_per_hour=price, price_per_gpu=price / count,
        location={"country": country}, specs={}, status="online",
        docker_in_docker=False, ip="1.2.3.4",
    )


EXECUTORS = [
    _executor("A100", 8, 3.60, "eight", "NVIDIA A100-SXM4-80GB"),   # what the API lists first
    _executor("A100", 1, 1.50, "one-dear", "NVIDIA A100-SXM4-80GB"),
    _executor("A100", 1, 1.20, "one-cheap", "NVIDIA A100-SXM4-80GB"),
    _executor("H200", 1, 2.75, "h200"),
    _executor("RTX4090", 1, 0.30, "rtx", "NVIDIA GeForce RTX 4090", country="Romania"),
]


def _pod(pod_id="pod-1"):
    return PodInfo(
        id=pod_id, name="remote-fn", status="RUNNING", huid="swift-fox-c8",
        ssh_cmd="ssh root@10.0.0.1 -p 22", ports={}, created_at="", updated_at="",
        executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


class FakeLium:
    """Records every call the decorator makes and runs the uploaded runner locally."""

    ready = True
    run_exit_code = None  # None = really run the runner; int = pretend it exited with that code

    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.calls = []
        self.uploaded = {}
        self.pods = {}      # id -> PodInfo the fake "server" has running
        self.rented = 0
        self.result_paths = []   # where each call's runner writes its envelope (+ ".npz" beside it)

    def ls(self, **kw):
        self.calls.append(("ls",))
        return list(EXECUTORS)

    def ps(self):
        self.calls.append(("ps",))
        return list(self.pods.values())

    def up(self, **kw):
        self.calls.append(("up", kw))
        self.rented += 1
        pod_id = f"pod-{self.rented}"
        pod = _pod(pod_id)
        pod.name = kw["name"]
        pod.executor = next(e for e in EXECUTORS if e.id == kw["executor_id"])
        self.pods[pod_id] = pod
        return {"id": pod_id, "name": kw["name"]}

    def schedule_termination(self, pod, *, termination_time):
        self.calls.append(("schedule_termination", pod.id, termination_time))
        return {}

    def wait_ready(self, pod, timeout=300):
        self.calls.append(("wait_ready", pod["id"], timeout))
        return self.pods[pod["id"]] if self.ready else None

    def upload(self, pod, *, local, remote):
        self.calls.append(("upload", remote))
        text = Path(local).read_text().replace("'/tmp/", f"'{self.sandbox}/")
        self.uploaded[remote] = text
        (self.sandbox / Path(remote).name).write_text(text)
        self.result_paths.append(str(self.sandbox / (Path(remote).stem + ".json")))

    def exec(self, pod, *, command, env=None):
        self.calls.append(("exec", command))
        return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    def stream_exec(self, pod, *, command, env=None, pty=True):
        self.calls.append(("stream_exec", command, pty))
        runner = next(r for r in self.uploaded if r in command)
        if self.run_exit_code is not None:
            return self.run_exit_code
        proc = subprocess.Popen([sys.executable, "-u", str(self.sandbox / Path(runner).name)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            yield {"type": "stdout", "data": line}
        for line in proc.stderr:
            yield {"type": "stderr", "data": line}
        return proc.wait(timeout=30)

    def download(self, pod, *, remote, local):
        Path(local).write_bytes((self.sandbox / Path(remote).name).read_bytes())

    @contextmanager
    def ssh_session(self, pod, timeout=30):
        self.calls.append(("ssh_session", pod.id))
        yield object()

    def down(self, pod):
        self.calls.append(("down", pod.id))
        self.pods.pop(pod.id, None)
        return {}


def _execs(fake):
    return [c[1] for c in fake.calls if c[0] in ("exec", "stream_exec")]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeLium(tmp_path)
    monkeypatch.setattr(D, "Lium", lambda: client)
    monkeypatch.setattr(D, "_WARM", {})
    FakeLium.ready = True
    FakeLium.run_exit_code = None
    return client


# --- spec parsing / node selection -------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("1xH200", (1, "H200")),
    ("H200", (1, "H200")),
    ("2xRTX 4090", (2, "RTX4090")),
    ("rtx4090", (1, "RTX4090")),
    ("8 X A100", (8, "A100")),
])
def test_parse_machine(spec, expected):
    assert D._parse_machine(spec) == expected


def test_parse_machine_rejects_empty():
    with pytest.raises(ValueError):
        D._parse_machine("")


def test_select_cheapest_single_gpu_not_first_listed():
    chosen = D._select_executor(EXECUTORS, "A100")
    assert chosen.huid == "one-cheap"          # not "eight" (first in API order), not "one-dear"


def test_select_honours_count():
    assert D._select_executor(EXECUTORS, "8xA100").huid == "eight"
    assert D._select_executor(EXECUTORS, "1xH200").huid == "h200"


def test_select_matches_spaced_names():
    assert D._select_executor(EXECUTORS, "RTX 4090").huid == "rtx"
    assert D._select_executor(EXECUTORS, "RTX4090").huid == "rtx"


def test_select_names_what_exists_when_count_is_missing():
    with pytest.raises(LiumError, match=r"2xA100.*Available: 1xA100 \$1.20/h, 1xA100 \$1.50/h, 8xA100 \$3.60/h"):
        D._select_executor(EXECUTORS, "2xA100")


def test_select_unknown_type():
    with pytest.raises(LiumError, match="No node found matching machine type: B200"):
        D._select_executor(EXECUTORS, "B200")


# --- the wrapper ----------------------------------------------------------------------------
def double(x):
    return x * 2


def one():
    return 1


def slow():
    pass


def test_call_rents_cheapest_sets_ttl_bounds_run_and_cleans_up(fake, capsys):
    remote = D.machine(machine="A100", timeout=600)(double)

    assert remote(21) == 42

    up = next(c[1] for c in fake.calls if c[0] == "up")
    assert up["executor_id"] == "one-cheap-id"
    assert up["template_id"] is None                     # Lium.up resolves the node's default itself

    _, pod_id, when = next(c for c in fake.calls if c[0] == "schedule_termination")
    assert pod_id == "pod-1"
    ttl = datetime.fromisoformat(when) - datetime.now(timezone.utc)
    assert 600 + 14 * 60 < ttl.total_seconds() <= 600 + 15 * 60

    run = next(cmd for cmd in _execs(fake) if cmd.endswith(".py"))
    assert run.startswith("timeout -k 5 600 ") and " -u " in run
    assert ("down", "pod-1") in fake.calls
    assert not any(cmd.startswith("rm -rf") for cmd in _execs(fake))

    err = capsys.readouterr().err
    assert "[lium] double: renting 1xA100 $1.20/h (one-cheap, US), removal in 0.4h" in err
    assert "[lium] double: done in" in err
    assert "[lium] double: pod removed" in err


def test_timeout_none_means_no_kill_and_24h_ttl(fake):
    assert D.machine(machine="A100", timeout=None, quiet=True)(one)() == 1
    run = next(cmd for cmd in _execs(fake) if cmd.endswith(".py"))
    assert not run.startswith("timeout")
    when = next(c for c in fake.calls if c[0] == "schedule_termination")[2]
    assert 23.9 * 3600 < (datetime.fromisoformat(when) - datetime.now(timezone.utc)).total_seconds() <= 24 * 3600


def test_timed_out_run_is_reported_as_timeout(fake):
    FakeLium.run_exit_code = 124

    with pytest.raises(LiumError, match="exceeded timeout=5s"):
        D.machine(machine="A100", timeout=5, quiet=True)(slow)()
    assert ("down", "pod-1") in fake.calls


def test_pod_that_never_becomes_ready_is_removed(fake):
    FakeLium.ready = False

    with pytest.raises(LiumError, match="failed to start within 300s"):
        D.machine(machine="A100", quiet=True)(one)()
    assert ("down", "pod-1") in fake.calls


def test_quiet_prints_nothing(fake, capsys):
    D.machine(machine="A100", quiet=True)(one)()
    assert capsys.readouterr().err == ""


def test_cleanup_false_keeps_pod_and_its_environment(fake):
    D.machine(machine="A100", cleanup=False, quiet=True)(one)()
    assert not any(c[0] == "down" for c in fake.calls)
    rm = next(cmd for cmd in _execs(fake) if cmd.startswith("rm "))
    assert "lium-venv" not in rm and rm.endswith(".json.npz")  # call files go (envelope + sidecar), the venv cache stays


# --- one environment per requirements list, built once (DAH-3017) --------------------------------

def test_environment_is_created_once_with_system_site_packages(fake, capsys):
    D.machine(machine="A100", requirements=["transformers", "torch"])(one)()
    setup = next(cmd for cmd in _execs(fake) if "venv" in cmd)
    venv = D._venv_path(["torch", "transformers"])
    assert venv == D._venv_path(["transformers", "torch"])           # order-independent cache key
    assert setup == (
        f"if test -f {venv}/.lium-ready; then echo LIUM_ENV_CACHED; else "
        f"python3 -m venv --system-site-packages {venv} && "
        f"{venv}/bin/python -m pip install -q --disable-pip-version-check transformers torch && "
        f"touch {venv}/.lium-ready; fi"
    )
    run = next(cmd for cmd in _execs(fake) if cmd.endswith(".py"))
    assert f"{venv}/bin/python -u " in run
    assert "environment ready in" in capsys.readouterr().err
    assert len([c for c in fake.calls if c[0] == "exec"]) == 1          # setup is one round trip


def test_cached_environment_is_reported(fake, capsys):
    fake.exec = lambda pod, *, command, env=None: {"stdout": "LIUM_ENV_CACHED\n", "stderr": "", "exit_code": 0, "success": True}
    D.machine(machine="A100", requirements=["numpy"])(one)()
    assert "environment already on the pod" in capsys.readouterr().err


def test_no_requirements_still_gets_a_venv_that_sees_the_image(fake):
    D.machine(machine="A100", quiet=True)(one)()
    setup = next(cmd for cmd in _execs(fake) if "venv" in cmd)
    assert "--system-site-packages" in setup and "pip install" not in setup


def test_failed_install_is_reported_with_pip_output(fake):
    fake.exec = lambda pod, *, command, env=None: {"stdout": "", "stderr": "ERROR: No matching distribution for nosuchpkg", "exit_code": 1, "success": False}
    with pytest.raises(LiumError, match="Failed preparing the environment \\(nosuchpkg\\):\\nERROR: No matching distribution"):
        D.machine(machine="A100", requirements=["nosuchpkg"], quiet=True)(one)()
    assert ("down", "pod-1") in fake.calls


# --- what travels to the pod (DAH-3015) ---------------------------------------------------------

SCALE = 3


def helper(x):
    return x


def test_functions_defined_inside_a_scope_run(fake):
    """`inspect.getsource` of a nested function is indented; the old runner hit IndentationError."""
    @D.machine(machine="A100", quiet=True)
    def nested(a, b=2):
        return a * b

    assert nested(3, b=4) == 12


def test_arguments_are_pickled_and_the_result_comes_back_through_the_json_envelope(fake):
    @D.machine(machine="A100", quiet=True)
    def identity(*args, **kwargs):
        return args, kwargs

    args = ((1, 2), {1, 2}, b"\x00", Path("/x"), None)
    out = identity(*args, k=3.5)
    assert out == (args, {"k": 3.5})
    assert type(out) is tuple and type(out[0][1]) is set and type(out[0][2]) is bytes and isinstance(out[0][3], Path)
    runner = next(iter(fake.uploaded.values()))
    assert "pickle.loads(base64.b64decode(" in runner            # arguments: the caller's own bytes
    assert "def encode(" in runner and "save_arrays(" in runner   # the result: the codec, shipped verbatim
    assert "pickle.dumps" not in runner.split("pickle.loads")[1]  # nothing the pod writes is a pickle


DOCUMENTED_VALUES = [
    None, True, 7, -2.5, float("inf"), "text", b"\x00\x01", [1, [2, [3]]], (1, "a"), {1, 2}, frozenset({"x"}),
    {"k": 1, "n": {"m": [None]}}, {1: "int key", (2, 3): "tuple key", None: "None key"}, {"__lium__": "a user's own key"},
    datetime(2026, 9, 8, 1, 22, tzinfo=timezone.utc), datetime(2026, 9, 8, 1, 22, 5, 123456),
    datetime(2026, 9, 8).date(), datetime(2026, 9, 8, 13, 45, 1).time(), timedelta(days=1, seconds=2, microseconds=3),
    Decimal("1.10"), Path("/root/out/model.pt"), uuid.UUID("12345678-1234-5678-1234-567812345678"),
]


def test_every_documented_stdlib_value_round_trips_with_its_type(fake):
    @D.machine(machine="A100", quiet=True)
    def identity(x):
        return x

    out = identity(DOCUMENTED_VALUES)
    assert out == DOCUMENTED_VALUES
    assert [type(v) for v in out] == [type(v) for v in DOCUMENTED_VALUES]
    assert out[14].tzinfo is not None and out[15].tzinfo is None


def _numpy_shapes(np):
    return [
        np.arange(3),                                                    # 1-D int64
        np.ones((2, 3), dtype=np.float32),                               # 2-D float32
        np.array(4.5),                                                   # 0-d
        np.array([True, False]),                                         # bool
        np.array(["a", "bc"]),                                           # unicode
        np.array([(1, 2.0), (3, 4.0)], dtype=[("i", "i4"), ("f", "f8")]),  # structured
        np.array(["2026-09-08", "2026-09-09"], dtype="datetime64[D]"),   # datetime64
        np.array([1, 2], dtype="timedelta64[s]"),                        # timedelta64
        np.zeros((0, 2)),                                                # empty
    ]


def test_every_documented_numpy_shape_round_trips_as_ndarray(fake):
    np = pytest.importorskip("numpy")

    @D.machine(machine="A100", quiet=True)
    def identity(x):
        return x

    out = identity({"arrays": _numpy_shapes(np), "one": np.arange(3)})
    for got, want in zip(out["arrays"], _numpy_shapes(np)):
        assert type(got) is np.ndarray and got.dtype == want.dtype and got.shape == want.shape
        assert np.array_equal(got, want)
    assert out["one"].tolist() == [0, 1, 2]
    assert Path(fake.result_paths[-1] + ".npz").exists()   # the arrays travelled in the sidecar, not the envelope


def test_numpy_scalars_round_trip_as_numpy_scalars(fake):
    np = pytest.importorskip("numpy")

    @D.machine(machine="A100", quiet=True)
    def scalars():
        import numpy as np
        return np.float64(1.5), np.int32(7), np.bool_(True), np.str_("s"), np.datetime64("2026-09-08")

    out = scalars()
    assert [type(v) for v in out] == [np.float64, np.int32, np.bool_, np.str_, np.datetime64]
    assert out[0] == 1.5 and out[1] == 7 and out[2] and out[3] == "s" and out[4] == np.datetime64("2026-09-08")


def test_an_unsupported_result_type_fails_on_the_pod_naming_the_type_and_where(fake):
    @D.machine(machine="A100", quiet=True)
    def namespace():
        import argparse
        from collections import OrderedDict
        return {"cfg": [OrderedDict(a=1)], "ns": argparse.Namespace(a=1)}

    with pytest.raises(D.ResultEncodingError, match=r"the result of namespace\['cfg'\]\[0\] is a collections.OrderedDict.*return dict\(x\) instead") as info:
        namespace()
    assert isinstance(info.value.__cause__, D.RemoteExecutionError)
    assert info.value.__cause__.exception_type == "ResultEncodingError"
    assert info.value.__cause__.exit_code == 1
    assert ("down", "pod-1") in fake.calls


def test_an_enum_member_is_refused_rather_than_coming_back_as_an_int(fake):
    @D.machine(machine="A100", quiet=True)
    def status():
        import http
        return http.HTTPStatus.OK          # an IntEnum member: isinstance(x, int) but not a plain int

    with pytest.raises(D.ResultEncodingError, match=r"the result of status is a http.HTTPStatus, which does not travel back from the pod; return plain data"):
        status()


def test_object_and_subclass_arrays_are_refused_on_the_pod(fake):
    np = pytest.importorskip("numpy")

    @D.machine(machine="A100", quiet=True)
    def objects():
        import numpy as np
        return np.array([object()])

    @D.machine(machine="A100", quiet=True)
    def masked():
        import numpy as np
        return np.ma.masked_array([1, 2], mask=[0, 1])

    with pytest.raises(D.ResultEncodingError, match="array of dtype object, which does not travel back"):
        objects()
    with pytest.raises(D.ResultEncodingError, match=r"numpy.ma.*MaskedArray.*arr.view\(numpy.ndarray\)"):
        masked()
    assert not any(Path(p + ".npz").exists() for p in fake.result_paths)   # nothing was written to a sidecar
    del np


def test_a_result_file_holding_pickle_bytes_is_refused_by_the_loader(tmp_path):
    """The result file is written by the pod (provider hardware). A pickle in place of the JSON envelope,
    or an .npz whose array needs pickle, must be refused — never loaded — whatever it names."""
    import os
    import pickle

    class Gadget:
        def __reduce__(self):
            return (os.system, ("echo pwned > /dev/null",))

    path = tmp_path / "result.json"
    path.write_bytes(pickle.dumps({"ok": True, "result": Gadget()}, protocol=4))
    with pytest.raises(ValueError, match="not a JSON envelope"):
        D._load_result(str(path))

    np = pytest.importorskip("numpy")
    path.write_text('{"ok": true, "npz": true, "result": {"__lium__": "ndarray", "key": "a0"}}')
    with open(str(path) + ".npz", "wb") as f:
        np.savez(f, a0=np.array([Gadget()], dtype=object))          # numpy pickles object arrays on write
    with pytest.raises(ValueError, match="allow_pickle=False"):      # and refuses them on read without pickle
        D._load_result(str(path))

    path.write_text('{"ok": true, "npz": true, "result": {"__lium__": "ndarray", "key": "missing"}}')
    with open(str(path) + ".npz", "wb") as f:
        np.savez(f, a0=np.arange(2))
    with pytest.raises(ValueError, match="names array 'missing'"):
        D._load_result(str(path))


def test_a_pickled_result_file_from_the_pod_is_a_remote_error_not_a_crash(fake):
    import pickle

    real_download = fake.download

    def poisoned(pod, *, remote, local):
        real_download(pod, remote=remote, local=local)
        if remote.endswith(".json"):
            Path(local).write_bytes(pickle.dumps({"ok": True, "result": 1}, protocol=4))

    fake.download = poisoned
    with pytest.raises(D.RemoteExecutionError, match="could not be loaded locally: the result file is not a JSON envelope"):
        D.machine(machine="A100", quiet=True)(one)()
    assert ("down", "pod-1") in fake.calls


def test_decorators_and_annotations_are_stripped_from_the_shipped_source(fake):
    @D.machine(machine="A100", quiet=True)
    def annotated(x: "np.ndarray", y: int = 1) -> "np.ndarray":  # noqa: F821 — the point: names nothing this module imports
        return x + y

    assert annotated(1) == 2
    shipped = next(iter(fake.uploaded.values()))
    body = shipped.split("def annotated")[1]
    assert "np.ndarray" not in body and "@D.machine" not in shipped and "@" not in shipped.split("def annotated")[0].split("import")[-1]


def test_async_functions_are_awaited(fake):
    @D.machine(machine="A100", quiet=True)
    async def coro(x):
        return x + 1

    assert coro(1) == 2


def test_remote_exception_is_reraised_with_its_type_and_traceback(fake):
    @D.machine(machine="A100", quiet=True)
    def boom(msg):
        raise ValueError(msg)

    with pytest.raises(ValueError, match="bad input") as info:
        boom("bad input")
    cause = info.value.__cause__
    assert isinstance(cause, D.RemoteExecutionError)
    assert cause.exception_type == "ValueError"
    assert "raise ValueError(msg)" in cause.remote_traceback
    assert cause.exit_code == 1
    assert isinstance(cause, LiumError)
    assert ("down", "pod-1") in fake.calls


def test_remote_sys_exit_does_not_exit_the_caller(fake):
    @D.machine(machine="A100", quiet=True)
    def quits():
        raise SystemExit(3)

    with pytest.raises(D.RemoteExecutionError, match="SystemExit: 3"):
        quits()


def test_builtin_exceptions_arrive_typed_whatever_their_args(fake):
    """The runner ships type/module/message/traceback for every exception and the args only when they are
    plain data; the loader rebuilds any builtin Exception subclass from those args, else from the message."""
    @D.machine(machine="A100", quiet=True)
    def stops():
        raise StopIteration("done")                      # no Error/Exception suffix

    @D.machine(machine="A100", quiet=True)
    def odd_key():
        import argparse
        raise KeyError(argparse.Namespace(a=1))          # args that are not plain data

    @D.machine(machine="A100", quiet=True)
    def os_error():
        raise FileNotFoundError(2, "No such file")        # two plain args, an errno subclass

    with pytest.raises(StopIteration, match="done") as info:
        stops()
    assert info.value.__cause__.exception_type == "StopIteration"
    with pytest.raises(KeyError, match="Namespace") as info:
        odd_key()
    assert info.value.args == ("Namespace(a=1)",) and info.value.__cause__.exception_type == "KeyError"
    with pytest.raises(FileNotFoundError) as info:
        os_error()
    assert (info.value.errno, info.value.strerror) == (2, "No such file")


def test_a_builtin_the_loader_cannot_construct_stays_a_remote_error():
    payload = {"ok": False, "module": "builtins", "type": "UnicodeDecodeError", "message": "bad bytes",
               "traceback": "tb", "args": None}
    assert D._builtin_exception(payload) is None                      # needs 5 args; neither the message nor None fits
    payload["args"] = ["utf-8", b"\xff", 0, 1, "invalid start byte"]
    assert isinstance(D._builtin_exception(payload), UnicodeDecodeError)
    assert D._builtin_exception({"module": "builtins", "type": "SystemExit", "message": "3", "args": [3]}) is None
    assert D._builtin_exception({"module": "builtins", "type": "eval", "message": "", "args": None}) is None
    assert D._builtin_exception({"module": "decimal", "type": "InvalidOperation", "message": "", "args": None}) is None


def test_a_remote_exception_of_a_custom_type_is_a_remote_error_with_its_name(fake):
    @D.machine(machine="A100", quiet=True)
    def custom():
        import decimal

        raise decimal.InvalidOperation("custom boom")   # a real class outside builtins

    with pytest.raises(D.RemoteExecutionError, match="InvalidOperation") as info:
        custom()
    assert info.value.exception_type == "InvalidOperation"


def test_a_generator_result_is_refused_on_the_pod_with_its_type(fake):
    @D.machine(machine="A100", quiet=True)
    def gen():
        return (i for i in range(3))

    with pytest.raises(D.ResultEncodingError, match="the result of gen is a generator, which does not travel back"):
        gen()


def test_no_result_file_reports_the_remote_output(fake):
    FakeLium.run_exit_code = 2

    with pytest.raises(D.RemoteExecutionError, match="produced no result") as info:
        D.machine(machine="A100", quiet=True)(one)()
    assert info.value.exit_code == 2


def test_killed_by_signal_is_named(fake):
    FakeLium.run_exit_code = -1   # paramiko: exit-signal, no exit-status

    with pytest.raises(D.RemoteExecutionError, match="killed by a signal"):
        D.machine(machine="A100", quiet=True)(one)()


def test_closure_is_refused_before_renting(fake):
    k = 10
    with pytest.raises(LiumError, match=r"closes over \['k'\]"):
        @D.machine(machine="A100", quiet=True)
        def add_k(x):
            return x + k
    assert fake.calls == []


def test_module_level_names_are_refused_before_renting(fake):
    with pytest.raises(LiumError, match=r"module-level names \['SCALE', 'helper'\]"):
        @D.machine(machine="A100", quiet=True)
        def uses_globals(x):
            return helper(x) * SCALE
    assert fake.calls == []


def test_recursion_and_inner_functions_are_fine(fake):
    @D.machine(machine="A100", quiet=True)
    def fact(n):
        def inner(m):
            return m
        return inner(1) if n <= 1 else n * fact(n - 1)

    assert fact(4) == 24


def test_unpicklable_argument_is_refused_before_renting(fake):
    @D.machine(machine="A100", quiet=True)
    def f(x):
        return x

    with pytest.raises(LiumError, match="Arguments of f cannot be pickled"):
        f(lambda: 1)
    assert fake.calls == []


def test_lambda_is_refused():
    with pytest.raises(LiumError, match="lambdas"):
        D.machine(machine="A100")(lambda x: x)


# --- output reaches the caller (DAH-3016) -------------------------------------------------------

def test_prints_are_relayed_live_and_kept_on_the_error(fake, capsys):
    @D.machine(machine="A100", quiet=True)
    def chatty(n):
        import sys
        for i in range(n):
            print("step", i)
        print("careful", file=sys.stderr)
        raise RuntimeError("after printing")

    with pytest.raises(RuntimeError) as info:
        chatty(2)
    captured = capsys.readouterr()
    assert captured.out == "step 0\nstep 1\n"
    assert "careful" in captured.err
    assert info.value.__cause__.stdout == "step 0\nstep 1\n"
    assert "careful" in info.value.__cause__.stderr
    assert any(c[0] == "stream_exec" and c[2] is False for c in fake.calls)  # no pty: streams stay apart


def test_inner_imports_shadowing_module_imports_are_not_flagged(fake):
    """`import sys` / `from pathlib import Path` inside the body must not trip the check
    just because this module also imports them."""
    @D.machine(machine="A100", quiet=True)
    def uses_inner_imports(p):
        import sys
        from pathlib import Path
        return Path(p).name + sys.platform[:0]

    assert uses_inner_imports("/a/b") == "b"


# --- warm pods, map/local/close, local=True (DAH-3018) -------------------------------------------

def _rents(fake):
    return [c for c in fake.calls if c[0] == "up"]


def test_default_still_rents_and_removes_per_call(fake):
    f = D.machine(machine="A100", quiet=True)(double)
    assert f(1) == 2 and f(2) == 4
    assert len(_rents(fake)) == 2
    assert [c for c in fake.calls if c[0] == "down"] == [("down", "pod-1"), ("down", "pod-2")]
    assert fake.pods == {}


def test_keep_warm_reuses_the_pod_and_rearms_its_ttl(fake, capsys):
    f = D.machine(machine="A100", keep_warm=300, timeout=600)(double)
    assert f(1) == 2
    assert f(2) == 4
    assert len(_rents(fake)) == 1
    assert _rents(fake)[0][1]["name"] == f"lium-fn-{D._warm_key('A100', None)}"   # findable by the next run
    assert not any(c[0] == "down" for c in fake.calls)
    ttls = [c for c in fake.calls if c[0] == "schedule_termination"]
    # rent: timeout + keep_warm + 15 min; after call: keep_warm + 2 min; before call 2: re-armed; after: again
    delays = [(datetime.fromisoformat(c[2]) - datetime.now(timezone.utc)).total_seconds() for c in ttls]
    assert len(delays) == 4
    assert 600 + 300 + 14 * 60 < delays[0] <= 600 + 300 + 15 * 60
    assert 300 + 60 < delays[1] <= 300 + 120
    assert 600 + 300 + 14 * 60 < delays[2] <= 600 + 300 + 15 * 60
    err = capsys.readouterr().err
    assert "pod stays warm 300s" in err
    assert err.count("renting") == 1
    assert "pod ready" in err.split("done in")[0] and "pod ready" not in err.split("done in")[1]

    f.close()
    assert ("down", "pod-1") in fake.calls and fake.pods == {}
    f.close()  # no-op when nothing is warm


def test_a_new_process_finds_the_warm_pod_by_name(fake, capsys):
    """`_WARM` is empty (fresh interpreter) but `ps` shows the pod the previous run left."""
    warm = _pod("pod-9")
    warm.name = f"lium-fn-{D._warm_key('1xA100', None)}"
    warm.executor = EXECUTORS[2]
    warm.removal_scheduled_at = "2099-01-01T00:00:00Z"   # the window the run that rented it had set
    fake.pods["pod-9"] = warm

    assert D.machine(machine="A100")(double)(4) == 8
    assert _rents(fake) == []
    assert "reusing warm pod swift-fox-c8 (1xA100 $1.20/h)" in capsys.readouterr().err
    assert "pod-9" in fake.pods                       # left as warm as it was found
    schedules = [c for c in fake.calls if c[0] == "schedule_termination"]
    # the call re-arms the TTL to cover its own run, then puts the previous window back
    assert schedules[-1][2] == "2099-01-01T00:00:00Z", schedules
    D._close_all()                                    # interpreter exit: the pod is not ours to remove
    assert "pod-9" in fake.pods
    assert not any(c[0] == "down" for c in fake.calls)


def test_a_found_pod_is_re_armed_only_when_the_call_asks_for_warmth(fake):
    warm = _pod("pod-9")
    warm.name = f"lium-fn-{D._warm_key('1xA100', None)}"
    warm.executor = EXECUTORS[2]
    fake.pods["pod-9"] = warm

    assert D.machine(machine="A100", keep_warm=300, quiet=True)(double)(4) == 8
    assert _rents(fake) == []
    assert any(c[0] == "schedule_termination" for c in fake.calls)
    D._close_all()
    assert "pod-9" in fake.pods                       # keep_warm: left to the new TTL, not removed


def test_a_non_positive_timeout_is_refused_when_decorating():
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        D.machine(machine="A100", timeout=0)
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        D.machine(machine="A100", timeout=-5)


def test_a_fractional_timeout_is_rounded_up_for_the_kill(fake):
    D.machine(machine="A100", timeout=0.5, quiet=True)(one)()

    run = next(c for c in fake.calls if c[0] == "stream_exec")
    assert "timeout -k 5 1 " in run[1], run   # int(0.5) would have been 0 = no limit


def test_map_rents_once_and_removes_at_the_end(fake):
    f = D.machine(machine="A100", quiet=True)(double)
    assert f.map([1, 2, 3]) == [2, 4, 6]
    assert len(_rents(fake)) == 1
    assert [c for c in fake.calls if c[0] == "down"] == [("down", "pod-1")]


def test_map_with_keep_warm_leaves_the_pod(fake):
    f = D.machine(machine="A100", keep_warm=60, quiet=True)(double)
    assert f.map([1, 2]) == [2, 4]
    assert not any(c[0] == "down" for c in fake.calls)


def test_map_on_a_pod_found_by_name_leaves_it_as_found(fake, capsys):
    """`.map()` with keep_warm=0 closes the pod it held — unless that pod was found by name from an
    earlier run: not rented here, so not removed here, and its removal time is put back."""
    warm = _pod("pod-9")
    warm.name = f"lium-fn-{D._warm_key('1xA100', None)}"
    warm.executor = EXECUTORS[2]
    warm.removal_scheduled_at = "2099-01-01T00:00:00Z"
    fake.pods["pod-9"] = warm

    assert D.machine(machine="A100")(double).map([1, 2]) == [2, 4]
    assert _rents(fake) == []
    assert not any(c[0] == "down" for c in fake.calls) and "pod-9" in fake.pods
    assert [c for c in fake.calls if c[0] == "schedule_termination"][-1][2] == "2099-01-01T00:00:00Z"
    assert D._WARM == {}
    assert "was found warm, not rented here; left as found" in capsys.readouterr().err


def test_remote_and_local_aliases(fake):
    f = D.machine(machine="A100", quiet=True)(double)
    assert f.remote(5) == 10 and len(_rents(fake)) == 1
    assert f.local(5) == 10 and len(_rents(fake)) == 1
    assert f.local is double


def test_local_true_never_touches_the_api(fake):
    k = 3

    @D.machine(machine="A100", local=True)
    def add_k(x):                # closure: would be refused by the portability check remotely
        return x + k

    assert add_k(1) == 4
    assert fake.calls == []


def test_env_var_forces_local(fake, monkeypatch):
    monkeypatch.setenv("LIUM_MACHINE_LOCAL", "1")
    assert D.machine(machine="A100")(double)(2) == 4
    assert fake.calls == []


def test_atexit_removes_held_pods_but_leaves_keep_warm_ones(fake, capsys):
    D._WARM["held"] = D._Warm(fake, fake.pods.setdefault("pod-h", _pod("pod-h")), EXECUTORS[2], 0)
    D._WARM["warm"] = D._Warm(fake, fake.pods.setdefault("pod-w", _pod("pod-w")), EXECUTORS[2], 120)

    D._close_all()

    assert ("down", "pod-h") in fake.calls
    assert not any(c == ("down", "pod-w") for c in fake.calls)
    assert D._WARM == {}
    assert "stays warm 120s for the next run" in capsys.readouterr().err


# --- one SSH connection per call (DAH-3027) -------------------------------------------------------

def test_pod_work_happens_inside_one_ssh_session(fake):
    D.machine(machine="A100", quiet=True)(one)()
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("ssh_session") == 1
    assert kinds.index("ssh_session") < kinds.index("upload") < kinds.index("stream_exec") < kinds.index("down")
