# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-11-12

### Added

- Read `HorizontalPodAutoscaler` objects and audit an autoscaled workload at its HPA floor:
  `minReplicas` in manifest mode, `max(status.replicas, minReplicas)` in live mode. Findings
  name the HPA, e.g. "replicas from HPA web-hpa (min 3)". New flag
  `--replicas-source spec|hpa|auto` (default `auto`, the larger of `spec.replicas` and the HPA
  count). Live mode now lists HPAs; without that permission `auto` warns and falls back to
  `spec.replicas`.

## [0.2.0] - 2026-11-05

### Changed

- Require Python 3.11 or newer; Python 3.9 and 3.10 are end-of-life. CI now tests 3.11 through 3.15.
- Declare the license as an SPDX expression (`license = "MIT"`) and drop the deprecated
  license classifier; the build now requires setuptools 77 or newer.
- Raise dependency lower bounds: PyYAML 6.0.1, kubernetes 28.1.0 (`live` extra), pytest 8.0 (`dev` extra).
- CI uses actions/checkout v7 and actions/setup-python v7.

### Added

- Dependabot configuration for GitHub Actions and pip.

## [0.1.0] - 2026-10-09

- Initial release.

[0.3.0]: https://github.com/moveeeax/k8s-topology-spread-audit/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/moveeeax/k8s-topology-spread-audit/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/moveeeax/k8s-topology-spread-audit/releases/tag/v0.1.0
