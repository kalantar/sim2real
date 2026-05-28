"""Progress persistence for the parallel pool orchestrator."""
from __future__ import annotations
import json
import re
import subprocess
from abc import ABC, abstractmethod


class ProgressStore(ABC):
    @abstractmethod
    def load(self) -> dict:
        """Return current progress dict, or {} if none exists."""

    @abstractmethod
    def save(self, data: dict) -> None:
        """Atomically persist the full dict."""


class ConfigMapProgressStore(ProgressStore):
    """Read/write progress as a Kubernetes ConfigMap."""

    BASE_NAME = "sim2real-progress"
    DATA_KEY = "progress"

    _K8S_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

    def __init__(self, namespace: str, *, run_name: str = "") -> None:
        if not namespace:
            raise ValueError("ConfigMapProgressStore requires a non-empty namespace")
        self._namespace = namespace
        if run_name:
            sanitized = re.sub(r"[^a-z0-9.\-]", "-", run_name.lower()).strip("-")
            candidate = f"{self.BASE_NAME}-{sanitized}"
            if len(candidate) > 253 or not self._K8S_NAME_RE.match(candidate):
                raise ValueError(
                    f"run_name {run_name!r} produces invalid ConfigMap name "
                    f"{candidate!r} — must be lowercase alphanumeric, hyphens, "
                    f"or dots, max 253 chars"
                )
            self.configmap_name = candidate
        else:
            self.configmap_name = self.BASE_NAME

    def load(self) -> dict:
        try:
            result = subprocess.run(
                ["kubectl", "get", "configmap", self.configmap_name,
                 "-n", self._namespace,
                 "-o", f"jsonpath={{.data.{self.DATA_KEY}}}"],
                check=False, text=True, capture_output=True,
            )
        except OSError as exc:
            raise RuntimeError(f"kubectl not available: {exc}") from exc
        if result.returncode != 0:
            if "(NotFound)" in result.stderr:
                return {}
            raise RuntimeError(
                f"kubectl get configmap {self.configmap_name} failed: "
                f"{result.stderr.strip()}"
            )
        raw = result.stdout.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Corrupt ConfigMap {self.configmap_name} in {self._namespace}"
            ) from exc

    def save(self, data: dict) -> None:
        cm = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self.configmap_name,
                "namespace": self._namespace,
            },
            "data": {
                self.DATA_KEY: json.dumps(data, indent=2),
            },
        }
        try:
            result = subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=json.dumps(cm),
                check=False, text=True, capture_output=True,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Failed to update ConfigMap {self.configmap_name}: {exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to update ConfigMap {self.configmap_name}: "
                f"{result.stderr.strip()}"
            )
