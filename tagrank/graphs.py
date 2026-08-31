"""Session-summary chart building and prediction-log loading."""

import json
from json import JSONDecodeError
from typing import Any

import matplotlib.pyplot as plt  # type: ignore
import numpy as np
from trueskill import Rating  # type: ignore

from config import DATA_DIR
from tagrank.rating import RatingSystem, trueskill_number_from_rating


def _migrate_legacy_prediction_log(legacy_path) -> list[dict[str, Any]]:
    """One-time upgrade from the old single-JSON-array log to JSONL."""
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            raw_entries = json.loads(f.read() or "[]")
    except (JSONDecodeError, ValueError):
        return []
    if not isinstance(raw_entries, list):
        return []
    entries = [entry for entry in raw_entries if isinstance(entry, dict)]
    jsonl_path = legacy_path.with_suffix(".jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    legacy_path.rename(legacy_path.with_suffix(".json.bak"))
    return entries


def load_prediction_entries() -> list[dict[str, Any]]:
    jsonl_path = DATA_DIR / "prediction_log.jsonl"
    legacy_path = DATA_DIR / "prediction_log.json"
    if not jsonl_path.exists() and legacy_path.exists():
        return _migrate_legacy_prediction_log(legacy_path)
    if not jsonl_path.exists():
        return []
    entries = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def top_tags_from_rating_system(rating_system: RatingSystem, amount_of_tags: int) -> list[tuple[str, Rating]]:
    return sorted(rating_system.current_ratings.items(),
                  key=lambda x: trueskill_number_from_rating(x[1]),
                  reverse=True)[:max(10, amount_of_tags)]


def calculate_tag_count_for_height(available_height_px: int | None = None, min_tags: int = 5, max_tags: int = 20) -> int:
    if available_height_px is None:
        return max_tags
    try:
        available_height_px = int(available_height_px)
    except (TypeError, ValueError):
        return max_tags
    if available_height_px <= 0:
        return min_tags
    min_height = 350
    max_height = 1200
    clamped_height = min(max(available_height_px, min_height), max_height)
    normalized = (clamped_height - min_height) / (max_height - min_height)
    tag_count = min_tags + round(normalized * (max_tags - min_tags))
    return max(min_tags, min(max_tags, tag_count))


def _create_placeholder_figure(title: str, message: str) -> plt.Figure:
    """Create an informational placeholder figure for when data is unavailable."""
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.text(0.5, 0.6, message, ha='center', va='center', fontsize=14, style='italic', color='#666666')
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.axis('off')
    fig.tight_layout()
    return fig


def build_session_summary_figures(
    prediction_entries: list[dict[str, Any]],
    top_tags: list[tuple[str, Rating]],
    *,
    figure_height: int = 700,
) -> list[plt.Figure]:
    if not isinstance(prediction_entries, list):
        prediction_entries = []
    figures: list[plt.Figure] = []

    # Figure 1: Rolling prediction accuracy or placeholder
    if prediction_entries:
        tag_correct = []
        photo_correct = []
        combo_correct = []
        running_tag = 0.0
        running_photo = 0.0
        running_combined = 0.0

        for index, entry in enumerate(prediction_entries, start=1):
            tag_is_correct = bool(entry.get("tag_prediction") == entry.get("user_selection"))
            photo_is_correct = bool(entry.get("photo_prediction") == entry.get("user_selection"))
            combined = (1.0 if tag_is_correct or photo_is_correct else 0.0)
            tag_correct.append(tag_is_correct)
            photo_correct.append(photo_is_correct)
            combo_correct.append(combined)
            running_tag += (1.0 if tag_is_correct else 0.0)
            running_photo += (1.0 if photo_is_correct else 0.0)
            running_combined += combined
            tag_accuracy = running_tag / index
            photo_accuracy = running_photo / index
            overall_accuracy = running_combined / index
            tag_correct[index - 1] = tag_accuracy
            photo_correct[index - 1] = photo_accuracy
            combo_correct[index - 1] = overall_accuracy

        fig, axes = plt.subplots(figsize=(11, 5))
        x_values = list(range(1, len(prediction_entries) + 1))
        axes.plot(x_values, tag_correct, label="Tag model", color="#4C78A8", linewidth=2.0, marker='o', markersize=3, alpha=0.7)
        axes.plot(x_values, photo_correct, label="Photo model", color="#F58518", linewidth=2.0, marker='s', markersize=3, alpha=0.7)
        axes.plot(x_values, combo_correct, label="Combined prediction", color="#54A24B", linewidth=2.5, marker='^', markersize=4, alpha=0.8)
        axes.set_title("Rolling Prediction Accuracy Over Time", fontsize=12, fontweight='bold')
        axes.set_xlabel("Comparison Number", fontsize=10)
        axes.set_ylabel("Cumulative Accuracy", fontsize=10)
        axes.set_ylim(-0.05, 1.1)
        axes.grid(True, alpha=0.2, linestyle='--')
        axes.legend(loc='lower right', fontsize=9)
        fig.tight_layout()
        figures.append(fig)
    else:
        figures.append(_create_placeholder_figure(
            "Rolling Prediction Accuracy Over Time",
            "No prediction data yet.\nComplete a ranking session to see accuracy trends."
        ))

    # Figure 2: Ratings per date or placeholder
    if prediction_entries:
        date_counter: dict[str, int] = {}
        for entry in prediction_entries:
            date_value = str(entry.get("date") or "unknown")
            date_counter[date_value] = date_counter.get(date_value, 0) + 1
        dates = list(date_counter.keys())
        counts = [date_counter[date] for date in dates]

        fig, ax = plt.subplots(figsize=(11, 5))
        x_positions = list(range(len(dates)))
        bars = ax.bar(x_positions, counts, color="#72B7B2", edgecolor="#4A8A84", linewidth=1.5)
        ax.set_xticks(x_positions)
        ax.set_xticklabels(dates)
        ax.set_title("Ratings per Session Date", fontsize=12, fontweight='bold')
        ax.set_xlabel("Date", fontsize=10)
        ax.set_ylabel("Number of Comparisons", fontsize=10)
        ax.grid(axis="y", alpha=0.2, linestyle='--')

        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{int(height)}',
                   ha='center', va='bottom', fontsize=8)

        fig.autofmt_xdate(rotation=45, ha='right')
        fig.tight_layout()
        figures.append(fig)
    else:
        figures.append(_create_placeholder_figure(
            "Ratings per Session Date",
            "No prediction data yet.\nComplete a ranking session to see activity by date."
        ))

    # Figure 3: Confidence calibration or placeholder
    calibration_figure = None
    if prediction_entries:
        bins = np.linspace(0, 1, 11)
        calibration_values = []
        calibration_labels = []
        sample_counts = []

        for start, end in zip(bins[:-1], bins[1:]):
            bucket = [entry for entry in prediction_entries if start <= float(entry.get("confidence", 0.0)) < end]
            if not bucket:
                accuracy = 0.0
                sample_counts.append(0)
            else:
                correctness = [1.0 if entry.get("tag_prediction") == entry.get("user_selection") else 0.0 for entry in bucket]
                accuracy = float(sum(correctness) / len(correctness))
                sample_counts.append(len(bucket))

            calibration_values.append(accuracy)
            calibration_labels.append(f"{start:.1f}–{end:.1f}")

        if len(calibration_values) > 0 and sum(sample_counts) > 0:
            fig, ax = plt.subplots(figsize=(11, 5))
            colors = ["#E45756" if v > 0 else "#CCCCCC" for v in calibration_values]
            bars = ax.bar(calibration_labels, calibration_values, color=colors, edgecolor="#A83339", linewidth=1.2)

            ax.axhline(0.5, color="black", linestyle="--", linewidth=1.5, alpha=0.6, label="Perfect calibration")

            ax.set_title("Tag Model Confidence Calibration", fontsize=12, fontweight='bold')
            ax.set_xlabel("Confidence Bin", fontsize=10)
            ax.set_ylabel("Observed Correctness", fontsize=10)
            ax.set_ylim(-0.05, 1.1)
            ax.grid(axis="y", alpha=0.2, linestyle='--')
            ax.legend(loc='upper left', fontsize=9)

            for bar, count in zip(bars, sample_counts):
                if count > 0:
                    height = bar.get_height()
                    ax.text(bar.get_x() + bar.get_width()/2., height + 0.02,
                           f'n={count}',
                           ha='center', va='bottom', fontsize=8, style='italic')

            fig.tight_layout()
            calibration_figure = fig
    if calibration_figure is None:
        calibration_figure = _create_placeholder_figure(
            "Tag Model Confidence Calibration",
            "No prediction data yet.\nComplete a ranking session to see calibration analysis."
        )
    figures.append(calibration_figure)

    # Figure 4: Final tag rankings by TrueSkill score
    ranked_tags = sorted(top_tags, key=lambda tag_rating: trueskill_number_from_rating(tag_rating[1]), reverse=True)
    visible_count = calculate_tag_count_for_height(figure_height)
    available_tags = ranked_tags[:visible_count]
    total_ranked_tags = len(ranked_tags)

    fig, ax = plt.subplots(figsize=(10, max(5.0, 0.35 * len(available_tags) + 1.5)))
    labels = [tag for tag, _ in available_tags]
    scores = [trueskill_number_from_rating(rating) for _, rating in available_tags]

    bars = ax.barh(labels[::-1], scores[::-1], color="#B279A2", edgecolor="#7D4F77", linewidth=1.2)
    ax.invert_yaxis()

    for i, (bar, score) in enumerate(zip(bars, scores[::-1])):
        ax.text(score + 0.3, bar.get_y() + bar.get_height()/2.,
               f'{score:.1f}',
               ha='left', va='center', fontsize=9, fontweight='bold')

    title_suffix = f"(showing {len(labels)} of {total_ranked_tags})" if len(labels) < total_ranked_tags else f"(all {total_ranked_tags})"
    ax.set_title(f"Final Tag Rankings by TrueSkill Score\n{title_suffix}", fontsize=12, fontweight='bold')
    ax.set_xlabel("TrueSkill Score", fontsize=10)
    ax.set_ylabel("Tag", fontsize=10)
    ax.grid(axis="x", alpha=0.2, linestyle='--')
    ax.set_xlim(0, max(scores) * 1.15 if scores else 10)

    fig.tight_layout()
    figures.append(fig)

    return figures
