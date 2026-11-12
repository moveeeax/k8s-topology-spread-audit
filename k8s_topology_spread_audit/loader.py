"""Turn YAML manifests or a live cluster into Workload objects."""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import REPLICA_SOURCES, Autoscaler, LoadError, SpreadConstraint, Workload

REPLICA_KINDS = frozenset({"Deployment", "StatefulSet", "ReplicaSet", "ReplicationController"})
DAEMON_KINDS = frozenset({"DaemonSet"})
SUPPORTED_KINDS = REPLICA_KINDS | DAEMON_KINDS
HPA_KIND = "HorizontalPodAutoscaler"

MANIFEST_SUFFIXES = (".yaml", ".yml")

ZONE_KEYS = (
    "topology.kubernetes.io/zone",
    "failure-domain.beta.kubernetes.io/zone",
)


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _constraints(pod_spec: Dict[str, Any]) -> List[SpreadConstraint]:
    raw = pod_spec.get("topologySpreadConstraints") or []
    if not isinstance(raw, list):
        return []
    out: List[SpreadConstraint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("topologyKey")
        if not key:
            continue
        out.append(
            SpreadConstraint(
                max_skew=_as_int(item.get("maxSkew"), 1) or 1,
                topology_key=str(key),
                when_unsatisfiable=str(item.get("whenUnsatisfiable") or "DoNotSchedule"),
                min_domains=_as_int(item.get("minDomains")),
            )
        )
    return out


def _anti_affinity_keys(pod_spec: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    affinity = pod_spec.get("affinity") or {}
    if not isinstance(affinity, dict):
        return [], []
    anti = affinity.get("podAntiAffinity") or {}
    if not isinstance(anti, dict):
        return [], []

    required: List[str] = []
    for term in anti.get("requiredDuringSchedulingIgnoredDuringExecution") or []:
        if isinstance(term, dict) and term.get("topologyKey"):
            required.append(str(term["topologyKey"]))

    preferred: List[str] = []
    for item in anti.get("preferredDuringSchedulingIgnoredDuringExecution") or []:
        if not isinstance(item, dict):
            continue
        term = item.get("podAffinityTerm") or {}
        if isinstance(term, dict) and term.get("topologyKey"):
            preferred.append(str(term["topologyKey"]))

    return required, preferred


def _selector_labels(spec: Dict[str, Any], pod_meta: Dict[str, Any]) -> Dict[str, str]:
    selector = spec.get("selector")
    if isinstance(selector, dict):
        match = selector.get("matchLabels")
        if isinstance(match, dict) and match:
            return {str(k): str(v) for k, v in match.items()}
        # ReplicationController uses a flat selector map.
        if match is None and selector and "matchExpressions" not in selector:
            return {str(k): str(v) for k, v in selector.items()}
    labels = pod_meta.get("labels")
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items()}
    return {}


def _is_owned(doc: Dict[str, Any]) -> bool:
    """A ReplicaSet owned by a Deployment would double-report its parent."""
    owners = (doc.get("metadata") or {}).get("ownerReferences") or []
    if not isinstance(owners, list):
        return False
    return any(isinstance(o, dict) and o.get("controller") for o in owners)


def parse_workload(doc: Any, source: str = "-") -> Optional[Workload]:
    """Build a Workload from a manifest dict, or None when it is not relevant."""
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    if kind not in SUPPORTED_KINDS:
        return None
    if _is_owned(doc):
        return None

    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    pod_spec = template.get("spec")
    if not isinstance(pod_spec, dict):
        pod_spec = {}
    pod_meta = template.get("metadata")
    if not isinstance(pod_meta, dict):
        pod_meta = {}

    metadata = doc.get("metadata") or {}
    name = str(metadata.get("name") or "<unnamed>")
    namespace = str(metadata.get("namespace") or "default")

    status = doc.get("status")
    status_replicas: Optional[int] = None
    if kind in DAEMON_KINDS:
        replicas: Optional[int] = None
    else:
        replicas = _as_int(spec.get("replicas"), 1)
        if isinstance(status, dict):
            status_replicas = _as_int(status.get("replicas"))

    required, preferred = _anti_affinity_keys(pod_spec)
    return Workload(
        kind=str(kind),
        name=name,
        namespace=namespace,
        replicas=replicas,
        constraints=_constraints(pod_spec),
        required_anti_affinity_keys=required,
        preferred_anti_affinity_keys=preferred,
        selector_labels=_selector_labels(spec, pod_meta),
        source=source,
        status_replicas=status_replicas,
    )


def parse_hpa(doc: Any, source: str = "-") -> Optional[Autoscaler]:
    """Build an Autoscaler from a HorizontalPodAutoscaler manifest, or None.

    scaleTargetRef and minReplicas have the same shape in autoscaling/v1 and v2,
    so both API versions are accepted.
    """
    if not isinstance(doc, dict) or doc.get("kind") != HPA_KIND:
        return None
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    target = spec.get("scaleTargetRef")
    if not isinstance(target, dict) or not target.get("kind") or not target.get("name"):
        return None

    metadata = doc.get("metadata") or {}
    # minReplicas defaults to 1; 0 is only legal behind the HPAScaleToZero gate.
    min_replicas = _as_int(spec.get("minReplicas"), 1)
    return Autoscaler(
        name=str(metadata.get("name") or "<unnamed>"),
        namespace=str(metadata.get("namespace") or "default"),
        target_kind=str(target["kind"]),
        target_name=str(target["name"]),
        min_replicas=max(min_replicas if min_replicas is not None else 1, 0),
        source=source,
    )


def resolve_replicas(
    workloads: Sequence[Workload],
    autoscalers: Sequence[Autoscaler],
    replicas_source: str = "auto",
    live: bool = False,
) -> List[Workload]:
    """Replace spec.replicas with the count an HPA keeps the workload at.

    The HPA count is minReplicas in manifest mode and max(status.replicas,
    minReplicas) in live mode. `hpa` always uses it for a targeted workload,
    `auto` only when it is larger than spec.replicas, `spec` never.
    """
    if replicas_source not in REPLICA_SOURCES:
        raise ValueError("unknown replicas source: %s" % replicas_source)
    if replicas_source == "spec" or not autoscalers:
        return list(workloads)

    # Two HPAs on one target is a misconfiguration; take the higher floor so the
    # pick is deterministic and never hides a workload.
    by_target: Dict[str, Autoscaler] = {}
    for hpa in sorted(autoscalers, key=lambda a: (a.target_ref, -a.min_replicas, a.name)):
        by_target.setdefault(hpa.target_ref, hpa)

    out: List[Workload] = []
    for workload in workloads:
        hpa = by_target.get(workload.ref)
        if hpa is None or workload.replicas is None:
            out.append(workload)
            continue

        count = hpa.min_replicas
        note = "replicas from HPA %s (min %d)" % (hpa.name, hpa.min_replicas)
        if live and workload.status_replicas is not None and workload.status_replicas > count:
            count = workload.status_replicas
            note = "replicas from HPA %s (min %d, status.replicas %d)" % (
                hpa.name,
                hpa.min_replicas,
                workload.status_replicas,
            )

        if replicas_source == "hpa" or count > workload.replicas:
            workload = dataclasses.replace(workload, replicas=count, replicas_note=note)
        out.append(workload)
    return out


def _flatten(doc: Any) -> Iterable[Any]:
    """Expand a `kind: List` wrapper into its items."""
    if isinstance(doc, dict) and doc.get("kind") in ("List", "ListMeta") and isinstance(doc.get("items"), list):
        for item in doc["items"]:
            yield item
    else:
        yield doc


def _load_objects(text: str, source: str) -> Tuple[List[Workload], List[Autoscaler]]:
    import yaml

    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise LoadError("%s: invalid YAML: %s" % (source, exc))

    workloads: List[Workload] = []
    autoscalers: List[Autoscaler] = []
    for doc in docs:
        for item in _flatten(doc):
            workload = parse_workload(item, source=source)
            if workload is not None:
                workloads.append(workload)
                continue
            hpa = parse_hpa(item, source=source)
            if hpa is not None:
                autoscalers.append(hpa)
    return workloads, autoscalers


def load_documents(text: str, source: str = "-", replicas_source: str = "auto") -> List[Workload]:
    """Parse a multi-document YAML string into workloads."""
    workloads, autoscalers = _load_objects(text, source)
    return resolve_replicas(workloads, autoscalers, replicas_source)


def manifest_files(root: str) -> List[str]:
    if os.path.isfile(root):
        return [root]
    if not os.path.isdir(root):
        raise LoadError("no such file or directory: %s" % root)
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if filename.endswith(MANIFEST_SUFFIXES):
                found.append(os.path.join(dirpath, filename))
    return found


def load_manifests(root: str, replicas_source: str = "auto") -> List[Workload]:
    """Read every YAML manifest under `root` (file or directory).

    An HPA may live in a different file from the workload it scales, so HPAs
    are matched only after every file has been read.
    """
    files = manifest_files(root)
    if not files:
        raise LoadError("no YAML manifests found under %s" % root)
    workloads: List[Workload] = []
    autoscalers: List[Autoscaler] = []
    for path in files:
        try:
            with open(path, "r") as handle:
                text = handle.read()
        except OSError as exc:
            raise LoadError("cannot read %s: %s" % (path, exc))
        found, hpas = _load_objects(text, source=path)
        workloads.extend(found)
        autoscalers.extend(hpas)
    return resolve_replicas(workloads, autoscalers, replicas_source)


def load_live(
    namespace: Optional[str],
    all_namespaces: bool,
    replicas_source: str = "auto",
) -> Tuple[List[Workload], List[str], int]:
    """Read workloads, their HPAs and node topology from the current kube context.

    Returns (workloads, topology keys present on nodes, distinct zone count).
    """
    try:
        from kubernetes import client, config  # type: ignore
    except ImportError:
        raise LoadError(
            "live mode needs the kubernetes client: pip install 'k8s-topology-spread-audit[live]'"
        )

    try:
        config.load_kube_config()
    except Exception:  # pragma: no cover - depends on the runtime environment
        try:
            config.load_incluster_config()
        except Exception as exc:
            raise LoadError("cannot load a kube config: %s" % exc)

    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    ns = None if all_namespaces else (namespace or "default")

    def listing(all_fn, ns_fn):
        return all_fn() if ns is None else ns_fn(ns)

    try:
        groups = [
            ("Deployment", listing(apps.list_deployment_for_all_namespaces, apps.list_namespaced_deployment)),
            ("StatefulSet", listing(apps.list_stateful_set_for_all_namespaces, apps.list_namespaced_stateful_set)),
            ("DaemonSet", listing(apps.list_daemon_set_for_all_namespaces, apps.list_namespaced_daemon_set)),
        ]
        nodes = core.list_node()
    except Exception as exc:  # pragma: no cover - needs a cluster
        raise LoadError("Kubernetes API call failed: %s" % exc)

    hpa_items: List[Any] = []
    if replicas_source != "spec":
        autoscaling = client.AutoscalingV2Api()
        try:
            hpa_items = listing(
                autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces,
                autoscaling.list_namespaced_horizontal_pod_autoscaler,
            ).items
        except Exception as exc:
            # `hpa` was asked for explicitly, so a missing HPA view is fatal there.
            # `auto` keeps working for service accounts without HPA read access.
            if replicas_source == "hpa":
                raise LoadError("cannot list HorizontalPodAutoscalers: %s" % exc)
            print(
                "warning: cannot list HorizontalPodAutoscalers (%s); using spec.replicas" % exc,
                file=sys.stderr,
            )

    workloads: List[Workload] = []
    for kind, result in groups:
        for obj in result.items:
            doc = client.ApiClient().sanitize_for_serialization(obj)
            doc["kind"] = kind
            workload = parse_workload(doc, source="cluster")
            if workload is not None:
                workloads.append(workload)

    autoscalers: List[Autoscaler] = []
    for obj in hpa_items:
        doc = client.ApiClient().sanitize_for_serialization(obj)
        doc["kind"] = HPA_KIND
        hpa = parse_hpa(doc, source="cluster")
        if hpa is not None:
            autoscalers.append(hpa)
    workloads = resolve_replicas(workloads, autoscalers, replicas_source, live=True)

    node_keys = set()
    zones = set()
    for node in nodes.items:
        labels = (node.metadata.labels or {}) if node.metadata else {}
        node_keys.update(labels.keys())
        for zone_key in ZONE_KEYS:
            if labels.get(zone_key):
                zones.add(labels[zone_key])

    return workloads, sorted(node_keys), len(zones)
