"""Performance benchmark for plugin startup loading.

Measures the time spent in different phases of plugin discovery and initialization:
1. Directory scanning (os.scandir vs Path.iterdir)
2. TOML parsing (plugin.toml files)
3. Dependency checking (if any)
4. Module imports (if loaded at startup)

Usage:
    uv run python scripts/benchmark_plugin_startup.py
    uv run python scripts/benchmark_plugin_startup.py --plugins-root ~/.neko/plugins
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

try:
    import tomli
except ImportError:
    import tomllib as tomli  # Python 3.11+


def timeit(func):
    """Decorator to measure execution time."""
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return result, elapsed_ms
    return wrapper


@timeit
def benchmark_directory_scan_scandir(root: Path) -> list[Path]:
    """Benchmark: os.scandir() approach."""
    import os

    plugin_dirs = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir() and not entry.name.startswith("."):
                plugin_dirs.append(Path(entry.path))
    return plugin_dirs


@timeit
def benchmark_directory_scan_iterdir(root: Path) -> list[Path]:
    """Benchmark: Path.iterdir() approach."""
    plugin_dirs = []
    for path in root.iterdir():
        if path.is_dir() and not path.name.startswith("."):
            plugin_dirs.append(path)
    return plugin_dirs


@timeit
def benchmark_toml_parsing(plugin_dirs: list[Path]) -> list[dict[str, Any]]:
    """Benchmark: Parse plugin.toml for all plugins."""
    manifests = []
    for plugin_dir in plugin_dirs:
        toml_path = plugin_dir / "plugin.toml"
        if not toml_path.exists():
            continue

        try:
            with open(toml_path, "rb") as f:
                manifest = tomli.load(f)
            manifests.append(manifest)
        except Exception:
            # Skip corrupted files
            continue

    return manifests


@timeit
def benchmark_version_extraction(manifests: list[dict[str, Any]]) -> list[str]:
    """Benchmark: Extract version from parsed manifests."""
    versions = []
    for manifest in manifests:
        version = manifest.get("plugin", {}).get("version", "unknown")
        versions.append(version)
    return versions


@timeit
def benchmark_combined_scan_and_parse(root: Path) -> tuple[list[Path], list[dict]]:
    """Benchmark: Combined scan + parse (realistic scenario)."""
    plugin_dirs = []
    manifests = []

    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue

        plugin_dirs.append(path)

        toml_path = path / "plugin.toml"
        if toml_path.exists():
            try:
                with open(toml_path, "rb") as f:
                    manifest = tomli.load(f)
                manifests.append(manifest)
            except Exception:
                continue

    return plugin_dirs, manifests


def run_benchmarks(plugins_root: Path, rounds: int = 5) -> dict[str, Any]:
    """Run all benchmarks multiple times and aggregate results."""

    if not plugins_root.exists():
        raise FileNotFoundError(f"Plugins root not found: {plugins_root}")

    print(f"Benchmarking plugin startup performance...")
    print(f"Plugins root: {plugins_root}")
    print(f"Rounds: {rounds}")
    print()

    results = {
        "plugins_root": str(plugins_root),
        "rounds": rounds,
        "benchmarks": {},
    }

    # Warmup
    print("Warming up...")
    list(plugins_root.iterdir())

    # Benchmark 1: Directory scan (scandir)
    print("1. Directory scan (os.scandir)...")
    times = []
    for _ in range(rounds):
        _, elapsed = benchmark_directory_scan_scandir(plugins_root)
        times.append(elapsed)

    plugin_dirs = benchmark_directory_scan_scandir(plugins_root)[0]
    results["benchmarks"]["directory_scan_scandir"] = {
        "times_ms": times,
        "avg_ms": sum(times) / len(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "plugin_count": len(plugin_dirs),
    }
    print(f"   Avg: {results['benchmarks']['directory_scan_scandir']['avg_ms']:.2f}ms")
    print(f"   Found {len(plugin_dirs)} plugins")

    # Benchmark 2: Directory scan (iterdir)
    print("2. Directory scan (Path.iterdir)...")
    times = []
    for _ in range(rounds):
        _, elapsed = benchmark_directory_scan_iterdir(plugins_root)
        times.append(elapsed)

    results["benchmarks"]["directory_scan_iterdir"] = {
        "times_ms": times,
        "avg_ms": sum(times) / len(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }
    print(f"   Avg: {results['benchmarks']['directory_scan_iterdir']['avg_ms']:.2f}ms")

    # Benchmark 3: TOML parsing
    print("3. TOML parsing...")
    times = []
    manifest_counts = []
    for _ in range(rounds):
        manifests, elapsed = benchmark_toml_parsing(plugin_dirs)
        times.append(elapsed)
        manifest_counts.append(len(manifests))

    results["benchmarks"]["toml_parsing"] = {
        "times_ms": times,
        "avg_ms": sum(times) / len(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "manifest_count": manifest_counts[0],
    }
    print(f"   Avg: {results['benchmarks']['toml_parsing']['avg_ms']:.2f}ms")
    print(f"   Parsed {manifest_counts[0]} manifests")

    # Benchmark 4: Version extraction
    manifests = benchmark_toml_parsing(plugin_dirs)[0]
    print("4. Version extraction...")
    times = []
    for _ in range(rounds):
        _, elapsed = benchmark_version_extraction(manifests)
        times.append(elapsed)

    results["benchmarks"]["version_extraction"] = {
        "times_ms": times,
        "avg_ms": sum(times) / len(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }
    print(f"   Avg: {results['benchmarks']['version_extraction']['avg_ms']:.2f}ms")

    # Benchmark 5: Combined (realistic)
    print("5. Combined scan + parse (realistic scenario)...")
    times = []
    for _ in range(rounds):
        _, elapsed = benchmark_combined_scan_and_parse(plugins_root)
        times.append(elapsed)

    results["benchmarks"]["combined_scan_parse"] = {
        "times_ms": times,
        "avg_ms": sum(times) / len(times),
        "min_ms": min(times),
        "max_ms": max(times),
    }
    print(f"   Avg: {results['benchmarks']['combined_scan_parse']['avg_ms']:.2f}ms")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    total_avg = (
        results["benchmarks"]["directory_scan_iterdir"]["avg_ms"]
        + results["benchmarks"]["toml_parsing"]["avg_ms"]
        + results["benchmarks"]["version_extraction"]["avg_ms"]
    )

    print(f"Total (scan + parse + extract): {total_avg:.2f}ms")
    print(f"  - Directory scan: {results['benchmarks']['directory_scan_iterdir']['avg_ms']:.2f}ms")
    print(f"  - TOML parsing: {results['benchmarks']['toml_parsing']['avg_ms']:.2f}ms")
    print(f"  - Version extraction: {results['benchmarks']['version_extraction']['avg_ms']:.2f}ms")
    print()
    print(f"Combined (realistic): {results['benchmarks']['combined_scan_parse']['avg_ms']:.2f}ms")
    print(f"Plugins scanned: {results['benchmarks']['directory_scan_scandir']['plugin_count']}")
    print(f"Manifests parsed: {results['benchmarks']['toml_parsing']['manifest_count']}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark plugin startup performance"
    )
    parser.add_argument(
        "--plugins-root",
        type=Path,
        help="Path to plugins root directory",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=5,
        help="Number of benchmark rounds (default: 5)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file for results",
    )

    args = parser.parse_args()

    # Determine plugins root
    if args.plugins_root:
        plugins_root = args.plugins_root.expanduser().resolve()
    else:
        # Try common locations
        candidates = [
            Path.home() / ".neko" / "plugins",
            Path("plugin") / "plugins",
        ]
        plugins_root = None
        for candidate in candidates:
            if candidate.exists():
                plugins_root = candidate
                break

        if not plugins_root:
            print("ERROR: Could not find plugins directory.")
            print("Please specify --plugins-root")
            return 1

    try:
        results = run_benchmarks(plugins_root, rounds=args.rounds)

        if args.output:
            args.output.write_text(
                json.dumps(results, indent=2),
                encoding="utf-8",
            )
            print()
            print(f"Results saved to: {args.output}")

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
