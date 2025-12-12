import io
import json
import math
import os
import traceback
import uuid

import qrcode
from PIL import Image
from flask import Flask, request, render_template, redirect, url_for, send_file

from config import BASE_URL, AUTO_SPEND, SPOOLMAN_BASE_URL, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID, PRINTER_NAME
from filament import generate_filament_brand_code, generate_filament_temperatures
from frontend_utils import color_is_dark
from messages import AMS_FILAMENT_SETTING
import mqtt_bambulab
import print_history as print_history_service
import spoolman_client
import spoolman_service
import test_data
from spoolman_service import augmentTrayDataWithSpoolMan, trayUid

_TEST_PATCH_CONTEXT = None
if test_data.TEST_MODE_FLAG:
  _TEST_PATCH_CONTEXT = test_data.activate_test_data_patches()

USE_TEST_DATA = test_data.test_data_active()
READ_ONLY_MODE = (not USE_TEST_DATA) and os.getenv("OPENSPOOLMAN_LIVE_READONLY") == "1"

if not USE_TEST_DATA:
  mqtt_bambulab.init_mqtt()

app = Flask(__name__)

@app.context_processor
def fronted_utilities():
  return dict(SPOOLMAN_BASE_URL=SPOOLMAN_BASE_URL, AUTO_SPEND=AUTO_SPEND, color_is_dark=color_is_dark, BASE_URL=BASE_URL, EXTERNAL_SPOOL_AMS_ID=EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID=EXTERNAL_SPOOL_ID, PRINTER_MODEL=mqtt_bambulab.getPrinterModel(), PRINTER_NAME=PRINTER_NAME, MQTT_CONNECTED=mqtt_bambulab.isMqttClientConnected())

@app.route("/issue")
def issue():
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="Printer is offline. This page requires printer connection to display tray information.")
    
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
    if spool.get("extra") and spool["extra"].get("active_tray") and spool["extra"]["active_tray"] == json.dumps(trayUid(ams_id, tray_id)):
      active_spool = spool
      break

  if tray_data:
    augmentTrayDataWithSpoolMan(spool_list, tray_data, trayUid(ams_id, tray_id))

  #TODO: Determine issue
  #New bambulab spool
  #Tray empty, but spoolman has record
  #Extra tag mismatch?
  #COLor/type mismatch

  return render_template('issue.html', fix_ams=fix_ams, tray_data=tray_data, ams_id=ams_id, tray_id=tray_id, active_spool=active_spool)

@app.route("/fill")
def fill():
  ams_id = request.args.get("ams")
  tray_id = request.args.get("tray")
  if not all([ams_id, tray_id]):
    return render_template('error.html', exception="Missing AMS ID, or Tray ID.")

  spool_id = request.args.get("spool_id")
  if spool_id:
    # Setting a spool requires MQTT connection to send commands to printer
    if not mqtt_bambulab.isMqttClientConnected():
      return render_template('error.html', exception="Printer is offline. Cannot assign spool to tray. Please ensure the printer is online and try again.")
    
    if READ_ONLY_MODE:
      return render_template('error.html', exception="Live read-only mode: assigning spools to trays is disabled.")

    spool_data = spoolman_client.getSpoolById(spool_id)
    mqtt_bambulab.setActiveTray(spool_id, spool_data["extra"], ams_id, tray_id)
    setActiveSpool(ams_id, tray_id, spool_data)
    return redirect(url_for('home', success_message=f"Updated Spool ID {spool_id} to AMS {ams_id}, Tray {tray_id}."))
  else:
    # Viewing the fill page can work without MQTT
    spools = mqtt_bambulab.fetchSpools()

    materials = extract_materials(spools)
    selected_materials = []

    try:
      if mqtt_bambulab.isMqttClientConnected():
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

@app.route("/spool_info")
def spool_info():
  try:
    tag_id = request.args.get("tag_id")
    spool_id = request.args.get("spool_id")
    
    # Get AMS config if available, otherwise use empty defaults
    if mqtt_bambulab.isMqttClientConnected():
      last_ams_config = mqtt_bambulab.getLastAMSConfig()
      ams_data = last_ams_config.get("ams", [])
      vt_tray_data = last_ams_config.get("vt_tray", {})
    else:
      ams_data = []
      vt_tray_data = {}
    
    spool_list = mqtt_bambulab.fetchSpools()

    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    if vt_tray_data:
      augmentTrayDataWithSpoolMan(spool_list, vt_tray_data, trayUid(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID))
      issue |= vt_tray_data.get("issue", False)

    for ams in ams_data:
      for tray in ams["tray"]:
        augmentTrayDataWithSpoolMan(spool_list, tray, trayUid(ams["id"], tray["id"]))
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
      return render_template('spool_info.html', tag_id=tag_id, current_spool=current_spool, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue)
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
  # This route sends commands to printer, so MQTT is required
  if not mqtt_bambulab.isMqttClientConnected():
    return render_template('error.html', exception="Printer is offline. Cannot assign spool to tray. Please ensure the printer is online and try again.")
  
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
    return None
  
  ams_message = AMS_FILAMENT_SETTING
  ams_message["print"]["sequence_id"] = 0
  ams_message["print"]["ams_id"] = int(ams_id)
  ams_message["print"]["tray_id"] = int(tray_id)
  
  if "color_hex" in spool_data["filament"]:
    ams_message["print"]["tray_color"] = spool_data["filament"]["color_hex"].upper() + "FF"
  else:
    ams_message["print"]["tray_color"] = spool_data["filament"]["multi_color_hexes"].split(',')[0].upper() + "FF"
      
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

  print(ams_message)
  mqtt_bambulab.publish(mqtt_bambulab.getMqttClient(), ams_message)

@app.route("/")
def home():
  try:
    # Get AMS config if available, otherwise use empty defaults
    if mqtt_bambulab.isMqttClientConnected():
      last_ams_config = mqtt_bambulab.getLastAMSConfig()
      ams_data = last_ams_config.get("ams", [])
      vt_tray_data = last_ams_config.get("vt_tray", {})
    else:
      ams_data = []
      vt_tray_data = {}
    
    spool_list = mqtt_bambulab.fetchSpools()
    success_message = request.args.get("success_message")
    
    issue = False
    #TODO: Fix issue when external spool info is reset via bambulab interface
    if vt_tray_data:
      augmentTrayDataWithSpoolMan(spool_list, vt_tray_data, trayUid(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID))
      issue |= vt_tray_data.get("issue", False)

    for ams in ams_data:
      for tray in ams["tray"]:
        augmentTrayDataWithSpoolMan(spool_list, tray, trayUid(ams["id"], tray["id"]))
        issue |= tray.get("issue", False)

    return render_template('index.html', success_message=success_message, ams_data=ams_data, vt_tray_data=vt_tray_data, issue=issue)
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

    # Check if tag already exists
    spool_data = spoolman_client.getSpoolById(spool_id)
    tag_value = spool_data.get("extra", {}).get("tag")
    
    if tag_value:
      # Use existing tag
      myuuid = json.loads(tag_value)
    else:
      # Create new tag
      myuuid = str(uuid.uuid4())
      existing_extra = spool_data.get("extra", {})
      spoolman_client.patchExtraTags(spool_id, existing_extra, {
        "tag": json.dumps(myuuid),
      })
    
    return render_template('write_tag.html', myuuid=myuuid, spool_id=spool_id)
  except Exception as e:
    traceback.print_exc()
    return render_template('error.html', exception=str(e))

@app.route("/qr_code")
def qr_code():
  try:
    spool_id = request.args.get("spool_id")
    tag_id = request.args.get("tag_id")
    download = request.args.get("download", "false").lower() == "true"

    if not spool_id and not tag_id:
      return render_template('error.html', exception="spool_id or tag_id is required as a query parameter")

    spool_data = None
    # If tag_id is provided, use it; otherwise get it from spool
    if not tag_id and spool_id:
      spool_data = spoolman_client.getSpoolById(spool_id)
      tag_value = spool_data.get("extra", {}).get("tag")
      if tag_value:
        tag_id = json.loads(tag_value)
      else:
        # If no tag exists, generate a new one
        tag_id = str(uuid.uuid4())
        existing_extra = spool_data.get("extra", {})
        spoolman_client.patchExtraTags(spool_id, existing_extra, {
          "tag": json.dumps(tag_id),
        })
    elif tag_id and spool_id:
      # If both are provided, get spool data for display
      try:
        spool_data = spoolman_client.getSpoolById(spool_id)
      except Exception:
        pass
    elif tag_id and not spool_id:
      # Try to find spool by tag_id from SpoolMan
      try:
        spools = spoolman_client.fetchSpoolList()
        for spool in spools:
          tag_value = spool.get("extra", {}).get("tag")
          if tag_value:
            stored_tag = json.loads(tag_value)
            if stored_tag == tag_id:
              spool_data = spool
              spool_id = spool.get('id')
              break
      except Exception:
        pass

    # Generate QR code URL
    qr_url = f"{BASE_URL}/spool_info?tag_id={tag_id}"
    
    # Create QR code
    qr = qrcode.QRCode(
      version=1,
      error_correction=qrcode.constants.ERROR_CORRECT_L,
      box_size=6,
      border=2,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)

    # Create QR code image
    qr_img = qr.make_image(fill_color="black", back_color="white")
    # Convert to RGB mode to ensure compatibility
    if qr_img.mode != 'RGB':
      qr_img = qr_img.convert('RGB')
    
    img = qr_img
    
    # Save to bytes
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)

    # Return as download or inline
    filename = f"spool_{spool_id}_qr.png" if spool_id else f"tag_{tag_id}_qr.png"
    return send_file(img_io, mimetype='image/png', as_attachment=download, download_name=filename)
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

  if all([ams_slot, print_id, spool_id]):
    filament = print_history_service.get_filament_for_slot(print_id, ams_slot)
    print_history_service.update_filament_spool(print_id, ams_slot, spool_id)

    if(filament["spool_id"] != int(spool_id) and (not old_spool_id or (old_spool_id and filament["spool_id"] == int(old_spool_id)))):
      if old_spool_id and int(old_spool_id) != -1:
        spoolman_client.consumeSpool(old_spool_id, filament["grams_used"] * -1)

      spoolman_client.consumeSpool(spool_id, filament["grams_used"])

  prints, total_prints = print_history_service.get_prints_with_filament(limit=per_page, offset=offset)

  spool_list = mqtt_bambulab.fetchSpools()

  for print in prints:
    print["filament_usage"] = json.loads(print["filament_info"])
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
