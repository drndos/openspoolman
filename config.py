import os
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from config.env when present so live runs have access
# to printer and Spoolman credentials without manual exports.
load_dotenv(Path(__file__).resolve().parent / "config.env")
EXTERNAL_SPOOL_AMS_ID = 255 # don't change
EXTERNAL_SPOOL_ID = 254 #  don't change


BASE_URL = os.getenv('OPENSPOOLMAN_BASE_URL') # Where will this app be accessible
PRINTER_ID = (os.getenv('PRINTER_ID') or "").upper()  # Printer serial number - Run init_bambulab.py
PRINTER_CODE = os.getenv('PRINTER_ACCESS_CODE')       # Printer access code - Run init_bambulab.py
PRINTER_IP = os.getenv('PRINTER_IP')     # Printer local IP address - Check wireless on printer
PRINTER_NAME = os.getenv('PRINTER_NAME')     # Printer name - Check wireless on printer
SPOOLMAN_BASE_URL = os.getenv('SPOOLMAN_BASE_URL')
SPOOLMAN_API_URL = f"{SPOOLMAN_BASE_URL}/api/v1"
AUTO_SPEND = os.getenv('AUTO_SPEND', False)
TRACK_LAYER_USAGE = os.getenv('TRACK_LAYER_USAGE', False)
SPOOL_SORTING = os.getenv('SPOOL_SORTING', "filament.material:asc,filament.vendor.name:asc,filament.name:asc")
DISABLE_MISMATCH_WARNING = os.getenv("DISABLE_MISMATCH_WARNING", "false").lower() in ("1", "true", "yes", "on")
