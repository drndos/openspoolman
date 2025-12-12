import json
import math
import os
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from config import EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID
from spoolman_client import consumeSpool
from spoolman_service import fetchSpools, getAMSFromTray, trayUid
from tools_3mf import download3mfFromCloud, download3mfFromFTP, download3mfFromLocalFilesystem
from print_history import update_filament_spool, update_filament_grams_used, get_all_filament_usage_for_print


CHECKPOINT_DIR = Path(__file__).resolve().parent / "data" / "checkpoint"


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
  print(f"[filament-tracker] Parsed {len(operations)} gcode operations")

  current_layer = 0
  current_extrusion = {}
  active_filament = None
  layer_filaments = {}

  for operation in operations:
    if operation.operation == "M73":
      next_layer = operation.params.get("L")
      if next_layer is not None:
        print(f"[filament-tracker] Layer change: {current_layer} -> {next_layer}")
        if current_extrusion:
          layer_filaments[current_layer] = current_extrusion.copy()
          current_extrusion = {}
        current_layer = int(next_layer)

    if operation.operation == "M620":
      filament = operation.params.get("S")
      if filament is not None:
        if filament == "255":
          print("[filament-tracker] Full unload (S255)")
          active_filament = None
          continue
        print(f"[filament-tracker] Filament change: {active_filament} -> {filament[:-1]}")
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

  def set_print_metadata(self, metadata: dict | None) -> None:
    self.print_metadata = metadata or {}
    self.print_id = self.print_metadata.get("print_id")

  def on_message(self, message: dict) -> None:
    if "print" not in message:
      return

    print_obj = message.get("print", {})
    command = print_obj.get("command")
    print(f"[filament-tracker] on_message command={command} gcode_state={print_obj.get('gcode_state')}")

    previous_state = self.gcode_state
    self.gcode_state = print_obj.get("gcode_state", self.gcode_state)

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

      if self.gcode_state == "RUNNING" and previous_state != "RUNNING" and self.active_model is None:
        task_id = print_obj.get("task_id")
        subtask_id = print_obj.get("subtask_id")
        self._attempt_print_resume(task_id, subtask_id)

  def _handle_print_start(self, print_obj: dict) -> None:
    print("[filament-tracker] Print start")
    clear_checkpoint()
    model_url = print_obj.get("url")
    self.spent_layers = set()
    self.cumulative_grams_used = {}  # Reset cumulative usage tracking

    model_path = self._retrieve_model(model_url)
    if model_path is None:
      print("Failed to retrieve model. Print will not be tracked.")
      return

    if print_obj.get("use_ams", False):
      self.ams_mapping = print_obj.get("ams_mapping", [])
      self.using_ams = True
      print(f"[filament-tracker] Using AMS mapping: {self.ams_mapping}")
    else:
      self.using_ams = False
      self.ams_mapping = None
      print("[filament-tracker] Not using AMS, defaulting to external spool")

    gcode_file_name = print_obj.get("param")
    self._load_model(model_path, gcode_file_name)

    save_checkpoint(
      model_path=model_path,
      current_layer=0,
      task_id=print_obj.get("task_id"),
      subtask_id=print_obj.get("subtask_id"),
      ams_mapping=self.ams_mapping,
      gcode_file_name=gcode_file_name,
    )

    os.remove(model_path)
    self._handle_layer_change(0)

  def _retrieve_model(self, model_url: str | None) -> str | None:
    if not model_url:
      print("[filament-tracker] No model URL provided")
      return None

    uri = urlparse(model_url)
    try:
      with tempfile.NamedTemporaryFile(suffix=".3mf", delete=False) as model_file:
        if uri.scheme in ("https", "http"):
          print(f"[filament-tracker] Downloading model via HTTP(S): {model_url}")
          download3mfFromCloud(model_url, model_file)
        elif uri.scheme == "local":
          print(f"[filament-tracker] Loading model from local path: {uri.path}")
          download3mfFromLocalFilesystem(uri.path, model_file)
        else:
          print(f"[filament-tracker] Downloading model via FTP: {model_url}")
          download3mfFromFTP(model_url.replace("ftp://", "").replace(".gcode", ""), model_file)
        return model_file.name
    except Exception as exc:
      print(f"Failed to fetch model: {exc}")
      return None

  def _handle_layer_change(self, layer: int) -> None:
    if self.active_model is None:
      return
    if layer in self.spent_layers:
      return

    print(f"[filament-tracker] Handle layer change -> {layer}")
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

    print("[filament-tracker] Print end, spending remaining layers")
    for layer in set(self.active_model.keys()) - self.spent_layers:
      self._handle_layer_change(layer)

    self.active_model = None
    self.ams_mapping = None
    self.using_ams = False
    self.current_layer = None
    self.print_metadata = None
    self.print_id = None
    self.cumulative_grams_used = {}
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

    print(f"[filament-tracker] Spending filament for layer {layer}")
    layer_usage = self.active_model.get(int(layer))
    if layer_usage is None:
      return

    for filament, usage_mm in layer_usage.items():
      mapping_value = self._resolve_tray_mapping(filament)
      if mapping_value is None:
        continue

      tray_uid = self._tray_uid_from_mapping(mapping_value)
      if tray_uid is None:
        continue

      spool_id = self._lookup_spool_for_tray(tray_uid)
      if spool_id is None:
        print(f"Skipping filament {filament}: no spool mapped for {tray_uid}")
        continue

      # Get spool data to access filament density and diameter
      spool_data = self._get_spool_data(spool_id)
      if spool_data is None:
        print(f"[filament-tracker] Could not get spool data for {spool_id}, skipping grams conversion")
        continue

      filament_data = spool_data.get("filament", {})
      diameter_mm = filament_data.get("diameter", 1.75)
      density_g_per_cm3 = filament_data.get("density", 1.24)

      # Convert mm to grams
      usage_grams = self._mm_to_grams(usage_mm, diameter_mm, density_g_per_cm3)
      
      # Track cumulative usage per filament
      filament_key = filament + 1  # Convert to 1-indexed for database
      if filament_key not in self.cumulative_grams_used:
        self.cumulative_grams_used[filament_key] = 0.0
      self.cumulative_grams_used[filament_key] += usage_grams

      usage_rounded = round(usage_mm, 5)
      grams_rounded = round(self.cumulative_grams_used[filament_key], 2)
      print(f"[filament-tracker] Consume spool {spool_id} for filament {filament} with {usage_rounded}mm ({grams_rounded}g cumulative) (tray_uid={tray_uid})")
      
      consumeSpool(spool_id, use_length=usage_rounded)
      
      if self.print_id:
        update_filament_spool(self.print_id, filament_key, spool_id)
        update_filament_grams_used(self.print_id, filament_key, grams_rounded)

  def _tray_uid_from_mapping(self, mapping_value: int) -> str | None:
    if mapping_value == EXTERNAL_SPOOL_ID:
      return trayUid(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)

    ams_id = getAMSFromTray(mapping_value)
    tray_id = mapping_value - ams_id * 4
    return trayUid(ams_id, tray_id)

  def _resolve_tray_mapping(self, filament_index: int) -> int | None:
    if self.using_ams:
      if self.ams_mapping is None or filament_index >= len(self.ams_mapping):
        print(f"No AMS mapping for filament {filament_index}")
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
      print("Failed to extract gcode from model")
      return
    self.active_model = evaluate_gcode(gcode)

  def _attempt_print_resume(self, task_id, subtask_id) -> None:
    result = recover_model(task_id, subtask_id)
    if result is None:
      print("[filament-tracker] No checkpoint to recover")
      return
    print(f"[filament-tracker] Recovering from checkpoint task={task_id} subtask={subtask_id}")
    model_path, gcode_file_name, current_layer, ams_mapping = result
    self._load_model(model_path, gcode_file_name)
    self.spent_layers = set(range(current_layer + 1))
    self.ams_mapping = ams_mapping
    self.current_layer = current_layer
    self.using_ams = ams_mapping is not None
    
    # Initialize cumulative usage from database to continue tracking correctly
    self.cumulative_grams_used = {}
    if self.print_id:
      existing_usage = get_all_filament_usage_for_print(self.print_id)
      for ams_slot, grams_used in existing_usage.items():
        self.cumulative_grams_used[ams_slot] = grams_used
        print(f"[filament-tracker] Resumed cumulative usage for filament {ams_slot}: {grams_used}g")

