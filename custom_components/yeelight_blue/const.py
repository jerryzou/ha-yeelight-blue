"""Constants for the Yeelight Blue II integration.

Protocol (ASCII over GATT service FFF0, no pairing or encryption):
  FFF1 write  "r,g,b,brightness"        RGB, brightness 0-100 (0 = off)
              ",,,brightness"           keep colour, change brightness / power
              "CLTMP kelvin,brightness," colour temperature 1700-6500 K
              every payload padded with "," to 18 bytes
  FFF5 write  "S"                       status query
  FFF6 notify "r,g,b,brightness,cf,dl," or "CLTMP kelvin,brightness,"
"""

DOMAIN = "yeelight_blue"

CHAR_CONTROL = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_STATUS_QUERY = "0000fff5-0000-1000-8000-00805f9b34fb"
CHAR_STATUS_NOTIFY = "0000fff6-0000-1000-8000-00805f9b34fb"

PAYLOAD_LEN = 18
MIN_KELVIN = 1700
MAX_KELVIN = 6500

# Keep the link up this long after the last command so slider drags and
# back-to-back automation steps don't reconnect each time, then free the slot.
IDLE_DISCONNECT_S = 30
STATUS_TIMEOUT_S = 3
STOP_NOTIFY_TIMEOUT_S = 2
# Generous: the bulb seems to answer a write only after its fade, and proxy links add latency.
WRITE_TIMEOUT_S = 10
POLL_INTERVAL_S = 300
# Bulb advertises ~1/s; this much silence while not connected = power cut.
ADVERT_SILENCE_S = 60
WATCHDOG_S = 15
# A failed command is retried via a status poll this soon (then every poll).
RETRY_AFTER_FAIL_S = 60
# Check that a write-without-response landed, after the bulb's fade.
VERIFY_AFTER_S = 5
# Cap on one turn_on/turn_off (connect retries can take a minute+ on a weak link).
COMMAND_TIMEOUT_S = 15
