import json
import ssl
import traceback
from threading import Thread

import paho.mqtt.client as mqtt

from config import PRINTER_ID, PRINTER_CODE, PRINTER_IP, AUTO_SPEND
from messages import GET_VERSION, PUSH_ALL
from spoolman_service import spendFilaments, setActiveTray, fetchSpools
from tools_3mf import getFilamentsUsageFrom3mf
import time

MQTT_CLIENT = {}  # Global variable storing MQTT Client
MQTT_CLIENT_CONNECTED = False
MQTT_KEEPALIVE = 60
LAST_AMS_CONFIG = {}  # Global variable storing last AMS configuration


def num2letter(num):
  return chr(ord("A") + int(num))


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
  global LAST_AMS_CONFIG
  try:
    data = json.loads(msg.payload.decode())
    #print(data)
    if AUTO_SPEND:
      # Prepare AMS spending estimation
      if "print" in data and "command" in data["print"] and data["print"]["command"] == "project_file" and "url" in \
          data["print"]:
        expected_filaments_usage = getFilamentsUsageFrom3mf(data["print"]["url"])
        ams_used = data["print"]["use_ams"]
        ams_mapping = data["print"]["ams_mapping"]
        if ams_used:
          spendFilaments(zip(ams_mapping, expected_filaments_usage))
        else:
          spendFilaments(zip([254], expected_filaments_usage))

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
            for spool in fetchSpools(True):
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

            if not found:
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
  
  print("🔄 Trying to connect ...", flush=True)
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
          time.sleep(15)
      except Exception as e:
          print(f"⚠️ connection failed: {e}, new try in 15 seconds...", flush=True)
          time.sleep(5)

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