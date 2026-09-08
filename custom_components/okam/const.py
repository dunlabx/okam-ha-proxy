"""Constants for the O-KAM Native Bridge integration."""

DOMAIN = "okam"
PLATFORMS = ["camera"]

CONF_BRIDGE_URL = "bridge_url"
CONF_API_TOKEN = "api_token"
CONF_CAMERA_ID = "camera_id"
CONF_CAMERA_UID = "camera_uid"
CONF_AUTH_METHOD = "auth_method"
CONF_CAMERA_PASSWORD = "camera_password"
CONF_IDLE_TIMEOUT = "idle_timeout"
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"

DEFAULT_IDLE_TIMEOUT = 120
DEFAULT_SNAPSHOT_INTERVAL = 900
DEFAULT_CAMERA_ID = "cabin"
DEFAULT_AUTH_METHOD = "automatic"
