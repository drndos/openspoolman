import json
from types import SimpleNamespace

import pytest
import requests

import mqtt_bambulab
import spoolman_client


BAMBU_TAG = "05E711D60D104B6FB092A6ECC895D6ED"
OTHER_TAG = "C1FCFCDE15C74FE0ACFA7C442D761E53"
TAGGED_SPOOL = {"id": 15, "extra": {"tag": json.dumps(BAMBU_TAG)}}
UNTAGGED_SPOOL = {"id": 1, "extra": {}}


def _status_report(*tray_uuids):
  trays = [{"id": str(i), "tray_sub_brands": "PLA Matte", "tray_color": "FFFFFFFF", "remain": 6, "tray_uuid": uuid}
           for i, uuid in enumerate(tray_uuids)]
  payload = {"print": {"ams": {"ams": [{"id": "0", "humidity": "1", "temp": "30.0", "tray": trays}]}}}
  return SimpleNamespace(payload=json.dumps(payload).encode())


@pytest.fixture
def printer(monkeypatch):
  """Stubs SpoolMan and the printer; records what on_message does with a tray."""
  calls = {"uncached_fetches": 0, "active": [], "assignment_cleared": [], "tray_resets": []}
  state = {"cached": [UNTAGGED_SPOOL], "fresh": [UNTAGGED_SPOOL], "spoolman_down": False}

  def fetch_spools(cached=False):
    if not cached:
      calls["uncached_fetches"] += 1
      if state["spoolman_down"]:
        raise requests.ConnectionError("SpoolMan unreachable")
      state["cached"] = list(state["fresh"])
    return list(state["cached"])

  monkeypatch.setattr(mqtt_bambulab, "AUTO_SPEND", False)
  monkeypatch.setattr(mqtt_bambulab, "append_to_rotating_file", lambda *args, **kwargs: None)
  monkeypatch.setattr(mqtt_bambulab, "fetchSpools", fetch_spools)
  monkeypatch.setattr(mqtt_bambulab, "setActiveTray",
                      lambda spool_id, extra, ams_id, tray_id: calls["active"].append(spool_id))
  monkeypatch.setattr(mqtt_bambulab, "clear_active_spool_for_tray",
                      lambda ams_id, tray_id: calls["assignment_cleared"].append((ams_id, tray_id)))
  monkeypatch.setattr(mqtt_bambulab, "clear_ams_tray_assignment",
                      lambda ams_id, tray_id: calls["tray_resets"].append((ams_id, tray_id)))
  monkeypatch.setattr(mqtt_bambulab, "LAST_TAG_REFETCH", None)
  monkeypatch.setattr(mqtt_bambulab, "CLEARED_UNMAPPED_TRAYS", {})
  return SimpleNamespace(calls=calls, state=state)


def test_tag_added_after_cache_fill_is_found(printer):
  # The cache was filled before the tagged spool existed in SpoolMan.
  printer.state["fresh"] = [UNTAGGED_SPOOL, TAGGED_SPOOL]

  mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))

  assert printer.calls["active"] == [15]
  assert printer.calls["assignment_cleared"] == []


def test_unknown_tag_clears_assignment_once_and_never_resets_the_tray(printer):
  for _ in range(5):
    mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))

  assert printer.calls["assignment_cleared"] == [("0", "0")]
  assert printer.calls["tray_resets"] == []  # the printer read the tray from RFID
  assert printer.calls["uncached_fetches"] == 1


def test_different_unknown_tag_in_same_tray_clears_again(printer):
  mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))
  mqtt_bambulab.on_message(None, None, _status_report(OTHER_TAG))

  assert printer.calls["assignment_cleared"] == [("0", "0"), ("0", "0")]


def test_non_bambu_spool_is_not_looked_up_or_cleared(printer):
  mqtt_bambulab.on_message(None, None, _status_report(mqtt_bambulab.EMPTY_TRAY_UUID))

  assert printer.calls["uncached_fetches"] == 0
  assert printer.calls["assignment_cleared"] == []


def test_failed_refetch_leaves_tray_alone_and_other_trays_are_still_matched(printer):
  printer.state["cached"] = [UNTAGGED_SPOOL, TAGGED_SPOOL]
  printer.state["spoolman_down"] = True

  # Slot 0 holds a tag the cache doesn't know; slot 1 holds a cached one.
  mqtt_bambulab.on_message(None, None, _status_report(OTHER_TAG, BAMBU_TAG))

  assert printer.calls["assignment_cleared"] == []
  assert printer.calls["active"] == [15]


def test_spool_list_fetch_has_a_timeout(monkeypatch):
  seen = {}

  def fake_get(url, **kwargs):
    seen.update(kwargs)
    return SimpleNamespace(json=lambda: [])

  monkeypatch.setattr(spoolman_client.requests, "get", fake_get)
  spoolman_client.fetchSpoolList()

  assert seen.get("timeout") == spoolman_client.SPOOLMAN_TIMEOUT_S


def test_refetches_again_once_the_interval_has_passed(printer, monkeypatch):
  clock = {"now": 1000.0}
  monkeypatch.setattr(mqtt_bambulab.time, "monotonic", lambda: clock["now"])

  mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))
  clock["now"] += mqtt_bambulab.TAG_REFETCH_INTERVAL_S / 2
  mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))
  assert printer.calls["uncached_fetches"] == 1

  # The spool is added in SpoolMan after the first miss; the next refetch finds it.
  printer.state["fresh"] = [UNTAGGED_SPOOL, TAGGED_SPOOL]
  clock["now"] += mqtt_bambulab.TAG_REFETCH_INTERVAL_S
  mqtt_bambulab.on_message(None, None, _status_report(BAMBU_TAG))

  assert printer.calls["uncached_fetches"] == 2
  assert printer.calls["active"] == [15]
