#!/usr/bin/env python3
"""
SV Score Analysis Script.

Analyzes speaker verification scores to suggest optimal thresholds.
Reads logs/sv_scores/*.jsonl and outputs recommended T_high/T_low.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import List, Tuple

import numpy as np


def load_scores(log_dir: Path) -> Tuple[List[float], List[float]]:
    """Load intra-speaker and inter-speaker scores from logs."""
    intra_scores = []
    inter_scores = []

    for log_file in log_dir.glob("*.jsonl"):
        with open(log_file, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    if data.get("type") == "enroll_intra":
                        intra_scores.append(data["score"])
                    elif data.get("type") == "verify_inter":
                        inter_scores.append(data["score"])
                except json.JSONDecodeError:
                    continue

    return intra_scores, inter_scores


def analyze_scores(intra: List[float], inter: List[float]) -> dict:
    """Compute statistics and suggested thresholds."""
    if not intra:
        return {"error": "No intra-speaker scores found"}

    intra_arr = np.array(intra)
    inter_arr = np.array(inter) if inter else np.array([0.45])  # default estimate

    results = {
        "intra_count": len(intra),
        "intra_min": float(intra_arr.min()),
        "intra_max": float(intra_arr.max()),
        "intra_mean": float(intra_arr.mean()),
        "intra_std": float(intra_arr.std()),
        "intra_percentiles": {
            "1%": float(np.percentile(intra_arr, 1)),
            "5%": float(np.percentile(intra_arr, 5)),
            "10%": float(np.percentile(intra_arr, 10)),
            "25%": float(np.percentile(intra_arr, 25)),
            "50%": float(np.percentile(intra_arr, 50)),
        },
    }

    if len(inter) > 0:
        results.update({
            "inter_count": len(inter),
            "inter_min": float(inter_arr.min()),
            "inter_max": float(inter_arr.max()),
            "inter_mean": float(inter_arr.mean()),
            "inter_std": float(inter_arr.std()),
        })

    # Suggested thresholds (configurable offsets)
    offset_high = 0.05
    offset_low = 0.05

    min_intra = intra_arr.min()
    max_inter = inter_arr.max() if len(inter) > 0 else 0.45

    t_high = min_intra - offset_high
    t_low = max_inter + offset_low

    results["suggested"] = {
        "threshold_high": round(t_high, 3),
        "threshold_low": round(t_low, 3),
        "gap": round(t_high - t_low, 3),
        "offset_high": offset_high,
        "offset_low": offset_low,
        "min_intra": round(float(min_intra), 3),
        "max_inter": round(float(max_inter), 3),
    }

    # Quality check
    if min_intra < 0.4:
        results["warning"] = f"min_intra ({min_intra:.3f}) < 0.4 - consider re-enrollment"

    return results


def print_results(results: dict) -> None:
    """Pretty print analysis results."""
    if "error" in results:
        print(f"Error: {results['error']}")
        return

    print("=" * 60)
    print("SPEAKER VERIFICATION SCORE ANALYSIS")
    print("=" * 60)

    print(f"\nIntra-speaker scores (n={results['intra_count']}):")
    print(f"  Min:     {results['intra_min']:.4f}")
    print(f"  Max:     {results['intra_max']:.4f}")
    print(f"  Mean:    {results['intra_mean']:.4f} ± {results['intra_std']:.4f}")
    print(f"  Percentiles: 1%={results['intra_percentiles']['1%']:.4f}, "
          f"5%={results['intra_percentiles']['5%']:.4f}, "
          f"10%={results['intra_percentiles']['10%']:.4f}")

    if "inter_count" in results:
        print(f"\nInter-speaker scores (n={results['inter_count']}):")
        print(f"  Min:     {results['inter_min']:.4f}")
        print(f"  Max:     {results['inter_max']:.4f}")
        print(f"  Mean:    {results['inter_mean']:.4f} ± {results['inter_std']:.4f}")

    print(f"\nSuggested Thresholds:")
    s = results["suggested"]
    print(f"  T_high = min_intra - {s['offset_high']} = {s['min_intra']:.3f} - {s['offset_high']} = {s['threshold_high']:.3f}")
    print(f"  T_low  = max_inter + {s['offset_low']} = {s['max_inter']:.3f} + {s['offset_low']} = {s['threshold_low']:.3f}")
    print(f"  Gap: {s['gap']:.3f}")

    if "warning" in results:
        print(f"\n⚠️  WARNING: {results['warning']}")

    print("\nConfig update:")
    print(f"  sv.threshold_high: {s['threshold_high']}")
    print(f"  sv.threshold_low:  {s['threshold_low']}")


def main():
    parser = argparse.ArgumentParser(description="Analyze SV scores for threshold tuning")
    parser.add_argument("--log-dir", default="logs/sv_scores", help="Directory with score logs")
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        print(f"Log directory not found: {log_dir}")
        print("Run enrollment/verification first to generate logs.")
        return

    intra, inter = load_scores(log_dir)
    results = analyze_scores(intra, inter)
    print_results(results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()