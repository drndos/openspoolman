import requests
import zipfile
import tempfile
import xml.etree.ElementTree as ET
import pycurl
import urllib.parse
import os
import re
import time
import io
from datetime import datetime
from config import PRINTER_CODE, PRINTER_IP, PRINTER_NAME, DOWNLOADED_FILES
from urllib.parse import urlparse
from logger import log

def parse_ftp_listing(line):
    """Parse a line from an FTP LIST command."""
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    return {
        'permissions': parts[0],
        'links': int(parts[1]),
        'owner': parts[2],
        'group': parts[3],
        'size': int(parts[4]),
        'month': parts[5],
        'day': int(parts[6]),
        'time_or_year': parts[7],
        'name': parts[8]
    }

def get_base_name(filename):
    return filename.rsplit('.', 1)[0]

def parse_date(item):
    """Parse the date and time from the FTP listing item."""
    try:
        date_str = f"{item['month']} {item['day']} {item['time_or_year']}"
        return datetime.strptime(date_str, "%b %d %H:%M")
    except ValueError:
        return None

def get_filament_order(file):
    filament_order = {} 
    switch_count = 0 

    for line in file:
        match_filament = re.match(r"^M620 S(\d+)[^;\r\n]*", line.decode("utf-8").strip())
        if match_filament:
            filament = int(match_filament.group(1))
            if filament not in filament_order and int(filament) != 255:
                filament_order[int(filament)] = switch_count
            switch_count += 1

    if len(filament_order) == 0:
       filament_order = {1:0}

    return filament_order

def download3mfFromCloud(url, destFile):
  log("Downloading 3MF file from cloud...")
  # Download the file and save it to the temporary file
  response = requests.get(url)
  response.raise_for_status()
  destFile.write(response.content)

def download3mfFromFTP(filename, destFile):
  log("Downloading 3MF file from FTP...")
  ftp_host = PRINTER_IP
  ftp_user = "bblp"
  ftp_pass = PRINTER_CODE
  remote_path = "/cache/" + filename
  local_path = destFile.name  # 🔹 Download into the current directory
  encoded_remote_path = urllib.parse.quote(remote_path)
  
  url = f"ftps://{ftp_host}{encoded_remote_path}"

  

  log(f"[DEBUG] Attempting file download of: {remote_path}") #Log attempted path

  # Setup a retry loop
  # Try to prevent race condition where trying to access file before it is fully in cache, causing File not found errors
  max_retries = 3
  for attempt in range(1, max_retries + 1):
    with open(local_path, "wb") as f:
      c = setupPycurlConnection(ftp_user, ftp_pass)
      try:
        c.setopt(c.URL, url)
        # Set output to file
        c.setopt(c.WRITEDATA, f)
        log(f"[DEBUG] Attempt {attempt}: Starting download of {filename}...")
                
        # Perform the transfer
        c.perform()
                
        log("[DEBUG] File successfully downloaded!")
        c.close()
        return True # Exit function on success

    # Error, check its just a file not found error before retry
      except pycurl.error as e:
        err_code = e.args[0]
        c.close()
        if err_code == 78: # File Not Found
          if attempt < max_retries:
            log(f"[WARNING] File not found. Printer might still be writing. Retrying in 1s...")
            time.sleep(2)
            continue 
          else:
            log("[ERROR] File not found after max retries.")
            log("[DEBUG] Listing found printer files in /cache directory")
            buffer = io.BytesIO()
            c = setupPycurlConnection(ftp_user, ftp_pass)
            c.setopt(c.URL, f"ftps://{ftp_host}/cache/")
            c.setopt(c.WRITEDATA, buffer)
            c.setopt(c.DIRLISTONLY, True)
            try:
                c.perform()
                log(f"[DEBUG] Directory Listing: {buffer.getvalue().decode('utf-8').splitlines()}")
            except:
                log("[ERROR] Could not retrieve directory listing.")
        # Check if external storage not setup or connected. /cache is denied access
        if err_code == 9: # Server denied you to change to the given directory
          log("[DEBUG] Printer denied access to /cache path. Ensure external storage is setup to store print files in printer settings.")
          break
        else:
          log(f"[ERROR] Fatal cURL error {err_code}: {e}")
          break # Don't retry for non-78 File Not Found errors

def setupPycurlConnection(ftp_user, ftp_pass):
  # Setup shared options for curl connections
  c = pycurl.Curl()

  # 🔹 Setup explicit FTPS connection (like FileZilla)
  
  c.setopt(c.USERPWD, f"{ftp_user}:{ftp_pass}")
  
    
  # 🔹 Enable SSL/TLS
  c.setopt(c.SSL_VERIFYPEER, 0)  # Disable SSL verification
  c.setopt(c.SSL_VERIFYHOST, 0)
    
  # 🔹 Enable passive mode (like FileZilla)
  c.setopt(c.FTP_SSL, c.FTPSSL_ALL)
    
  # 🔹 Enable proper TLS authentication
  c.setopt(c.FTPSSLAUTH, c.FTPAUTH_TLS)

  return c

def download3mfFromLocalFilesystem(path, destFile):
  with open(path, "rb") as src_file:
    destFile.write(src_file.read())

def getMetaDataFrom3mf(url):
  """
  Download a 3MF file from a URL, unzip it, and parse filament usage.

  Args:
      url (str): URL to the 3MF file.

  Returns:
      list[dict]: List of dictionaries with `tray_info_idx` and `used_g`.
  """
  try:
    metadata = {}

    # Retrieve the model
    temp_file_name = retrieveModel(url)
      
    
    metadata["model_path"] = url

    parsed_url = urlparse(url)
    metadata["file"] = os.path.basename(parsed_url.path)

    

    # Unzip the 3MF file
    with zipfile.ZipFile(temp_file_name, 'r') as z:
      # Check for the Metadata/slice_info.config file
      slice_info_path = "Metadata/slice_info.config"
      if slice_info_path in z.namelist():
        with z.open(slice_info_path) as slice_info_file:
          # Parse the XML content of the file
          tree = ET.parse(slice_info_file)
          root = tree.getroot()

          # Extract id and used_g from each filament
          """
          <?xml version="1.0" encoding="UTF-8"?>
          <config>
            <header>
              <header_item key="X-BBL-Client-Type" value="slicer"/>
              <header_item key="X-BBL-Client-Version" value="01.10.01.50"/>
            </header>
            <plate>
              <metadata key="index" value="1"/>
              <metadata key="printer_model_id" value="N2S"/>
              <metadata key="nozzle_diameters" value="0.4"/>
              <metadata key="timelapse_type" value="0"/>
              <metadata key="prediction" value="5450"/>
              <metadata key="weight" value="26.91"/>
              <metadata key="outside" value="false"/>
              <metadata key="support_used" value="false"/>
              <metadata key="label_object_enabled" value="true"/>
              <object identify_id="930" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1030" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1130" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1230" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1330" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1430" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1530" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1630" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1730" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1830" name="FILENAME.3mf" skipped="false" />
              <object identify_id="1930" name="FILENAME.3mf" skipped="false" />
              <object identify_id="2030" name="FILENAME.3mf" skipped="false" />
              <object identify_id="2130" name="FILENAME.3mf" skipped="false" />
              <object identify_id="2230" name="FILENAME.3mf" skipped="false" />
              <filament id="1" tray_info_idx="GFL99" type="PLA" color="#0DFF00" used_m="6.79" used_g="20.26" />
              <filament id="2" tray_info_idx="GFL99" type="PLA" color="#000000" used_m="0.72" used_g="2.15" />
              <filament id="6" tray_info_idx="GFL99" type="PLA" color="#0DFF00" used_m="1.20" used_g="3.58" />
              <filament id="7" tray_info_idx="GFL99" type="PLA" color="#000000" used_m="0.31" used_g="0.92" />
              <warning msg="bed_temperature_too_high_than_filament" level="1" error_code ="1000C001"  />
            </plate>
          </config>
          """
          
          for meta in root.findall(".//plate/metadata"):
            if meta.attrib.get("key") == "index":
                metadata["plateID"] = meta.attrib.get("value", "")

          usage = {}
          filaments= {}
          filamentId = 1
          for plate in root.findall(".//plate"):
            for filament in plate.findall(".//filament"):
              used_g = filament.attrib.get("used_g")
              #filamentId = int(filament.attrib.get("id"))
              
              usage[filamentId] = used_g
              filaments[filamentId] = {"id": filamentId,
                                        "tray_info_idx": filament.attrib.get("tray_info_idx"), 
                                        "type":filament.attrib.get("type"), 
                                        "color": filament.attrib.get("color"), 
                                        "used_g": used_g, 
                                        "used_m":filament.attrib.get("used_m")}
              filamentId += 1

          metadata["filaments"] = filaments
          metadata["usage"] = usage
      else:
        log(f"File '{slice_info_path}' not found in the archive.")
        return {}

      metadata["image"] = time.strftime('%Y%m%d%H%M%S') + ".png"

      with z.open("Metadata/plate_"+metadata["plateID"]+".png") as source_file:
        with open(os.path.join(os.getcwd(), 'static', 'prints', metadata["image"]), 'wb') as target_file:
            target_file.write(source_file.read())

      # Check for the Metadata/slice_info.config file
      gcode_path = "Metadata/plate_"+metadata["plateID"]+".gcode"
      metadata["gcode_path"] = gcode_path
      if gcode_path in z.namelist():
        with z.open(gcode_path) as gcode_file:
          metadata["filamentOrder"] =  get_filament_order(gcode_file)

      log(metadata)

      return metadata

  except requests.exceptions.RequestException as e:
    log(f"Error downloading file: {e}")
    return {}
  except zipfile.BadZipFile:
    log("The downloaded file is not a valid 3MF archive.")
    return {}
  except ET.ParseError:
    log("Error parsing the XML file.")
    return {}
  except Exception as e:
    log(f"An unexpected error occurred: {e}")
    return {}

# Reteive the model from temp file cache if we already have it, otherwise grab it from the printer.
def retrieveModel(url):
  if not url:
    log("[DEBUG] No URL supplied to retireve printer model")
    return None
  
  try:
    temp_file_name = ""

    # Check if we have already downloaded the file previously
    if not DOWNLOADED_FILES.get(f"{PRINTER_NAME}_{PRINTER_IP}"):
      log("[DEBUG] Don't already have the model in temp file cache")
    
      # Create a temporary file, DELETE is FALSE, make sure to cleanup when printing ends
      #TODO handle cleanup on crashes

      # Create OpenSpoolMan temp directory for easier/safer file cleanup
      temp_dir = os.path.join(tempfile.gettempdir(), "OpenSpoolMan")
      os.makedirs(temp_dir, exist_ok=True)

      with tempfile.NamedTemporaryFile(dir=temp_dir, delete_on_close=False,delete=False,prefix=f"{PRINTER_NAME}_{PRINTER_IP}_", suffix=".3mf") as temp_file:
        temp_file_name = temp_file.name
        
        if url.startswith("http"):
          download3mfFromCloud(url, temp_file)
        elif url.startswith("local:"):
          download3mfFromLocalFilesystem(url.replace("local:", ""), temp_file)
        else:
          download3mfFromFTP(url.rpartition('/')[-1], temp_file) # Pull just filename to clear out any unexpected paths
        
        temp_file.close()
        DOWNLOADED_FILES[f"{PRINTER_NAME}_{PRINTER_IP}"] = temp_file_name # Use Printer name + IP for future multiple printer support
        log(f"3MF file downloaded and saved as {temp_file_name}.")
    
    # Set to previously downloaded temp file
    else:
      temp_file_name = DOWNLOADED_FILES[f"{PRINTER_NAME}_{PRINTER_IP}"]
      log(f"[DEBUG] Using already cached temp file, path: {temp_file_name}")
    
    return temp_file_name
  
  except requests.exceptions.RequestException as e:
    log(f"Error downloading file: {e}")
    return None
  except Exception as e:
    log(f"An unexpected error occurred: {e}")
    return None

def clearTempFile(PRINTER_NAME, PRINTER_IP):
  
  temp_file_path = DOWNLOADED_FILES.pop(f"{PRINTER_NAME}_{PRINTER_IP}", None)

  if temp_file_path:
      if os.path.exists(temp_file_path):
          os.remove(temp_file_path)
      print(f"Temp file: {temp_file_path} cleared for printer: {PRINTER_NAME} {PRINTER_IP}.")

