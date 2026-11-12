import os

from k8s_topology_spread_audit.loader import load_manifests, parse_workload
from k8s_topology_spread_audit.model import SpreadConstraint, Workload
from k8s_topology_spread_audit.rules import (
    DEFAULT_KNOWN_TOPOLOGY_KEYS,
    evaluate,
    evaluate_workload,
    suggest_constraints,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def workload(**kwargs):
    base = dict(kind="Deployment", name="web", namespace="shop", replicas=3)
    base.update(kwargs)
    return Workload(**base)


def rules_for(w, **kwargs):
    return [f.rule for f in evaluate_workload(w, **kwargs)]


def test_bare_multi_replica_deployment_is_high_severity():
    findings = evaluate_workload(workload())
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "no-spread-rule"
    assert finding.severity == "high"
    assert "3 replicas" in finding.message
    assert "topologySpreadConstraints" in finding.suggestion


def test_single_replica_is_never_flagged():
    assert rules_for(workload(replicas=1)) == []


def test_required_zone_anti_affinity_is_accepted():
    w = workload(required_anti_affinity_keys=["topology.kubernetes.io/zone"])
    assert rules_for(w) == []


def test_preferred_only_anti_affinity_is_medium():
    w = workload(preferred_anti_affinity_keys=["kubernetes.io/hostname"])
    findings = evaluate_workload(w)
    assert [f.rule for f in findings] == ["preferred-anti-affinity-only"]
    assert findings[0].severity == "medium"


def test_schedule_anyway_only_is_medium():
    w = workload(
        replicas=5,
        constraints=[
            SpreadConstraint(1, "topology.kubernetes.io/zone", "ScheduleAnyway"),
        ],
    )
    assert rules_for(w) == ["schedule-anyway"]


def test_schedule_anyway_is_forgiven_when_required_anti_affinity_enforces_spread():
    w = workload(
        replicas=5,
        constraints=[SpreadConstraint(1, "topology.kubernetes.io/zone", "ScheduleAnyway")],
        required_anti_affinity_keys=["kubernetes.io/hostname"],
    )
    assert rules_for(w) == []


def test_unknown_topology_key_and_unbindable_skew_are_separate_findings():
    w = workload(
        replicas=3,
        constraints=[SpreadConstraint(3, "topology.example.com/rack", "DoNotSchedule")],
    )
    rules = rules_for(w)
    assert sorted(rules) == ["unbindable-max-skew", "unknown-topology-key"]


def test_unknown_key_respects_custom_known_list():
    w = workload(constraints=[SpreadConstraint(1, "topology.example.com/rack", "DoNotSchedule")])
    assert rules_for(w, known_keys=["topology.example.com/rack"]) == []


def test_unknown_key_on_required_anti_affinity_is_reported():
    w = workload(required_anti_affinity_keys=["rack"])
    assert rules_for(w) == ["unknown-topology-key"]


def test_max_skew_below_replicas_binds():
    w = workload(replicas=4, constraints=[SpreadConstraint(1, "kubernetes.io/hostname", "DoNotSchedule")])
    assert rules_for(w) == []


def test_replicas_below_zones_is_low_severity():
    w = workload(replicas=2, constraints=[SpreadConstraint(1, "topology.kubernetes.io/zone", "DoNotSchedule")])
    findings = evaluate_workload(w, zones=3)
    assert [f.rule for f in findings] == ["replicas-below-zones"]
    assert findings[0].severity == "low"


def test_replicas_covering_every_zone_is_clean():
    w = workload(replicas=3, constraints=[SpreadConstraint(1, "topology.kubernetes.io/zone", "DoNotSchedule")])
    assert rules_for(w, zones=3) == []


def test_single_zone_cluster_does_not_trigger_zone_rule():
    w = workload(replicas=1)
    assert rules_for(w, zones=1) == []


def test_daemonset_skips_replica_rules_but_keeps_key_validation():
    bare = workload(kind="DaemonSet", name="agent", replicas=None)
    assert rules_for(bare) == []
    typo = workload(
        kind="DaemonSet",
        name="agent",
        replicas=None,
        constraints=[SpreadConstraint(1, "rack", "DoNotSchedule")],
    )
    assert rules_for(typo) == ["unknown-topology-key"]


def test_min_replicas_threshold_is_honoured():
    assert rules_for(workload(replicas=2), min_replicas=4) == []
    assert rules_for(workload(replicas=4), min_replicas=4) == ["no-spread-rule"]


def test_hpa_note_is_appended_to_replica_count_findings():
    note = "replicas from HPA web-hpa (min 3)"
    w = workload(replicas=3, replicas_note=note)
    [finding] = evaluate_workload(w)
    assert finding.rule == "no-spread-rule"
    assert finding.message.endswith("; " + note)

    skew = workload(
        replicas=3,
        replicas_note=note,
        constraints=[SpreadConstraint(3, "topology.kubernetes.io/zone", "DoNotSchedule")],
    )
    findings = evaluate_workload(skew, zones=4)
    assert [f.rule for f in findings] == ["unbindable-max-skew", "replicas-below-zones"]
    assert all(f.message.endswith(note) for f in findings)


def test_hpa_note_is_not_added_to_findings_that_ignore_replicas():
    w = workload(
        replicas=3,
        replicas_note="replicas from HPA web-hpa (min 3)",
        constraints=[SpreadConstraint(1, "rack", "ScheduleAnyway")],
    )
    findings = evaluate_workload(w)
    assert sorted(f.rule for f in findings) == ["schedule-anyway", "unknown-topology-key"]
    assert all("HPA" not in f.message for f in findings)


def test_suggestion_uses_the_workloads_own_labels():
    w = workload(selector_labels={"tier": "frontend", "app": "web"})
    block = suggest_constraints(w)
    assert "app: web" in block
    assert "tier: frontend" in block
    # Sorted keys keep the output stable across runs.
    assert block.index("app: web") < block.index("tier: frontend")
    assert block.count("maxSkew: 1") == 2


def test_suggestion_is_valid_yaml_and_round_trips_into_the_auditor():
    import yaml

    w = workload(selector_labels={"app": "web"})
    block = yaml.safe_load(suggest_constraints(w))
    assert [c["topologyKey"] for c in block["topologySpreadConstraints"]] == [
        "topology.kubernetes.io/zone",
        "kubernetes.io/hostname",
    ]
    assert block["topologySpreadConstraints"][0]["labelSelector"]["matchLabels"] == {"app": "web"}

    # Pasting the suggestion back in must silence the finding it came from.
    fixed = parse_workload(
        {
            "kind": "Deployment",
            "metadata": {"name": "web", "namespace": "shop"},
            "spec": {
                "replicas": 3,
                "selector": {"matchLabels": {"app": "web"}},
                "template": {"metadata": {"labels": {"app": "web"}}, "spec": block},
            },
        }
    )
    assert evaluate_workload(fixed) == []


def test_suggestion_falls_back_to_the_workload_name():
    assert "app: web" in suggest_constraints(workload(selector_labels={}))


def test_evaluate_sorts_worst_first_and_is_deterministic():
    workloads = load_manifests(os.path.join(FIXTURES, "dirty"))
    findings = evaluate(workloads)
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert [(f.ref, f.rule) for f in findings] == [
        (f.ref, f.rule) for f in evaluate(load_manifests(os.path.join(FIXTURES, "dirty")))
    ]
    assert ("shop/Deployment/web", "no-spread-rule") in [(f.ref, f.rule) for f in findings]


def test_evaluate_finds_nothing_in_the_clean_fixture():
    workloads = load_manifests(os.path.join(FIXTURES, "clean"))
    assert [w.name for w in workloads] == ["api", "cron-runner", "etcd"]
    assert evaluate(workloads) == []


def test_evaluate_filters_by_severity_ignore_and_kind():
    workloads = load_manifests(os.path.join(FIXTURES, "dirty"))

    high_only = evaluate(workloads, severity_threshold="high")
    assert {f.severity for f in high_only} == {"high"}

    assert all(f.name != "web" for f in evaluate(workloads, ignore=["web*"]))
    assert all(f.namespace != "shop" for f in evaluate(workloads, ignore_namespaces=["shop"]))
    assert all(f.kind != "StatefulSet" for f in evaluate(workloads, skip_kinds=["statefulset"]))


def test_known_keys_default_covers_the_standard_zone_label():
    assert "topology.kubernetes.io/zone" in DEFAULT_KNOWN_TOPOLOGY_KEYS


def test_examples_directory_has_both_a_clean_and_a_dirty_workload():
    examples = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples", "k8s")
    workloads = load_manifests(examples)
    findings = evaluate(workloads)
    flagged = {f.name for f in findings}
    assert "web" in flagged
    assert "api" not in flagged
    assert "node-exporter" not in flagged
    assert parse_workload({"kind": "Deployment", "spec": {"template": {"spec": {}}}}).name == "<unnamed>"
