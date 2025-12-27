# mapping adapted from https://github.com/spuder/OpenSpool/blob/main/firmware/conf.d/automation.yaml
from logger import log
def generate_filament_brand_code(filament_type, filament_brand, filament_variant):
  filament_sub_brand = ""
  filament_brand_code = ""

  if filament_type == "TPU":
    if filament_brand == "Bambu":
      filament_brand_code = "GFU01"
      filament_sub_brand = "TPU 95A"
    else:
      filament_brand_code = "GFU99"
      filament_sub_brand = "TPU"

  elif filament_type == "PLA":
    if filament_brand == "PolyTerra":
      filament_brand_code = "GFL01"
      filament_sub_brand = "PolyTerra PLA"
    elif filament_brand == "PolyLite":
      filament_brand_code = "GFL00"
      filament_sub_brand = "PolyLite PLA"
    elif filament_brand == "Bambu":
      if filament_variant == "Basic":
        filament_brand_code = "GFA00"
        filament_sub_brand = "PLA Basic"
      elif filament_variant == "Matte":
        filament_brand_code = "GFA01"
        filament_sub_brand = "PLA Matte"
      elif filament_variant == "Metal":
        filament_brand_code = "GFA02"
        filament_sub_brand = "PLA Metal"
      elif filament_variant == "Impact":
        filament_brand_code = "GFA03"
        filament_sub_brand = "PLA Impact"
      else:
        filament_brand_code = "GFA00"
        filament_sub_brand = "PLA Basic"
    else:
      filament_brand_code = "GFL99"
      filament_sub_brand = "PLA"

  elif filament_type == "PETG":
    if filament_brand == "Overture":
      filament_brand_code = "GFG99"  # Placeholder code
      filament_sub_brand = "PETG"
    else:
      filament_brand_code = "GFG99"
      filament_sub_brand = "PETG"

  elif filament_type == "PET-CF":
    if filament_brand == "Bambu":
      filament_brand_code = "GFT00"
      filament_sub_brand = "PET-CF"
    else:
      filament_brand_code = "GFG99"
      filament_sub_brand = "PET-CF"

  elif filament_type == "ASA":
    filament_brand_code = "GFB98"
    filament_sub_brand = "ASA"

  elif filament_type == "ABS":
    if filament_brand == "Bambu":
      filament_brand_code = "GFB00"
      filament_sub_brand = "ABS"
    else:
      filament_brand_code = "GFB99"
      filament_sub_brand = "ABS"

  elif filament_type == "PC":
    if filament_brand == "Bambu":
      filament_brand_code = "GFC00"
      filament_sub_brand = "PC"
    else:
      filament_brand_code = "GFC99"
      filament_sub_brand = "PC"

  elif filament_type == "PA":
    filament_brand_code = "GFN99"
    filament_sub_brand = "PA"

  elif filament_type == "PA-CF":
    if filament_brand == "Bambu":
      filament_brand_code = "GFN03"
      filament_sub_brand = "PA-CF"
    else:
      filament_brand_code = "GFN98"
      filament_sub_brand = "PA-CF"

  elif filament_type == "PLA-CF":
    filament_brand_code = "GFL98"
    filament_sub_brand = "PLA-CF"

  elif filament_type == "PVA":
    filament_brand_code = "GFS99"
    filament_sub_brand = "PVA"

  elif filament_type == "Support":
    if filament_variant == "G":
      filament_brand_code = "GFS01"
      filament_sub_brand = "Support G"
    elif filament_variant == "W":
      filament_brand_code = "GFS00"
      filament_sub_brand = "Support W"
    else:
      filament_brand_code = "GFS00"
      filament_sub_brand = "Support W"
  else:
    log(f"Unknown filament type: {filament_type}")

  return {"brand_code": filament_brand_code,
          "sub_brand_code": filament_sub_brand
          }


def generate_filament_temperatures(filament_type, filament_brand):
  filament_min_temp = 150
  filament_max_temp = 300

  if not filament_type:
    log("Skipping temperature generation as filament_type is empty.")
    return

  if filament_type == "TPU":
    if filament_brand == "Generic":
      filament_min_temp = 200
      filament_max_temp = 250
    else:
      log(f"Unknown temperatures for TPU brand: {filament_brand}")
      filament_min_temp = 200
      filament_max_temp = 250
  elif filament_type == "PLA":
    if filament_brand == "Generic":
      filament_min_temp = 190
      filament_max_temp = 240
    else:
      log(f"Unknown temperatures for PLA brand: {filament_brand}")
      filament_min_temp = 190
      filament_max_temp = 240
  elif filament_type == "PETG":
    if filament_brand == "Generic":
      filament_min_temp = 220
      filament_max_temp = 270
    else:
      log(f"Unknown temperatures for PETG brand: {filament_brand}")
      filament_min_temp = 220
      filament_max_temp = 270
  elif filament_type == "ASA":
    if filament_brand == "Generic":
      filament_min_temp = 240
      filament_max_temp = 280
    else:
      log(f"Unknown temperatures for ASA brand: {filament_brand}")
      filament_min_temp = 240
      filament_max_temp = 280

  elif filament_type == "PC":
    if filament_brand == "Generic":
      filament_min_temp = 250
      filament_max_temp = 300
    else:
      log(f"Unknown temperatures for PC brand: {filament_brand}")
      filament_min_temp = 250
      filament_max_temp = 300


  elif filament_type == "PA":
    if filament_brand == "Generic":
      filament_min_temp = 260
      filament_max_temp = 300
    else:
      log(f"Unknown temperatures for PA brand: {filament_brand}")
      filament_min_temp = 260
      filament_max_temp = 300
  else:
    log(f"Unknown filament type: {filament_type}")

  return {"filament_min_temp": filament_min_temp,
          "filament_max_temp": filament_max_temp
          }
