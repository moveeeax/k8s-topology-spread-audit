# k8s-topology-spread-audit
Audit Kubernetes workloads for missing or ineffective spread rules: no topologySpreadConstraints, best-effort-only anti-affinity, unbindable maxSkew, unknown topologyKeys. Table/JSON, exit 1 on findings.
