"""Core data types: workloads, spread posture, findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

SEVERITIES = ("low", "medium", "high")


def severity_rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        raise ValueError("unknown severity: %s" % severity)


@dataclass(frozen=True)
class SpreadConstraint:
    """One entry of spec.template.spec.topologySpreadConstraints."""

    max_skew: int
    topology_key: str
    when_unsatisfiable: str
    min_domains: Optional[int] = None

    @property
    def enforced(self) -> bool:
        return self.when_unsatisfiable == "DoNotSchedule"


@dataclass
class Workload:
    """A controller that owns more than one pod, flattened to what spread needs."""

    kind: str
    name: str
    namespace: str
    replicas: Optional[int]
    constraints: List[SpreadConstraint] = field(default_factory=list)
    required_anti_affinity_keys: List[str] = field(default_factory=list)
    preferred_anti_affinity_keys: List[str] = field(default_factory=list)
    selector_labels: Dict[str, str] = field(default_factory=dict)
    source: str = "-"

    @property
    def ref(self) -> str:
        return "%s/%s/%s" % (self.namespace, self.kind, self.name)

    @property
    def counts_replicas(self) -> bool:
        """DaemonSets spread by construction, so replica-count rules do not apply."""
        return self.replicas is not None

    @property
    def has_constraints(self) -> bool:
        return bool(self.constraints)

    @property
    def has_required_anti_affinity(self) -> bool:
        return bool(self.required_anti_affinity_keys)

    @property
    def has_preferred_anti_affinity(self) -> bool:
        return bool(self.preferred_anti_affinity_keys)

    @property
    def has_any_spread_rule(self) -> bool:
        return (
            self.has_constraints
            or self.has_required_anti_affinity
            or self.has_preferred_anti_affinity
        )


@dataclass
class Finding:
    rule: str
    severity: str
    kind: str
    name: str
    namespace: str
    message: str
    suggestion: Optional[str] = None
    source: str = "-"

    @property
    def ref(self) -> str:
        return "%s/%s/%s" % (self.namespace, self.kind, self.name)

    def sort_key(self):
        # Highest severity first, then a stable alphabetical walk of the cluster.
        return (-severity_rank(self.severity), self.namespace, self.kind, self.name, self.rule)

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "rule": self.rule,
            "severity": self.severity,
            "kind": self.kind,
            "name": self.name,
            "namespace": self.namespace,
            "message": self.message,
            "source": self.source,
        }
        if self.suggestion:
            out["suggestion"] = self.suggestion
        return out


class LoadError(Exception):
    """Raised when inputs cannot be read or parsed; maps to exit code 2."""
