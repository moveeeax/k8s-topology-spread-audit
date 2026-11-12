import copy
import os
import sys
import types

import pytest

from k8s_topology_spread_audit.loader import (
    load_documents,
    load_live,
    load_manifests,
    manifest_files,
    parse_hpa,
    parse_workload,
    resolve_replicas,
)
from k8s_topology_spread_audit.model import Autoscaler, LoadError, Workload

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_parse_deployment_defaults_replicas_to_one():
    workload = parse_workload(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "solo"},
            "spec": {"template": {"spec": {}}},
        }
    )
    assert workload is not None
    assert workload.replicas == 1
    assert workload.namespace == "default"
    assert workload.counts_replicas is True


def test_parse_daemonset_has_no_replica_count():
    workload = parse_workload(
        {
            "kind": "DaemonSet",
            "metadata": {"name": "agent", "namespace": "kube-system"},
            "spec": {"template": {"spec": {}}},
        }
    )
    assert workload is not None
    assert workload.replicas is None
    assert workload.counts_replicas is False


def test_parse_skips_unrelated_and_owned_objects():
    assert parse_workload({"kind": "Service", "spec": {}}) is None
    assert parse_workload({"kind": "Deployment"}) is None
    owned = {
        "kind": "ReplicaSet",
        "metadata": {
            "name": "api-123",
            "ownerReferences": [{"kind": "Deployment", "name": "api", "controller": True}],
        },
        "spec": {"template": {"spec": {}}},
    }
    assert parse_workload(owned) is None


def test_parse_reads_constraints_and_affinity():
    doc = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "prod"},
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "api"}},
            "template": {
                "metadata": {"labels": {"app": "api"}},
                "spec": {
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 2,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "minDomains": 3,
                        },
                        {"maxSkew": 1},  # no topologyKey, must be dropped
                    ],
                    "affinity": {
                        "podAntiAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": [
                                {"topologyKey": "kubernetes.io/hostname"}
                            ],
                            "preferredDuringSchedulingIgnoredDuringExecution": [
                                {"weight": 50, "podAffinityTerm": {"topologyKey": "topology.kubernetes.io/zone"}}
                            ],
                        }
                    },
                },
            },
        },
    }
    workload = parse_workload(doc, source="inline.yaml")
    assert len(workload.constraints) == 1
    constraint = workload.constraints[0]
    assert constraint.max_skew == 2
    assert constraint.min_domains == 3
    assert constraint.enforced is False
    assert workload.required_anti_affinity_keys == ["kubernetes.io/hostname"]
    assert workload.preferred_anti_affinity_keys == ["topology.kubernetes.io/zone"]
    assert workload.selector_labels == {"app": "api"}
    assert workload.ref == "prod/Deployment/api"
    assert workload.source == "inline.yaml"


def test_selector_falls_back_to_pod_template_labels():
    workload = parse_workload(
        {
            "kind": "Deployment",
            "metadata": {"name": "web"},
            "spec": {
                "selector": {"matchExpressions": [{"key": "app", "operator": "Exists"}]},
                "template": {"metadata": {"labels": {"app": "web"}}, "spec": {}},
            },
        }
    )
    assert workload.selector_labels == {"app": "web"}


def test_load_documents_expands_list_kind():
    text = """
kind: List
items:
  - kind: Deployment
    metadata: {name: a}
    spec: {replicas: 2, template: {spec: {}}}
  - kind: Deployment
    metadata: {name: b}
    spec: {replicas: 2, template: {spec: {}}}
"""
    workloads = load_documents(text, source="list.yaml")
    assert [w.name for w in workloads] == ["a", "b"]


def test_load_documents_raises_on_invalid_yaml():
    with pytest.raises(LoadError):
        load_documents("a: [1, 2", source="bad.yaml")


def test_load_manifests_walks_directory_deterministically():
    workloads = load_manifests(os.path.join(FIXTURES, "dirty"))
    assert [w.name for w in workloads] == ["web", "cache", "queue", "ingest"]
    assert all(w.source.endswith("workloads.yaml") for w in workloads)


def test_load_manifests_accepts_single_file():
    path = os.path.join(FIXTURES, "dirty", "workloads.yaml")
    assert len(load_manifests(path)) == 4
    assert manifest_files(path) == [path]


def test_load_manifests_errors_on_missing_path_and_empty_dir(tmp_path):
    with pytest.raises(LoadError):
        load_manifests(str(tmp_path / "nope"))
    with pytest.raises(LoadError):
        load_manifests(str(tmp_path))


def test_load_manifests_propagates_parse_error():
    with pytest.raises(LoadError):
        load_manifests(os.path.join(FIXTURES, "broken"))


def hpa_doc(name="web-hpa", target="web", namespace="shop", min_replicas=3, kind="Deployment"):
    spec = {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": kind, "name": target}, "maxReplicas": 10}
    if min_replicas is not None:
        spec["minReplicas"] = min_replicas
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


def test_parse_hpa_reads_target_and_floor():
    hpa = parse_hpa(hpa_doc(), source="hpa.yaml")
    assert hpa == Autoscaler("web-hpa", "shop", "Deployment", "web", 3, "hpa.yaml")
    assert hpa.target_ref == "shop/Deployment/web"


def test_parse_hpa_defaults_min_replicas_to_one():
    assert parse_hpa(hpa_doc(min_replicas=None)).min_replicas == 1


def test_parse_hpa_skips_other_kinds_and_targetless_hpas():
    assert parse_hpa({"kind": "Deployment", "spec": {}}) is None
    broken = hpa_doc()
    del broken["spec"]["scaleTargetRef"]["name"]
    assert parse_hpa(broken) is None
    assert parse_hpa({"kind": "HorizontalPodAutoscaler"}) is None


def test_parse_workload_keeps_status_replicas():
    workload = parse_workload(
        {
            "kind": "Deployment",
            "metadata": {"name": "web"},
            "spec": {"replicas": 2, "template": {"spec": {}}},
            "status": {"replicas": 6},
        }
    )
    assert workload.status_replicas == 6


def bare(replicas=1, status=None, name="web", kind="Deployment"):
    return Workload(kind=kind, name=name, namespace="shop", replicas=replicas, status_replicas=status)


def test_resolve_replicas_auto_raises_the_count_to_the_hpa_floor():
    [workload] = resolve_replicas([bare(1)], [parse_hpa(hpa_doc())])
    assert workload.replicas == 3
    assert workload.replicas_note == "replicas from HPA web-hpa (min 3)"


def test_resolve_replicas_auto_keeps_a_larger_spec_count():
    [workload] = resolve_replicas([bare(5)], [parse_hpa(hpa_doc())], "auto")
    assert workload.replicas == 5
    assert workload.replicas_note is None


def test_resolve_replicas_hpa_uses_the_floor_even_when_lower():
    [workload] = resolve_replicas([bare(5)], [parse_hpa(hpa_doc())], "hpa")
    assert workload.replicas == 3
    assert workload.replicas_note == "replicas from HPA web-hpa (min 3)"


def test_resolve_replicas_spec_ignores_hpas():
    [workload] = resolve_replicas([bare(1)], [parse_hpa(hpa_doc())], "spec")
    assert workload.replicas == 1
    assert workload.replicas_note is None


def test_resolve_replicas_live_takes_status_replicas_above_the_floor():
    hpas = [parse_hpa(hpa_doc())]
    [running] = resolve_replicas([bare(1, status=7)], hpas, "hpa", live=True)
    assert running.replicas == 7
    assert running.replicas_note == "replicas from HPA web-hpa (min 3, status.replicas 7)"
    [floor] = resolve_replicas([bare(1, status=2)], hpas, "hpa", live=True)
    assert floor.replicas == 3
    # status.replicas from an exported manifest is stale, so manifest mode ignores it.
    [manifest] = resolve_replicas([bare(1, status=7)], hpas, "hpa")
    assert manifest.replicas == 3


def test_resolve_replicas_matches_namespace_kind_and_name():
    hpas = [
        parse_hpa(hpa_doc(namespace="staging", min_replicas=9)),
        parse_hpa(hpa_doc(kind="StatefulSet", min_replicas=8)),
        parse_hpa(hpa_doc(target="other", min_replicas=7)),
    ]
    [workload] = resolve_replicas([bare(1)], hpas)
    assert workload.replicas == 1
    assert workload.replicas_note is None


def test_resolve_replicas_picks_the_higher_floor_when_two_hpas_collide():
    hpas = [parse_hpa(hpa_doc(name="a", min_replicas=2)), parse_hpa(hpa_doc(name="b", min_replicas=4))]
    [workload] = resolve_replicas([bare(1)], hpas, "hpa")
    assert workload.replicas == 4
    assert "HPA b" in workload.replicas_note


def test_resolve_replicas_leaves_daemonsets_alone():
    hpas = [parse_hpa(hpa_doc(kind="DaemonSet"))]
    [workload] = resolve_replicas([bare(None, kind="DaemonSet")], hpas, "hpa")
    assert workload.replicas is None


def test_resolve_replicas_rejects_an_unknown_source():
    with pytest.raises(ValueError):
        resolve_replicas([], [], "status")


def test_load_documents_applies_an_hpa_in_the_same_text():
    text = """
kind: Deployment
metadata: {name: web, namespace: shop}
spec: {template: {spec: {}}}
---
kind: List
items:
  - apiVersion: autoscaling/v1
    kind: HorizontalPodAutoscaler
    metadata: {name: web-hpa, namespace: shop}
    spec:
      scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: web}
      minReplicas: 4
      maxReplicas: 8
"""
    [workload] = load_documents(text, source="inline.yaml")
    assert workload.replicas == 4
    [spec_only] = load_documents(text, source="inline.yaml", replicas_source="spec")
    assert spec_only.replicas == 1


def test_load_manifests_matches_hpas_across_files():
    root = os.path.join(FIXTURES, "hpa")
    by_name = {w.name: w for w in load_manifests(root)}
    assert set(by_name) == {"worker", "api", "batch"}
    assert by_name["worker"].replicas == 3
    assert by_name["api"].replicas == 2
    assert by_name["batch"].replicas == 5
    assert {w.name: w.replicas for w in load_manifests(root, replicas_source="hpa")} == {
        "worker": 3,
        "api": 2,
        "batch": 2,
    }


class _Listing:
    def __init__(self, items):
        self.items = items


def fake_kubernetes(monkeypatch, workloads, hpas, hpa_error=None):
    """Install a stand-in `kubernetes` package so load_live runs offline."""

    def namespaced(self, namespace):  # pragma: no cover
        raise AssertionError("not used with --all-namespaces")

    class AppsV1Api:
        def list_deployment_for_all_namespaces(self):
            return _Listing(copy.deepcopy(workloads))

        def list_stateful_set_for_all_namespaces(self):
            return _Listing([])

        def list_daemon_set_for_all_namespaces(self):
            return _Listing([])

        list_namespaced_deployment = namespaced
        list_namespaced_stateful_set = namespaced
        list_namespaced_daemon_set = namespaced

    class CoreV1Api:
        def list_node(self):
            node = types.SimpleNamespace(
                metadata=types.SimpleNamespace(
                    labels={"kubernetes.io/hostname": "n1", "topology.kubernetes.io/zone": "a"}
                )
            )
            return _Listing([node])

    class AutoscalingV2Api:
        def list_horizontal_pod_autoscaler_for_all_namespaces(self):
            if hpa_error is not None:
                raise hpa_error
            return _Listing(copy.deepcopy(hpas))

        list_namespaced_horizontal_pod_autoscaler = namespaced

    class ApiClient:
        def sanitize_for_serialization(self, obj):
            return obj

    client = types.SimpleNamespace(
        AppsV1Api=AppsV1Api, CoreV1Api=CoreV1Api, AutoscalingV2Api=AutoscalingV2Api, ApiClient=ApiClient
    )
    config = types.SimpleNamespace(load_kube_config=lambda: None, load_incluster_config=lambda: None)
    package = types.ModuleType("kubernetes")
    package.client = client
    package.config = config
    monkeypatch.setitem(sys.modules, "kubernetes", package)


LIVE_DEPLOYMENT = {
    "metadata": {"name": "web", "namespace": "shop"},
    "spec": {"replicas": 2, "template": {"spec": {}}},
    "status": {"replicas": 5},
}


def test_load_live_uses_hpa_and_status_replicas(monkeypatch):
    hpa = hpa_doc(min_replicas=3)
    del hpa["kind"]  # the API client leaves kind unset on list items
    fake_kubernetes(monkeypatch, [LIVE_DEPLOYMENT], [hpa])
    workloads, keys, zones = load_live(None, True, replicas_source="hpa")
    assert [(w.name, w.replicas) for w in workloads] == [("web", 5)]
    assert workloads[0].replicas_note == "replicas from HPA web-hpa (min 3, status.replicas 5)"
    assert zones == 1
    assert "topology.kubernetes.io/zone" in keys


def test_load_live_spec_source_does_not_touch_hpas(monkeypatch):
    fake_kubernetes(monkeypatch, [LIVE_DEPLOYMENT], [], hpa_error=AssertionError("must not list"))
    workloads, _, _ = load_live(None, True, replicas_source="spec")
    assert workloads[0].replicas == 2


def test_load_live_auto_warns_when_hpas_cannot_be_listed(monkeypatch, capsys):
    fake_kubernetes(monkeypatch, [LIVE_DEPLOYMENT], [], hpa_error=RuntimeError("403 Forbidden"))
    workloads, _, _ = load_live(None, True, replicas_source="auto")
    assert workloads[0].replicas == 2
    assert "cannot list HorizontalPodAutoscalers" in capsys.readouterr().err


def test_load_live_hpa_source_fails_when_hpas_cannot_be_listed(monkeypatch):
    fake_kubernetes(monkeypatch, [LIVE_DEPLOYMENT], [], hpa_error=RuntimeError("403 Forbidden"))
    with pytest.raises(LoadError, match="HorizontalPodAutoscalers"):
        load_live(None, True, replicas_source="hpa")
