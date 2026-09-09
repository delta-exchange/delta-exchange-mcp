"""Command-line entry point for the local P&L calculator."""

import argparse
import json
import logging
import os
import re
import tempfile
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from delta_exchange_mcp.report.contract import ReportInput
from delta_exchange_mcp.report.fifo import read_fills
from delta_exchange_mcp.report.metrics import calculate

_DATA_ISLAND = re.compile(
    r'(<script id="pnl-data" type="application/json">).*?(</script>)', re.DOTALL
)


def _write(path: Path, text: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _json_for_html(payload: dict[str, object]) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_dashboard(payload: dict[str, object]) -> str:
    """Embed a validated report in the shipped offline dashboard."""
    template = (
        resources.files("delta_exchange_mcp")
        .joinpath("skills_data/pnl-analytics/assets/dashboard.html")
        .read_text(encoding="utf-8")
    )
    rendered, count = _DATA_ISLAND.subn(
        lambda match: f"{match.group(1)}\n{_json_for_html(payload)}\n{match.group(2)}",
        template,
        count=1,
    )
    if count != 1:
        raise ValueError("dashboard template has no unique pnl-data island")
    return rendered


def parser() -> argparse.ArgumentParser:
    """Build the calculator command parser."""
    value = argparse.ArgumentParser(
        prog="delta-exchange-pnl",
        description="Calculate a deterministic P&L report from local Delta exports.",
    )
    value.add_argument(
        "--input", required=True, type=Path, help="Versioned input JSON."
    )
    value.add_argument("--output", required=True, type=Path, help="Report JSON path.")
    value.add_argument(
        "--dashboard", type=Path, help="Optional offline dashboard path."
    )
    return value


def run(
    input_path: Path, output_path: Path, dashboard_path: Path | None = None
) -> None:
    """Validate inputs, calculate the report, and write requested artifacts."""
    source = input_path.expanduser().resolve()
    data = ReportInput.model_validate_json(source.read_text(encoding="utf-8"))
    fills_path = data.fills_csv.expanduser()
    if not fills_path.is_absolute():
        fills_path = source.parent / fills_path
    fills_path = fills_path.resolve()
    output = output_path.expanduser().resolve()
    dashboard = dashboard_path.expanduser().resolve() if dashboard_path else None
    destinations = [output, *([dashboard] if dashboard is not None else [])]
    if len(set(destinations)) != len(destinations):
        raise ValueError("output and dashboard must use different paths")
    if any(path in {source, fills_path} for path in destinations):
        raise ValueError("output paths must not overwrite the input JSON or fills CSV")

    report = calculate(data, read_fills(fills_path))
    payload = report.model_dump(mode="json", exclude_none=False)
    dashboard_text = render_dashboard(payload) if dashboard is not None else None
    _write(output, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    if dashboard is not None and dashboard_text is not None:
        _write(dashboard, dashboard_text)


def main() -> int:
    """Run the local report calculator."""
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        run(args.input, args.output, args.dashboard)
    except (OSError, ValueError, ValidationError) as exc:
        logging.error("P&L report failed: %s", exc)
        return 1
    logging.info("Wrote %s", args.output.expanduser().resolve())
    if args.dashboard is not None:
        logging.info("Wrote %s", args.dashboard.expanduser().resolve())
    return 0
