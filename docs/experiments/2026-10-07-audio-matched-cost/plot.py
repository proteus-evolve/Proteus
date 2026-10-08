"""Reproduce the two published axes from the packaged data; no live calls."""

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

BASE = Path(__file__).resolve().parent
COLORS = {"proteus": "#d66b2c", "meta": "#266d9b"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    assert output != BASE / "figures", "Preserve the published original figures."
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads((BASE / "data/experiment.json").read_text())
    for axis, name, xlabel in [
        (
            "cumulative_recorded_cost_usd",
            "matched_cost_all_45",
            "Cumulative recorded API cost equivalent (USD)",
        ),
        ("cumulative_tool_calls", "matched_cost_all_45_tool_calls", "Cumulative tool calls"),
    ]:
        fig, axes = plt.subplots(1, 3, figsize=(14.4, 4.7), sharex=True, sharey=True)
        maximum = max(p[axis] for c in data["conditions"] for p in c["points"])
        step = 0.5 if axis == "cumulative_recorded_cost_usd" else 500
        maximum = math.ceil(maximum / step) * step
        for ax, goal, title in zip(
            axes,
            ["G1", "G2", "G3"],
            ["G1: detailed (historical)", "G2: aspects only", "G3: partial + exploration"],
        ):
            cells = {c["method"]: c for c in data["conditions"] if c["goal_id"] == goal}
            pro = cells["proteus"]
            ax.axvline(pro["points"][-1][axis], color="#777777", alpha=0.28, lw=1, linestyle=":")
            for method, cell in cells.items():
                for metric, style in [
                    ("all_45", dict(lw=2.2, marker="o", ms=4.5)),
                    ("visible", dict(lw=1.25, ls="--", marker="x", ms=3.5, alpha=0.45)),
                ]:
                    segments = []
                    current = []
                    for p in cell["points"]:
                        if p[metric] is not None:
                            current.append(p)
                        elif not p["unavailable"]:
                            if current:
                                segments.append(current)
                                current = []
                    if current:
                        segments.append(current)
                    for points in segments:
                        ax.plot(
                            [p[axis] for p in points],
                            [p[metric] * 100 for p in points],
                            color=COLORS[method],
                            **style,
                        )
                last = next(p for p in reversed(cell["points"]) if p["all_45"] is not None)
                ax.annotate(
                    f"{last['all_45'] * 100:.1f}% · E{last['episode']}",
                    (last[axis], last["all_45"] * 100),
                    xytext=(0, 9 if method == "proteus" else -16),
                    textcoords="offset points",
                    ha="right",
                    fontsize=9,
                    color=COLORS[method],
                )
                if method == "meta":
                    start = cell["points"][10][axis]
                    if cell["points"][-1][axis] > start:
                        ax.axvspan(
                            start, cell["points"][-1][axis], color=COLORS[method], alpha=0.045
                        )
                    final = cell["points"][-1]
                    if final["all_45"] is None:
                        ax.text(
                            final[axis],
                            5,
                            f"E{final['episode']}\n"
                            + ("unsubmitted" if final["unavailable"] else "Hidden pending"),
                            ha="right",
                            fontsize=7,
                            color=COLORS[method],
                        )
            ax.set(
                title=title, xlabel=xlabel, xlim=(-maximum * 0.025, maximum * 1.025), ylim=(-2, 105)
            )
            ax.grid(axis="y", alpha=0.18)
            ax.spines[["top", "right"]].set_visible(False)
        axes[0].set_ylabel("Score (%)")
        handles = [
            Line2D(
                [0],
                [0],
                color=COLORS[m],
                lw=2,
                marker="o",
                label=("Proteus" if m == "proteus" else "Meta + GOAL.md") + " · Hidden",
            )
            for m in ["proteus", "meta"]
        ]
        handles += [
            Line2D(
                [0],
                [0],
                color=COLORS[m],
                lw=1.25,
                ls="--",
                alpha=0.45,
                label=("Proteus" if m == "proteus" else "Meta + GOAL.md") + " · Visible",
            )
            for m in ["proteus", "meta"]
        ]
        fig.legend(
            handles=handles,
            loc="lower center",
            ncol=4,
            frameon=False,
            bbox_to_anchor=(0.5, 0.095),
            fontsize=9,
        )
        fig.suptitle("Audio · Meta continued to Proteus recorded-token cost", fontsize=15)
        note = "All 45 Hidden cases. Pale blue: Meta rounds after E10. Dotted line: corresponding Proteus endpoint budget. Unsubmitted work receives no score."
        if axis == "cumulative_recorded_cost_usd":
            note += "\nFixed common tariff applied to known recorded tokens; not an invoice. Unknown usage is not imputed. Includes failed attempts; excludes offline evaluation and machines."
        fig.text(0.5, 0.023, note, ha="center", fontsize=7.7, color="#555555")
        fig.tight_layout(rect=(0, 0.19, 1, 0.91))
        for ext in ["png", "pdf"]:
            fig.savefig(output / (name + "." + ext), dpi=190, bbox_inches="tight")
        plt.close(fig)


if __name__ == "__main__":
    main()
