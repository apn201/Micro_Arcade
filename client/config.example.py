# Copy to config.py on the device and fill in. config.py is gitignored so
# credentials never end up in the repo or the project write-up.

WIFI_SSID = "your-ssid"
WIFI_PASS = "your-password"

SERVER_HOST = "192.168.1.10"   # IP or hostname of the DOOM server
SERVER_PORT = 20002
TOKEN = ""                     # must match the server's --md-token

# Display
ROTATION = 0                   # 0-3, rotate if the cabinet mounts the board sideways
WIDTH = 128
HEIGHT = 128

# How often input is sent, in milliseconds. 33ms = 30Hz. Lower means crisper
# control and more packets; the server also uses these as its liveness signal.
INPUT_PERIOD_MS = 33

# Optional GPIO joystick, wired active-low to the bottom header. Leave any of
# these as None until the switches exist. The server needs no changes: these
# just set the movement bits the tilt path would otherwise set.
PIN_UP = None
PIN_DOWN = None
PIN_LEFT = None
PIN_RIGHT = None
PIN_FIRE = None                # dedicated fire button, in addition to the onboard one
PIN_USE = None

# Optional piezo on a PWM-capable pin. The server never streams audio; it only
# says what happened, and the device makes its own noise. For now the only cue
# is the local one: a click when you pull the trigger.
PIN_PIEZO = None
PIEZO_FIRE_HZ = 220
PIEZO_FIRE_MS = 25

DEBUG = False                  # print packet/frame stats to the serial console
