import json
import math
import os
import traceback
import uuid
from collections import Counter

from flask import Flask, request, render_template, redirect, url_for

from config import (
    BASE_URL,
    AUTO_SPEND,
    SPOOLMAN_BASE_URL,
    EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_ID,
    PRINTER_NAME,
    CLEAR_ASSIGNMENT_WHEN_EMPTY,
)
from filament import generate_filament_brand_code, generate_filament_temperatures
from frontend_utils import color_is_dark
from messages import AMS_FILAMENT_SETTING
import mqtt_bambulab
import print_history as print_history_service
import spoolman_client
import spoolman_service
import test_data
from spoolman_service import augmentTrayDataWithSpoolMan, trayUid, normalize_color_hex
from logger import log

_TEST_PATCH_CONTEXT = None
if test_data.TEST_MODE_FLAG:
  _TEST_PATCH_CONTEXT = test_data.activate_test_data_patches()

USE_TEST_DATA = test_data.test_data_active()
READ_ONLY_MODE = (not USE_TEST_DATA) and os.getenv("OPENSPOOLMAN_LIVE_READONLY") == "1"

LAYER_TRACKING_STATUS_DISPLAY = {
    "RUNNING": ("Printing", "warning"),
    "COMPLETED": ("Finished", "success"),
    "ABORTED": ("Cancelled", "danger"),
    "FAILED": ("Failed", "danger"),
}

if not USE_TEST_DATA:
  mqtt_bambulab.init_mqtt()

app = Flask(__name__)

@app.context_processor
def fronted_utilities():
  printer_model = mqtt_bambulab.getPrinterModel() or {}
  ams_models_by_id = mqtt_bambulab.getDetectedAmsModelsById()

  return dict(
    SPOOLMAN_BASE_URL=SPOOLMAN_BASE_URL,
    AUTO_SPEND=AUTO_SPEND,
    AMS_MODELS_BY_ID=ams_models_by_id,
    color_is_dark=color_is_dark,
    BASE_URL=BASE_URL,
    EXTERNAL_SPOOL_AMS_ID=EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_ID=EXTERNAL_SPOOL_ID,
    PRINTER_MODEL=printer_model,
    PRINTER_NAME=PRINTER_NAME,
  )


def build_ams_labels(ams_data):
  models_by_id = mqtt_bambulab.getDetectedAmsModelsById()
  base_labels = []
  for ams in ams_data:
    ams_id = ams.get("id")
    key = str(ams_id)
    base_name = models_by_id.get(key) or models_by_id.get(ams_id) or "AMS"
    base_labels.append(base_name)

  totals = Counter(base_labels)
  takt = Counter()
  labels = {}

  for ams, base_name in zip(ams_data, base_labels):
    takt[base_name] += 1
    suffix = f" {takt[base_name]}" if totals[base_name] > 1 else ""
    label = f"{base_name}{suffix}"
    ams_id = ams.get("id")
    labels[str(ams_id)] = label
    labels[ams_id] = label

  return labels


def _augment_tray(spool_list, tray_data, ams_id, tray_id):
  augmentTrayDataWithSpoolMan(spool_list, tray_data, ams_id, tray_id)
  if tray_data.get("unmapped_bambu_tag"):
    spoolman_service.clear_active_spool_for_tray(ams_id, tray_id)
    augmentTrayDataWithSpoolMan(spool_list, tray_data, ams_id, tray_id)
  empty_condition = (
      CLEAR_ASSIGNMENT_WHEN_EMPTY
      and not tray_data.get("spool_material")
      and not tray_data.get("unmapped_bambu_tag")
  )
  if empty_condition:
    spoolman_service.clear_active_spool_for_tray(ams_id, tray_id)
    mqtt_bambulab.clear_ams_tray_assignment(ams_id, tray_id)


def _select_spool_color_hex(spool_data):
  filament = spool_data.get("filament", {})
  multi = filament.get("multi_color_hexes")
  candidate = ""

  if multi:
    if isinstance(multi, (list, tuple)) and multi:
      candidate = multi[0]
    elif isinstance(multi, str):
      candidate = multi.split(",")[0]

  if not candidate:
    candidate = filament.get("color_hex") or ""

  return normalize_color_hex(candidate)

@app.route("/issue")
def issue():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
    
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  if not all([ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID.")

  fix_ams = None
  tray_data = None

  spool_list = mqtt_bambulab.fetchSpools()
  last_ams_config = mqtt_bambulab.getLastAMSConfig()
  if ams_id == EXTERNAL_SPOOL_AMS_ID:
    fix_ams = last_ams_config.get("vt_tray", {})
    tray_data = fix_ams
  else:
    for ams in last_ams_config.get("ams", []):
      if str(ams["id"]) == str(ams_id):
        fix_ams = ams
        break

  if fix_ams:
    for tray in fix_ams.get("tray", []):
      if str(tray["id"]) == str(tray_id):
        tray_data = tray
        break

  active_spool = None
  for spool in spool_list:
    if spool.get("extra") and spool["extra"].get("active_tray") and spool["extra"].get("active_tray") == json.dumps(trayUid(ams_id, tray_id)):
      active_spool = spool
      break

  if tray_data:
    _augment_tray(spool_list, tray_data, ams_id, tray_id)

  #TODO: Determine issue
  #New bambulab spool
  #Tray empty, but spoolman has record
  #Extra tag mismatch?
  #COLor/type mismatch

  return render_template('issue.html', fix_ams=fix_ams, tray_data=tray_data, ams_id=ams_id, tray_id=tray_id, active_spool=active_spool)

@app.route("/fill")
def fill():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
    
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  if not all([ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID.")

  spool_id = request.args.get("spool_id")
  if spool_id:
    if READ_ONLY_MODE:
      return render_template('error.html', exception="Live read-only mode: assigning spools to trays is disabled.")

    spool_data = spoolman_client.getSpoolById(spool_id)
    mqtt_bambulab.setActiveTray(spool_id, spool_data["extra"], ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_data)
    return redirect(url_for('home', success_message=f"Updated Spool ID {spool_id} to AMS {ams_id}, Tray {tray_id}."))
  else:
    spools = mqtt_bambulab.fetchSpools()

    materials = extract_materials(spools)
    selected_materials = []

    try:
      last_ams_config = mqtt_bambulab.getLastAMSConfig()
      default_material = None

      if ams_id == EXTERNAL_SPOOL_AMS_ID:
        default_material = last_ams_config.get("vt_tray", {}).get("tray_type")
      else:
        for ams in last_ams_config.get("ams", []):
          if str(ams.get("id")) != str(ams_id):
            continue

          for tray in ams.get("tray", []):
            if str(tray.get("id")) == str(tray_id):
              default_material = tray.get("tray_type")
              break

      if default_material and default_material in materials:
        selected_materials.append(default_material)
    except Exception:
      pass

    return render_template('fill.html', spools=spools, ams_id=ams_id, tray_id=tray_id, materials=materials, selected_materials=selected_materials)

@app.route("/assign_bambu_spool")
def assign_bambu_spool():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  bambu_tag = request.args.get("tag")
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  spool_id = request.args.get("spool_id")

  if not all([bambu_tag, ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, Tray ID, or Bambu spool tag.")

  if bambu_tag == "00000000000000000000000000000000":
    return render_template('error.html', exception="No Bambu spool was detected in this tray.")

  if spool_id:
    if READ_ONLY_MODE:
      return render_template('error.html', exception="Live read-only mode: linking Bambu spools is disabled.")

    spool_data = spoolman_client.getSpoolById(spool_id)
    extras = spool_data.get("extra") or {}

    spoolman_client.patchExtraTags(spool_id, extras, {
      "tag": json.dumps(bambu_tag),
    })

    mqtt_bambulab.setActiveTray(spool_id, extras, ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_data)

    return redirect(url_for('home', success_message=f"Linked Bambu spool to SpoolMan spool {spool_id} on AMS {ams_id}, Tray {tray_id}."))

  spools = mqtt_bambulab.fetchSpools()
  materials = extract_materials(spools)
  selected_materials = []

  try:
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    default_material = None

    if ams_id == EXTERNAL_SPOOL_AMS_ID:
      default_material = last_ams_config.get("vt_tray", {}).get("tray_type")
    else:
      for ams in last_ams_config.get("ams", []):
        if str(ams.get("id")) != str(ams_id):
          continue

        for tray in ams.get("tray", []):
          if str(tray.get("id")) == str(tray_id):
            default_material = tray.get("tray_type")
            break

    if default_material and default_material in materials:
      selected_materials.append(default_material)
  except Exception:
    pass

  return render_template(
    'assign_bambu_spool.html',
    spools=spools,
    ams_id=ams_id,
    tray_id=tray_id,
    bambu_tag=bambu_tag,
    materials=materials,
    selected_materials=selected_materials,
  )

@app.route("/spool_info")
def spool_info():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  try:
    tag_id = request.args.get("tag_id")
    spool_id = request.args.get("spool_id")
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    ams_data = last_ams_config.get("ams", [])
    vt_tray_data = last_ams_config.get("vt_tray", {})
    spool_list = mqtt_bambulab.fetchSpools()

    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    _augment_tray(spool_list, vt_tray_data, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)
    issue |= vt_tray_data.get("issue", False)

    for ams in ams_data:
      for tray in ams["tray"]:
        _augment_tray(spool_list, tray, ams["id"], tray["id"])
        issue |= tray.get("issue", False)

    if not tag_id and not spool_id:
      return render_template('error.html', exception="TAG ID or spool_id is required as a query parameter (e.g., ?tag_id=RFID123 or ?spool_id=1)")

    spools = mqtt_bambulab.fetchSpools()
    current_spool = None

    spool_id_int = None
    if spool_id is not None:
      try:
        spool_id_int = int(spool_id)
      except ValueError:
        return render_template('error.html', exception="Invalid spool_id provided")

    for spool in spools:
      if spool_id_int is not None and spool['id'] == spool_id_int:
        current_spool = spool
        if not tag_id:
          tag_value = spool.get("extra", {}).get("tag")
          if tag_value:
            tag_id = json.loads(tag_value)
        break

      if not tag_id:
        continue

      if not spool.get("extra", {}).get("tag"):
        continue

      tag = json.loads(spool["extra"]["tag"])
      if tag != tag_id:
        continue

      current_spool = spool
      break

    if current_spool:
      ams_labels = build_ams_labels(ams_data)
      return render_template('spool_info.html', tag_id=tag_id, current_spool=current_spool, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue, ams_labels=ams_labels)
    else:
      return render_template('error.html', exception="Spool not found")
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))


@app.route("/spool/info/<int:spool_id>")
@app.route("/spool/show/<int:spool_id>")
def spoolman_compatible_spool_info(spool_id):
  query_params = {"spool_id": spool_id}
  tag_id = request.args.get("tag_id")

  if tag_id:
    query_params["tag_id"] = tag_id

  return redirect(url_for('spool_info', **query_params))


@app.route("/tray_load")
def tray_load():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
  
  tag_id = request.args.get("tag_id")
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  spool_id = request.args.get("spool_id")

  if not all([ams_id, tray_id, spool_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID or spool_id.")

  if READ_ONLY_MODE:
    return render_template('error.html', exception="Live read-only mode: assigning spools to trays is disabled.")

  try:
    # Update Spoolman with the selected tray
    spool_data = spoolman_client.getSpoolById(spool_id)
    mqtt_bambulab.setActiveTray(spool_id, spool_data["extra"], ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_data)

    return redirect(url_for('home', success_message=f"Updated Spool ID {spool_id} with TAG id {tag_id} to AMS {ams_id}, Tray {tray_id}."))
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

def setActiveSpool(ams_id, tray_id, spool_data):
  if USE_TEST_DATA or READ_ONLY_MODE:
    return None

  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")
  
  ams_message = AMS_FILAMENT_SETTING
  ams_message["print"]["sequence_id"] = 0
  ams_message["print"]["ams_id"] = int(ams_id)
  ams_message["print"]["tray_id"] = int(tray_id)
  color_hex = _select_spool_color_hex(spool_data)
  if color_hex:
    ams_message["print"]["tray_color"] = color_hex.upper() + "FF"
  else:
    ams_message["print"]["tray_color"] = ""
      
  if "nozzle_temperature" in spool_data["filament"]["extra"]:
    nozzle_temperature_range = spool_data["filament"]["extra"]["nozzle_temperature"].strip("[]").split(",")
    ams_message["print"]["nozzle_temp_min"] = int(nozzle_temperature_range[0])
    ams_message["print"]["nozzle_temp_max"] = int(nozzle_temperature_range[1])
  else:
    nozzle_temperature_range_obj = generate_filament_temperatures(spool_data["filament"]["material"],
                                                                  spool_data["filament"]["vendor"]["name"])
    ams_message["print"]["nozzle_temp_min"] = int(nozzle_temperature_range_obj["filament_min_temp"])
    ams_message["print"]["nozzle_temp_max"] = int(nozzle_temperature_range_obj["filament_max_temp"])

  ams_message["print"]["tray_type"] = spool_data["filament"]["material"]

  filament_brand_code = {}
  filament_brand_code["brand_code"] = spool_data["filament"]["extra"].get("filament_id", "").strip('"')
  filament_brand_code["sub_brand_code"] = ""

  if filament_brand_code["brand_code"] == "":
    filament_brand_code = generate_filament_brand_code(spool_data["filament"]["material"],
                                                      spool_data["filament"]["vendor"]["name"],
                                                      spool_data["filament"]["extra"].get("type", ""))
    
  ams_message["print"]["tray_info_idx"] = filament_brand_code["brand_code"]

  # TODO: test sub_brand_code
  # ams_message["print"]["tray_sub_brands"] = filament_brand_code["sub_brand_code"]
  ams_message["print"]["tray_sub_brands"] = ""

  log(ams_message)
  mqtt_bambulab.publish(mqtt_bambulab.getMqttClient(), ams_message)

@app.route("/")
def home():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  try:
    last_ams_config = mqtt_bambulab.getLastAMSConfig()
    ams_data = last_ams_config.get("ams", [])
    vt_tray_data = last_ams_config.get("vt_tray", {})
    spool_list = mqtt_bambulab.fetchSpools()
    success_message = request.args.get("success_message")
    
    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    _augment_tray(spool_list, vt_tray_data, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID)
    issue |= vt_tray_data["issue"]

    for ams in ams_data:
      for tray in ams["tray"]:
        _augment_tray(spool_list, tray, ams["id"], tray["id"])
        issue |= tray["issue"]

    ams_labels = build_ams_labels(ams_data)
    return render_template('index.html', success_message=success_message, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue, ams_labels=ams_labels)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

def sort_spools(spools):
  def condition(item):
    # Ensure the item has an "extra" key and is a dictionary
    if not isinstance(item, dict) or "extra" not in item or not isinstance(item["extra"], dict):
      return False

    # Check the specified condition
    return item["extra"].get("tag") or item["extra"].get("tag") == ""

  # Sort with the custom condition: False values come first
  return sorted(spools, key=lambda spool: bool(condition(spool)))


def extract_materials(spools):
  materials = set()

  for spool in spools:
    filament = None

    if isinstance(spool, dict):
      filament = spool.get("filament")
    else:
      filament = getattr(spool, "filament", None)

    if isinstance(filament, dict):
      material = filament.get("material")
    else:
      material = getattr(filament, "material", None)

    if material:
      materials.add(material)

  return sorted(materials)

@app.route("/assign_tag")
def assign_tag():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="MQTT is disconnected. Is the printer online?")

  try:
    spools = sort_spools(mqtt_bambulab.fetchSpools())

    materials = extract_materials(spools)
    selected_materials = []
    requested_material = request.args.get("material")

    if requested_material and requested_material in materials:
      selected_materials.append(requested_material)

    return render_template('assign_tag.html', spools=spools, materials=materials, selected_materials=selected_materials)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

@app.route("/write_tag")
def write_tag():
  try:
    spool_id = request.args.get("spool_id")

    if not spool_id:
      return render_template('error.html', exception="spool ID is required as a query parameter (e.g., ?spool_id=1)")

    myuuid = str(uuid.uuid4())

    spoolman_client.patchExtraTags(spool_id, {}, {
      "tag": json.dumps(myuuid),
    })
    return render_template('write_tag.html', myuuid=myuuid)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

@app.route('/health', methods=['GET'])
def health():
  return "OK", 200

@app.route("/print_history")
def print_history():
  spoolman_settings = spoolman_service.getSettings()

  try:
    def _to_float(value):
      try:
        return float(value)
      except (TypeError, ValueError):
        return None

    def _to_int(value):
      try:
        return int(value)
      except (TypeError, ValueError):
        return None

    page = max(int(request.args.get("page", 1)), 1)
  except ValueError:
    page = 1
  per_page = 50
  offset = max((page - 1) * per_page, 0)

  ams_slot = request.args.get("ams_slot")
  print_id = request.args.get("print_id")
  spool_id = request.args.get("spool_id")
  old_spool_id = request.args.get("old_spool_id")

  if not old_spool_id:
    old_spool_id = -1

  if READ_ONLY_MODE and all([ams_slot, print_id, spool_id]):
    return render_template('error.html', exception="Live read-only mode: updating print-to-spool assignments is disabled.")

  def _consume_for_spool(spool_id_value, grams_value=None, length_value=None):
    if spool_id_value is None:
      return
    if length_value is not None:
      spoolman_client.consumeSpool(spool_id_value, use_length=length_value)
    elif grams_value is not None:
      spoolman_client.consumeSpool(spool_id_value, use_weight=grams_value)

  if all([ams_slot, print_id, spool_id]):
    filament = print_history_service.get_filament_for_slot(print_id, ams_slot)
    print_history_service.update_filament_spool(print_id, ams_slot, spool_id)

    if(filament["spool_id"] != int(spool_id) and (not old_spool_id or (old_spool_id and filament["spool_id"] == int(old_spool_id)))):
      grams_used = _to_float(filament.get("grams_used"))
      length_used = _to_float(filament.get("length_used"))
      use_length = length_used is not None and length_used > 0

      if old_spool_id and int(old_spool_id) != -1:
        _consume_for_spool(
            old_spool_id,
            grams_value=-(grams_used or 0),
            length_value=-(length_used or 0) if use_length else None,
        )

      _consume_for_spool(
          spool_id,
          grams_value=grams_used,
          length_value=length_used if use_length else None,
      )

  prints, total_prints = print_history_service.get_prints_with_filament(limit=per_page, offset=offset)
  layer_tracking_map = print_history_service.get_layer_tracking_for_prints([print["id"] for print in prints])

  spool_list = mqtt_bambulab.fetchSpools()

  for print in prints:
    tracking_row = layer_tracking_map.get(print["id"])
    if tracking_row:
      status_key = (tracking_row.get("status") or "").upper()
      status_label, status_badge = LAYER_TRACKING_STATUS_DISPLAY.get(
          status_key, ("Unbekannt", "secondary")
      )
      total_layers = _to_int(tracking_row.get("total_layers"))
      layers_printed = _to_int(tracking_row.get("layers_printed")) or 0
      billed = _to_float(tracking_row.get("filament_grams_billed"))
      total_grams = _to_float(tracking_row.get("filament_grams_total"))

      progress = None
      if total_layers:
        progress = min(100, int(layers_printed / total_layers * 100))

      print["layer_tracking"] = {
        "status_label": status_label,
        "status_badge": status_badge,
        "layers_printed": layers_printed,
        "total_layers": total_layers,
        "progress_percent": progress,
        "filament_grams_billed": billed,
        "filament_grams_total": total_grams,
        "predicted_end_time": tracking_row.get("predicted_end_time"),
        "actual_end_time": tracking_row.get("actual_end_time"),
      }
    else:
      print["layer_tracking"] = None

    filament_usage_data = json.loads(print["filament_info"])
    filament_usage_sum = sum(
        _to_float(f.get("grams_used")) or 0 for f in filament_usage_data
    )
    tracking_total = (
        _to_float(print["layer_tracking"]["filament_grams_total"])
        if print["layer_tracking"]
        else None
    )
    print["display_filament_total"] = tracking_total if tracking_total is not None else filament_usage_sum

    print["filament_usage"] = filament_usage_data
    print["total_cost"] = 0

    for filament in print["filament_usage"]:
      if filament["spool_id"]:
        for spool in spool_list:
          if spool['id'] == filament["spool_id"]:
            filament["spool"] =  spool
            filament["cost"] = filament['grams_used'] * filament['spool']['cost_per_gram']
            print["total_cost"] += filament["cost"]
            break
  
  total_pages = max(1, math.ceil(total_prints / per_page))

  return render_template(
    'print_history.html',
    prints=prints,
    currencysymbol=spoolman_settings["currency_symbol"],
    page=page,
    total_pages=total_pages,
    per_page=per_page,
  )

@app.route("/print_select_spool")
def print_select_spool():

  try:
    ams_slot = request.args.get("ams_slot")
    print_id = request.args.get("print_id")
    old_spool_id = request.args.get("old_spool_id")
    
    change_spool = request.args.get("change_spool", "false").lower() == "true"
    
    if not old_spool_id:
      old_spool_id = -1

    if not all([ams_slot, print_id]):
      return render_template('error.html', exception="Missing spool ID or print ID.")

    spools = mqtt_bambulab.fetchSpools()

    materials = extract_materials(spools)
    selected_materials = []

    filament = print_history_service.get_filament_for_slot(print_id, ams_slot)

    try:
      filament_material = filament["filament_type"] if filament else None

      if filament_material and filament_material in materials:
        selected_materials.append(filament_material)
    except Exception:
      pass

    return render_template(
      'print_select_spool.html',
      spools=spools,
      ams_slot=ams_slot,
      print_id=print_id,
      old_spool_id=old_spool_id,
      change_spool=change_spool,
      materials=materials,
      selected_materials=selected_materials,
    )
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))
