import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from logger import log

# Ensure repository root is importable and the working directory matches the project root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Align the working directory so relative paths (e.g., SQLite DBs) resolve like running from repo root
os.chdir(REPO_ROOT)

mqtt_bambulab = importlib.import_module("mqtt_bambulab")
print_history = importlib.import_module("print_history")
spoolman_service = importlib.import_module("spoolman_service")
spoolman_client = importlib.import_module("spoolman_client")

getLastAMSConfig = mqtt_bambulab.getLastAMSConfig
getPrinterModel = mqtt_bambulab.getPrinterModel
init_mqtt = mqtt_bambulab.init_mqtt
isMqttClientConnected = mqtt_bambulab.isMqttClientConnected
get_prints_with_filament = print_history.get_prints_with_filament
getSettings = spoolman_service.getSettings
fetchSpoolList = spoolman_client.fetchSpoolList


DEFAULT_OUTPUT = Path("data/live_snapshot.json")


def wait_for_mqtt_ready(timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if isMqttClientConnected():
            return True
        time.sleep(0.5)
    return False


def wait_for_ams_data(timeout: int = 30) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        config = getLastAMSConfig() or {}
        if config.get("ams") or config.get("vt_tray"):
            return config
        time.sleep(0.5)
    return getLastAMSConfig() or {}


def export_snapshot(path: Path, include_prints: bool = True) -> None:
    init_mqtt(daemon=True)

    last_ams_config = {}

    if wait_for_mqtt_ready():
        last_ams_config = wait_for_ams_data()
        if not (last_ams_config.get("ams") or last_ams_config.get("vt_tray")):
            log("⚠️ AMS data not received before timeout; continuing without trays")
    else:
        log("⚠️ MQTT connection not ready; continuing without AMS tray data")

    spools = fetchSpoolList()
    for spool in spools:
        if "cost_per_gram" not in spool:
            initial_weight = spool.get("initial_weight") or spool.get("filament", {}).get("weight")
            price = spool.get("price") or spool.get("filament", {}).get("price")

            if initial_weight and price:
                try:
                    spool["cost_per_gram"] = float(price) / float(initial_weight)
                except (TypeError, ValueError, ZeroDivisionError):
                    spool["cost_per_gram"] = 0
            else:
                spool["cost_per_gram"] = 0
    settings = getSettings()
    printer = getPrinterModel()

    prints: list[dict] = []
    if include_prints:
        prints, _ = get_prints_with_filament(limit=None, offset=None)

    snapshot = {
        "spools": spools,
        "last_ams_config": last_ams_config,
        "settings": settings,
        "prints": prints,
        "printer": printer,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    log(f"✅ Wrote live snapshot to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export live spool/AMS data for reuse as test snapshots")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to write the snapshot JSON")
    parser.add_argument("--skip-prints", action="store_true", help="Exclude print history from the snapshot")
    args = parser.parse_args()

    if os.getenv("OPENSPOOLMAN_TEST_DATA") == "1":
        log("⚠️ OPENSPOOLMAN_TEST_DATA is set; run against a live instance to snapshot real data")
        return 1

    export_snapshot(args.output, include_prints=not args.skip_prints)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
