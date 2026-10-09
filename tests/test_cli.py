import json
import os

import pytest

from k8s_topology_spread_audit.cli import main
from k8s_topology_spread_audit.loader import load_manifests
from k8s_topology_spread_audit.report import render_markdown, render_table
from k8s_topology_spread_audit.rules import evaluate

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CLEAN = os.path.join(FIXTURES, "clean")
DIRTY = os.path.join(FIXTURES, "dirty")


def test_exit_zero_on_a_clean_tree(capsys):
    assert main(["--manifests", CLEAN]) == 0
    assert "No spread problems found" in capsys.readouterr().out


def test_exit_one_on_findings(capsys):
    assert main(["--manifests", DIRTY]) == 1
    out = capsys.readouterr().out
    assert "no-spread-rule" in out
    assert "SEVERITY" in out
    assert "finding(s) across 4 workload(s)" in out


def test_exit_two_on_a_load_error(capsys):
    assert main(["--manifests", os.path.join(FIXTURES, "broken")]) == 2
    assert "error:" in capsys.readouterr().err


def test_exit_two_on_a_missing_path(capsys):
    assert main(["--manifests", os.path.join(FIXTURES, "does-not-exist")]) == 2
    assert "error:" in capsys.readouterr().err


def test_json_output_is_parseable_and_carries_a_summary(capsys):
    assert main(["--manifests", DIRTY, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 4
    assert payload["summary"]["high"] >= 1
    assert sum(payload["summary"].values()) == len(payload["findings"])
    first = payload["findings"][0]
    assert first["severity"] == "high"
    assert set(["rule", "severity", "kind", "name", "namespace", "message", "source"]) <= set(first)


def test_json_clean_run_has_an_empty_findings_list(capsys):
    assert main(["--manifests", CLEAN, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == []
    assert payload["summary"] == {"high": 0, "medium": 0, "low": 0}


def test_markdown_output_renders_a_table(capsys):
    assert main(["--manifests", DIRTY, "--markdown"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("## Topology spread audit")
    assert "| Severity | Rule | Workload | Detail |" in out


def test_severity_threshold_drops_lower_findings(capsys):
    assert main(["--manifests", DIRTY, "--severity", "high", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert {f["severity"] for f in payload["findings"]} == {"high"}


def test_ignore_glob_can_silence_everything(capsys):
    assert main(["--manifests", DIRTY, "--ignore", "*"]) == 0
    assert "No spread problems found" in capsys.readouterr().out


def test_no_fail_keeps_exit_zero_with_findings(capsys):
    assert main(["--manifests", DIRTY, "--no-fail"]) == 0
    assert "no-spread-rule" in capsys.readouterr().out


def test_known_topology_keys_flag_accepts_a_custom_label(capsys):
    args = ["--manifests", DIRTY, "--known-topology-keys", "topology.example.com/rack", "--json"]
    assert main(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert all(f["rule"] != "unknown-topology-key" for f in payload["findings"])
    # The custom key is now valid, but its maxSkew is still unbindable.
    assert any(f["rule"] == "unbindable-max-skew" for f in payload["findings"])


def test_zones_flag_adds_the_low_severity_coverage_finding(capsys):
    assert main(["--manifests", DIRTY, "--zones", "6", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert any(f["rule"] == "replicas-below-zones" for f in payload["findings"])


def test_min_replicas_must_be_positive(capsys):
    assert main(["--manifests", DIRTY, "--min-replicas", "0"]) == 2
    assert "--min-replicas" in capsys.readouterr().err


def test_list_rules_prints_the_catalogue(capsys):
    assert main(["--list-rules"]) == 0
    out = capsys.readouterr().out
    assert "no-spread-rule" in out
    assert "unbindable-max-skew" in out


def test_version_flag_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "k8s-topology-spread-audit" in capsys.readouterr().out


def test_live_mode_reports_a_clear_error_without_a_cluster(capsys, monkeypatch):
    import k8s_topology_spread_audit.cli as cli_module
    from k8s_topology_spread_audit.model import LoadError

    def boom(namespace, all_namespaces):
        raise LoadError("cannot load a kube config: no context")

    monkeypatch.setattr(cli_module, "load_live", boom)
    assert main(["--all-namespaces"]) == 2
    assert "cannot load a kube config" in capsys.readouterr().err


def test_manifests_and_namespace_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["--manifests", DIRTY, "--namespace", "prod"])


def test_renderers_handle_the_empty_case():
    assert "No spread problems found across 0 workload(s)" in render_table([], 0)
    assert "No spread problems found" in render_markdown([], 0)


def test_markdown_escapes_pipes_in_messages():
    workloads = load_manifests(DIRTY)
    findings = evaluate(workloads)
    findings[0].message = "a | b"
    assert "a \\| b" in render_markdown(findings, len(workloads))
