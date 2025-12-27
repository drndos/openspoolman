import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from logger import log

# Ensure repository root is importable when executed from the scripts directory
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

DEFAULT_SNAPSHOT_PATH = Path("data") / "live_snapshot.json"


@dataclass(frozen=True)
class ScreenshotJob:
    output: str
    route: str
    viewport: tuple[int, int]
    max_height: int | None
    device: str
    name: str
    full_page: bool


def parse_viewport(raw_viewport: str | tuple[int, int] | list[int]) -> tuple[int, int]:
    """Parse a viewport specification from CLI, pytest, or config options."""

    if isinstance(raw_viewport, (tuple, list)) and len(raw_viewport) == 2:
        return int(raw_viewport[0]), int(raw_viewport[1])

    if isinstance(raw_viewport, str) and "x" in raw_viewport:
        width, height = raw_viewport.lower().split("x", 1)
        return int(width), int(height)

    raise ValueError("Viewport must be WIDTHxHEIGHT or two integers")


def load_config(config_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Load the screenshot configuration JSON (defaults to scripts/screenshot_config.json)."""

    if config_path is None:
        config_path = Path(__file__).with_name("screenshot_config.json")

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _device_viewport(device: dict[str, Any]) -> tuple[int, int]:
    viewport = device.get("viewport")
    if viewport is None:
        raise ValueError("Each device in the screenshot config must define a viewport")
    return parse_viewport(viewport)


def _rewrite_output_path(output: str, device: str, target_devices: list[str], output_dir: str | None) -> str:
    """Rewrite the output path to include the device prefix and optional directory."""

    path = Path(output)

    if not path.name.startswith(f"{device}_"):
        path = path.with_name(f"{device}_{path.name}")

    if output_dir:
        path = Path(output_dir) / path.name

    return str(path)


def build_jobs(
    config: dict[str, Any],
    devices: list[str] | None = None,
    output_dir: str | None = None,
    default_max_height: int | None = None,
) -> list[ScreenshotJob]:
    """Build the set of screenshots to capture from the JSON configuration."""

    device_defs = config.get("devices") or {}
    if not device_defs:
        raise ValueError("Screenshot config must define at least one device")

    selected_devices = devices or config.get("default_devices") or list(device_defs.keys())
    jobs: list[ScreenshotJob] = []

    for target in config.get("targets", []):
        target_devices = target.get("devices") or selected_devices
        route = target["route"]
        name = target.get("name") or route
        target_max_height = target.get("max_height")
        full_page = bool(target.get("full_page"))

        for device in target_devices:
            if device not in selected_devices:
                continue
            if device not in device_defs:
                raise ValueError(f"Device '{device}' referenced by target '{name}' is not defined in the config")

            viewport = _device_viewport(device_defs[device])
            resolved_max_height = None

            if not full_page:
                if isinstance(target_max_height, dict):
                    resolved_max_height = target_max_height.get(device)
                else:
                    resolved_max_height = target_max_height

                if resolved_max_height is None:
                    resolved_max_height = default_max_height

                if resolved_max_height is None:
                    resolved_max_height = viewport[1]
            output = target.get("output") or f"docs/img/{name}.png"

            if template := target.get("output_template"):
                output = template.format(device=device, name=name)

            output = _rewrite_output_path(output, device, target_devices, output_dir)
            jobs.append(
                ScreenshotJob(
                    output=output,
                    route=route,
                    viewport=viewport,
                    max_height=resolved_max_height,
                    device=device,
                    name=name,
                    full_page=full_page,
                )
            )

    return jobs


async def capture_pages(base_url: str, jobs: list[ScreenshotJob], color_scheme: str | None = None) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for job in jobs:
            viewport_width, viewport_height = job.viewport
            page_height = max(viewport_height, job.max_height or viewport_height)

            context = await browser.new_context(
                viewport={"width": viewport_width, "height": page_height},
                color_scheme=None if color_scheme == "auto" else color_scheme,
            )
            page = await context.new_page()

            url = f"{base_url}{job.route}"
            log(f"Capturing {url} -> {job.output} ({job.device})")
            await page.goto(url, wait_until="networkidle")
            await page.wait_for_timeout(1000)
            Path(job.output).parent.mkdir(parents=True, exist_ok=True)

            screenshot_kwargs: dict = {"path": job.output}
            if job.full_page:
                screenshot_kwargs["full_page"] = True
            else:
                screenshot_kwargs.update(
                    {
                        "full_page": False,
                        "clip": {"x": 0, "y": 0, "width": viewport_width, "height": job.max_height},
                    }
                )

            await page.screenshot(**screenshot_kwargs)
            await context.close()

        await browser.close()


def wait_for_server(url: str, timeout: int = 30) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code < 500:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Server at {url} did not become ready in time")


def start_server(
    port: int,
    use_test_data: bool = True,
    snapshot_path: str | None = None,
    live_read_only: bool = True,
    print_history_db: str | None = None,
) -> subprocess.Popen:
    env = os.environ.copy()
    env.setdefault("FLASK_APP", "app")
    env["FLASK_RUN_PORT"] = str(port)
    snapshot_for_env = snapshot_path or str(DEFAULT_SNAPSHOT_PATH)
    if use_test_data:
        resolved_snapshot = Path(snapshot_for_env)
        if not resolved_snapshot.exists():
            raise FileNotFoundError(
                f"Snapshot not found at {resolved_snapshot}. Create one with 'python scripts/export_live_snapshot.py --output {resolved_snapshot}'."
            )

        env["OPENSPOOLMAN_TEST_DATA"] = "1"
        env["OPENSPOOLMAN_TEST_SNAPSHOT"] = str(resolved_snapshot)
    if live_read_only:
        env["OPENSPOOLMAN_LIVE_READONLY"] = "1"
    if print_history_db:
        env["OPENSPOOLMAN_PRINT_HISTORY_DB"] = print_history_db
    env.setdefault("OPENSPOOLMAN_BASE_URL", f"http://127.0.0.1:{port}")

    process = subprocess.Popen(
        [sys.executable, "-m", "flask", "run", "--port", str(port), "--host", "0.0.0.0"],
        stdout=None,
        stderr=None,
        env=env,
    )
    return process


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate UI screenshots using a seeded dataset or live server")
    parser.add_argument("--port", type=int, default=5001, help="Port to run the Flask app on")
    parser.add_argument(
        "--config",
        dest="config_path",
        default=None,
        help="Path to screenshot configuration JSON (defaults to scripts/screenshot_config.json)",
    )
    parser.add_argument(
        "--devices",
        help="Comma-separated list of device names from the config to capture (defaults to config default_devices)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=None,
        help=(
            "Default maximum screenshot height; per-target/device overrides in the config win."
            " If omitted, captures are clipped to the viewport height unless a target sets full_page=true."
        ),
    )
    parser.add_argument("--output-dir", dest="output_dir", help="Directory to write screenshots (defaults to config outputs)")
    parser.add_argument("--base-url", dest="base_url", help="Use an already-running server instead of starting one")
    parser.add_argument("--mode", choices=["seed", "live"], default="seed", help="Start Flask in seeded test mode or against live data")
    parser.add_argument(
        "--snapshot",
        dest="snapshot",
        default=str(DEFAULT_SNAPSHOT_PATH),
        help="Path to a snapshot JSON to load when using test data (defaults to data/live_snapshot.json)",
    )
    parser.add_argument(
        "--print-history-db",
        dest="print_history_db",
        default=str(Path("data") / "demo.db"),
        help="Path to a SQLite DB for print history (defaults to data/demo.db for screenshot runs)",
    )
    parser.add_argument(
        "--test-data",
        action="store_true",
        help="Explicitly set OPENSPOOLMAN_TEST_DATA=1 when starting the Flask server",
    )
    parser.add_argument(
        "--live-readonly",
        action="store_true",
        help="Explicitly set OPENSPOOLMAN_LIVE_READONLY=1 when starting the Flask server",
    )
    parser.add_argument("--allow-live-actions", action="store_true", help="Permit live mode to make state changes instead of running read-only")
    parser.add_argument(
        "--color-scheme",
        choices=["auto", "light", "dark"],
        default=None,
        help="Force Playwright to render pages in light or dark mode (defaults to config color_scheme or auto)",
    )
    args = parser.parse_args()

    config = load_config(args.config_path)
    color_scheme = args.color_scheme or config.get("color_scheme") or "auto"
    selected_devices = args.devices.split(",") if args.devices else None
    jobs = build_jobs(
        config,
        devices=[device.strip() for device in selected_devices] if selected_devices else None,
        output_dir=args.output_dir,
        default_max_height=args.max_height,
    )

    server_process = None

    base_url = args.base_url or f"http://127.0.0.1:{args.port}"

    try:
        if base_url == f"http://127.0.0.1:{args.port}":
            use_test_data = args.test_data or args.mode == "seed"
            live_read_only = args.live_readonly or (not args.allow_live_actions)
            server_process = start_server(
                args.port,
                use_test_data=use_test_data,
                snapshot_path=args.snapshot,
                live_read_only=live_read_only,
                print_history_db=args.print_history_db,
            )
            wait_for_server(f"{base_url}/health")
        elif args.mode == "live" and not args.allow_live_actions:
            log("Live mode reminder: set OPENSPOOLMAN_LIVE_READONLY=1 on the target server to avoid state changes.")

        asyncio.run(capture_pages(base_url, jobs, color_scheme=color_scheme))
        return 0
    except FileNotFoundError as exc:
        log(exc)
        return 1
    finally:
        if server_process is not None:
            stop_server(server_process)


if __name__ == "__main__":
    raise SystemExit(main())
