"""Build a side-by-side HTML report from persisted live VPR miss diagnostics."""
from __future__ import annotations

import argparse
import html
import os
import json
from datetime import datetime
from pathlib import Path

import cv2

from fmc.api.live_diagnostics import image_quality_metrics
from fmc.config import load_site_config
from fmc.dataset.schema import load_records


def _relative_asset(path: Path, report_dir: Path) -> str:
    return Path(os.path.relpath(path, report_dir)).as_posix()


def _format(value, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _age_days(live_timestamp, reference_timestamp) -> str:
    if not live_timestamp or not reference_timestamp:
        return "n/a"
    try:
        live_dt = datetime.fromtimestamp(float(live_timestamp)).astimezone()
        reference_dt = datetime.fromisoformat(str(reference_timestamp).replace("Z", "+00:00"))
        return f"{abs((live_dt - reference_dt).total_seconds()) / 86400:.1f} days"
    except (TypeError, ValueError, OSError):
        return "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--out", type=Path, default=None, help="HTML output path (default: diagnostics/live_misses/report.html)")
    args = parser.parse_args()

    site = load_site_config(args.site)
    diagnostics_dir = site.data_dir / "diagnostics" / "live_misses"
    log_path = diagnostics_dir / "misses.jsonl"
    if not log_path.exists():
        parser.error(f"No live miss log found at {log_path}")

    with log_path.open("r", encoding="utf-8") as log_file:
        rows = [json.loads(line) for line in log_file if line.strip()]
    client_events_path = site.data_dir / "diagnostics" / "client_events.jsonl"
    if client_events_path.exists():
        with client_events_path.open("r", encoding="utf-8") as event_file:
            client_events = [json.loads(line) for line in event_file if line.strip()]
    else:
        client_events = []
    records_by_id = {record.image_id: record for record in load_records(site.dataset_jsonl_path)}
    report_path = args.out or diagnostics_dir / "report.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    stage_counts: dict[str, int] = {}
    for row in rows:
        stage = row.get("failure_stage", "unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    cards = []
    for row in reversed(rows):
        image_html = "<p class='missing'>Frame sample was not saved (sample cap reached or sampling disabled).</p>"
        if row.get("frame_path"):
            frame_path = site.data_dir / row["frame_path"]
            if frame_path.exists():
                image_src = html.escape(_relative_asset(frame_path, report_path.parent))
                image_html = f"<img class='query-image' src='{image_src}' alt='Failed query frame'>"

        quality = row.get("server_image", {})
        client = row.get("client", {})
        ground_truth = row.get("ground_truth_location_id")
        candidates = []
        for candidate in row.get("candidates", []):
            record = records_by_id.get(candidate.get("image_id"))
            reference_html = "<span class='missing'>Reference image missing</span>"
            reference_quality = {}
            if record is not None:
                reference_path = site.processed_dir / record.processed_path
                if reference_path.exists():
                    reference_src = html.escape(_relative_asset(reference_path, report_path.parent))
                    reference_html = f"<img src='{reference_src}' alt='Retrieved reference'>"
                    reference_image = cv2.imread(str(reference_path))
                    if reference_image is not None:
                        reference_quality = image_quality_metrics(reference_image)
            correct_tag = ""
            if ground_truth:
                correct_tag = "ground-truth location" if candidate.get("location_id") == ground_truth else "other location"
            candidates.append(
                "<article class='candidate'>"
                f"<div class='candidate-title'>Rank {candidate.get('rank')} · "
                f"{html.escape(str(candidate.get('image_id', 'unknown')))} · "
                f"score {_format(candidate.get('similarity'), 4)}</div>"
                f"{reference_html}"
                f"<p>{html.escape(correct_tag)} · location={html.escape(str(candidate.get('location_id') or 'unknown'))} "
                f"({candidate.get('x', 0):.2f}, {candidate.get('y', 0):.2f})</p>"
                f"<p>ORB {candidate.get('num_matches', 0)} matches, "
                f"{candidate.get('num_inliers', 0)} inliers, ratio={candidate.get('inlier_ratio', 0):.3f}, "
                f"accepted={candidate.get('passed', False)}</p>"
                f"<p>Reference brightness={_format(reference_quality.get('brightness_mean'))}; "
                f"sharpness={_format(reference_quality.get('laplacian_variance'))}; "
                f"surveyed={html.escape(str(candidate.get('timestamp', 'unknown')))} "
                f"({_age_days(row.get('received_at'), candidate.get('timestamp'))} before live query)</p>"
                "</article>"
            )

        cards.append(
            "<section class='miss'>"
            f"<h2>{html.escape(str(row.get('sample_id', 'miss')))} · "
            f"{html.escape(str(row.get('failure_stage', 'unknown')))}</h2>"
            f"<p>time={html.escape(str(row.get('received_at')))} · device={html.escape(str(row.get('device_id')))} · "
            f"trigger={html.escape(str(row.get('capture_trigger')))} · ground truth="
            f"{html.escape(str(ground_truth or 'unknown'))} · retrieved ground truth={row.get('ground_truth_in_top_k')}</p>"
            f"<p>Client camera={client.get('source_width')}×{client.get('source_height')} → "
            f"{client.get('encoded_width')}×{client.get('encoded_height')} JPEG q={client.get('jpeg_quality')} "
            f"client sharpness={_format(client.get('laplacian_variance'))}; server decoded="
            f"{quality.get('width')}×{quality.get('height')} {row.get('uploaded_bytes')} bytes, "
            f"sharpness={_format(quality.get('laplacian_variance'))}, brightness="
            f"{_format(quality.get('brightness_mean'))}±{_format(quality.get('brightness_std'))}</p>"
            "<div class='comparison'>"
            f"<figure><figcaption>Failed live query</figcaption>{image_html}</figure>"
            f"<div class='candidates'>{''.join(candidates) or '<p>No retrieved candidates.</p>'}</div>"
            "</div></section>"
        )

    trigger_counts: dict[str, int] = {}
    for row in rows:
        trigger = row.get("capture_trigger", "unknown")
        trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
    skipped_blurry = sum(event.get("event") == "frame_skipped_blur" for event in client_events)
    summary_html = " ".join(f"<span>{html.escape(stage)}: {count}</span>" for stage, count in sorted(stage_counts.items()))
    trigger_html = " ".join(f"<span>{html.escape(trigger)} captures: {count}</span>" for trigger, count in sorted(trigger_counts.items()))
    document = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live VPR miss diagnostics · {html.escape(args.site)}</title>
<style>
body{{font:15px/1.5 system-ui,sans-serif;margin:24px;background:#f2f4f3;color:#17231f}}
h1{{font-size:24px}}.summary{{display:flex;gap:10px;flex-wrap:wrap}}.summary span{{padding:6px 10px;background:#dfe9e3;border-radius:6px}}
.miss{{padding:18px 0;border-top:2px solid #b9c9bf}}.miss h2{{font-size:18px}}.comparison{{display:grid;grid-template-columns:minmax(280px,1fr) minmax(360px,1.3fr);gap:18px;align-items:start}}
figure{{margin:0}}figcaption,.candidate-title{{font-weight:700;margin-bottom:8px}}img{{display:block;max-width:100%;height:auto;border:1px solid #aebbb3;border-radius:4px}}.query-image{{max-height:560px;object-fit:contain;background:#d8dedb}}
.candidates{{display:grid;gap:12px}}.candidate{{padding:12px;background:white;border:1px solid #cad4ce;border-radius:6px}}.candidate img{{max-height:300px;object-fit:contain}}p{{margin:6px 0;font-size:13px}}.missing{{color:#8b4a32}}
@media(max-width:800px){{body{{margin:14px}}.comparison{{grid-template-columns:1fr}}}}
</style><body><h1>Live VPR miss diagnostics: {html.escape(args.site)}</h1>
<p>{len(rows)} VPR misses · {skipped_blurry} client frames skipped by blur gate</p>
<div class="summary">{summary_html}{trigger_html}</div>{''.join(cards)}</body></html>"""
    report_path.write_text(document, encoding="utf-8")
    print(f"Analyzed {len(rows)} live misses. Report: {report_path}")
    print("Miss categories:", ", ".join(f"{stage}={count}" for stage, count in sorted(stage_counts.items())))
    print(f"Client blur-gate skips: {skipped_blurry}")


if __name__ == "__main__":
    main()