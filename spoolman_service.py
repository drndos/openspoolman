import os
import math
from config import PRINTER_ID, EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_ID
from datetime import datetime
from zoneinfo import ZoneInfo
from print_history import update_filament_spool
import json

from spoolman_client import consumeSpool, patchExtraTags, fetchSpoolList, fetchSettings

SPOOLS = {}
SPOOLMAN_SETTINGS = {}

COLOR_DISTANCE_TOLERANCE = 80

currency_symbols = {
    "AED": "د.إ", "AFN": "؋", "ALL": "Lek", "AMD": "դր.", "ANG": "ƒ", "AOA": "Kz", 
    "ARS": "$", "AUD": "$", "AWG": "Afl.", "AZN": "₼", "BAM": "KM", "BBD": "$", 
    "BDT": "৳", "BGN": "лв", "BHD": "د.ب", "BIF": "Fr", "BMD": "$", "BND": "$", 
    "BOB": "$b", "BRL": "R$", "BSD": "$", "BTN": "Nu.", "BWP": "P", "BYN": "Br", 
    "BZD": "$", "CAD": "$", "CDF": "Fr", "CHF": "CHF", "CLP": "$", "CNY": "¥", 
    "COP": "$", "CRC": "₡", "CUP": "$", "CVE": "$", "CZK": "Kč", "DJF": "Fr", 
    "DKK": "kr", "DOP": "RD$", "DZD": "دج", "EGP": "ج.م", "ERN": "Nfk", "ETB": "ታማ", 
    "EUR": "€", "FJD": "$", "FKP": "£", "GBP": "£", "GEL": "₾", "GHS": "₵", 
    "GIP": "£", "GMD": "D", "GNF": "Fr", "GTQ": "Q", "GYD": "$", "HKD": "$", 
    "HNL": "L", "HRK": "kn", "HTG": "G", "HUF": "Ft", "IDR": "Rp", "ILS": "₪", 
    "INR": "₹", "IQD": "د.ع", "IRR": "﷼", "ISK": "kr", "JMD": "$", "JOD": "د.أ", 
    "JPY": "¥", "KES": "Sh", "KGS": "с", "KHR": "៛", "KMF": "Fr", "KRW": "₩", 
    "KWD": "د.ك", "KYD": "$", "KZT": "₸", "LAK": "₭", "LBP": "ل.ل", "LKR": "Rs", 
    "LRD": "$", "LSL": "L", "LTL": "Lt", "LVL": "Ls", "LYD": "د.ل", "MAD": "د.م.", 
    "MDL": "lei", "MGA": "Ar", "MKD": "ден", "MMK": "K", "MNT": "₮", "MOP": "MOP", 
    "MRO": "UM", "MRU": "MRU", "MUR": "Rs", "MVR": "Rf", "MWK": "MK", "MXN": "$", 
    "MYR": "RM", "MZN": "MT", "NAD": "$", "NGN": "₦", "NIO": "C$", "NOK": "kr", 
    "NPR": "₨", "NZD": "$", "OMR": "ر.ع.", "PAB": "B/.", "PEN": "S/", "PGK": "K", 
    "PHP": "₱", "PKR": "₨", "PLN": "zł", "PYG": "₲", "QAR": "ر.ق", "RON": "lei", 
    "RSD": "дин.", "RUB": "₽", "RWF": "Fr", "SAR": "ر.س", "SBD": "$", "SCR": "₨", 
    "SEK": "kr", "SGD": "$", "SHP": "£", "SLL": "Le", "SOS": "Sh", "SRD": "$", 
    "SSP": "£", "STD": "Db", "STN": "STN", "SYP": "ل.س", "SZL": "L", "THB": "฿", 
    "TJS": "ЅМ", "TMT": "m", "TND": "د.ت", "TOP": "T$", "TRY": "₺", "TTD": "$", 
    "TWD": "NT$", "TZS": "Sh", "UAH": "₴", "UGX": "Sh", "UYU": "$", "UZS": "лв", 
    "VES": "Bs.S", "VND": "₫", "VUV": "Vt", "WST": "T", "XAF": "XAF", "XAG": "XAG", 
    "XAU": "XAU", "XCD": "XCD", "XDR": "XDR", "XOF": "XOF", "XPF": "XPF", "YER": "ر.ي", 
    "ZAR": "R", "ZMW": "ZK", "ZWL": "$"
}

def get_currency_symbol(code):
    return currency_symbols.get(code, code)

def trayUid(ams_id, tray_id):
  return f"{PRINTER_ID}_{ams_id}_{tray_id}"

def getAMSFromTray(n):
    return n // 4


def normalize_color_hex(color_hex):
  if not color_hex or isinstance(color_hex, list):
    return ""

  color = color_hex.strip().upper()
  if color.startswith("#"):
    color = color[1:]

  if len(color) == 8:
    color = color[:6]

  if len(color) < 6:
    return ""

  return color[:6]


def color_distance(color1, color2):
  c1 = normalize_color_hex(color1)
  c2 = normalize_color_hex(color2)

  if not c1 or not c2:
    return None

  r1, g1, b1 = (int(c1[i:i+2], 16) for i in (0, 2, 4))
  r2, g2, b2 = (int(c2[i:i+2], 16) for i in (0, 2, 4))

  return math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2)

def augmentTrayDataWithSpoolMan(spool_list, tray_data, tray_id):
  tray_data["matched"] = False
  tray_data["mismatch"] = False
  tray_data["issue"] = False
  tray_data["color_mismatch"] = False
  tray_data["color_mismatch_message"] = ""

  for spool in spool_list:
    if spool.get("extra") and spool["extra"].get("active_tray") and spool["extra"]["active_tray"] == json.dumps(tray_id):
      tray_data["name"] = spool["filament"]["name"]
      tray_data["vendor"] = spool["filament"]["vendor"]["name"]
      tray_data["spool_material"] = spool["filament"].get("material", "")
      tray_data["spool_sub_brand"] = (spool["filament"].get("extra", {}).get("type") or "").replace('"', '').strip()
      tray_data["remaining_weight"] = spool["remaining_weight"]


      if "last_used" in spool:
        try:
            dt = datetime.strptime(spool["last_used"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            dt = datetime.strptime(spool["last_used"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=ZoneInfo("UTC"))

        local_time = dt.astimezone()
        tray_data["last_used"] = local_time.strftime("%d.%m.%Y %H:%M:%S")
      else:
          tray_data["last_used"] = "-"

      if "multi_color_hexes" in spool["filament"]:
        tray_data["spool_color"] = spool["filament"]["multi_color_hexes"]
        tray_data["spool_color_orientation"] = spool["filament"].get("multi_color_direction")
      else:
        tray_data["spool_color"] = normalize_color_hex(spool["filament"].get("color_hex") or "")
        tray_data.pop('spool_color_orientation', None)

      tray_material = (tray_data.get("tray_type") or "").strip().lower()
      spool_material = (spool["filament"].get("material") or "").strip().lower()
      material_mismatch = bool(tray_material and spool_material and tray_material != spool_material)

      tray_sub_brand = (tray_data.get("tray_sub_brands") or "").replace("Basic","").strip().lower()
      filament_sub_brand =  tray_data["spool_sub_brand"].replace("Basic", "").strip().lower()

      if tray_sub_brand:
        filament_sub_brand = (spool_material + " " + filament_sub_brand).strip()

      sub_brand_mismatch = bool(tray_sub_brand and tray_sub_brand != filament_sub_brand)

      tray_data["tray_sub_brand"] = tray_data.get("tray_sub_brands", "").replace(tray_data.get("tray_type", ""), '').replace("Basic","").strip()
      tray_data["spool_sub_brand"] = tray_data["spool_sub_brand"].replace("Basic", '').strip()
      tray_data["mismatch"] = material_mismatch or sub_brand_mismatch
      tray_data["issue"] = tray_data["mismatch"]
      tray_data["matched"] = True

      if "multi_color_hexes" not in spool["filament"]:
        color_difference = color_distance(tray_data.get("tray_color"), tray_data["spool_color"] )
        if  color_difference is not None and color_difference > COLOR_DISTANCE_TOLERANCE:
          tray_data["color_mismatch"] = True
          tray_data["color_mismatch_message"] = "Colors are not similar."

      break

  if tray_data.get("tray_type") and tray_data["tray_type"] != "" and tray_data["matched"] == False:
    tray_data["issue"] = True

def spendFilaments(printdata):
  if printdata["ams_mapping"]:
    ams_mapping = printdata["ams_mapping"]
  else:
    ams_mapping = [EXTERNAL_SPOOL_ID]

  """
  "ams_mapping": [
            1,
            0,
            -1,
            -1,
            -1,
            1,
            0
        ],
  """
  tray_id = EXTERNAL_SPOOL_ID
  ams_id = EXTERNAL_SPOOL_AMS_ID
  
  ams_usage = []
  for filamentId, filament in printdata["filaments"].items():
    if ams_mapping[0] != EXTERNAL_SPOOL_ID:
      tray_id = ams_mapping[filamentId - 1]   # get tray_id from ams_mapping for filament
      ams_id = getAMSFromTray(tray_id)        # caclulate ams_id from tray_id
      tray_id = tray_id - ams_id * 4          # correct tray_id for ams
    
    #if ams_usage.get(trayUid(ams_id, tray_id)):
    #    ams_usage[trayUid(ams_id, tray_id)]["usedGrams"] += float(filament["used_g"])
    #else:
    ams_usage.append({"trayUid": trayUid(ams_id, tray_id), "id": filamentId, "usedGrams":float(filament["used_g"])})

  for spool in fetchSpools():
    #TODO: What if there is a mismatch between AMS and SpoolMan?
                 
    if spool.get("extra") and spool.get("extra").get("active_tray"):
      #filament = ams_usage.get()
      active_tray = json.loads(spool.get("extra").get("active_tray"))

      # iterate over all ams_trays and set spool in print history, at the same time sum the usage for the tray and consume it from the spool
      used_grams = 0
      for ams_tray in ams_usage:
        if active_tray == ams_tray["trayUid"]:
          used_grams += ams_tray["usedGrams"]
          update_filament_spool(printdata["print_id"], ams_tray["id"], spool["id"])
        
      if used_grams != 0:
        consumeSpool(spool["id"], used_grams)
        

def setActiveTray(spool_id, spool_extra, ams_id, tray_id):
  if spool_extra == None:
    spool_extra = {}

  if not spool_extra.get("active_tray") or json.loads(spool_extra.get("active_tray")) != trayUid(ams_id, tray_id):
    patchExtraTags(spool_id, spool_extra, {
      "active_tray": json.dumps(trayUid(ams_id, tray_id)),
    })

    # Remove active tray from inactive spools
    for old_spool in fetchSpools(cached=True):
      if spool_id != old_spool["id"] and old_spool.get("extra") and old_spool["extra"].get("active_tray") and json.loads(old_spool["extra"]["active_tray"]) == trayUid(ams_id, tray_id):
        patchExtraTags(old_spool["id"], old_spool["extra"], {"active_tray": json.dumps("")})
  else:
    print("Skipping set active tray")

# Fetch spools from spoolman
def fetchSpools(cached=False):
  global SPOOLS
  if not cached or not SPOOLS:
    SPOOLS = fetchSpoolList()
    
    for spool in SPOOLS:
      initial_weight = 0

      if "initial_weight" in spool and spool["initial_weight"] > 0 :
        initial_weight = spool["initial_weight"]
      elif "weight" in spool["filament"] and spool["filament"]["weight"] > 0:
        initial_weight = spool["filament"]["weight"]

      price = 0
      if "price" in spool and spool["price"] > 0:
        price = spool["price"]
      elif "price" in spool["filament"] and spool["filament"]["price"] > 0:
        price = spool["filament"]["price"]

      if initial_weight > 0 and price > 0:
        spool["cost_per_gram"] = price / initial_weight
      else:
        spool["cost_per_gram"] = 0

      if "multi_color_hexes" in spool["filament"]:
        spool["filament"]["multi_color_hexes"] = spool["filament"]["multi_color_hexes"].split(',')
        
  return SPOOLS

def getSettings(cached=False):
  global SPOOLMAN_SETTINGS
  if not cached or not SPOOLMAN_SETTINGS:
    SPOOLMAN_SETTINGS = fetchSettings()
    SPOOLMAN_SETTINGS['currency_symbol'] = get_currency_symbol(SPOOLMAN_SETTINGS["currency"])

  return SPOOLMAN_SETTINGS
