import json
import math
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from config import EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID, TRACK_LAYER_USAGE
from spoolman_client import consumeSpool
from spoolman_service import fetchSpools, getAMSFromTray, trayUid
from tools_3mf import download3mfFromCloud, download3mfFromFTP, download3mfFromLocalFilesystem
from print_history import update_filament_spool, update_filament_grams_used, get_all_filament_usage_for_print, update_layer_tracking
from logger import log


CHECKPOINT_DIR = Path(__file__).resolve().parent / "data" / "checkpoint"
LAYER_TRACKING_STATUS_RUNNING = "RUNNING"
LAYER_TRACKING_STATUS_COMPLETED = "COMPLETED"
LAYER_TRACKING_STATUS_ABORTED = "ABORTED"
LAYER_TRACKING_STATUS_FAILED = "FAILED"
ABORT_INDICATOR_STATES = {
    "STOP",
    "ABORT",
    "ABORTED",
    "CANCEL",
    "CANCELLED",
    "ERROR",
    "IDLE",
    "FAILED",
}


def _checkpoint_dir() -> Path:
  CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
  return CHECKPOINT_DIR


def _checkpoint_metadata_path() -> Path:
  return _checkpoint_dir() / "metadata.json"


def _get_checkpoint_metadata() -> dict:
  metadata_path = _checkpoint_metadata_path()
  if not metadata_path.exists():
    return {}
  try:
    return json.loads(metadata_path.read_text())
  except Exception:
    return {}


def _save_checkpoint_metadata(metadata: dict) -> None:
  _checkpoint_dir()
  _checkpoint_metadata_path().write_text(json.dumps(metadata))


def save_checkpoint(*, model_path: str, current_layer: int, task_id, subtask_id, ams_mapping, gcode_file_name: str) -> None:
  dest = _checkpoint_dir() / "model.3mf"
  dest.write_bytes(Path(model_path).read_bytes())

  existing = _get_checkpoint_metadata()
  existing["task_id"] = task_id
  existing["subtask_id"] = subtask_id
  existing["current_layer"] = current_layer
  existing["ams_mapping"] = ams_mapping
  existing["gcode_file_name"] = gcode_file_name
  _save_checkpoint_metadata(existing)


def clear_checkpoint() -> None:
  if CHECKPOINT_DIR.exists():
    for item in CHECKPOINT_DIR.iterdir():
      if item.is_file():
        item.unlink()
    try:
      CHECKPOINT_DIR.rmdir()
    except OSError:
      pass


def update_checkpoint_layer(layer: int) -> None:
  metadata = _get_checkpoint_metadata()
  metadata["current_layer"] = layer
  _save_checkpoint_metadata(metadata)


def recover_model(task_id, subtask_id):
  metadata = _get_checkpoint_metadata()

  checkpoint_task_id = metadata.get("task_id")
  checkpoint_subtask_id = metadata.get("subtask_id")

  if checkpoint_task_id is None or checkpoint_subtask_id is None:
    return None

  if checkpoint_task_id != task_id or checkpoint_subtask_id != subtask_id:
    return None

  model_path = _checkpoint_dir() / "model.3mf"
  if not model_path.exists():
    return None

  current_layer = metadata.get("current_layer")
  ams_mapping = metadata.get("ams_mapping")
  gcode_file_name = metadata.get("gcode_file_name")

  if current_layer is None or gcode_file_name is None:
    return None

  return str(model_path), gcode_file_name, current_layer, ams_mapping


class GCodeOperation:
  def __init__(self, raw_line: str):
    self.operation = None
    self.params = {}
    self.comment = None
    self._parse(raw_line)

  def _parse(self, raw_line: str) -> None:
    parts = list(map(lambda x: x.strip(), raw_line.split(";")))
    if len(parts) > 1:
      self.comment = parts[1].strip()

    parts = parts[0].split()
    self.operation = parts[0]
    for part in parts[1:]:
      key = part[0]
      value = part[1:]
      self.params[key] = value


def _parse_gcode(gcode: str) -> list[GCodeOperation]:
  operations = []
  for line in gcode.split("\n"):
    line = line.strip()
    if not line or line.startswith(";"):
      continue
    operations.append(GCodeOperation(line))
  return operations


def evaluate_gcode(gcode: str) -> dict:
  """
  Evaluate the gcode and return the filament usage (in mm) per layer.
  """
  operations = _parse_gcode(gcode)
  log(f"[filament-tracker] Parsed {len(operations)} gcode operations")

  current_layer = 0
  current_extrusion = {}
  active_filament = None
  layer_filaments = {}

  for operation in operations:
    if operation.operation == "M73":
      next_layer = operation.params.get("L")
      if next_layer is not None:
        log(f"[filament-tracker] Layer change: {current_layer} -> {next_layer}")
        if current_extrusion:
          layer_filaments[current_layer] = current_extrusion.copy()
          current_extrusion = {}
        current_layer = int(next_layer)

    if operation.operation == "M620":
      filament = operation.params.get("S")
      if filament is not None:
        if filament == "255":
          log("[filament-tracker] Full unload (S255)")
          active_filament = None
          continue
        log(f"[filament-tracker] Filament change: {active_filament} -> {filament[:-1]}")
        active_filament = int(filament[:-1])

    if operation.operation in ("G0", "G1", "G2", "G3"):
      extrusion = operation.params.get("E")
      if extrusion is None:
        continue
      if active_filament is None:
        continue
      extrusion_amount = float(extrusion)
      current_extruded = current_extrusion.get(active_filament, 0)
      current_extrusion[active_filament] = current_extruded + extrusion_amount

  if current_extrusion:
    layer_filaments[current_layer] = current_extrusion.copy()
  return layer_filaments


def extract_gcode_from_3mf(path: str, gcode_path: str | None) -> str | None:
  with zipfile.ZipFile(path, "r") as z:
    if gcode_path is None:
      config_path = "Metadata/model_settings.config"
      if config_path not in z.namelist():
        return None
      root = ET.parse(z.open(config_path)).getroot()
      plate = root[0]
      for item in plate:
        if item.attrib.get("key") == "gcode_file":
          gcode_path = item.attrib.get("value")
          break
      if gcode_path is None:
        return None
    if gcode_path not in z.namelist():
      return None
    with z.open(gcode_path) as g_file:
      return g_file.read().decode("utf-8")


class FilamentUsageTracker:
  def __init__(self):
    self.active_model = None
    self.ams_mapping = None
    self.spent_layers = set()
    self.using_ams = False
    self.gcode_state = None
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}  # Track cumulative grams used per filament index
    self.cumulative_length_used = {}  # Track cumulative length (mm) per filament index
    self._layer_tracking_total_layers = None
    self._total_usage_mm_per_filament = {}
    self._layer_tracking_predicted_total = None
    self._filament_spool_id_map = {}
    self._spool_data_cache = {}
    self._layer_tracking_status = None
    self._layer_tracking_start_time = None
    self._pending_usage_mm = {}
    self._mc_remaining_time_minutes = None

  def set_print_metadata(self, metadata: dict | None) -> None:
    metadata = metadata or {}
    incoming_id = metadata.get("print_id")
    if (
        self.print_id
        and incoming_id
        and incoming_id != self.print_id
        and self._layer_tracking_status == LAYER_TRACKING_STATUS_RUNNING
    ):
      self._set_layer_tracking_status(
          LAYER_TRACKING_STATUS_ABORTED, target_print_id=self.print_id
      )
    self.print_metadata = metadata
    self.print_id = incoming_id

  def on_message(self, message: dict) -> None:
    if "print" not in message:
      return

    print_obj = message.get("print", {})
    command = print_obj.get("command")
    if print_obj.get('gcode_state') is not None:
      log(f"[filament-tracker] on_message command={command} gcode_state={print_obj.get('gcode_state')}")

    previous_state = self.gcode_state
    self.gcode_state = print_obj.get("gcode_state", self.gcode_state)
    if "mc_remaining_time" in print_obj:
      try:
        self._mc_remaining_time_minutes = float(print_obj["mc_remaining_time"])
      except (TypeError, ValueError):
        self._mc_remaining_time_minutes = None

    if command == "project_file":
      self._handle_print_start(print_obj)

    if command == "push_status":
      if "layer_num" in print_obj:
        last_layer = self.current_layer
        layer = print_obj["layer_num"]
        if layer != last_layer:
          self._handle_layer_change(layer)
          self.current_layer = layer

    if self.gcode_state == "FINISH" and previous_state != "FINISH" and self.active_model is not None:
      self._handle_print_end()

    elif (
        previous_state == "RUNNING"
        and self._is_abort_state(self.gcode_state)
        and self.active_model is not None
    ):
        status = (
            LAYER_TRACKING_STATUS_FAILED
            if self.gcode_state == "FAILED"
            else LAYER_TRACKING_STATUS_ABORTED
        )
        self._handle_print_abort(status=status)

    if self.gcode_state == "RUNNING" and previous_state != "RUNNING" and self.active_model is None:
      task_id = print_obj.get("task_id")
      subtask_id = print_obj.get("subtask_id")
      self._attempt_print_resume(task_id, subtask_id)

  def _handle_print_start(self, print_obj: dict) -> None:
    log("[filament-tracker] Print start")

    model_url = print_obj.get("url")
    model_path = self._retrieve_model(model_url)

    if model_path is None:
      log("Failed to retrieve model. Print will not be tracked.")
      return

    use_ams = bool(print_obj.get("use_ams", False))
    ams_mapping = print_obj.get("ams_mapping", []) if use_ams else None
    gcode_file_name = print_obj.get("param")
    self._start_layer_tracking_for_model(
      model_path=model_path,
      gcode_file_name=gcode_file_name,
      use_ams=use_ams,
      ams_mapping=ams_mapping,
      task_id=print_obj.get("task_id"),
      subtask_id=print_obj.get("subtask_id"),
    )

  def _start_layer_tracking_for_model(
      self,
      model_path: str,
      gcode_file_name: str | None,
      use_ams: bool,
      ams_mapping: list[int] | None,
      task_id,
      subtask_id,
  ) -> None:
    self._reset_layer_tracking_state()
    clear_checkpoint()
    self.spent_layers = set()
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}

    if use_ams:
      self.ams_mapping = ams_mapping or []
      self.using_ams = True
      log(f"[filament-tracker] Using AMS mapping: {self.ams_mapping}")
    else:
      self.using_ams = False
      self.ams_mapping = None
      log("[filament-tracker] Not using AMS, defaulting to external spool")

    self._load_model(model_path, gcode_file_name)

    if self.active_model:
      self._layer_tracking_total_layers = self._infer_total_layers()
      self._accumulate_total_usage_mm()
      self._layer_tracking_start_time = datetime.now()
      self._bind_initial_spools()
      self._maybe_update_predicted_total()
      if self.print_id:
        initial_fields = {"status": LAYER_TRACKING_STATUS_RUNNING}
        if self._layer_tracking_total_layers is not None:
          initial_fields["total_layers"] = self._layer_tracking_total_layers
        update_layer_tracking(self.print_id, **initial_fields)
        self._layer_tracking_status = LAYER_TRACKING_STATUS_RUNNING
        self._update_layer_tracking_progress()

    save_checkpoint(
      model_path=model_path,
      current_layer=0,
      task_id=task_id,
      subtask_id=subtask_id,
      ams_mapping=self.ams_mapping,
      gcode_file_name=gcode_file_name,
    )

    try:
      os.remove(model_path)
    except OSError:
      pass

    self._handle_layer_change(0)

  def start_local_print_from_metadata(self, metadata: dict | None) -> None:
    if not metadata:
      return
    model_path = metadata.get("model_path", "").replace("local:", "")
    model_url = metadata.get("model_url")

    if model_path and not model_url:
      model_url = model_path

    if not model_path and not model_url:
      log("[filament-tracker] Metadata missing model_path or URL, cannot start local tracking")
      return

    log("[filament-tracker] Starting local print from cached metadata")
    self.set_print_metadata(metadata)

    ams_mapping = metadata.get("ams_mapping") or []
    fake_print = {
      "param": metadata.get("gcode_path"),
      "use_ams": bool(ams_mapping),
      "ams_mapping": ams_mapping,
      "task_id": metadata.get("task_id"),
      "subtask_id": metadata.get("subtask_id"),
    }
    
    fake_print["url"] = model_url

    self._handle_print_start(fake_print)

  def apply_ams_mapping(self, ams_mapping: list[int] | None) -> None:
    if not ams_mapping:
      return
    if self.ams_mapping == ams_mapping and self.using_ams:
      return

    log(f"[filament-tracker] Applying AMS mapping: {ams_mapping}")
    self.ams_mapping = ams_mapping
    self.using_ams = True
    if self.print_metadata is not None:
      self.print_metadata["ams_mapping"] = ams_mapping

    self._bind_initial_spools()
    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()

  def _retrieve_model(self, model_url: str | None) -> str | None:
    if not model_url:
      log("[filament-tracker] No model URL provided")
      return None

    uri = urlparse(model_url)
    try:
      with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as model_file:
        if uri.scheme in ("https", "http"):
          log(f"[filament-tracker] Downloading model via HTTP(S): {model_url}")
          download3mfFromCloud(model_url, model_file)
        elif uri.scheme == "local":
          log(f"[filament-tracker] Loading model from local path: {uri.path}")
          download3mfFromLocalFilesystem(uri.path, model_file)
        else:
          log(f"[filament-tracker] Downloading model via FTP: {model_url}")
          download3mfFromFTP(model_url.replace("ftp://", "").replace(".gcode", ""), model_file)
        return model_file.name
    except Exception as exc:
      log(f"Failed to fetch model: {exc}")
      return None

  def _handle_layer_change(self, layer: int) -> None:
    if self.active_model is None:
      return
    if layer in self.spent_layers:
      return

    log(f"[filament-tracker] Handle layer change -> {layer}")
    self.spent_layers.add(layer)
    last_layer = self.current_layer

    if last_layer is not None:
      for i in range(last_layer + 1, layer + 1):
        self._spend_filament_for_layer(i)
    else:
      self._spend_filament_for_layer(layer)

    update_checkpoint_layer(layer)

  def _handle_print_end(self) -> None:
    if self.active_model is None:
      return

    log("[filament-tracker] Print end, spending remaining layers")
    for layer in set(self.active_model.keys()) - self.spent_layers:
      self._handle_layer_change(layer)

    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()
    if self.print_id:
      self._set_layer_tracking_status(
          LAYER_TRACKING_STATUS_COMPLETED,
          extra_fields={"actual_end_time": self._format_timestamp(datetime.now())},
      )

    self.active_model = None
    self.ams_mapping = None
    self.using_ams = False
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    self._reset_layer_tracking_state()
    clear_checkpoint()

  def _handle_print_abort(self, status: str = LAYER_TRACKING_STATUS_ABORTED) -> None:
    if self.active_model is None:
      return

    log("[filament-tracker] Print aborted, stopping tracking")
    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()
    if self.print_id:
      self._set_layer_tracking_status(
          status,
          extra_fields={"actual_end_time": self._format_timestamp(datetime.now())},
      )

    self.active_model = None
    self.ams_mapping = None
    self.using_ams = False
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    self._reset_layer_tracking_state()
    clear_checkpoint()

  def _mm_to_grams(self, length_mm: float, diameter_mm: float, density_g_per_cm3: float) -> float:
    """
    Convert filament length in mm to grams.
    Formula: grams = (π * (diameter/2)^2 * length_mm / 1000) * density
    """
    radius_cm = (diameter_mm / 2) / 10  # Convert mm to cm
    volume_cm3 = math.pi * radius_cm * radius_cm * (length_mm / 10)
    grams = volume_cm3 * density_g_per_cm3
    return grams

  def _spend_filament_for_layer(self, layer: int) -> None:
    if self.active_model is None:
      return

    log(f"[filament-tracker] Spending filament for layer {layer}")
    if not TRACK_LAYER_USAGE:
      log("[filament-tracker] Layer usage tracking disabled, skipping filament spend")
      self._update_layer_tracking_progress()
      return
    layer_usage = self.active_model.get(int(layer))
    if layer_usage is None:
      return

    for filament, usage_mm in layer_usage.items():
      self._apply_usage_for_filament(filament, usage_mm)

    self._flush_all_pending_usage()
    self._maybe_update_predicted_total()
    self._update_layer_tracking_progress()

  def _apply_usage_for_filament(self, filament: int, usage_mm: float) -> bool:
    if not TRACK_LAYER_USAGE:
      return False
    pending_mm = self._pending_usage_mm.pop(filament, 0.0)
    total_mm = (pending_mm or 0.0) + (usage_mm or 0.0)
    if total_mm <= 0:
      return False

    mapping_value = self._resolve_tray_mapping(filament)
    if mapping_value is None:
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    tray_uid = self._tray_uid_from_mapping(mapping_value)
    if tray_uid is None:
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    spool_id = self._lookup_spool_for_tray(tray_uid)
    if spool_id is None:
      log(f"[filament-tracker] Queued {round(total_mm, 5)}mm for filament {filament} (tray {tray_uid} has no assigned spool)")
      self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
      return False

    spool_data = self._spool_data_cache.get(spool_id)
    if spool_data is None:
      spool_data = self._get_spool_data(spool_id)
      if spool_data is None:
        log(f"[filament-tracker] Could not get spool data for {spool_id}, re-queueing usage")
        self._pending_usage_mm[filament] = self._pending_usage_mm.get(filament, 0.0) + total_mm
        return False
      self._spool_data_cache[spool_id] = spool_data

    filament_data = spool_data.get("filament", {})
    diameter_mm = filament_data.get("diameter", 1.75)
    density = filament_data.get("density", 1.24)
    usage_grams = self._mm_to_grams(total_mm, diameter_mm, density)

    usage_rounded = round(total_mm, 5)
    filament_key = filament + 1
    previous_grams = self.cumulative_grams_used.get(filament_key, 0.0)
    self.cumulative_grams_used[filament_key] = previous_grams + usage_grams
    previous_length = self.cumulative_length_used.get(filament_key, 0.0)
    cumulative_length = previous_length + usage_rounded
    self.cumulative_length_used[filament_key] = cumulative_length

    grams_rounded = round(self.cumulative_grams_used[filament_key], 2)
    log(f"[filament-tracker] Consume spool {spool_id} for filament {filament} with {usage_rounded}mm ({grams_rounded}g cumulative) (tray_uid={tray_uid})")

    consumeSpool(spool_id, use_length=usage_rounded)

    if self.print_id:
      update_filament_spool(self.print_id, filament_key, spool_id)
      update_filament_grams_used(self.print_id, filament_key, grams_rounded, length_used=cumulative_length)

    self._filament_spool_id_map[filament] = spool_id
    self._spool_data_cache[spool_id] = spool_data
    return True

  def _flush_all_pending_usage(self) -> None:
    if not self._pending_usage_mm:
      return
    for filament in list(self._pending_usage_mm.keys()):
      self._apply_usage_for_filament(filament, 0.0)

  def _reset_layer_tracking_state(self) -> None:
    self._layer_tracking_total_layers = None
    self._total_usage_mm_per_filament = {}
    self._layer_tracking_predicted_total = None
    self._filament_spool_id_map = {}
    self._spool_data_cache = {}
    self._layer_tracking_status = None
    self._layer_tracking_start_time = None
    self._pending_usage_mm = {}
    self._mc_remaining_time_minutes = None
    self.cumulative_length_used = {}
    self.cumulative_grams_used = {}

  def _is_abort_state(self, state: str | None) -> bool:
    if not state:
      return False
    return state.upper() in ABORT_INDICATOR_STATES

  def _infer_total_layers(self) -> int | None:
    if not self.active_model:
      return None
    try:
      return max(self.active_model.keys()) + 1
    except Exception:
      return None

  def _accumulate_total_usage_mm(self) -> None:
    self._total_usage_mm_per_filament = {}
    for layer_usage in self.active_model.values():
      for filament, usage_mm in layer_usage.items():
        self._total_usage_mm_per_filament[filament] = (
            self._total_usage_mm_per_filament.get(filament, 0.0) + usage_mm
        )

  def _format_timestamp(self, moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")

  def _compute_predicted_end_time(self, layers_printed: int, total_layers: int | None) -> str | None:
    if (
        not self._layer_tracking_start_time
        or layers_printed <= 0
        or not total_layers
        or total_layers <= layers_printed
    ):
      return None

    now = datetime.now()
    elapsed_seconds = (now - self._layer_tracking_start_time).total_seconds()
    if layers_printed == 0:
      return None

    rate_per_layer = elapsed_seconds / layers_printed
    remaining_layers = total_layers - layers_printed
    remaining_seconds = max(rate_per_layer * remaining_layers, 0)
    predicted = now + timedelta(seconds=remaining_seconds)
    return self._format_timestamp(predicted)

  def _get_spool_id_for_filament(self, filament_index: int) -> int | None:
    mapping_value = self._resolve_tray_mapping(filament_index)
    if mapping_value is None:
      return None
    tray_uid = self._tray_uid_from_mapping(mapping_value)
    if tray_uid is None:
      return None
    return self._lookup_spool_for_tray(tray_uid)

  def _maybe_update_predicted_total(self) -> None:
    if self._layer_tracking_predicted_total is not None:
      return
    if not self._total_usage_mm_per_filament:
      return

    total_grams = 0.0

    for filament, total_mm in self._total_usage_mm_per_filament.items():
      spool_id = self._filament_spool_id_map.get(filament)
      if spool_id is None:
        spool_id = self._get_spool_id_for_filament(filament)
        if spool_id is None:
          return
        self._filament_spool_id_map[filament] = spool_id

      spool_data = self._spool_data_cache.get(spool_id)
      if spool_data is None:
        spool_data = self._get_spool_data(spool_id)
        if spool_data is None:
          return
        self._spool_data_cache[spool_id] = spool_data

      filament_info = spool_data.get("filament", {})
      diameter_mm = filament_info.get("diameter", 1.75)
      density = filament_info.get("density", 1.24)
      total_grams += self._mm_to_grams(total_mm, diameter_mm, density)

    self._layer_tracking_predicted_total = total_grams
    if self.print_id:
      update_layer_tracking(self.print_id, filament_grams_total=round(total_grams, 2))

  def _bind_initial_spools(self) -> None:
    if not self.print_id:
      return

    for filament_index in self._total_usage_mm_per_filament:
      spool_id = self._get_spool_id_for_filament(filament_index)
      if spool_id is None:
        continue

      update_filament_spool(self.print_id, filament_index + 1, spool_id)
      self._filament_spool_id_map[filament_index] = spool_id

      if spool_id not in self._spool_data_cache:
        spool_data = self._get_spool_data(spool_id)
        if spool_data is not None:
          self._spool_data_cache[spool_id] = spool_data

  def _update_layer_tracking_progress(self) -> None:
    if not self.print_id:
      return

    layers_printed = len(self.spent_layers)
    grams_used = sum(self.cumulative_grams_used.values())

    payload = {
        "layers_printed": layers_printed,
        "filament_grams_billed": round(grams_used, 2),
    }
    if self._layer_tracking_total_layers is not None:
      payload["total_layers"] = self._layer_tracking_total_layers
    if self._layer_tracking_predicted_total is not None:
      payload["filament_grams_total"] = round(self._layer_tracking_predicted_total, 2)
    predicted_end = None
    if self._mc_remaining_time_minutes is not None and self._mc_remaining_time_minutes > 0:
      predicted_end = self._format_timestamp(
          datetime.now() + timedelta(minutes=self._mc_remaining_time_minutes)
      )
    else:
      predicted_end = self._compute_predicted_end_time(layers_printed, self._layer_tracking_total_layers)
    if predicted_end:
      payload["predicted_end_time"] = predicted_end

    update_layer_tracking(self.print_id, **payload)

  def _set_layer_tracking_status(
      self,
      status: str,
      target_print_id: int | None = None,
      extra_fields: dict | None = None,
  ) -> None:
    print_id = target_print_id or self.print_id
    if not print_id:
      return
    if target_print_id is None and self._layer_tracking_status == status:
      return
    payload = {"status": status}
    if extra_fields:
      payload.update(extra_fields)
    update_layer_tracking(print_id, **payload)
    if target_print_id is None:
      self._layer_tracking_status = status

  def _tray_uid_from_mapping(self, mapping_value: int) -> str | None:
    if mapping_value == EXTERNAL_SPOOL_ID:
      return trayUid(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)

    ams_id = getAMSFromTray(mapping_value)
    tray_id = mapping_value - ams_id * 4
    return trayUid(ams_id, tray_id)

  def _resolve_tray_mapping(self, filament_index: int) -> int | None:
    if self.using_ams:
      if self.ams_mapping is None or filament_index >= len(self.ams_mapping):
        log(f"No AMS mapping for filament {filament_index}")
        return None
      return self.ams_mapping[filament_index]
    return EXTERNAL_SPOOL_ID

  def _lookup_spool_for_tray(self, tray_uid: str):
    for spool in fetchSpools():
      extra = spool.get("extra") or {}
      active = extra.get("active_tray")
      if not active:
        continue
      try:
        active_value = json.loads(active)
      except Exception:
        active_value = active
      if active_value == tray_uid:
        return spool.get("id")
    return None

  def _get_spool_data(self, spool_id: int):
    """Get full spool data including filament information."""
    for spool in fetchSpools():
      if spool.get("id") == spool_id:
        return spool
    return None

  def _load_model(self, model_path: str, gcode_file: str | None) -> None:
    gcode = extract_gcode_from_3mf(model_path, gcode_file)
    if gcode is None:
      log("Failed to extract gcode from model")
      return
    self.active_model = evaluate_gcode(gcode)

  def _attempt_print_resume(self, task_id, subtask_id) -> None:
    result = recover_model(task_id, subtask_id)
    if result is None:
      log("[filament-tracker] No checkpoint to recover")
      return
    log(f"[filament-tracker] Recovering from checkpoint task={task_id} subtask={subtask_id}")
    model_path, gcode_file_name, current_layer, ams_mapping = result
    self._load_model(model_path, gcode_file_name)
    self.spent_layers = set(range(current_layer + 1))
    self.ams_mapping = ams_mapping
    self.current_layer = current_layer
    self.using_ams = ams_mapping is not None
    
    # Initialize cumulative usage from database to continue tracking correctly
    self.cumulative_grams_used = {}
    self.cumulative_length_used = {}
    if self.print_id:
      existing_usage = get_all_filament_usage_for_print(self.print_id)
      for ams_slot, usage in existing_usage.items():
        grams_value = usage.get("grams_used") if isinstance(usage, dict) else usage
        length_value = usage.get("length_used") if isinstance(usage, dict) else None
        if grams_value is not None:
          self.cumulative_grams_used[ams_slot] = grams_value
        if length_value is not None:
          self.cumulative_length_used[ams_slot] = length_value
        log(f"[filament-tracker] Resumed cumulative usage for filament {ams_slot}: {grams_value}g, {length_value or 0}mm")
