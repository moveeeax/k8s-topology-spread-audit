"""Command line entrypoint."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import __version__
from .loader import load_live, load_manifests
from .model import REPLICA_SOURCES, LoadError
from .report import render_json, render_markdown, render_table
from .rules import DEFAULT_KNOWN_TOPOLOGY_KEYS, RULES, evaluate

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k8s-topology-spread-audit",
        description="Find workloads whose replicas are not actually spread across failure domains.",
        epilog="Exit codes: 0 clean, 1 findings, 2 load error.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifests", metavar="PATH", help="audit a YAML file or directory instead of a cluster")
    source.add_argument("-n", "--namespace", help="cluster namespace to audit (default: default)")
    source.add_argument("-A", "--all-namespaces", action="store_true", help="audit every namespace")

    parser.add_argument(
        "--min-replicas",
        type=int,
        default=2,
        metavar="N",
        help="only audit workloads with at least N replicas (default: 2)",
    )
    parser.add_argument(
        "--replicas-source",
        choices=REPLICA_SOURCES,
        default="auto",
        help="replica count to audit: spec.replicas, the HPA that targets the workload "
        "(minReplicas; max(status.replicas, minReplicas) in live mode), or auto, the larger "
        "of the two (default: auto)",
    )
    parser.add_argument(
        "--known-topology-keys",
        metavar="KEYS",
        help="comma-separated extra topologyKeys your nodes carry, added to the standard "
        "Kubernetes set (manifest mode)",
    )
    parser.add_argument(
        "--zones",
        type=int,
        metavar="N",
        help="number of failure domains in manifest mode, to flag replicas < zones",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip workloads whose name matches GLOB (repeatable)",
    )
    parser.add_argument(
        "--ignore-namespace",
        action="append",
        default=[],
        metavar="GLOB",
        help="skip namespaces matching GLOB (repeatable)",
    )
    parser.add_argument(
        "--skip-kinds",
        metavar="KINDS",
        default="",
        help="comma-separated kinds to skip, e.g. DaemonSet,ReplicaSet",
    )
    parser.add_argument(
        "--severity",
        choices=("low", "medium", "high"),
        default="low",
        help="only report findings at or above this severity (default: low)",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit JSON")
    output.add_argument("--markdown", action="store_true", help="emit a Markdown table for a PR comment")

    parser.add_argument("--no-fail", action="store_true", help="always exit 0, even with findings")
    parser.add_argument("--list-rules", action="store_true", help="print the rule catalogue and exit")
    parser.add_argument("--version", action="version", version="k8s-topology-spread-audit %s" % __version__)
    return parser


def _split(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for rule in sorted(RULES):
            print("%-30s %s" % (rule, RULES[rule]))
        return EXIT_OK

    if args.min_replicas < 1:
        print("--min-replicas must be at least 1", file=sys.stderr)
        return EXIT_ERROR

    # Additive: a cluster carries the standard labels plus whatever it was given.
    known = DEFAULT_KNOWN_TOPOLOGY_KEYS + tuple(_split(args.known_topology_keys))
    zones = args.zones

    try:
        if args.manifests:
            workloads = load_manifests(args.manifests, replicas_source=args.replicas_source)
        else:
            workloads, node_keys, live_zones = load_live(
                args.namespace, args.all_namespaces, replicas_source=args.replicas_source
            )
            # Node labels are ground truth, so they replace the default key list.
            if node_keys:
                known = tuple(node_keys)
            if zones is None:
                zones = live_zones
    except LoadError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    findings = evaluate(
        workloads,
        known_keys=known,
        min_replicas=args.min_replicas,
        zones=zones,
        ignore=args.ignore,
        ignore_namespaces=args.ignore_namespace,
        skip_kinds=_split(args.skip_kinds),
        severity_threshold=args.severity,
    )

    if args.json:
        print(render_json(findings, len(workloads)))
    elif args.markdown:
        print(render_markdown(findings, len(workloads)))
    else:
        print(render_table(findings, len(workloads)))

    if findings and not args.no_fail:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
