

import json
import ssl
import traceback
from threading import Thread
from typing import Any, Iterable

import paho.mqtt.client as mqtt

from config import (
    PRINTER_ID,
    PRINTER_CODE,
    PRINTER_IP,
    AUTO_SPEND,
    EXTERNAL_SPOOL_ID,
    TRACK_LAYER_USAGE,
    CLEAR_ASSIGNMENT_WHEN_EMPTY,
)
from messages import GET_VERSION, PUSH_ALL, AMS_FILAMENT_SETTING
from spoolman_service import spendFilaments, setActiveTray, fetchSpools, clear_active_spool_for_tray
from tools_3mf import getMetaDataFrom3mf
import time
import copy
from collections.abc import Mapping
from logger import append_to_rotating_file, log
from print_history import insert_print, insert_filament_usage
from filament_usage_tracker import FilamentUsageTracker
MQTT_CLIENT = {}  # Global variable storing MQTT Client
MQTT_CLIENT_CONNECTED = False
MQTT_KEEPALIVE = 60
LAST_AMS_CONFIG = {}  # Global variable storing last AMS configuration

PRINTER_STATE = {}
PRINTER_STATE_LAST = {}

PENDING_PRINT_METADATA = {}
FILAMENT_TRACKER = FilamentUsageTracker()
LOG_FILE = "/home/app/logs/mqtt.log"

def getPrinterModel():
    global PRINTER_ID
    model_code = PRINTER_ID[:3]

    model_map = {
      # H2-Serie
      "093": "H2S",
      "094": "H2D",
      "239": "H2D Pro",
      "109": "H2C",

      # X1-Serie
      "00W": "X1",
      "00M": "X1 Carbon",
      "03W": "X1E",

      # P1-Serie
      "01S": "P1P",
      "01P": "P1S",

      # P2-Serie
      "22E": "P2S",

      # A1-Serie
      "039": "A1",
      "030": "A1 Mini"
    }

    model_name = model_map.get(model_code, f"Unknown model ({model_code})")

    numeric_tail = ''.join(filter(str.isdigit, PRINTER_ID))
    device_id = numeric_tail[-3:] if len(numeric_tail) >= 3 else numeric_tail

    device_name = f"3DP-{model_code}-{device_id}"

    return {
        "model": model_name,
        "devicename": device_name
    }

def identify_ams_model_from_module(module: dict[str, Any]) -> str | None:
    """Guess the AMS variant that a version module represents."""

    product_name = (module.get("product_name") or "").strip().lower()
    module_name = (module.get("name") or "").strip().lower()

    if "ams lite" in product_name or module_name.startswith("ams_f1"):
        return "AMS Lite"
    if "ams 2 pro" in product_name or module_name.startswith("n3f"):
        return "AMS 2 Pro"
    if "ams ht" in product_name or module_name.startswith("ams_ht"):
        return "AMS HT"
    if module_name == "ams" or module_name.startswith("ams/"):
        return "AMS"

    return None


def identify_ams_models_from_modules(modules: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  """Return per-module metadata, including the detected model when available."""

  results: dict[str, dict[str, Any]] = {}
  for module in modules or []:
    name = module.get("name")
    if not name:
      continue

    results[name] = {
      "model": identify_ams_model_from_module(module),
      "product_name": module.get("product_name"),
      "serial": module.get("sn"),
      "hw_ver": module.get("hw_ver"),
    }

  return results


def extract_ams_id_from_module_name(name: str) -> int | None:
  parts = name.split("/")
  if len(parts) != 2:
    return None
  try:
    return int(parts[1])
  except ValueError:
    return None


def identify_ams_models_by_id(modules: Iterable[dict[str, Any]]) -> dict[str, str]:
  """Return the detected AMS model per numeric AMS ID (module suffix)."""

  results: dict[str, str] = {}
  for module in modules or []:
    name = module.get("name")
    if not name:
      continue

    ams_id = extract_ams_id_from_module_name(name)
    if ams_id is None:
      continue

    model = identify_ams_model_from_module(module)
    if model:
      results[str(ams_id)] = model
      results[ams_id] = model

  return results


def num2letter(num):
  return chr(ord("A") + int(num))
  
def update_dict(original: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, Mapping) and key in original and isinstance(original[key], Mapping):
            original[key] = update_dict(original[key], value)
        else:
            original[key] = value
    return original


def _parse_grams(value):
  try:
    return float(value)
  except (TypeError, ValueError):
    return None

def map_filament(tray_tar):
  global PENDING_PRINT_METADATA
  # Prüfen, ob ein Filamentwechsel aktiv ist (stg_cur == 4)
  #if stg_cur == 4 and tray_tar is not None:
  if PENDING_PRINT_METADATA:
    PENDING_PRINT_METADATA["filamentChanges"].append(tray_tar)  # Jeder Wechsel zählt, auch auf das gleiche Tray
    log(f'Filamentchange {len(PENDING_PRINT_METADATA["filamentChanges"])}: Tray {tray_tar}')

    # Anzahl der erkannten Wechsel
    change_count = len(PENDING_PRINT_METADATA["filamentChanges"]) - 1  # -1, weil der erste Eintrag kein Wechsel ist

    filament_order = PENDING_PRINT_METADATA.get("filamentOrder") or {}
    ordered_filaments = sorted(filament_order.items(), key=lambda entry: entry[1])
    assigned_trays = PENDING_PRINT_METADATA.setdefault("assigned_trays", [])
    filament_assigned = None
    if tray_tar not in assigned_trays:
      assigned_trays.append(tray_tar)
      unique_index = len(assigned_trays) - 1
      if unique_index < len(ordered_filaments):
        filament_assigned = ordered_filaments[unique_index][0]
      else:
        for filamentId, usage_count in filament_order.items():
          if usage_count == change_count:
            filament_assigned = filamentId
            break

    if filament_assigned is not None:
      mapping = PENDING_PRINT_METADATA.setdefault("ams_mapping", [])
      filament_idx = int(filament_assigned)
      while len(mapping) <= filament_idx:
        mapping.append(None)
      mapping[filament_idx] = tray_tar
      log(f"✅ Tray {tray_tar} assigned to Filament {filament_assigned}")

      for filament, tray in enumerate(mapping):
        if tray is None:
          continue
        log(f"  Filament pos: {filament} → Tray {tray}")

    target_filaments = set(filament_order.keys())
    if target_filaments:
      assigned_filaments = {
        idx for idx, tray in enumerate(PENDING_PRINT_METADATA.get("ams_mapping", []))
        if tray is not None
      }
      if target_filaments.issubset(assigned_filaments):
        log("\n✅ All trays assigned:")
        return True
  
  return False
  
def processMessage(data):
  global LAST_AMS_CONFIG, PRINTER_STATE, PRINTER_STATE_LAST, PENDING_PRINT_METADATA

   # Prepare AMS spending estimation
  if "print" in data:    
    update_dict(PRINTER_STATE, data)
    
    if data["print"].get("command") == "project_file" and data["print"].get("url"):
      PENDING_PRINT_METADATA = getMetaDataFrom3mf(data["print"]["url"])
      PENDING_PRINT_METADATA["print_type"] = PRINTER_STATE["print"].get("print_type")
      PENDING_PRINT_METADATA["task_id"] = PRINTER_STATE["print"].get("task_id")
      PENDING_PRINT_METADATA["subtask_id"] = PRINTER_STATE["print"].get("subtask_id")
      if TRACK_LAYER_USAGE:
        FILAMENT_TRACKER.set_print_metadata(PENDING_PRINT_METADATA)

      print_id = insert_print(PRINTER_STATE["print"]["subtask_name"], "cloud", PENDING_PRINT_METADATA["image"])

      if PRINTER_STATE["print"].get("use_ams"):
        PENDING_PRINT_METADATA["ams_mapping"] = PRINTER_STATE["print"]["ams_mapping"]
      else:
        PENDING_PRINT_METADATA["ams_mapping"] = [EXTERNAL_SPOOL_ID]

      PENDING_PRINT_METADATA["print_id"] = print_id
      PENDING_PRINT_METADATA["complete"] = True

      for id, filament in PENDING_PRINT_METADATA["filaments"].items():
        parsed_grams = _parse_grams(filament.get("used_g"))
        parsed_length_m = _parse_grams(filament.get("used_m"))
        estimated_length_mm = parsed_length_m * 1000 if parsed_length_m is not None else None
        grams_used = parsed_grams if parsed_grams is not None else 0.0
        length_used = estimated_length_mm if estimated_length_mm is not None else 0.0
        if TRACK_LAYER_USAGE:
          grams_used = 0.0
          length_used = 0.0
        insert_filament_usage(
            print_id,
            filament["type"],
            filament["color"],
            grams_used,
            id,
            estimated_grams=parsed_grams,
            length_used=length_used,
            estimated_length=estimated_length_mm,
        )
  
    #if ("gcode_state" in data["print"] and data["print"]["gcode_state"] == "RUNNING") and ("print_type" in data["print"] and data["print"]["print_type"] != "local") \
    #  and ("tray_tar" in data["print"] and data["print"]["tray_tar"] != "255") and ("stg_cur" in data["print"] and data["print"]["stg_cur"] == 0 and PRINT_CURRENT_STAGE != 0):
    
    #TODO: What happens when printed from external spool, is ams and tray_tar set?
    if PRINTER_STATE.get("print", {}).get("print_type") == "local" and PRINTER_STATE_LAST.get("print"):

      if (
          PRINTER_STATE["print"].get("gcode_state") == "RUNNING" and
          PRINTER_STATE_LAST["print"].get("gcode_state") == "PREPARE" and 
          PRINTER_STATE["print"].get("gcode_file")
        ):

        if not PENDING_PRINT_METADATA:
          PENDING_PRINT_METADATA = getMetaDataFrom3mf(PRINTER_STATE["print"]["gcode_file"])
        if PENDING_PRINT_METADATA:
          PENDING_PRINT_METADATA["print_type"] = PRINTER_STATE["print"].get("print_type")
          PENDING_PRINT_METADATA["task_id"] = PRINTER_STATE["print"].get("task_id")
          PENDING_PRINT_METADATA["subtask_id"] = PRINTER_STATE["print"].get("subtask_id")

          if not PENDING_PRINT_METADATA.get("tracking_started"):
            print_id = insert_print(PENDING_PRINT_METADATA["file"], PRINTER_STATE["print"]["print_type"], PENDING_PRINT_METADATA["image"])

            PENDING_PRINT_METADATA["ams_mapping"] = []
            PENDING_PRINT_METADATA["filamentChanges"] = []
            PENDING_PRINT_METADATA["assigned_trays"] = []
            PENDING_PRINT_METADATA["complete"] = False
            PENDING_PRINT_METADATA["print_id"] = print_id
            FILAMENT_TRACKER.start_local_print_from_metadata(PENDING_PRINT_METADATA)

            for id, filament in PENDING_PRINT_METADATA["filaments"].items():
              parsed_grams = _parse_grams(filament.get("used_g"))
              parsed_length_m = _parse_grams(filament.get("used_m"))
              estimated_length_mm = parsed_length_m * 1000 if parsed_length_m is not None else None
              grams_used = parsed_grams if parsed_grams is not None else 0.0
              length_used = estimated_length_mm if estimated_length_mm is not None else 0.0
              if TRACK_LAYER_USAGE:
                grams_used = 0.0
                length_used = 0.0
              insert_filament_usage(
                  print_id,
                  filament["type"],
                  filament["color"],
                  grams_used,
                  id,
                  estimated_grams=parsed_grams,
                  length_used=length_used,
                  estimated_length=estimated_length_mm,
              )

            PENDING_PRINT_METADATA["tracking_started"] = True

        #TODO 
    
      # When stage changed to "change filament" and PENDING_PRINT_METADATA is set
      if (PENDING_PRINT_METADATA and 
          (
            (
              int(PRINTER_STATE["print"].get("stg_cur", -1)) == 4 and      # change filament stage (beginning of print)
              ( 
                PRINTER_STATE_LAST["print"].get("stg_cur", -1) == -1 or                                           # last stage not known
                (
                  int(PRINTER_STATE_LAST["print"].get("stg_cur")) != int(PRINTER_STATE["print"].get("stg_cur")) and
                  PRINTER_STATE_LAST["print"].get("ams", {}).get("tray_tar") == "255"             # stage has changed and last state was 255 (retract to ams)
                )
                or not PRINTER_STATE_LAST["print"].get("ams")                                               # ams not set in last state
              )
            )
            or                                                                                            # filament changes during printing are in mc_print_sub_stage
            (
              int(PRINTER_STATE_LAST["print"].get("mc_print_sub_stage", -1)) == 4  # last state was change filament
              and int(PRINTER_STATE["print"].get("mc_print_sub_stage", -1)) == 2                                                           # current state 
            )
            or (
              PRINTER_STATE["print"].get("ams", {}).get("tray_tar") == "254"
            )
            or 
            (
              int(PRINTER_STATE["print"].get("stg_cur", -1)) == 24 and int(PRINTER_STATE_LAST["print"].get("stg_cur", -1)) == 13
            )
            or (
              int(PRINTER_STATE["print"].get("stg_cur", -1)) == 4 and
              PRINTER_STATE["print"].get("ams", {}).get("tray_tar") not in (None, "255") and
              (PRINTER_STATE_LAST["print"].get("ams", {}).get("tray_tar") is None or PRINTER_STATE_LAST["print"].get("ams", {}).get("tray_tar") != PRINTER_STATE["print"].get("ams", {}).get("tray_tar"))
            )

          )
      ):
        if PRINTER_STATE["print"].get("ams"):
            mapped = False
            tray_tar_value = PRINTER_STATE["print"].get("ams").get("tray_tar")
            if tray_tar_value and tray_tar_value != "255":
                mapped = map_filament(int(tray_tar_value))
            FILAMENT_TRACKER.apply_ams_mapping(PENDING_PRINT_METADATA.get("ams_mapping") or [])
            if mapped:
                PENDING_PRINT_METADATA["complete"] = True

    if PENDING_PRINT_METADATA and PENDING_PRINT_METADATA.get("complete"):
      if TRACK_LAYER_USAGE:
        if PENDING_PRINT_METADATA.get("print_type") == "local":
          FILAMENT_TRACKER.apply_ams_mapping(PENDING_PRINT_METADATA.get("ams_mapping") or [])
        else:
          FILAMENT_TRACKER.set_print_metadata(PENDING_PRINT_METADATA)
        # Per-layer tracker will handle consumption; skip upfront spend.
      else:
        spendFilaments(PENDING_PRINT_METADATA)

      PENDING_PRINT_METADATA = {}
  
    PRINTER_STATE_LAST = copy.deepcopy(PRINTER_STATE)

def publish(client, msg):
  result = client.publish(f"device/{PRINTER_ID}/request", json.dumps(msg))
  status = result[0]
  if status == 0:
    log(f"Sent {msg} to topic device/{PRINTER_ID}/request")
    return True

  log(f"Failed to send message to topic device/{PRINTER_ID}/request")
  return False


def clear_ams_tray_assignment(ams_id, tray_id):
  if not MQTT_CLIENT:
    return

  ams_message = copy.deepcopy(AMS_FILAMENT_SETTING)
  ams_message["print"]["ams_id"] = int(ams_id)
  ams_message["print"]["tray_id"] = int(tray_id)
  ams_message["print"]["tray_color"] = ""
  ams_message["print"]["nozzle_temp_min"] = None
  ams_message["print"]["nozzle_temp_max"] = None
  ams_message["print"]["tray_type"] = ""
  ams_message["print"]["setting_id"] = ""
  ams_message["print"]["tray_info_idx"] = ""

  publish(MQTT_CLIENT, ams_message)

# Inspired by https://github.com/Donkie/Spoolman/issues/217#issuecomment-2303022970
def on_message(client, userdata, msg):
  global LAST_AMS_CONFIG, PRINTER_STATE, PRINTER_STATE_LAST, PENDING_PRINT_METADATA, PRINTER_MODEL
  
  try:
    data = json.loads(msg.payload.decode())

    info = data.get("info")
    if info and info.get("command") == "get_version":
      modules = info.get("module", [])
      detected = identify_ams_models_from_modules(modules)
      models_by_id = identify_ams_models_by_id(modules)
      LAST_AMS_CONFIG["get_version"] = {
        "info": info,
        "modules": modules,
        "detected_models": detected,
        "models_by_id": models_by_id,
      }

    if "print" in data:
      append_to_rotating_file("/home/app/logs/mqtt.log", msg.payload.decode())

    #print(data)

    if AUTO_SPEND:
        processMessage(data)
        FILAMENT_TRACKER.on_message(data)
      
    # Save external spool tray data
    if "print" in data and "vt_tray" in data["print"]:
      LAST_AMS_CONFIG["vt_tray"] = data["print"]["vt_tray"]

    # Save ams spool data
    if "print" in data and "ams" in data["print"] and "ams" in data["print"]["ams"]:
      LAST_AMS_CONFIG["ams"] = data["print"]["ams"]["ams"]
      for ams in data["print"]["ams"]["ams"]:
        log(f"AMS [{num2letter(ams['id'])}] (hum: {ams['humidity']}, temp: {ams['temp']}ºC)")
        for tray in ams["tray"]:
          if "tray_sub_brands" in tray:
            log(
                f"    - [{num2letter(ams['id'])}{tray['id']}] {tray['tray_sub_brands']} {tray['tray_color']} ({str(tray['remain']).zfill(3)}%) [[ {tray['tray_uuid']} ]]")

            found = False
            tray_uuid = "00000000000000000000000000000000"

            for spool in fetchSpools(True):

              tray_uuid = tray["tray_uuid"]

              if not spool.get("extra", {}).get("tag"):
                continue
              tag = json.loads(spool["extra"]["tag"])
              if tag != tray["tray_uuid"]:
                continue

              found = True

              setActiveTray(spool['id'], spool["extra"], ams['id'], tray["id"])

              # TODO: filament remaining - Doesn't work for AMS Lite
              # requests.patch(f"http://{SPOOLMAN_IP}:7912/api/v1/spool/{spool['id']}", json={
              #  "remaining_weight": tray["remain"] / 100 * tray["tray_weight"]
              # })

            if not found and tray_uuid == "00000000000000000000000000000000":
              log("      - non Bambulab Spool!")
            elif not found:
              log("      - Not found. Update spool tag!")
              tray["unmapped_bambu_tag"] = tray_uuid
              tray["issue"] = True
              clear_active_spool_for_tray(ams['id'], tray['id'])
              clear_ams_tray_assignment(ams['id'], tray['id'])
          else:
            log(
                f"    - [{num2letter(ams['id'])}{tray['id']}]")
            log("      - No Spool!")

  except Exception:
    traceback.print_exc()

def on_connect(client, userdata, flags, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = True
  log("Connected with result code " + str(rc))
  client.subscribe(f"device/{PRINTER_ID}/report")
  publish(client, GET_VERSION)
  publish(client, PUSH_ALL)

def on_disconnect(client, userdata, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = False
  log("Disconnected with result code " + str(rc))
  
def async_subscribe():
  global MQTT_CLIENT
  global MQTT_CLIENT_CONNECTED
  
  MQTT_CLIENT_CONNECTED = False
  MQTT_CLIENT = mqtt.Client()
  MQTT_CLIENT.username_pw_set("bblp", PRINTER_CODE)
  ssl_ctx = ssl.create_default_context()
  ssl_ctx.check_hostname = False
  ssl_ctx.verify_mode = ssl.CERT_NONE
  MQTT_CLIENT.tls_set_context(ssl_ctx)
  MQTT_CLIENT.tls_insecure_set(True)
  MQTT_CLIENT.on_connect = on_connect
  MQTT_CLIENT.on_disconnect = on_disconnect
  MQTT_CLIENT.on_message = on_message
  
  while True:
    while not MQTT_CLIENT_CONNECTED:
      try:
          log("🔄 Trying to connect ...", flush=True)
          MQTT_CLIENT.connect(PRINTER_IP, 8883, MQTT_KEEPALIVE)
          MQTT_CLIENT.loop_start()
          
      except Exception as exc:
          log(f"⚠️ connection failed: {exc}, new try in 15 seconds...", flush=True)

      time.sleep(15)

    time.sleep(15)

def init_mqtt(daemon: bool = False):
  # Start the asynchronous processing in a separate thread
  thread = Thread(target=async_subscribe, daemon=daemon)
  thread.start()

def getLastAMSConfig():
  global LAST_AMS_CONFIG
  return LAST_AMS_CONFIG


def getDetectedAmsModelsById():
  global LAST_AMS_CONFIG
  detected = LAST_AMS_CONFIG.get("get_version", {}).get("models_by_id") or {}
  return dict(detected)


def getMqttClient():
  global MQTT_CLIENT
  return MQTT_CLIENT

def isMqttClientConnected():
  global MQTT_CLIENT_CONNECTED

  return MQTT_CLIENT_CONNECTED
