import json
import ssl
import traceback
from threading import Thread

import paho.mqtt.client as mqtt

from config import PRINTER_ID, PRINTER_CODE, PRINTER_IP, AUTO_SPEND, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID
from messages import GET_VERSION, PUSH_ALL
from spoolman_service import spendFilaments, setActiveTray, fetchSpools
from tools_3mf import getMetaDataFrom3mf
import time
import copy
from collections.abc import Mapping
from logger import append_to_rotating_file
from print_history import  insert_print, insert_filament_usage, update_filament_spool

MQTT_CLIENT = {}  # Global variable storing MQTT Client
MQTT_CLIENT_CONNECTED = False
MQTT_KEEPALIVE = 60
LAST_AMS_CONFIG = {}  # Global variable storing last AMS configuration

PRINTER_STATE = {}
PRINTER_STATE_LAST = {}

PENDING_PRINT_METADATA = {}

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

def num2letter(num):
  return chr(ord("A") + int(num))
  
def update_dict(original: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, Mapping) and key in original and isinstance(original[key], Mapping):
            original[key] = update_dict(original[key], value)
        else:
            original[key] = value
    return original

def map_filament(tray_tar):
  global PENDING_PRINT_METADATA
  # Prüfen, ob ein Filamentwechsel aktiv ist (stg_cur == 4)
  #if stg_cur == 4 and tray_tar is not None:
  if PENDING_PRINT_METADATA:
    PENDING_PRINT_METADATA["filamentChanges"].append(tray_tar)  # Jeder Wechsel zählt, auch auf das gleiche Tray
    print(f'Filamentchange {len(PENDING_PRINT_METADATA["filamentChanges"])}: Tray {tray_tar}')

    # Anzahl der erkannten Wechsel
    change_count = len(PENDING_PRINT_METADATA["filamentChanges"]) - 1  # -1, weil der erste Eintrag kein Wechsel ist

    # Slot in der Wechselreihenfolge bestimmen
    for tray, usage_count in PENDING_PRINT_METADATA["filamentOrder"].items():
        if usage_count == change_count:
            PENDING_PRINT_METADATA["ams_mapping"].append(tray_tar)
            print(f"✅ Tray {tray_tar} assigned Filament to {tray}")

            for filament, tray in enumerate(PENDING_PRINT_METADATA["ams_mapping"]):
              print(f"  Filament {filament} → Tray {tray}")


    # Falls alle Slots zugeordnet sind, Ausgabe der Zuordnung
    if len(PENDING_PRINT_METADATA["ams_mapping"]) == len(PENDING_PRINT_METADATA["filamentOrder"]):
        print("\n✅ All trays assigned:")
        for filament, tray in enumerate(PENDING_PRINT_METADATA["ams_mapping"]):
            print(f"  Filament {tray} → Tray {tray}")

        return True
  
  return False
  
def processMessage(data):
  global LAST_AMS_CONFIG, PRINTER_STATE, PRINTER_STATE_LAST, PENDING_PRINT_METADATA

   # Prepare AMS spending estimation
  if "print" in data:    
    update_dict(PRINTER_STATE, data)
    
    if "command" in data["print"] and data["print"]["command"] == "project_file" and "url" in data["print"]:
      PENDING_PRINT_METADATA = getMetaDataFrom3mf(data["print"]["url"])

      print_id = insert_print(PRINTER_STATE["print"]["subtask_name"], "cloud", PENDING_PRINT_METADATA["image"])

      if "use_ams" in PRINTER_STATE["print"] and PRINTER_STATE["print"]["use_ams"]:
        PENDING_PRINT_METADATA["ams_mapping"] = PRINTER_STATE["print"]["ams_mapping"]
      else:
        PENDING_PRINT_METADATA["ams_mapping"] = [EXTERNAL_SPOOL_ID]

      PENDING_PRINT_METADATA["print_id"] = print_id
      PENDING_PRINT_METADATA["complete"] = True

      for id, filament in PENDING_PRINT_METADATA["filaments"].items():
        insert_filament_usage(print_id, filament["type"], filament["color"], filament["used_g"], id)
  
    #if ("gcode_state" in data["print"] and data["print"]["gcode_state"] == "RUNNING") and ("print_type" in data["print"] and data["print"]["print_type"] != "local") \
    #  and ("tray_tar" in data["print"] and data["print"]["tray_tar"] != "255") and ("stg_cur" in data["print"] and data["print"]["stg_cur"] == 0 and PRINT_CURRENT_STAGE != 0):
    
    #TODO: What happens when printed from external spool, is ams and tray_tar set?
    if ( "print_type" in PRINTER_STATE["print"] and PRINTER_STATE["print"]["print_type"] == "local" and
        "print" in PRINTER_STATE_LAST
      ):

      if (
          "gcode_state" in PRINTER_STATE["print"] and 
          PRINTER_STATE["print"]["gcode_state"] == "RUNNING" and
          PRINTER_STATE_LAST["print"]["gcode_state"] == "PREPARE" and 
          "gcode_file" in PRINTER_STATE["print"]
        ):

        PENDING_PRINT_METADATA = getMetaDataFrom3mf(PRINTER_STATE["print"]["gcode_file"])

        print_id = insert_print(PENDING_PRINT_METADATA["file"], PRINTER_STATE["print"]["print_type"], PENDING_PRINT_METADATA["image"])

        PENDING_PRINT_METADATA["ams_mapping"] = []
        PENDING_PRINT_METADATA["filamentChanges"] = []
        PENDING_PRINT_METADATA["complete"] = False
        PENDING_PRINT_METADATA["print_id"] = print_id

        for id, filament in PENDING_PRINT_METADATA["filaments"].items():
          insert_filament_usage(print_id, filament["type"], filament["color"], filament["used_g"], id)

        #TODO 
    
      # When stage changed to "change filament" and PENDING_PRINT_METADATA is set
      if (PENDING_PRINT_METADATA and 
          (
            ("stg_cur" in PRINTER_STATE["print"] and (int(PRINTER_STATE["print"]["stg_cur"]) == 4) and      # change filament stage (beginning of print)
              ( 
                "stg_cur" not in PRINTER_STATE_LAST["print"] or                                           # last stage not known
                (
                  PRINTER_STATE_LAST["print"]["stg_cur"] != PRINTER_STATE["print"]["stg_cur"]             # stage has changed and last state was 255 (retract to ams)
                  and "ams" in PRINTER_STATE_LAST["print"] and int(PRINTER_STATE_LAST["print"]["ams"]["tray_tar"]) == 255
                )
                or "ams" not in PRINTER_STATE_LAST["print"]                                               # ams not set in last state
              )
            )
            or                                                                                            # filament changes during printing are in mc_print_sub_stage
            (
              "mc_print_sub_stage" in PRINTER_STATE_LAST["print"] and int(PRINTER_STATE_LAST["print"]["mc_print_sub_stage"]) == 4  # last state was change filament
              and int(PRINTER_STATE["print"]["mc_print_sub_stage"]) == 2                                                           # current state 
            )
            or (
              "ams" in PRINTER_STATE["print"] and int(PRINTER_STATE["print"]["ams"]["tray_tar"]) == 254
            )
            or 
            (
              int(PRINTER_STATE["print"]["stg_cur"]) == 24 and int(PRINTER_STATE_LAST["print"]["stg_cur"]) == 13
            )

          )
      ):
        if "ams" in PRINTER_STATE["print"] and map_filament(int(PRINTER_STATE["print"]["ams"]["tray_tar"])):
            PENDING_PRINT_METADATA["complete"] = True
          

    if PENDING_PRINT_METADATA and PENDING_PRINT_METADATA["complete"]:
      spendFilaments(PENDING_PRINT_METADATA)

      PENDING_PRINT_METADATA = {}
  
    PRINTER_STATE_LAST = copy.deepcopy(PRINTER_STATE)

def publish(client, msg):
  result = client.publish(f"device/{PRINTER_ID}/request", json.dumps(msg))
  status = result[0]
  if status == 0:
    print(f"Sent {msg} to topic device/{PRINTER_ID}/request")
    return True

  print(f"Failed to send message to topic device/{PRINTER_ID}/request")
  return False

# Inspired by https://github.com/Donkie/Spoolman/issues/217#issuecomment-2303022970
def on_message(client, userdata, msg):
  global LAST_AMS_CONFIG, PRINTER_STATE, PRINTER_STATE_LAST, PENDING_PRINT_METADATA, PRINTER_MODEL
  
  try:
    data = json.loads(msg.payload.decode())

    if "print" in data:
      append_to_rotating_file("/home/app/logs/mqtt.log", msg.payload.decode())

    #print(data)

    if AUTO_SPEND:
        processMessage(data)
      
    # Save external spool tray data
    if "print" in data and "vt_tray" in data["print"]:
      LAST_AMS_CONFIG["vt_tray"] = data["print"]["vt_tray"]

    # Save ams spool data
    if "print" in data and "ams" in data["print"] and "ams" in data["print"]["ams"]:
      LAST_AMS_CONFIG["ams"] = data["print"]["ams"]["ams"]
      for ams in data["print"]["ams"]["ams"]:
        print(f"AMS [{num2letter(ams['id'])}] (hum: {ams['humidity']}, temp: {ams['temp']}ºC)")
        for tray in ams["tray"]:
          if "tray_sub_brands" in tray:
            print(
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
              print("      - No Spool or non Bambulab Spool!")
            elif not found:
              print("      - Not found. Update spool tag!")
              
  except Exception as e:
    traceback.print_exc()

def on_connect(client, userdata, flags, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = True
  print("Connected with result code " + str(rc))
  client.subscribe(f"device/{PRINTER_ID}/report")
  publish(client, GET_VERSION)
  publish(client, PUSH_ALL)

def on_disconnect(client, userdata, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = False
  print("Disconnected with result code " + str(rc))
  
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
          print("🔄 Trying to connect ...", flush=True)
          MQTT_CLIENT.connect(PRINTER_IP, 8883, MQTT_KEEPALIVE)
          MQTT_CLIENT.loop_start()
          
      except Exception as e:
          print(f"⚠️ connection failed: {e}, new try in 15 seconds...", flush=True)

      time.sleep(15)

    time.sleep(15)

def init_mqtt():
  # Start the asynchronous processing in a separate thread
  thread = Thread(target=async_subscribe)
  thread.start()

def getLastAMSConfig():
  global LAST_AMS_CONFIG
  return LAST_AMS_CONFIG


def getMqttClient():
  global MQTT_CLIENT
  return MQTT_CLIENT

def isMqttClientConnected():
  global MQTT_CLIENT_CONNECTED

  return MQTT_CLIENT_CONNECTED
