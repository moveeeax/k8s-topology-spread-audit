import os

import pytest

from k8s_topology_spread_audit.loader import (
    load_documents,
    load_manifests,
    manifest_files,
    parse_workload,
)
from k8s_topology_spread_audit.model import LoadError

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
