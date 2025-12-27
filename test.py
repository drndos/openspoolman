import json
import re
from dotenv import load_dotenv
from mqtt_bambulab import processMessage 
from logger import log

load_dotenv()

def run_test():
  i = 1
  with open("mqtt.log", "r", encoding="utf-8") as file:
    for line in file:
        cleaned_line = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} :: ", "", line.strip())
        log("row "+ str(i))
        i= i + 1
        processMessage(json.loads(cleaned_line))


run_test()
