"""Lium SDK - Clean, Unix-style SDK for GPU pod management."""

import getpass
import os
import re
import shlex
import socket
import subprocess
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Generator, List, Optional, Union
from urllib.parse import parse_qs, urlparse

import paramiko
import requests
from dotenv import load_dotenv

from lium.__about__ import __version__ as fallback_version

from . import detach
from .config import Config
from .exceptions import (
    LiumAuthError,
    LiumError,
    LiumNotFoundError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
)
from .jobs import (
    DEFAULT_JOB_DIR,
    _NAME_RE,
    Job,
    build_job_launcher,
    build_port_probe,
    default_job_name,
    job_paths,
    validate_job_name,
)
from .models import (
    BackupConfig,
    BackupLog,
    ExecutorInfo,
    GpuStats,
    PodInfo,
    RestoreLog,
    SSHKey,
    Template,
    VolumeInfo,
)
from .ssh_key_cache import fingerprint, load_cache, save_cache
from .utils import expand_gpu_shorthand, extract_gpu_type, generate_huid, with_retry

load_dotenv()

# Public API key for the pay API (pay-tao-api-v2). Single source of truth so the
# literal is not re-typed across every pay-API call site.
_PAY_API_KEY = "6RhXQ788J9BdnqeLua8z7ZSkXBDahclxhwjMB17qW1M"


def _response_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text or "Request failed"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    response_message = payload.get("message") if isinstance(payload, dict) else None
    validation_errors = (
        payload.get("validation_errors") if isinstance(payload, dict) else None
    )
    structured_error = (
        detail
        if isinstance(detail, dict)
        else response_message if isinstance(response_message, dict) else None
    )
    if isinstance(validation_errors, list) and validation_errors:
        messages: list[str] = []
        for error in validation_errors:
            if not isinstance(error, dict):
                messages.append(str(error))
                continue
            field = error.get("field")
            reason = error.get("message") or error.get("msg") or "Invalid value"
            messages.append(f"{field}: {reason}" if field else str(reason))
        validation_summary = "; ".join(messages)
        message = (
            f"{response_message}: {validation_summary}"
            if isinstance(response_message, str)
            else validation_summary
        )
    elif isinstance(detail, list) and detail:
        message = detail[0].get("msg") if isinstance(detail[0], dict) else str(detail[0])
    elif structured_error:
        message = structured_error.get("message") or "Request failed"
        if structured_error.get("active_operation_id"):
            message += f" (active operation: {structured_error['active_operation_id']})"
    else:
        message = detail or response_message
    return str(message or "Request failed")


def _get_client_version() -> str:
    try:
        return version("lium.io")
    except PackageNotFoundError:
        return os.environ.get("LIUM_BUILD_VERSION", fallback_version)


@dataclass(frozen=True)
class AlphaQuote:
    """USD -> alpha quote from ``GET /balance/convert/alpha``.

    ``netuid`` is the subnet the alpha must be transferred on (the same subnet the
    pay-tao-api-v2 listener credits), so it — not a hardcoded constant — drives the
    on-chain ``transfer_stake``.
    """

    usd: Decimal           # echoes the API's ``original``
    alpha_amount: Decimal  # the API's ``converted`` (raw Decimal; floored by the caller)
    rate: Decimal          # the API's ``rate`` = USD per alpha
    netuid: int            # the API's ``netuid`` — drives the transfer


# Main SDK Class
class Lium:
    """Clean Unix-style SDK for Lium."""

    def __init__(self, config: Optional[Config] = None, source: str = "sdk"):
        self.config = config or Config.load()
        self.source = source
        self.headers = {
            "X-API-KEY": self.config.api_key,
            "X-Source": source,
            "X-Lium-Client-Version": _get_client_version(),
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        """Make API request with error handling.

        Transient failures (429, 5xx, network errors) are retried unless
        ``retry`` is False. A call that creates something must pass ``False``:
        a timed-out POST may well have succeeded server-side, and repeating it
        blindly creates a duplicate.
        """
        if retry:
            return self._request_with_retry(method, endpoint, base_url=base_url, headers=headers, **kwargs)
        return self._request_once(method, endpoint, base_url=base_url, headers=headers, **kwargs)

    @with_retry()
    def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        return self._request_once(method, endpoint, **kwargs)

    def _request_once(
        self,
        method: str,
        endpoint: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> requests.Response:
        url = f"{base_url or self.config.base_url}/{endpoint.lstrip('/')}"
        request_headers = headers or self.headers
        resp = requests.request(method, url, headers=request_headers, timeout=30, **kwargs)

        if resp.ok:
            return resp

        # Map errors
        if resp.status_code == 401:
            raise LiumAuthError("Invalid API key")
        if resp.status_code == 403:
            raise LiumPermissionError(f"Permission denied: {_response_error_message(resp)}")
        if resp.status_code == 404:
            raise LiumNotFoundError(f"Resource not found: {_response_error_message(resp)}")
        if resp.status_code == 429:
            raise LiumRateLimitError("Rate limit exceeded")
        if 500 <= resp.status_code < 600:
            raise LiumServerError(f"Server error: {resp.status_code}")
        raise LiumError(f"API error {resp.status_code}: {_response_error_message(resp)}")

    def _dict_to_backup_config(self, config_dict: Dict) -> BackupConfig:
        """Convert backup config dict to BackupConfig object."""
        return BackupConfig(
            id=config_dict.get("id", ""),
            huid=generate_huid(config_dict.get("id", "")),
            pod_executor_id=config_dict.get("pod_executor_id", ""),
            backup_frequency_hours=config_dict.get("backup_frequency_hours", 0),
            retention_days=config_dict.get("retention_days", 0),
            backup_path=config_dict.get("backup_path", ""),
            is_active=config_dict.get("is_active", True),
            created_at=config_dict.get("created_at", ""),
            updated_at=config_dict.get("updated_at")
        )

    def _dict_to_backup_log(self, log_dict: Dict) -> BackupLog:
        """Convert backup log dict to BackupLog object."""
        return BackupLog(
            id=log_dict.get("id", ""),
            huid=generate_huid(log_dict.get("id", "")),
            backup_config_id=log_dict.get("backup_config_id", ""),
            status=log_dict.get("status", "unknown"),
            started_at=log_dict.get("started_at", ""),
            completed_at=log_dict.get("completed_at"),
            error_message=log_dict.get("error_message"),
            progress=log_dict.get("progress"),
            backup_volume_id=log_dict.get("backup_volume_id"),
            created_at=log_dict.get("created_at"),
            stage=log_dict.get("stage"),
            total_files=log_dict.get("total_files"),
            processed_files=log_dict.get("processed_files"),
            total_bytes=log_dict.get("total_bytes"),
            processed_bytes=log_dict.get("processed_bytes"),
            deletion_state=log_dict.get("deletion_state"),
            physical_cleanup_at=log_dict.get("physical_cleanup_at"),
            status_message=log_dict.get("status_message"),
            elapsed_seconds=log_dict.get("elapsed_seconds"),
            throughput_bytes_per_second=log_dict.get("throughput_bytes_per_second"),
            estimated_remaining_seconds=log_dict.get("estimated_remaining_seconds"),
        )

    def _dict_to_restore_log(self, log_dict: Dict) -> RestoreLog:
        """Convert restore log dict to RestoreLog object."""
        return RestoreLog(
            id=log_dict.get("id", ""),
            huid=generate_huid(log_dict.get("id", "")),
            backup_id=log_dict.get("backup_id", ""),
            pod_id=log_dict.get("pod_id", ""),
            status=log_dict.get("status", "unknown"),
            progress=log_dict.get("progress", 0),
            started_at=log_dict.get("started_at"),
            completed_at=log_dict.get("completed_at"),
            error_message=log_dict.get("error_message"),
            logs=log_dict.get("logs"),
            restore_path=log_dict.get("restore_path"),
            created_at=log_dict.get("created_at", ""),
            backup_engine=log_dict.get("backup_engine"),
            restore_mode=log_dict.get("restore_mode"),
            stage=log_dict.get("stage"),
            last_heartbeat_at=log_dict.get("last_heartbeat_at"),
            total_files=log_dict.get("total_files"),
            processed_files=log_dict.get("processed_files"),
            total_bytes=log_dict.get("total_bytes"),
            processed_bytes=log_dict.get("processed_bytes"),
            elapsed_seconds=log_dict.get("elapsed_seconds"),
            throughput_bytes_per_second=log_dict.get("throughput_bytes_per_second"),
            estimated_remaining_seconds=log_dict.get("estimated_remaining_seconds"),
        )

    def _dict_to_volume_info(self, volume_dict: Dict) -> VolumeInfo:
        """Convert volume dict to VolumeInfo object."""
        return VolumeInfo(
            id=volume_dict.get("id", ""),
            huid=generate_huid(volume_dict.get("id", "")),
            name=volume_dict.get("name", ""),
            description=volume_dict.get("description", ""),
            created_at=volume_dict.get("created_at", ""),
            updated_at=volume_dict.get("updated_at"),
            current_size_bytes=volume_dict.get("current_size_bytes", 0),
            current_file_count=volume_dict.get("current_file_count", 0),
            current_size_gb=volume_dict.get("current_size_gb", 0.0),
            current_size_mb=volume_dict.get("current_size_mb", 0.0),
            last_metrics_update=volume_dict.get("last_metrics_update")
        )

    def _dict_to_executor_info(self, executor_dict: Dict) -> Optional[ExecutorInfo]:
        """Convert executor dict to ExecutorInfo object."""
        if not executor_dict:
            return None

        # Extract GPU info from specs or machine_name
        specs = executor_dict.get("specs", {})
        gpu_info = specs.get("gpu", {})
        gpu_count = gpu_info.get("count", 1)

        # Extract GPU type from machine_name or specs
        machine_name = executor_dict.get("machine_name", "")
        gpu_type = extract_gpu_type(machine_name)

        # If we couldn't extract from machine_name, try specs
        if gpu_type == machine_name.split()[-1] and gpu_info.get("details"):
            gpu_details = gpu_info.get("details", [])
            if gpu_details:
                gpu_name = gpu_details[0].get("name", "")
                if gpu_name:
                    gpu_type = extract_gpu_type(gpu_name)

        price_per_gpu = executor_dict.get("price_per_gpu") or 0
        price_per_hour = price_per_gpu * gpu_count

        return ExecutorInfo(
            id=executor_dict.get("id", ""),
            ip=executor_dict.get("executor_ip_address", ""),
            huid=generate_huid(executor_dict.get("id", "")),
            machine_name=machine_name,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            price_per_hour=price_per_hour,
            price_per_gpu=price_per_gpu,
            location=executor_dict.get("location", {}),
            specs=specs,
            status=executor_dict.get("status", "unknown"),
            docker_in_docker=specs.get("sysbox_runtime", False),
            available_port_count=specs.get("available_port_count"),
            effective_upload_speed_mbps=executor_dict.get("effective_upload_speed_mbps"),
            effective_download_speed_mbps=executor_dict.get("effective_download_speed_mbps"),
            max_cuda_version=executor_dict.get("max_cuda_version"),
            tier=executor_dict.get("tier"),
        )

    def list_ssh_keys(self) -> List[SSHKey]:
        """Return SSH keys registered for the current user."""
        data = self._request("GET", "/ssh-keys").json()
        if not isinstance(data, list):
            return []
        return [
            SSHKey(
                id=str(row.get("id", "")),
                name=row.get("name", ""),
                public_key=row.get("public_key", ""),
                created_at=row.get("created_at"),
            )
            for row in data
            if isinstance(row, dict)
        ]

    def register_ssh_key(self, *, name: str, public_key: str) -> SSHKey:
        """Register a new SSH public key under the current user."""
        payload = {"name": name, "public_key": public_key}
        data = self._request("POST", "/ssh-keys", json=payload).json()
        if not isinstance(data, dict):
            data = {}
        return SSHKey(
            id=str(data.get("id", "")),
            name=data.get("name", name),
            public_key=data.get("public_key", public_key),
            created_at=data.get("created_at"),
        )

    @staticmethod
    def default_ssh_key_name() -> str:
        """``cli-<user>@<host>`` sanitised to ``[A-Za-z0-9._@-]``."""
        user = getpass.getuser() or "user"
        host = socket.gethostname() or "host"
        return re.sub(r"[^A-Za-z0-9._@-]", "-", f"cli-{user}@{host}")[:64]

    def _ensure_ssh_keys_registered(
        self,
        public_keys: List[str],
        name: Optional[str] = None,
    ) -> None:
        """Make sure each pubkey in ``public_keys`` is registered server-side.

        Lazy + cached: skips the network call when every fingerprint is already
        in ``~/.lium/ssh_keys_cache.json``. On any registration failure we warn
        and return — the rent that follows must never be blocked by this step.
        """
        if not public_keys:
            return

        fps = {pk: fingerprint(pk) for pk in public_keys if pk.strip()}
        cached = load_cache(self.config)
        missing_locally = [pk for pk, fp in fps.items() if fp not in cached]
        if not missing_locally:
            return

        try:
            server_keys = {k.public_key.strip() for k in self.list_ssh_keys() if k.public_key}
        except LiumError as exc:
            warnings.warn(
                f"lium: could not list ssh-keys ({exc}); skipping registration",
                stacklevel=2,
            )
            return

        new_fps = set(cached)
        default_name = name or self.default_ssh_key_name()

        for pk in missing_locally:
            stripped = pk.strip()
            if stripped in server_keys:
                new_fps.add(fps[pk])
                continue
            try:
                self.register_ssh_key(name=default_name, public_key=stripped)
                new_fps.add(fps[pk])
            except LiumError as exc:
                warnings.warn(
                    f"lium: could not register ssh key ({exc}); continuing rent",
                    stacklevel=2,
                )

        if new_fps != cached:
            try:
                save_cache(self.config, new_fps)
            except OSError as exc:
                warnings.warn(f"lium: could not write ssh-keys cache ({exc})", stacklevel=2)

    def up(
        self,
        *,
        executor_id: str,
        name: str = "Your Pod",
        template_id: Optional[str] = None,
        dockerfile_content: Optional[str] = None,
        volume_id: Optional[str] = None,
        ports: Optional[int] = None,
        ssh_keys: Optional[List[str]] = None,
        ssh_name: Optional[str] = None,
        enable_volume_encryption: bool | None = True,
        backup_id: Optional[str] = None,
        restore_path: Optional[str] = None,
        wait: bool = False,
        timeout: int = 600,
    ) -> Union[Dict[str, Any], PodInfo]:
        """Start a new pod on a specific node.

        Args:
            executor_id: Target node ID string.
            name: Human-friendly pod name (defaults to ``"Your Pod"``).
            template_id: Template ID. Defaults to the node's default template.
                Mutually exclusive with ``dockerfile_content``.
            dockerfile_content: Raw Dockerfile text to build the pod image from on
                the node (custom build). Mutually exclusive with ``template_id`` —
                pass exactly one. The image is built remotely with no network
                access, so the Dockerfile must be self-contained (no ``ADD <url>``
                or ``ADD ${var}`` directives).
            volume_id: Optional volume ID to attach on spawn.
            ports: Number of exposed ports to request.
            ssh_keys: SSH public keys to authorize. Defaults to the keys discovered by the Config.
            ssh_name: Optional name to use when registering a new SSH key with the
                backend. Defaults to ``cli-<user>@<hostname>``. Only applied to keys
                that are not already registered server-side.
            enable_volume_encryption: Whether to request encryption for the local
                pod volume. Enabled by default. The image must support Lium volume
                encryption.
            backup_id: Optional backup ID to restore after the pod starts.
            restore_path: New or empty subdirectory where the backup is restored.
                Required when ``backup_id`` is provided.
            wait: When ``True``, block until the pod is RUNNING with an SSH
                endpoint and return it as a :class:`PodInfo`. The pod is billing
                from the moment the rent call returns, so a timeout raises a
                :class:`LiumError` that names the pod id rather than hiding it.
            timeout: Seconds to wait for readiness when ``wait`` is set.

        Returns:
            Pod metadata as returned by the rent API (id, name, status, ssh command,
            etc.), or the ready :class:`PodInfo` when ``wait`` is ``True``.
        """
        created = self._rent(
            executor_id=executor_id,
            name=name,
            template_id=template_id,
            dockerfile_content=dockerfile_content,
            volume_id=volume_id,
            ports=ports,
            ssh_keys=ssh_keys,
            ssh_name=ssh_name,
            enable_volume_encryption=enable_volume_encryption,
            backup_id=backup_id,
            restore_path=restore_path,
        )
        if not wait:
            return created

        ready = self.wait_ready(created, timeout=timeout)
        if ready is None:
            raise LiumError(
                f"Pod {created.get('id')} ({created.get('name') or name}) was created but "
                f"did not become ready within {timeout}s; it is still billing — "
                f"check 'lium ps' and remove it if unwanted"
            )
        return ready

    def _rent(
        self,
        *,
        executor_id: str,
        name: str,
        template_id: Optional[str],
        dockerfile_content: Optional[str],
        volume_id: Optional[str],
        ports: Optional[int],
        ssh_keys: Optional[List[str]],
        ssh_name: Optional[str],
        enable_volume_encryption: bool | None,
        backup_id: Optional[str],
        restore_path: Optional[str],
    ) -> Dict[str, Any]:
        """The rent call itself; :meth:`up` adds the optional wait on top."""
        if template_id is not None and dockerfile_content is not None:
            raise ValueError(
                "Provide either template_id or dockerfile_content, not both"
            )
        if bool(backup_id) != bool(restore_path):
            raise ValueError("backup_id and restore_path must be provided together")

        executor_info = self.get_executor(executor_id)
        if not executor_info:
            raise ValueError(f"Node with ID '{executor_id}' not found")

        if template_id is None and dockerfile_content is None:
            selected_template = self.default_docker_template(executor_info.id)
            template_id = selected_template.id

        ssh_material = ssh_keys or self.config.ssh_public_keys
        if not ssh_material:
            raise ValueError("No SSH keys found")

        self._ensure_ssh_keys_registered(ssh_material, name=ssh_name)

        payload = {
            "pod_name": name,
            "template_id": template_id,
            "dockerfile_content": dockerfile_content,
            "volume_id": volume_id,
            "user_public_key": ssh_material,
            "initial_port_count": ports,
            "enable_volume_encryption": enable_volume_encryption,
            "backup_log_id": backup_id,
            "restore_path": restore_path,
        }

        # The rent call is not idempotent, so it is never retried blindly. A
        # timeout or a 5xx may have created the pod anyway; look for it before
        # sending the request a second time. The Idempotency-Key lets a server
        # that honours it collapse the two requests; one that does not ignores it.
        rent_endpoint = f"/executors/{executor_info.id}/rent"
        rent_headers = {**self.headers, "Idempotency-Key": str(uuid.uuid4())}
        # Pods that exist before the rent can never be the one this call created.
        # With GPU splitting a node hosts several pods, and the default pod name
        # is the node huid, so a stale same-name pod on the same node would
        # otherwise be handed back for a rent the server never received.
        known_pod_ids = self._pod_ids_before_rent()
        try:
            response = self._request(
                "POST", rent_endpoint, json=payload, headers=rent_headers, retry=False
            ).json()
        except (requests.RequestException, LiumServerError, LiumRateLimitError):
            # "Could not look" (the network that failed the POST fails ps() the
            # same way) must fall through to the second POST, not escape here.
            existing = self._find_pod_by_name(
                name, executor_info.id, attempts=3, interval=3, exclude=known_pod_ids
            )
            if existing:
                return existing
            time.sleep(1)
            response = self._request(
                "POST", rent_endpoint, json=payload, headers=rent_headers, retry=False
            ).json()

        # API should return pod info
        if response and "id" in response:
            return response

        # The rent route answers {"success": true, "pod_id": ...}: the id is
        # exact, so the pod is read back by it rather than guessed by name. If
        # the listing cannot be read, the id alone is still the truth: the pod
        # exists, and the caller gets its id rather than an error.
        pod_id = (response or {}).get("pod_id")
        if pod_id:
            existing = self._find_pod_by_id(str(pod_id), executor_info.id, attempts=3, interval=2)
            return existing or {
                "id": str(pod_id),
                "name": name,
                "status": "PENDING",
                "huid": generate_huid(str(pod_id)),
                "ssh_cmd": None,
                "executor_id": executor_info.id,
            }

        # Fallback: find pod by name after creation
        existing = self._find_pod_by_name(
            name, executor_info.id, attempts=2, interval=3, exclude=known_pod_ids
        )
        if existing:
            return existing

        raise LiumError(f"Failed to create pod{' ' + name if name else ''}")

    def _list_pods_or_none(self) -> Optional[List[PodInfo]]:
        """``ps()`` for the rent lookups: ``None`` when the listing itself failed,
        so a network that is down for the POST is not mistaken for "no pod"."""
        try:
            return self.ps()
        except (requests.RequestException, LiumError):
            return None

    def _find_pod_by_id(
        self, pod_id: str, executor_id: str, *, attempts: int, interval: float
    ) -> Optional[Dict[str, Any]]:
        """The pod ``pod_id`` from ``ps``; the server already committed it, so the
        listing is read at once and only re-read if the row is not there yet."""
        for attempt in range(attempts):
            if attempt:
                time.sleep(interval)
            for pod in self._list_pods_or_none() or []:
                if pod.id == pod_id:
                    return self._created_pod_record(pod, executor_id)
        return None

    def _pod_ids_before_rent(self) -> frozenset:
        """Ids of the pods that exist right now, taken before a rent is sent.

        A listing failure of any kind must not turn into a failed ``up``; an
        empty snapshot only means the by-name lookup cannot rule out older pods.
        """
        try:
            return frozenset(pod.id for pod in self.ps())
        except Exception:  # noqa: BLE001 - best effort by design
            return frozenset()

    def _find_pod_by_name(
        self,
        name: Optional[str],
        executor_id: str,
        *,
        attempts: int,
        interval: float,
        exclude: frozenset = frozenset(),
    ) -> Optional[Dict[str, Any]]:
        """A pod called ``name`` on ``executor_id`` if one shows up in ``ps``.

        Used when the rent response did not say what it created. The executor
        is matched when the listing includes one, so two pods sharing a generic
        name on different nodes are not confused, and pods whose id is in
        ``exclude`` (the ones that existed before the rent) are never returned.
        """
        if not name:
            return None
        for _ in range(attempts):
            time.sleep(interval)
            for pod in self._list_pods_or_none() or []:
                if pod.id in exclude or pod.name != name:
                    continue
                if pod.executor is not None and pod.executor.id and pod.executor.id != executor_id:
                    continue
                return self._created_pod_record(pod, executor_id)
        return None

    @staticmethod
    def _created_pod_record(pod: PodInfo, executor_id: str) -> Dict[str, Any]:
        """The dict :meth:`up` returns for a pod read back from the listing."""
        return {
            "id": pod.id,
            "name": pod.name,
            "status": pod.status,
            "huid": pod.huid,
            "ssh_cmd": pod.ssh_cmd,
            "executor_id": executor_id,
        }

    def pod_by_name(self, name: str) -> Optional[PodInfo]:
        """The pod called ``name`` — or whose huid or id is ``name`` — if it exists.

        Names are chosen by the caller and huids are what ``lium ps`` prints, so
        both are accepted; the first match wins when several pods share a name.

        Args:
            name: Pod name, huid, or id.

        Returns:
            The matching :class:`PodInfo`, or ``None``.
        """
        for pod in self.ps():
            if name in (pod.name, pod.huid, pod.id):
                return pod
        return None

    @contextmanager
    def rent(
        self,
        *,
        executor_id: str,
        timeout: int = 600,
        **up_kwargs: Any,
    ) -> Generator[PodInfo, None, None]:
        """Rent a pod for the duration of a ``with`` block and always remove it.

        The pod is removed on the way out whether the block returned, raised, or
        was interrupted, and also when the pod was created but never became
        ready. A pod that outlives the code that needed it is the most common
        way to pay for nothing.

        Args:
            executor_id: Target node ID.
            timeout: Seconds to wait for the pod to become ready.
            **up_kwargs: Any other keyword argument :meth:`up` accepts
                (``name``, ``template_id``, ``ports`` ...).

        Yields:
            The ready :class:`PodInfo`.

        Example:
            >>> with lium.rent(executor_id=node.id, name="job") as pod:
            ...     lium.exec(pod, command="nvidia-smi")
        """
        rent_args = dict(
            name="Your Pod", template_id=None, dockerfile_content=None, volume_id=None,
            ports=None, ssh_keys=None, ssh_name=None, enable_volume_encryption=True,
            backup_id=None, restore_path=None,
        )
        unknown = set(up_kwargs) - set(rent_args)
        if unknown:
            raise TypeError(f"rent() got unexpected keyword arguments: {sorted(unknown)}")
        rent_args.update(up_kwargs)

        # The rent itself can raise after the server created the pod (a timed-out
        # or failed second POST, a listing that failed while looking for it), so
        # it runs inside the try; the ids snapshot lets the failure path remove
        # only a pod that appeared during this call, never an older one that
        # happens to carry the same name.
        pods_before = self._pod_ids_or_none()
        created: Optional[Dict[str, Any]] = None
        try:
            created = self._rent(executor_id=executor_id, **rent_args)
            ready = self.wait_ready(created, timeout=timeout)
            if ready is None:
                raise LiumError(
                    f"Pod {created.get('id')} did not become ready within {timeout}s"
                )
            yield ready
        finally:
            if created is not None:
                self._remove_quietly(created)
            elif pods_before is not None:
                self._remove_strays(rent_args["name"], executor_id, exclude=pods_before)

    def _pod_ids_or_none(self) -> Optional[frozenset]:
        """Ids of the pods that exist now, or ``None`` when the listing failed."""
        try:
            return frozenset(pod.id for pod in self.ps())
        except Exception:  # noqa: BLE001 - a listing failure must not fail the rent
            return None

    def _remove_strays(self, name: str, executor_id: str, *, exclude: frozenset) -> None:
        """Remove pods called ``name`` on ``executor_id`` that were not there before the rent."""
        try:
            pods = self.ps()
        except Exception:  # noqa: BLE001 - cleanup must not mask the rent's error
            return
        for pod in pods:
            if pod.id in exclude or pod.name != name:
                continue
            if pod.executor is not None and pod.executor.id and pod.executor.id != executor_id:
                continue
            self._remove_quietly(pod)

    def _remove_quietly(self, pod: Union[Dict[str, Any], PodInfo]) -> None:
        """Best-effort removal for cleanup paths; a failure here must not mask the real error."""
        pod_id = pod.id if isinstance(pod, PodInfo) else pod.get("id")
        if not pod_id:
            return
        try:
            self._request("DELETE", f"/pods/{pod_id}")
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            warnings.warn(
                f"lium: could not remove pod {pod_id} ({exc}); remove it with 'lium rm'",
                stacklevel=3,
            )

    def pod(
        self,
        pod_id: str
    ) -> Dict[str, Any]:
        """Retrieve detailed information about a specific pod.

        Args:
            pod_id: The unique identifier of the pod to retrieve.

        Returns:
            Raw pod data dictionary including template, node, status, and connection info.
        """
        return self._request("GET", f"/pods/{pod_id}").json()

    def logs(
        self,
        pod_id: str,
        *,
        tail: int = 100,
        follow: bool = False,
    ) -> Generator[bytes, None, None]:
        """Stream logs from a pod.

        Args:
            pod_id: The unique identifier of the pod.
            tail: Number of lines to retrieve from the end of the logs (default: 100).
            follow: If True, stream logs continuously (default: False).

        Yields:
            Log lines as bytes.
        """
        params = {"tail": tail, "follow": str(follow).lower()}
        url = f"{self.config.base_url}/pods/{pod_id}/logs"

        with requests.get(url, headers=self.headers, params=params, stream=True, timeout=None if follow else 30) as response:
            if not response.ok:
                if response.status_code == 401:
                    raise LiumAuthError("Invalid API key")
                if response.status_code == 403:
                    raise LiumPermissionError(f"Permission denied: {response.text}")
                if response.status_code == 404:
                    raise LiumNotFoundError(f"Pod not found: {pod_id}")
                if response.status_code == 429:
                    raise LiumRateLimitError("Rate limit exceeded")
                if 500 <= response.status_code < 600:
                    raise LiumServerError(f"Server error: {response.status_code}")
                raise LiumError(f"API error {response.status_code}: {response.text}")

            for line in response.iter_lines():
                if line:
                    yield line

    def edit(
        self,
        pod_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Edit a pod's template configuration.

        Updates the template associated with a pod by merging the provided
        keyword arguments with the existing template settings.

        Args:
            pod_id: The unique identifier of the pod whose template to edit.
            **kwargs: Template fields to update. Common fields include:
                - docker_image (str): Docker image repository.
                - docker_image_tag (str): Docker image tag.
                - startup_commands (str): Commands to run on container start.
                - internal_ports (List[int]): Ports to expose.
                - environment (Dict[str, str]): Environment variables.
                - volumes (List[str]): Volume mount paths.

        Returns:
            Updated template data dictionary from the API.

        Example:
            >>> lium.edit(pod_id, startup_commands="python main.py", environment={"DEBUG": "1"})
        """
        pod = self.pod(pod_id=pod_id)

        payload = {
            **pod["template"],
            **kwargs,
        }

        return self._request("PUT", f"/templates/{pod['template']['id']}", json=payload).json()

    def ls(
        self,
        *,
        gpu_type: Optional[str] = None,
        gpu_count: Optional[int] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        max_distance_miles: Optional[int] = None,
        min_cuda_version: Optional[float] = None,
    ) -> List[ExecutorInfo]:
        """List available nodes.

        Args:
            gpu_type: Optional GPU filter such as ``"A100"`` or ``"H200"``.
            gpu_count: Exact GPU count to match (defaults to 8, pass ``None`` to disable).
            lat: Optional latitude for geospatial filtering. Must be used together with ``lon`` and ``max_distance_miles``.
            lon: Optional longitude for geospatial filtering. Must be used together with ``lat`` and ``max_distance_miles``.
            max_distance_miles: Optional radius (in miles) for geospatial filtering. Must be used together with ``lat`` and ``lon``.
            min_cuda_version: Optional minimum CUDA version to require (e.g. ``12.4``). Nodes whose
                ``max_cuda_version`` is ``None`` or below this threshold are excluded. NVIDIA drivers are
                backward compatible, so a node with a higher driver CUDA version satisfies the requirement.

        Returns:
            A list of :class:`ExecutorInfo` objects that satisfy the filters.
        """
        params: Dict[str, Any] = {"size": 1000}
        if gpu_type:
            # Try to map short GPU name to full machine name
            machine_name = self._resolve_machine_name(gpu_type)
            if machine_name:
                params["machine_names"] = machine_name
            else:
                # If no match found, use the input as-is (might be already a full name)
                params["machine_names"] = gpu_type
        if gpu_count:
            params["gpu_count_gte"] = gpu_count
            params["gpu_count_lte"] = gpu_count
        if lat is not None and lon is not None:
            params["lat"] = lat
            params["lon"] = lon
            if max_distance_miles is not None:
                params["max_distance_mile"] = max_distance_miles
        elif max_distance_miles is not None:
            params["max_distance_mile"] = max_distance_miles

        data = self._request("GET", "/executors", params=params).json()
        executors = [self._dict_to_executor_info(d) for d in data]
        executors = [e for e in executors if e]  # Filter None values

        if min_cuda_version is not None:
            executors = [
                e for e in executors
                if e.max_cuda_version is not None and e.max_cuda_version >= min_cuda_version
            ]

        return executors

    def ps(self) -> List[PodInfo]:
        """List active pods.

        Returns:
            List of :class:`PodInfo` objects representing the caller's running pods.
        """
        data = self._request("GET", "/pods").json()

        pods = []
        for d in data:
            executor = self._dict_to_executor_info(d.get("executor") or {}) if d.get("executor") else None
            # The /pods endpoint returns the authoritative total $/h as pod.price; the
            # nested executor.price_per_gpu is not populated in this payload. Anchor
            # executor.price_per_hour on pod.price and derive per-GPU from it.
            pod_price = d.get("price")
            if executor is not None and pod_price is not None:
                executor.price_per_hour = float(pod_price)
                executor.price_per_gpu = float(pod_price) / max(1, executor.gpu_count)
            pods.append(PodInfo(
                id=d.get("id", ""),
                name=d.get("pod_name", ""),
                status=d.get("status", "unknown"),
                huid=generate_huid(d.get("id", "")),
                ssh_cmd=d.get("ssh_connect_cmd"),
                ports=d.get("ports_mapping", {}),
                created_at=d.get("created_at", ""),
                updated_at=d.get("updated_at", ""),
                executor=executor,
                template=d.get("template", {}),
                removal_scheduled_at=d.get("removal_scheduled_at"),
                jupyter_installation_status=d.get("jupyter_installation_status"),
                jupyter_url=d.get("jupyter_url"),
                enable_volume_encryption=d.get("enable_volume_encryption"),
                volume_encryption_status=d.get("volume_encryption_status"),
            ))

        return pods

    def down(self, pod: PodInfo) -> Dict[str, Any]:
        """Stop a pod.

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        return self._request("DELETE", f"/pods/{pod.id}").json()

    def rm(self, pod: PodInfo) -> Dict[str, Any]:
        """Remove pod (alias for :meth:`down`).

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        return self.down(pod)

    def reboot(self, pod: PodInfo, volume_id: Optional[str] = None) -> Dict[str, Any]:
        """Reboot a pod.

        Args:
            pod: Pod to reboot.
            volume_id: Optional volume ID to attach for the reboot request.

        Returns:
            Pod data from the API response after issuing the reboot.
        """
        payload: Dict[str, Optional[str]] = {}
        if volume_id is not None:
            payload["volume_id"] = volume_id

        return self._request("POST", f"/pods/{pod.id}/reboot", json=payload or {}).json()

    def get_default_images(self, gpu_model: Optional[str], driver_version: Optional[str]) -> list[dict]:
        """Get default images for GPU type and driver version."""
        params = {
            "gpu_model": gpu_model,
            "driver_version": driver_version
        }
        data = self._request("GET", "/executors/default-docker-image", params=params).json()
        return data

    def _select_fallback_template(self) -> Optional[Template]:
        """Pick a reasonable default template (prefer PyTorch, else first available)."""
        templates = self.templates()
        if not templates:
            return None

        def is_pytorch(template: Template) -> bool:
            category = (template.category or "").upper()
            image = (template.docker_image or "").lower()
            return "PYTORCH" in category or "pytorch" in image

        pytorch_templates = [t for t in templates if is_pytorch(t)]

        if pytorch_templates:
            def version_key(template: Template):
                tag = template.docker_image_tag or ""
                version_part = tag.split('-')[0]
                parts = []
                for piece in version_part.split('.'):
                    if piece.isdigit():
                        parts.append(int(piece))
                    else:
                        break
                return tuple(parts)

            return max(pytorch_templates, key=version_key)

        return templates[0]

    def default_docker_template(self, executor_id: str) -> Template:
        """Resolve the best default template for a node ID.

        Args:
            executor_id: Node identifier returned by :meth:`ls`.

        Returns:
            :class:`Template` best suited for the node.

        Raises:
            ValueError: If no matching node or template exists.
        """
        executor = self.get_executor(executor_id)
        if not executor:
            raise ValueError(f"No node found with id {executor_id}")

        default_images = self.get_default_images(executor.gpu_model, executor.driver_version)

        pytorch_image = next(
            (img for img in default_images if "pytorch" in img.get("docker_image", "").lower()), None
        )
        # set pytorch_image as first image
        if pytorch_image:
            default_images = [pytorch_image] + default_images
        for img in default_images:
            template = self.get_template_by_image_name(img.get("docker_image"), img.get("docker_image_tag"))
            if template:
                return template

        fallback = self._select_fallback_template()
        if fallback:
            return fallback

        raise ValueError("No templates available to use for node")


    def templates(self, filter: Optional[str] = None, only_my: bool = False) -> List[Template]:
        """List available templates.

        Args:
            filter: Optional substring to filter by image or name.
            only_my: When ``True`` return only templates owned by the caller.

        Returns:
            List of :class:`Template`.
        """
        data = self._request("GET", "/templates").json()

        if only_my:
            user_id = self.get_my_user_id()
            data = [d for d in data if d.get("user_id") == user_id]

        templates = [
            Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
            for d in data
        ]
        if filter:
            filter_lower = filter.lower()
            templates = [
                t for t in templates
                if filter_lower in t.docker_image.lower() or filter_lower in t.name.lower()
            ]

        return templates


    def get_executor(self, executor: str) -> Optional[ExecutorInfo]:
        """Resolve a node by ID.

        Args:
            executor: Node ID string.

        Returns:
            Matching :class:`ExecutorInfo` or ``None`` if not found.
        """
        for e in self.ls():
            if e.id == executor:
                return e
        return None

    def _resolve_machine_name(self, gpu_short: str) -> Optional[str]:
        """Resolve a short GPU name to all matching full machine names from API.

        Args:
            gpu_short: Short GPU name like "A100", "H200", etc.

        Returns:
            Comma-separated string of all matching machine names, or None if not found.
        """
        try:
            available_machines = self._request("GET", "/machines").json()
            gpu_short_normalized = gpu_short.upper()
            matching_machines = []

            for machine in available_machines:
                machine_name = machine.get("name", "")
                # Check if the short name matches the extracted GPU type
                if extract_gpu_type(machine_name).upper() == gpu_short_normalized:
                    matching_machines.append(machine_name)

            # Return comma-separated list of all matches
            if matching_machines:
                return ",".join(matching_machines)
        except Exception:
            pass
        return None

    def gpu_types(self) -> set[str]:
        """Get list of available GPU types.

        Returns:
            Set of GPU type strings advertised by the API.
        """
        available_machines = self._request("GET", "/machines").json()
        gpu_types = {machine.get("name") or "" for machine in available_machines}
        return gpu_types

    def get_template(self, template_id: str) -> Optional[Template]:
        """Fetch a template by ID/HUID/name.

        Args:
            template_id: Template ID, HUID, or name to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        try:
            d = self._request("GET", f"/templates/{template_id}").json()
            return Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
        except Exception:
            return None

    def get_template_by_image_name(self, image_name: Optional[str] = None, image_tag: Optional[str] = None) -> Optional[Template]:
        """Fetch a template by its Docker image + tag.

        Args:
            image_name: Repository/image name.
            image_tag: Tag to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        templates = self.templates()
        for t in templates:
            if t.docker_image == image_name and t.docker_image_tag == image_tag:
                return t

    @contextmanager
    def ssh_connection(self, pod: PodInfo, timeout: int = 30):
        """SSH connection context manager.

        Args:
            pod: Pod whose SSH metadata is used.
            timeout: Connection timeout in seconds.

        Yields:
            An active ``paramiko.SSHClient``.
        """
        if not pod.ssh_cmd:
            raise ValueError(f"No SSH for pod {pod.name}")

        if not self.config.ssh_key_path:
            raise ValueError("No SSH key configured")

        # Parse SSH command
        parts = shlex.split(pod.ssh_cmd)
        user_host = parts[1]
        user, host = user_host.split("@")
        port = pod.ssh_port

        # Load SSH key
        key = None
        for key_type in [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]:
            try:
                key = key_type.from_private_key_file(str(self.config.ssh_key_path))
                break
            except (paramiko.SSHException, FileNotFoundError, PermissionError):
                continue

        # Connect
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": timeout,
            "look_for_keys": False,
        }
        if key:
            connect_kwargs["pkey"] = key
            connect_kwargs["allow_agent"] = False
        else:
            # System ssh can still work for encrypted keys via ssh-agent even when
            # Paramiko cannot parse the private key file directly.
            connect_kwargs["key_filename"] = str(self.config.ssh_key_path)
            connect_kwargs["allow_agent"] = True
        client.connect(**connect_kwargs)

        try:
            yield client
        finally:
            client.close()

    def _prep_command(self, command: str, env: Optional[Dict[str, str]] = None) -> str:
        """Prepare command with environment variables."""
        if env:
            env_str = " && ".join([f'export {k}="{v}"' for k, v in env.items()])
            return f"{env_str} && {command}"
        return command

    def exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        detach: bool = False,
        log_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a shell command on a pod over SSH.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.
            timeout: Seconds to wait for the command to finish. When it runs
                out the channel is closed and :class:`TimeoutError` is raised;
                the remote process may keep running.
            detach: Start the command in the background on the pod and return
                at once. The command runs under ``nohup setsid`` with stdin
                closed and stdout/stderr to ``log_path``, so it survives the
                SSH session ending — the shape every user otherwise rediscovers
                by hand.
            log_path: Log file for ``detach`` (default
                ``/workspace/logs/exec-<UTC timestamp>.log``).

        Returns:
            Dict containing stdout, stderr, exit_code, and success flag; with
            ``detach`` a dict with ``pid``, ``log_path`` and ``command``.
        """
        if log_path is not None and not detach:
            raise ValueError("log_path only applies with detach=True")
        if detach:
            return self._exec_detached(pod, command=command, env=env, log_path=log_path, timeout=timeout)

        command = self._prep_command(command, env)

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command)
            # Send EOF: a remote command that reads stdin waits forever otherwise,
            # and this call has no stdin to give it.
            stdin.close()
            channel = stdout.channel
            if timeout is not None:
                deadline = time.monotonic() + timeout
                while not channel.exit_status_ready():
                    if time.monotonic() >= deadline:
                        channel.close()
                        raise TimeoutError(
                            f"Command did not finish within {timeout}s on pod {pod.name or pod.huid}: {command}"
                        )
                    time.sleep(0.1)
            exit_code = channel.recv_exit_status()
            return {
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace"),
                "exit_code": exit_code,
                "success": exit_code == 0
            }

    @staticmethod
    def default_detach_log_path() -> str:
        """``/workspace/logs/exec-<UTC stamp>-<6 hex>.log``; the tail keeps two
        jobs started in the same second from sharing (and truncating) one file."""
        return detach.default_detach_log_path(detach.detach_token())

    # The launcher line has one home, lium.sdk.detach, shared with `lium exec
    # --detach`; this is the SDK's public name for it.
    build_detached_command = staticmethod(detach.build_detached_command)

    def _exec_detached(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]],
        log_path: Optional[str],
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        log_path = log_path or self.default_detach_log_path()
        launcher = self.build_detached_command(self._prep_command(command, env), log_path)
        result = self.exec(pod, command=launcher, timeout=timeout or 60)
        pid = self._parse_pid(result.get("stdout", ""))
        if pid is None:
            raise LiumError(
                f"Could not start detached command on pod {pod.name or pod.huid}: "
                f"{result.get('stderr', '').strip() or 'launcher printed no PID'}"
            )
        return {"pid": pid, "log_path": log_path, "command": command}

    @staticmethod
    def _parse_pid(stdout: str) -> Optional[int]:
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None

    # -- background jobs -------------------------------------------------------------------------

    def run_background(
        self,
        pod: PodInfo,
        command: str,
        *,
        name: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        workdir: Optional[str] = None,
        job_dir: str = DEFAULT_JOB_DIR,
        timeout: float = 60,
    ) -> Job:
        """Start ``command`` in the background on a pod and return a :class:`Job` to follow it.

        The job survives this SSH session and this process: it runs under
        ``nohup setsid`` with stdin closed, logs to ``<job_dir>/<name>.log``, records
        its PID in ``<name>.pid`` and its exit code in ``<name>.exit`` when it ends.
        :meth:`job` re-attaches later by name.

        Args:
            pod: Pod to run on.
            command: Shell command (run through ``bash -lc``, so the login
                environment — conda, PATH — applies).
            name: Job name, also the stem of its files; default ``job-<UTC timestamp>``.
                Refused when a job of that name is still running on the pod.
            env: Environment variables exported before the command.
            workdir: Directory to ``cd`` into first.
            job_dir: Where the job files live (default ``/workspace/logs``, the
                fast local volume rather than the encrypted ``/root``).
            timeout: Seconds allowed for the launcher itself (not the job).

        Raises:
            LiumError: the launcher printed no PID, or the name is taken by a live job.
        """
        name = validate_job_name(name or default_job_name())
        # The raw command goes to the launcher; env is exported inside the job
        # shell there, so the .cmd file (what job() reads back) never carries it.
        launcher = build_job_launcher(command, name=name, job_dir=job_dir, workdir=workdir, env=env)
        result = self.exec(pod, command=launcher, timeout=timeout)
        pid = self._parse_pid(result.get("stdout", "")) if result.get("success") else None
        if pid is None:
            detail = result.get("stderr", "").strip() or result.get("stdout", "").strip() or "launcher printed no PID"
            raise LiumError(f"Could not start job {name} on pod {pod.name or pod.huid}: {detail}")
        return Job(self, pod, name=name, pid=pid, command=command, job_dir=job_dir)

    def job(self, pod: PodInfo, name: str, *, job_dir: str = DEFAULT_JOB_DIR) -> Job:
        """Re-attach to a job started earlier with :meth:`run_background`, by name.

        Raises:
            LiumNotFoundError: no job of that name has files on the pod.
        """
        name = validate_job_name(name)
        paths = job_paths(name, job_dir)
        command = (
            f"cat {shlex.quote(paths['pid_file'])} 2>/dev/null && echo && echo ---cmd--- && "
            f"cat {shlex.quote(paths['cmd_file'])} 2>/dev/null"
        )
        stdout = self.exec(pod, command=command, timeout=30).get("stdout", "")
        head, _, cmd = stdout.partition("---cmd---")
        pid = self._parse_pid(head)
        if pid is None:
            raise LiumNotFoundError(f"No job named {name} under {job_dir} on pod {pod.name or pod.huid}")
        return Job(self, pod, name=name, pid=pid, command=cmd.strip("\n"), job_dir=job_dir)

    def jobs(self, pod: PodInfo, *, job_dir: str = DEFAULT_JOB_DIR) -> List[Job]:
        """Every job that has a PID file under ``job_dir`` on the pod, running or finished."""
        q = shlex.quote(job_dir.rstrip("/"))
        command = f"for f in {q}/*.pid; do [ -f \"$f\" ] || continue; printf '%s %s\\n' \"$(basename \"$f\" .pid)\" \"$(cat \"$f\")\"; done"
        stdout = self.exec(pod, command=command, timeout=30).get("stdout", "")
        found: List[Job] = []
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and _NAME_RE.match(parts[0]):
                found.append(Job(self, pod, name=parts[0], pid=int(parts[1]), command="", job_dir=job_dir))
        return found

    def wait_for_port(
        self,
        pod: PodInfo,
        port: int,
        *,
        timeout: float = 600,
        host: str = "127.0.0.1",
        poll_interval: float = 3,
    ) -> None:
        """Block until TCP ``port`` accepts connections inside the pod (a server that is up).

        The probe runs on the pod over SSH (bash ``/dev/tcp``, no ``nc`` needed),
        so it sees the port as the pod does. For a job started with
        :meth:`run_background` prefer :meth:`Job.wait_for_port`, which also
        stops early when the job dies.

        Raises:
            TimeoutError: the port did not answer within ``timeout`` seconds.
        """
        probe = build_port_probe(port, host)
        deadline = time.monotonic() + timeout
        last_error: Optional[str] = None
        while True:
            try:
                stdout = self.exec(pod, command=probe, timeout=30).get("stdout", "")
                last_error = None
            except (OSError, LiumError, paramiko.SSHException) as exc:  # not reachable yet: keep polling
                stdout, last_error = "", str(exc)
            if "port open" in stdout:
                return
            if time.monotonic() >= deadline:
                why = f" (last SSH error: {last_error})" if last_error else ""
                raise TimeoutError(f"Port {port} on pod {pod.name or pod.huid} did not answer within {timeout}s{why}")
            time.sleep(poll_interval)

    GPU_QUERY_FIELDS = (
        "index", "name", "utilization.gpu", "memory.used", "memory.total",
        "temperature.gpu", "power.draw",
    )
    GPU_QUERY_COMMAND = (
        "nvidia-smi --query-gpu=" + ",".join(GPU_QUERY_FIELDS) + " --format=csv,noheader,nounits"
    )

    @staticmethod
    def parse_gpu_stats(csv_text: str) -> List[GpuStats]:
        """Parse ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` output.

        ``[N/A]`` and ``[Not Supported]`` become ``None`` rather than failing
        the whole reading; a GPU whose power sensor is missing still has a
        utilisation figure worth showing.
        """

        def number(value: str) -> Optional[float]:
            value = value.strip()
            if not value or value.startswith("["):
                return None
            try:
                return float(value)
            except ValueError:
                return None

        stats: List[GpuStats] = []
        for raw in csv_text.strip().splitlines():
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 7 or not parts[0].isdigit():
                continue
            # The name may itself contain a comma; re-join the middle.
            name = ", ".join(parts[1:-5])
            util, mem_used, mem_total, temp, power = parts[-5:]
            stats.append(GpuStats(
                index=int(parts[0]),
                name=name,
                utilization_pct=number(util),
                memory_used_mib=number(mem_used),
                memory_total_mib=number(mem_total),
                temperature_c=number(temp),
                power_draw_w=number(power),
            ))
        return stats

    def gpu_stats(self, pod: PodInfo, *, timeout: float = 30) -> List[GpuStats]:
        """Per-GPU utilisation, memory, temperature and power on a pod, via ``nvidia-smi``.

        Raises:
            LiumError: when ``nvidia-smi`` failed or printed nothing usable.
        """
        result = self.exec(pod, command=self.GPU_QUERY_COMMAND, timeout=timeout)
        if not result["success"]:
            raise LiumError(
                f"nvidia-smi failed on pod {pod.name or pod.huid} "
                f"(exit {result['exit_code']}): {result['stderr'].strip() or result['stdout'].strip()}"
            )
        stats = self.parse_gpu_stats(result["stdout"])
        if not stats:
            raise LiumError(f"nvidia-smi on pod {pod.name or pod.huid} reported no GPUs")
        return stats

    def stream_exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
    ) -> Generator[Dict[str, str], None, None]:
        """Execute a shell command and stream incremental output.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.

        Yields:
            Streaming output chunks as ``{"type": "stdout"|"stderr", "data": str}``.
        """
        command = self._prep_command(command, env)

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command, get_pty=True)
            stdin.close()

            channel = stdout.channel
            channel.settimeout(0.1)

            while not channel.closed or channel.recv_ready() or channel.recv_stderr_ready():
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stdout", "data": data}

                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        yield {"type": "stderr", "data": data}

    def exec_all(
        self,
        pods: List[PodInfo],
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
        max_workers: int = 10,
    ) -> List[Dict]:
        """Execute a shell command on multiple pods in parallel.

        Args:
            pods: List of pods to target.
            command: Shell command to run on each pod.
            env: Optional environment variables exported before each command.
            max_workers: Maximum number of SSH workers to spawn.

        Returns:
            List of result dictionaries mirroring :meth:`exec`.
        """
        def exec_single(pod: PodInfo):
            try:
                result = self.exec(pod, command=command, env=env)
                result["pod"] = pod.id
                return result
            except Exception as e:
                return {"pod": pod, "error": str(e), "success": False}

        with ThreadPoolExecutor(max_workers=min(max_workers, len(pods))) as executor:
            return list(executor.map(exec_single, pods))

    def wait_ready(
        self,
        pod: Union[str, PodInfo, Dict],
        *,
        timeout: int = 300,
        poll_interval: int = 10,
        ready_port: Optional[int] = None,
    ) -> Optional[PodInfo]:
        """Poll until a pod reports RUNNING + SSH metadata.

        Args:
            pod: Pod identifier, PodInfo, or dict with an ``id`` field.
            timeout: Maximum number of seconds to wait.
            poll_interval: Interval between successive ``ps`` calls.
            ready_port: When given, also wait until this TCP port answers inside
                the pod (a template that serves a model on start is not usable
                when RUNNING, only when its port accepts). The same ``timeout``
                bounds both phases together.

        Returns:
            PodInfo when the pod is ready, otherwise ``None`` if timeout expires.
        """
        if isinstance(pod, PodInfo):
            pod_id = pod.id
        elif isinstance(pod, dict) and 'id' in pod:
            pod_id = pod['id']
        else:
            pod_id = pod

        start = time.time()
        while time.time() - start < timeout:
            fresh_pods = self.ps()
            current = next((p for p in fresh_pods if p.id == pod_id), None)

            if current and current.status.upper() == "RUNNING" and current.ssh_cmd:
                if ready_port is None:
                    return current
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    return None
                try:
                    self.wait_for_port(current, ready_port, timeout=remaining)
                except TimeoutError:
                    return None
                return current

            time.sleep(poll_interval)
        return None

    def scp(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a local file to a pod via SFTP."""
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.put(local, remote)
            sftp.close()

    def download(self, pod: PodInfo, *, remote: str, local: str) -> None:
        """Download a file from a pod via SFTP.

        Args:
            pod: The pod to download from.
            remote: Remote file path on the pod.
            local: Local destination path.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.get(remote, local)
            sftp.close()

    def upload(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a file to a pod via SFTP.

        This is an alias for :meth:`scp` for parity with the CLI.

        Args:
            pod: The pod to upload to.
            local: Local file path to upload.
            remote: Remote destination path on the pod.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        self.scp(pod, local=local, remote=remote)

    def ssh(self, pod: PodInfo) -> str:
        """Get SSH command string for connecting to a pod.

        Args:
            pod: The pod to generate SSH command for.

        Returns:
            SSH command string with the configured SSH key path.

        Raises:
            ValueError: If SSH is not configured for the pod or no SSH key path is set.
        """
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        return pod.ssh_cmd.replace("ssh ", f"ssh -i {self.config.ssh_key_path} ")

    def rsync(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Sync directories with rsync.

        Args:
            pod: Pod to sync.
            local: Local path or directory (rsync source).
            remote: Remote path on the pod.

        Raises:
            RuntimeError: If the rsync command fails.
        """
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        ssh_cmd = f"ssh -i {self.config.ssh_key_path} -p {pod.ssh_port} -o StrictHostKeyChecking=no"
        cmd = ["rsync", "-avz", "-e", ssh_cmd, local,  f"{pod.username}@{pod.host}:{remote}"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Rsync failed: {result.stderr}")
    
    def switch_template(self, pod: PodInfo, *, template_id: str) -> PodInfo:
        """Switch the template of a running pod.
        
        Args:
            pod: Pod to update.
            template_id: ID of the template to switch to.
            
        Returns:
            PodInfo object with updated pod information.
        """
        payload = {
            "template_id": template_id
        }
        
        response = self._request("PUT", f"/pods/{pod.id}/switch-template", json=payload).json()
        
        # Parse the response into a PodInfo object
        return PodInfo(
            id=pod.id,  # Keep the original pod ID
            name=response.get("pod_name", pod.name),
            status=response.get("status", "PENDING"),
            huid=pod.huid,  # Keep the original HUID
            ssh_cmd=response.get("ssh_connect_cmd"),
            ports=response.get("ports_mapping", {}),
            created_at=response.get("created_at", ""),
            updated_at=response.get("updated_at", ""),
            executor=ExecutorInfo(
                id=response.get("executor_id", ""),
                huid="",
                machine_name="",
                gpu_type=response.get("gpu_name", ""),
                gpu_count=int(response.get("gpu_count", 0) or 0),
                price_per_hour=0.0,
                price_per_gpu=0.0,
                location={},
                specs={},
                status="",
                docker_in_docker=False
            ) if response.get("executor_id") else None,
            template={"id": response.get("template_id", template_id)},
            removal_scheduled_at=None,
            jupyter_installation_status=None,
            jupyter_url=None,
            enable_volume_encryption=response.get("enable_volume_encryption"),
            volume_encryption_status=response.get("volume_encryption_status"),
        )

    
    def create_template(
        self,
        name: str,
        docker_image: str,
        docker_image_digest: str = "",
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Create a new template.

        Args:
            name: Friendly template name.
            docker_image: Image repository (e.g., ``"daturaai/pytorch"``).
            docker_image_digest: Digest string for pinning (defaults to empty string).
            docker_image_tag: Image tag (defaults to ``"latest"``).
            ports: Internal ports to expose (defaults to ``[22, 8000]``).
            start_command: Optional command executed on container start.
            **kwargs: Additional template fields:
                - category (str): Template category (defaults to ``"UBUNTU"``).
                - is_private (bool): Whether template is private (defaults to ``True``).
                - volumes (List[str]): Volume mount paths (defaults to ``["/workspace"]``).
                - description (str): Template description.
                - environment (Dict[str, str]): Environment variables.
                - entrypoint (str): Container entrypoint.
                - one_time_template (bool): Whether to delete template after pod removal
                  (defaults to ``False``).

        Returns:
            Newly created :class:`Template`.
        """
        payload = {
            "name": name,
            "docker_image": docker_image,
            "docker_image_digest": docker_image_digest,
            "docker_image_tag": docker_image_tag,
            "internal_ports": ports or [22, 8000],
            "startup_commands": start_command or "",
            "category": kwargs.get("category", "UBUNTU"),
            "container_start_immediately": kwargs.get("container_start_immediately", True),
            "description": kwargs.get("description", name),
            "entrypoint": kwargs.get("entrypoint", ""),
            "environment": kwargs.get("environment") or {},
            "is_private": kwargs.get("is_private", True),
            "one_time_template": kwargs.get("one_time_template", False),
            # Internal backend clone marker; intentionally omitted from the public SDK docs.
            "is_temporary": kwargs.get("is_temporary", False),
            "readme": kwargs.get("readme", name),
            "volumes": kwargs.get("volumes", ["/workspace"]),
        }

        response = self._request("POST", "/templates", json=payload).json()
        return Template(
            id=response.get("id", ""),
            huid=generate_huid(response.get("id", "")),
            name=response.get("name", ""),
            docker_image=response.get("docker_image", ""),
            docker_image_tag=response.get("docker_image_tag", "latest"),
            category=response.get("category", "general"),
            status=response.get("status", "unknown"),
        )

    def wait_template_ready(self, template_id: str, timeout: int = 300) -> Optional[Template]:
        """Wait for template verification to complete.

        Args:
            template_id: Template identifier.
            timeout: Maximum seconds to wait.

        Returns:
            Template when verification succeeds, otherwise ``None`` if the timeout expires.

        Raises:
            LiumError: If template verification fails.
        """

        start = time.time()
        while time.time() - start < timeout:
            templates = self.templates(only_my=True)
            current = next((t for t in templates if t.id == template_id), None)

            if current:
                status = current.status.upper()
                if status == "VERIFY_SUCCESS":
                    return current
                elif status == "VERIFY_FAILED":
                    raise LiumError(f"Template verification failed: {current.name}")

            time.sleep(10)
        return None

    def get_my_user_id(self) -> str:
        """Get the current user's ID.

        Returns:
            The ID returned by ``/users/me``.
        """
        return self._request("GET", "/users/me").json()["id"]

    def update_template(
        self,
        template_id: str,
        name: str,
        docker_image: str,
        docker_image_digest: str,
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Update an existing template owned by the caller.

        Args:
            template_id: Template identifier.
            name: Friendly name.
            docker_image: Image repository.
            docker_image_digest: Optional digest.
            docker_image_tag: Image tag.
            ports: Internal ports to expose.
            start_command: Startup command.
            **kwargs: Additional override fields.

        Returns:
            Updated :class:`Template`.

        Raises:
            ValueError: If the template is missing or not owned by the caller.
        """
        templates = self._request("GET", "/templates").json()
        current = next((t for t in templates if t["id"] == template_id), None)

        if not current:
            raise ValueError(f"Template with ID {template_id} not found")

        if current.get("user_id") != self.get_my_user_id():
            raise ValueError(f"Cannot update template {template_id}: not owned by current user")

        payload = current.copy()
        payload.update({
                "name": name,
                "docker_image": docker_image,
                "docker_image_digest": docker_image_digest,
                "docker_image_tag": docker_image_tag,
                "internal_ports": ports or [22, 8000],
                "startup_commands": start_command or "",
                "category": kwargs.get("category", payload.get("category", "UBUNTU")),
                "container_start_immediately": kwargs.get("container_start_immediately", payload.get("container_start_immediately", True)),
                "description": kwargs.get("description", payload.get("description", name)),
                "entrypoint": kwargs.get("entrypoint", payload.get("entrypoint", "")),
                "environment": kwargs.get("environment", payload.get("environment", {})),
                "is_private": kwargs.get("is_private", payload.get("is_private", False)),
                "readme": kwargs.get("readme", payload.get("readme", name)),
                "volumes": kwargs.get("volumes", payload.get("volumes", [])),
        })

        resp = self._request("PUT", f"/templates/{template_id}", json=payload).json()
        return Template(
            id=template_id,
            huid=generate_huid(template_id),
            name=payload['name'],
            docker_image=payload['docker_image'],
            docker_image_tag=payload['docker_image_tag'],
            category=payload['category'],
            status=resp.get("status", "unknown"),
        )


    def wallets(self) -> List[Dict[str, Any]]:
        """Get the caller's configured funding wallets.

        Returns:
            Raw wallet records returned by the pay API.
        """
        user = self._request("GET", "/users/me").json()
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        resp = self._request(
            "GET",
            f"/wallet/available-wallets/{user['stripe_customer_id']}",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
        )
        return resp.json()

    def _create_transfer_app_credentials(self) -> tuple[str, str]:
        """Read ``(app_id, customer_id)`` from the ``/tao/create-transfer`` redirect.

        This is the single parse both :meth:`add_wallet` and :meth:`_discover_app_id`
        share, so the alpha flow never issues more than one ``/tao/create-transfer``
        round-trip.

        Note the deliberate HTTP shape: this call uses the DEFAULT ``base_url`` (the
        main Lium API) and NO pay ``X-API-KEY`` header — unlike the pay-API calls
        (:meth:`wallets`, :meth:`convert_alpha`, :meth:`company_wallet`). Do not
        "harmonize" it onto ``base_pay_url`` / the pay key; that returns 401/404.
        """
        create_transfer_response = self._request(
            "POST", "/tao/create-transfer", json={"amount": 10}
        )
        redirect_url = create_transfer_response.json()["url"]
        params = parse_qs(urlparse(redirect_url).query)
        return params["app_id"][0], params["customer_id"][0]

    def _discover_app_id(self, bt_wallet: Any = None) -> str:
        """Resolve the pay-app id without registering a wallet.

        Reuses :meth:`_create_transfer_app_credentials`, so it works identically
        whether or not the coldkey is already registered. ``bt_wallet`` is accepted
        for call-site symmetry but unused (the create-transfer parse needs no wallet).
        """
        app_id, _ = self._create_transfer_app_credentials()
        return app_id

    def add_wallet(self, bt_wallet: Any) -> tuple[str, str]:
        """Link a Bittensor wallet with the user account.

        Args:
            bt_wallet: Wallet object exposing ``coldkey``/``coldkeypub`` for signing.

        Returns:
            ``(app_id, customer_id)`` parsed from the ``/tao/create-transfer``
            redirect — surfaced so the alpha funding flow can reuse the same single
            round-trip for company-wallet lookup instead of issuing a second POST.

        Raises:
            LiumError: If verification or wallet polling fails.
        """
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        access_key = self._request(
            "GET", "/token/generate", base_url=self.config.base_pay_url, headers=pay_headers
        ).json()["access_key"]
        sig = bt_wallet.coldkey.sign(access_key.encode()).hex()
        app_id, stripe_customer_id = self._create_transfer_app_credentials()

        verify_response = self._request(
            "POST",
            "/token/verify",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
            json={
                "coldkey_address": bt_wallet.coldkeypub.ss58_address,
                "access_key": access_key,
                "signature": sig,
                "stripe_customer_id": stripe_customer_id,
                "application_id": app_id,
            },
        )
        if verify_response.json()["status"].lower() != "ok":
            raise LiumError(f"Failed to add wallet: {verify_response.text}")

        for i in range(5):
            wallets = [w.get('wallet_hash', '') for w in self.wallets()]
            if bt_wallet.coldkeypub.ss58_address in wallets:
                return app_id, stripe_customer_id
            time.sleep(2)
        raise LiumError("Failed to add wallet. Wallet not found after 5 attempts.")

    def convert_alpha(self, usd: Any) -> AlphaQuote:
        """Quote ``usd`` (USD) -> alpha via ``GET /balance/convert/alpha``.

        The response carries both the alpha amount to transfer (``converted``) and
        the subnet ``netuid`` the transfer must happen on. Hard-fails (no fallback)
        on a pay-API error: ``_request`` maps 503 -> ``LiumServerError`` (a
        ``LiumError``), so a down subtensor / unavailable alpha price aborts the
        fund before any on-chain call.
        """
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        resp = self._request(
            "GET",
            "/balance/convert/alpha",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
            params={"amount": str(usd)},
        ).json()
        return AlphaQuote(
            usd=Decimal(str(resp["original"])),
            alpha_amount=Decimal(str(resp["converted"])),
            rate=Decimal(str(resp["rate"])),
            netuid=int(resp["netuid"]),
        )

    def company_wallet(self, app_id: str) -> str:
        """Resolve the Lium destination coldkey via ``GET /wallet/company/?app_id=``.

        Returns the company ``wallet_hash`` (the SS58 the pay-tao-api-v2 listener
        credits). Hard-fails (no fallback): a 404 (app has no wallet) maps to
        ``LiumNotFoundError`` (a ``LiumError``), aborting before any on-chain call.
        """
        resp = self._request(
            "GET",
            "/wallet/company/",
            base_url=self.config.base_pay_url,
            headers={"X-API-KEY": _PAY_API_KEY},
            params={"app_id": app_id},
        ).json()
        return resp["wallet_hash"]

    def backup_create(
        self,
        pod: PodInfo,
        *,
        path: str,
        frequency_hours: int = 6,
        retention_days: int = 7,
    ) -> BackupConfig:
        """Create or replace a backup configuration for a pod.

        Args:
            pod: Pod to configure.
            path: Explicit filesystem path inside the pod volume to back up.
            frequency_hours: Backup interval in hours.
            retention_days: Retention period in days.

        Returns:
            Created :class:`BackupConfig`.
        """
        if self.source != "cli" and path.rstrip("/") == pod.volume_path.rstrip("/"):
            warnings.warn(
                "Backing up the entire volume is less reliable when files are actively changing; "
                "prefer a stable subdirectory when possible.",
                UserWarning,
                stacklevel=2,
            )
        payload = {
            "pod_id": pod.id,
            "backup_frequency_hours": frequency_hours,
            "retention_days": retention_days,
            "backup_path": path
        }
        
        response = self._request("POST", "/backup-configs", json=payload).json()
        
        return self._dict_to_backup_config(response)

    def backup_now(
        self,
        pod: PodInfo,
        *,
        name: str,
        description: str = "",
    ) -> Dict[str, Any]:
        """Trigger an immediate backup for a pod.

        Args:
            pod: Pod to back up.
            name: Backup name.
            description: Optional description.

        Returns:
            API response payload from the run-now endpoint.
        """
        payload = {
            "name": name,
            "description": description
        }
        
        return self._request("POST", f"/pods/{pod.id}/backup", json=payload).json()

    def backup_config(self, pod: PodInfo) -> Optional[BackupConfig]:
        """Return the backup configuration for a pod if one exists.

        Args:
            pod: Pod to inspect.

        Returns:
            :class:`BackupConfig` if present, otherwise ``None``.
        """
        try:
            response = self._request("GET", f"/backup-configs/pod/{pod.id}").json()
            return self._dict_to_backup_config(response) if response else None
        except LiumNotFoundError:
            # No backup config exists for this pod
            return None
    
    def backup_list(self) -> List[BackupConfig]:
        """List all backup configurations across all pods.

        Returns:
            List of :class:`BackupConfig`.
        """
        configs = self._request("GET", "/backup-configs").json()
        return [self._dict_to_backup_config(c) for c in configs]

    def backup_logs(self, pod: PodInfo) -> List[BackupLog]:
        """Get recent backup logs for a pod.

        Args:
            pod: Pod to inspect.

        Returns:
            List of :class:`BackupLog` entries (possibly empty).
        """
        try:
            response = self._request("GET", f"/backup-logs/pod/{pod.id}").json()
            
            # Handle paginated response - extract items from the response
            if isinstance(response, dict) and 'items' in response:
                logs = response['items']
            else:
                # Fallback for non-paginated response
                logs = response if isinstance(response, list) else []
            
            return [self._dict_to_backup_log(log) for log in logs]
        except LiumNotFoundError:
            # No backup logs exist for this pod, return empty list
            return []

    def backup_logs_all(self) -> List[BackupLog]:
        """Get all backup logs available to the current user."""
        logs: List[BackupLog] = []
        page = 1
        while True:
            response = self._request(
                "GET", "/backup-logs/", params={"page": page, "limit": 100}
            ).json()
            if not isinstance(response, dict):
                return logs
            logs.extend(self._dict_to_backup_log(log) for log in response.get("items", []))
            if not response.get("has_next"):
                return logs
            page += 1

    def backup_log(self, backup_id: str) -> BackupLog:
        """Get one backup log owned by the authenticated user."""
        response = self._request("GET", f"/backup-logs/{backup_id}").json()
        return self._dict_to_backup_log(response)

    def resolve_backup_id(self, backup_id: str) -> str:
        """Resolve an eight-character backup ID shown by the CLI."""
        if not re.fullmatch(r"[0-9a-fA-F]{8}", backup_id):
            return backup_id
        normalized_backup_id = backup_id.lower()
        matches = {
            log.id
            for log in self.backup_logs_all()
            if log.id.startswith(normalized_backup_id)
        }
        return self._resolve_short_id(backup_id, matches, "backup")

    def backup_delete(self, config_id: str) -> Dict[str, Any]:
        """Delete a backup configuration by ID.

        Args:
            config_id: Backup configuration identifier.

        Returns:
            API response payload.
        """
        return self._request("DELETE", f"/backup-configs/{config_id}").json()

    def backup_cancel(self, backup_id: str) -> Dict[str, Any]:
        """Request cancellation of an active backup while retaining its history."""
        return self._request("POST", f"/backup-logs/{backup_id}/cancel").json()

    def backup_log_delete(self, backup_id: str) -> Dict[str, Any]:
        """Delete the stored data for a completed backup and retain its audit row."""
        return self._request("DELETE", f"/backup-logs/{backup_id}").json()
    
    def restore(
        self,
        pod: PodInfo,
        *,
        backup_id: str,
        restore_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Restore a backup to a pod.
        
        Args:
            pod: Pod to restore to.
            backup_id: ID of the backup to restore.
            restore_path: New or empty subdirectory where the backup is restored.
                Defaults to ``<pod volume>/restored``.
            
        Returns:
            Response from the restore API.
        """
        target_path = restore_path or pod.default_restore_path
        payload = {
            "backup_id": backup_id,
            "restore_path": target_path,
        }
        
        return self._request("POST", f"/pods/{pod.id}/restore", json=payload).json()

    def restore_logs(self, pod: PodInfo) -> List[RestoreLog]:
        """Get recent restore logs for a pod.

        Args:
            pod: Pod to inspect.

        Returns:
            List of :class:`RestoreLog` entries (possibly empty).
        """
        try:
            response = self._request("GET", f"/pods/{pod.id}/restore-logs").json()

            if isinstance(response, dict) and "items" in response:
                logs = response["items"]
            else:
                logs = response if isinstance(response, list) else []

            return [self._dict_to_restore_log(log) for log in logs]
        except LiumNotFoundError:
            return []

    def resolve_restore_id(self, restore_id: str) -> str:
        """Resolve an eight-character restore ID shown by the CLI."""
        if not re.fullmatch(r"[0-9a-fA-F]{8}", restore_id):
            return restore_id
        normalized_restore_id = restore_id.lower()
        matches = {
            log.id
            for pod in self.ps()
            for log in self.restore_logs(pod)
            if log.id.startswith(normalized_restore_id)
        }
        return self._resolve_short_id(restore_id, matches, "restore")

    @staticmethod
    def _resolve_short_id(short_id: str, matches: set[str], resource_name: str) -> str:
        if not matches:
            raise LiumNotFoundError(f"No {resource_name} matches ID '{short_id}'")
        if len(matches) > 1:
            raise LiumError(f"{resource_name.capitalize()} ID '{short_id}' is ambiguous")
        return matches.pop()

    def restore_cancel(self, restore_id: str) -> Dict[str, Any]:
        """Request cancellation of an active restore."""
        return self._request("POST", f"/restore-logs/{restore_id}/cancel").json()

    def get_deployment_estimate(self, executor_id: str, template_id: str) -> dict:
        """Estimate deployment time for a template on a node.

        Args:
            executor_id: Node UUID.
            template_id: Template UUID.

        Returns:
            Dict with ``estimated_seconds``, ``is_slow_machine``, ``warning_message``, ``is_cached_template``,
            and ``docker_image_size`` (image size in bytes, or ``None`` if unknown).
        """
        resp = self._request(
            "GET",
            "/executors/deployment-estimate",
            params={"executor_id": executor_id, "template_id": template_id},
        )
        return resp.json()

    def balance(self) -> float:
        """Get current account balance.

        Returns:
            Floating-point balance value reported by ``/users/me``.
        """
        return float(self._request("GET", "/users/me").json().get("balance") or 0)

    def topup_currencies(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """List stablecoin currencies/networks supported for self-serve top-ups.

        Args:
            refresh: Bypass the server-side cache and re-fetch from the provider.

        Returns:
            List of ``{"code", "network", "decimals", "display_decimals"}`` dicts.
        """
        params = {"refresh": "true"} if refresh else None
        data = self._request("GET", "/tmc-pay/currencies", params=params).json()
        return data.get("currencies", [])

    def topup_create_invoice(
        self, amount: float, crypto_currency: str, crypto_network: str
    ) -> Dict[str, Any]:
        """Create a stablecoin top-up invoice for the current account.

        The returned ``deposit_address`` is where the exact ``crypto_amount`` of
        ``crypto_currency`` (on ``crypto_network``) must be sent. Once the provider
        confirms the transfer, the account balance is credited automatically.

        Args:
            amount: Top-up amount in USD.
            crypto_currency: Stablecoin code (e.g. ``"USDT"``), see :meth:`topup_currencies`.
            crypto_network: Network the stablecoin is sent on (e.g. ``"tron"``).

        Returns:
            Invoice dict including ``invoice_id``, ``deposit_address``, ``crypto_amount``,
            ``crypto_currency``, ``crypto_network``, ``exchange_rate`` and ``expires_at``.
        """
        payload = {
            "amount": amount,
            "crypto_currency": crypto_currency,
            "crypto_network": crypto_network,
        }
        return self._request("POST", "/tmc-pay/create-invoice", json=payload).json()

    def volumes(self) -> List[VolumeInfo]:
        """List all volumes for the current user.

        Returns:
            List of :class:`VolumeInfo`.
        """
        data = self._request("GET", "/volumes").json()
        return [self._dict_to_volume_info(v) for v in data]

    def volume(self, volume_id: str) -> VolumeInfo:
        """Get a specific volume by ID.

        Args:
            volume_id: Volume identifier.

        Returns:
            :class:`VolumeInfo` for the requested volume.
        """
        response = self._request("GET", f"/volumes/{volume_id}").json()
        return self._dict_to_volume_info(response)

    def volume_create(self, name: str, *, description: str = "") -> VolumeInfo:
        """Create a new volume.

        Args:
            name: Volume name.
            description: Optional description.

        Returns:
            Created :class:`VolumeInfo`.
        """
        payload = {"name": name, "description": description}
        response = self._request("POST", "/volumes", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_update(self, volume_id: str, *, name: Optional[str] = None, description: Optional[str] = None) -> VolumeInfo:
        """Update a volume's metadata.

        Args:
            volume_id: Volume identifier.
            name: Optional new name.
            description: Optional description.

        Returns:
            Updated :class:`VolumeInfo`.

        Raises:
            ValueError: If neither ``name`` nor ``description`` is provided.
        """
        payload = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if not payload:
            raise ValueError("At least one of name or description must be provided")
        response = self._request("PUT", f"/volumes/{volume_id}", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_delete(self, volume_id: str) -> Dict[str, Any]:
        """Delete a volume.

        Args:
            volume_id: Volume identifier.

        Returns:
            API response payload from the delete request.
        """
        return self._request("DELETE", f"/volumes/{volume_id}").json()

    def schedule_termination(self, pod: PodInfo, *, termination_time: str) -> Dict[str, Any]:
        """Schedule a pod for automatic termination at a future date and time.

        Args:
            pod: Pod to schedule
            termination_time: ISO 8601 formatted datetime string (e.g., "2025-10-17T15:30:00Z")

        Returns:
            Response from the schedule termination API
        """
        payload = {"removal_scheduled_at": termination_time}
        return self._request("POST", f"/pods/{pod.id}/schedule-removal", json=payload).json()

    def cancel_scheduled_termination(self, pod: PodInfo) -> Dict[str, Any]:
        """Cancel a scheduled termination for a pod.

        Args:
            pod: Pod to cancel the schedule for

        Returns:
            Response from the cancel scheduled termination API
        """
        return self._request("DELETE", f"/pods/{pod.id}/schedule-removal").json()

    def install_jupyter(self, pod: PodInfo, *, jupyter_internal_port: int) -> Dict[str, Any]:
        """Install Jupyter Notebook on a pod.

        Args:
            pod: Pod to install Jupyter on
            jupyter_internal_port: Internal port for Jupyter Notebook

        Returns:
            Response from the install Jupyter API
        """
        payload = {"jupyter_internal_port": jupyter_internal_port}
        return self._request("POST", f"/pods/{pod.id}/install-jupyter", json=payload).json()
