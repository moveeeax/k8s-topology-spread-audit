# k8s-topology-spread-audit

[![ci](https://github.com/moveeeax/k8s-topology-spread-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/moveeeax/k8s-topology-spread-audit/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Three replicas is not high availability. The scheduler spreads pods on a best-effort basis, and
on a packed cluster — or one big node with room for all three — every replica of a service can
land in the same failure domain. The Deployment still reports `3/3 Ready`.

`k8s-topology-spread-audit` reads your workloads, works out whether anything actually forces the
replicas apart, and names the ones that only look highly available. It also catches the rules that
look strict but enforce nothing: `ScheduleAnyway` constraints, a `topologyKey` no node carries, and
a `maxSkew` so large that any placement satisfies it.

## Install

```bash
pip install git+https://github.com/moveeeax/k8s-topology-spread-audit

# live cluster mode also needs the Kubernetes client
pip install "k8s-topology-spread-audit[live] @ git+https://github.com/moveeeax/k8s-topology-spread-audit"
```

## Usage

Audit a directory of manifests, no cluster required:

```console
$ k8s-topology-spread-audit --manifests ./examples/k8s
SEVERITY  RULE                          NAMESPACE  KIND         NAME    DETAIL
HIGH      unknown-topology-key          shop       Deployment   cache   topologyKey 'topology.example.com/rack' is not present on any node, so the constraint groups every pod into one domain and enforces nothing
HIGH      no-spread-rule                shop       Deployment   web     3 replicas with no topologySpreadConstraints and no podAntiAffinity: the scheduler may place every replica in one failure domain
HIGH      no-spread-rule                shop       Deployment   worker  3 replicas with no topologySpreadConstraints and no podAntiAffinity: the scheduler may place every replica in one failure domain; replicas from HPA worker-hpa (min 3)
MEDIUM    unbindable-max-skew           shop       Deployment   cache   maxSkew 3 on topology.example.com/rack is >= the 3 replicas, so any placement satisfies it
MEDIUM    preferred-anti-affinity-only  shop       StatefulSet  queue   3 replicas spread only by preferred podAntiAffinity (kubernetes.io/hostname), which the scheduler may ignore under pressure

5 finding(s) across 6 workload(s): 3 high, 2 medium, 0 low.
$ echo $?
1
```

Audit a live cluster and filter with `jq`:

```bash
k8s-topology-spread-audit --all-namespaces --json | jq '.findings[] | select(.severity=="high")'
```

In live mode the node labels are read directly, so the known-topology-key check is grounded in
what your cluster really carries, and the zone count comes from the nodes instead of a flag.

Every `high` finding for a missing rule ships a ready-to-paste fix:

```console
$ k8s-topology-spread-audit --manifests ./examples/k8s --json | jq -r '.findings[1].suggestion'
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: web
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: web
```

The suggestion uses the workload's own selector labels, and the test suite pastes it back in to
prove the finding goes away.

## Autoscaled workloads

A Deployment managed by a HorizontalPodAutoscaler usually has `replicas: 1` or no `replicas`
field in Git, while it runs several pods in production. Read on its own, `spec.replicas` would
skip it as single-replica. The auditor therefore also reads `HorizontalPodAutoscaler` objects
(`autoscaling/v2` or `v1`), matches `scaleTargetRef` to the workload in the same namespace, and
audits the count the HPA keeps it at:

- manifest mode: `minReplicas` (1 when unset), from any file under `--manifests`;
- live mode: `max(status.replicas, minReplicas)`.

`--replicas-source` picks the count:

| Value | Count audited |
| --- | --- |
| `auto` (default) | the larger of `spec.replicas` and the HPA count |
| `hpa` | the HPA count whenever an HPA targets the workload, even below `spec.replicas` |
| `spec` | `spec.replicas` only, HPAs are not read |

When the count came from an HPA, the finding says so, e.g.
`... failure domain; replicas from HPA worker-hpa (min 3)`. In live mode HPAs are listed with
the `autoscaling/v2` API; if the service account cannot list them, `auto` prints a warning and
falls back to `spec.replicas`, while `hpa` exits with a load error.

## Rules

| Rule | Severity | Fires when |
| --- | --- | --- |
| `no-spread-rule` | high | more than one replica, no `topologySpreadConstraints`, no pod anti-affinity |
| `unknown-topology-key` | high | a `topologyKey` no node carries, so the whole cluster is one domain |
| `schedule-anyway` | medium | every constraint is `whenUnsatisfiable: ScheduleAnyway` |
| `preferred-anti-affinity-only` | medium | spread rests on `preferredDuringScheduling...`, which is dropped under pressure |
| `unbindable-max-skew` | medium | `maxSkew >= replicas`, so any placement satisfies the constraint |
| `replicas-below-zones` | low | fewer replicas than failure domains, so some domain holds none |

`k8s-topology-spread-audit --list-rules` prints the same catalogue.

What it deliberately does not flag: single-replica workloads (after the HPA floor is applied), a
required pod anti-affinity at the zone level, DaemonSets (they spread by construction, though
their `topologyKey`s are still validated), and ReplicaSets owned by a Deployment.

## How it works

Both inputs — YAML files and the live API — are flattened into the same small `Workload` record:
replica count (already raised to the HPA floor where one applies), `topologySpreadConstraints`, and the `topologyKey`s of required and preferred pod
anti-affinity. Every rule is then a pure function of that record, which is why the whole suite runs
offline against fixture manifests and the output ordering is deterministic (severity first, then
namespace, kind, name, rule).

## Flags

```
--manifests PATH            audit a YAML file or directory instead of a cluster
-n, --namespace NS          cluster namespace to audit (default: default)
-A, --all-namespaces        audit every namespace
--min-replicas N            only audit workloads with at least N replicas (default: 2)
--replicas-source {spec,hpa,auto}  where the replica count comes from (default: auto)
--known-topology-keys KEYS  extra topologyKeys your nodes carry, added to the standard set
--zones N                   failure-domain count for manifest mode
--ignore GLOB               skip workloads by name glob (repeatable)
--ignore-namespace GLOB     skip namespaces by glob (repeatable)
--skip-kinds KINDS          comma-separated kinds to skip, e.g. DaemonSet
--severity {low,medium,high}  report only at or above this severity
--json / --markdown         machine-readable or PR-comment output
--no-fail                   always exit 0
--list-rules                print the rule catalogue
```

## CI gate

```yaml
- name: Audit topology spread
  run: |
    pip install git+https://github.com/moveeeax/k8s-topology-spread-audit
    k8s-topology-spread-audit --manifests ./k8s --severity high
```

Exit codes: `0` clean, `1` findings, `2` load error. Use `--markdown` and append to
`$GITHUB_STEP_SUMMARY` for a readable report, as this repo's own CI does.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## License

MIT
