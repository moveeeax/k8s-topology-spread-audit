"""The actual audit: turn a workload's spread posture into findings."""

from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, List, Optional, Sequence

from .model import Finding, Workload, severity_rank

#: Topology keys that real clusters carry. Anything else is almost always a typo
#: or a label that was never applied to the nodes.
DEFAULT_KNOWN_TOPOLOGY_KEYS = (
    "kubernetes.io/hostname",
    "kubernetes.io/os",
    "kubernetes.io/arch",
    "topology.kubernetes.io/zone",
    "topology.kubernetes.io/region",
    "node.kubernetes.io/instance-type",
    "failure-domain.beta.kubernetes.io/zone",
    "failure-domain.beta.kubernetes.io/region",
    "beta.kubernetes.io/instance-type",
    "topology.ebs.csi.aws.com/zone",
    "topology.gke.io/zone",
)

RULES: Dict[str, str] = {
    "no-spread-rule": "multi-replica workload with no topologySpreadConstraints and no pod anti-affinity",
    "preferred-anti-affinity-only": "spread relies on preferred (best-effort) anti-affinity",
    "schedule-anyway": "every topologySpreadConstraint is ScheduleAnyway, so skew is advisory",
    "unknown-topology-key": "topologyKey is not a label any node is known to carry",
    "unbindable-max-skew": "maxSkew is at least the replica count, so the constraint can never bind",
    "replicas-below-zones": "fewer replicas than failure domains, so the workload cannot cover the cluster",
}

PREFERRED_SPREAD_KEY = "topology.kubernetes.io/zone"


def suggest_constraints(workload: Workload) -> str:
    """A ready-to-paste block, using the workload's own selector labels."""
    labels = workload.selector_labels or {"app": workload.name}
    label_lines = "\n".join(
        "        %s: %s" % (key, labels[key]) for key in sorted(labels)
    )
    blocks = []
    for key in (PREFERRED_SPREAD_KEY, "kubernetes.io/hostname"):
        blocks.append(
            "  - maxSkew: 1\n"
            "    topologyKey: %s\n"
            "    whenUnsatisfiable: DoNotSchedule\n"
            "    labelSelector:\n"
            "      matchLabels:\n%s" % (key, label_lines)
        )
    return "topologySpreadConstraints:\n" + "\n".join(blocks)


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def evaluate_workload(
    workload: Workload,
    known_keys: Sequence[str] = DEFAULT_KNOWN_TOPOLOGY_KEYS,
    min_replicas: int = 2,
    zones: Optional[int] = None,
) -> List[Finding]:
    """Every finding for one workload, in rule-definition order."""
    findings: List[Finding] = []
    known = set(known_keys)
    replicas = workload.replicas
    multi = workload.counts_replicas and replicas is not None and replicas >= min_replicas

    def add(rule: str, severity: str, message: str, suggestion: Optional[str] = None) -> None:
        findings.append(
            Finding(
                rule=rule,
                severity=severity,
                kind=workload.kind,
                name=workload.name,
                namespace=workload.namespace,
                message=message,
                suggestion=suggestion,
                source=workload.source,
            )
        )

    def counted(message: str) -> str:
        # Say where the replica count came from when it is not spec.replicas.
        if workload.replicas_note:
            return "%s; %s" % (message, workload.replicas_note)
        return message

    if multi and not workload.has_any_spread_rule:
        add(
            "no-spread-rule",
            "high",
            counted(
                "%d replicas with no topologySpreadConstraints and no podAntiAffinity: "
                "the scheduler may place every replica in one failure domain" % replicas
            ),
            suggest_constraints(workload),
        )
    elif multi and not workload.has_constraints and not workload.has_required_anti_affinity:
        keys = ", ".join(sorted(set(workload.preferred_anti_affinity_keys)))
        add(
            "preferred-anti-affinity-only",
            "medium",
            counted(
                "%d replicas spread only by preferred podAntiAffinity (%s), which the scheduler "
                "may ignore under pressure" % (replicas, keys)
            ),
            suggest_constraints(workload),
        )

    if (
        workload.has_constraints
        and not workload.has_required_anti_affinity
        and all(not c.enforced for c in workload.constraints)
    ):
        add(
            "schedule-anyway",
            "medium",
            "all %d topologySpreadConstraint(s) use whenUnsatisfiable: ScheduleAnyway, so the "
            "skew limit is a hint and not a guarantee" % len(workload.constraints),
        )

    for constraint in workload.constraints:
        if constraint.topology_key not in known:
            add(
                "unknown-topology-key",
                "high",
                "topologyKey %r is not present on any node, so the constraint groups every pod "
                "into one domain and enforces nothing" % constraint.topology_key,
            )

    for key in sorted(set(workload.required_anti_affinity_keys)):
        if key not in known:
            add(
                "unknown-topology-key",
                "high",
                "required podAntiAffinity topologyKey %r is not present on any node, so the rule "
                "treats the whole cluster as a single domain" % key,
            )

    if multi and replicas is not None:
        for constraint in workload.constraints:
            if constraint.max_skew >= replicas:
                add(
                    "unbindable-max-skew",
                    "medium",
                    counted(
                        "maxSkew %d on %s is >= the %d replicas, so any placement satisfies it"
                        % (constraint.max_skew, constraint.topology_key, replicas)
                    ),
                )

    if zones is not None and zones > 1 and workload.counts_replicas and replicas is not None:
        if replicas < zones:
            add(
                "replicas-below-zones",
                "low",
                counted(
                    "%d replicas across %d failure domains: at least one domain holds no replica"
                    % (replicas, zones)
                ),
            )

    return findings


def evaluate(
    workloads: Iterable[Workload],
    known_keys: Sequence[str] = DEFAULT_KNOWN_TOPOLOGY_KEYS,
    min_replicas: int = 2,
    zones: Optional[int] = None,
    ignore: Sequence[str] = (),
    ignore_namespaces: Sequence[str] = (),
    skip_kinds: Sequence[str] = (),
    severity_threshold: str = "low",
) -> List[Finding]:
    """Audit every workload and return findings sorted worst-first."""
    threshold = severity_rank(severity_threshold)
    skip = {k.lower() for k in skip_kinds}
    findings: List[Finding] = []

    for workload in workloads:
        if workload.kind.lower() in skip:
            continue
        if ignore and _matches_any(workload.name, ignore):
            continue
        if ignore_namespaces and _matches_any(workload.namespace, ignore_namespaces):
            continue
        for finding in evaluate_workload(workload, known_keys, min_replicas, zones):
            if severity_rank(finding.severity) >= threshold:
                findings.append(finding)

    findings.sort(key=lambda f: f.sort_key())
    return findings
