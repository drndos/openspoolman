import requests
from config import SPOOLMAN_API_URL


def patchExtraTags(spool_id, old_extras, new_extras):
  for key, value in new_extras.items():
    old_extras[key] = value

  resp = requests.patch(f"{SPOOLMAN_API_URL}/spool/{spool_id}", json={
    "extra": old_extras
  })
  print(resp.text)
  print(resp.status_code)


def getSpoolById(spool_id):
  response = requests.get(f"{SPOOLMAN_API_URL}/spool/{spool_id}")
  print(response.status_code)
  print(response.text)
  return response.json()


def fetchSpoolList():
  response = requests.get(f"{SPOOLMAN_API_URL}/spool")
  print(response.status_code)
  print(response.text)
  return response.json()

def consumeSpool(spool_id, use_weight):
  print(f'Consuming {use_weight} from spool {spool_id}')

  response = requests.put(f"{SPOOLMAN_API_URL}/spool/{spool_id}/use", json={
    "use_weight": use_weight
  })
  print(response.status_code)
  print(response.text)
