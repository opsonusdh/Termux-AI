# Orion Tools Package

from .tool_wrappers import notify, toast, dialog, tts_speak
from .wrapper_termux_battery_status import get_battery_status
from .wrapper_termux_wifi_scaninfo import get_wifi_scan_info
from .wrapper_termux_clipboard import get_clipboard, set_clipboard
from .wrapper_termux_telephony import get_telephony_device_info
from .wrapper_termux_vibrate import vibrate
from .wrapper_termux_volume import get_volume_info, set_volume
from .wrapper_termux_torch import toggle_torch
from .wrapper_termux_location import get_location
from .wrapper_termux_brightness import set_brightness
from .wrapper_termux_sms import get_sms_messages, send_sms
from .wrapper_termux_contacts import get_contacts, get_contact_by_id
from .wrapper_termux_sensors import list_sensors, get_sensor_data, start_sensor_stream
from .wrapper_termux_wallpaper import set_wallpaper, get_wallpaper
from .wrapper_termux_camera import take_photo, record_video, get_camera_info
from .wrapper_termux_audio import start_recording, stop_recording, record_audio
from .wrapper_termux_filepicker import pick_file, share_file, share_text

# Re-export core tools functions
import sys
import os
import importlib.util
_core_path = os.path.join(os.path.dirname(__file__), '..', 'core')
if _core_path not in sys.path:
    sys.path.insert(0, _core_path)

# Import from the core tools module
_tools_spec = importlib.util.spec_from_file_location("core_tools", os.path.join(_core_path, "tools.py"))
_core_tools = importlib.util.module_from_spec(_tools_spec)
_tools_spec.loader.exec_module(_core_tools)

# Now extract all the functions we need
log_write = _core_tools.log_write
wa_log_write = _core_tools.wa_log_write
build_memory_block = _core_tools.build_memory_block
save_memory = _core_tools.save_memory
retrieve_memory = _core_tools.retrieve_memory
read_file = _core_tools.read_file
write_file = _core_tools.write_file
index_files = _core_tools.index_files
web_scrape = _core_tools.web_scrape
sleep_mode = _core_tools.sleep_mode
intermediate_print = _core_tools.intermediate_print
send_whatsapp_message = _core_tools.send_whatsapp_message
get_whatsapp_status = _core_tools.get_whatsapp_status
get_whatsapp_chats = _core_tools.get_whatsapp_chats
get_pending_whatsapp_messages = _core_tools.get_pending_whatsapp_messages
fetch_whatsapp_chat_history = _core_tools.fetch_whatsapp_chat_history
set_whatsapp_busy_mode = _core_tools.set_whatsapp_busy_mode
get_whatsapp_report = _core_tools.get_whatsapp_report
set_whatsapp_user_profile = _core_tools.set_whatsapp_user_profile
archive_whatsapp_chat = _core_tools.archive_whatsapp_chat
search_whatsapp_chat = _core_tools.search_whatsapp_chat
get_whatsapp_group_participants = _core_tools.get_whatsapp_group_participants
get_whatsapp_contact_info = _core_tools.get_whatsapp_contact_info
react_to_whatsapp_message = _core_tools.react_to_whatsapp_message
download_whatsapp_media = _core_tools.download_whatsapp_media
schedule_whatsapp_message = _core_tools.schedule_whatsapp_message
silence_whatsapp_contact = _core_tools.silence_whatsapp_contact
set_whatsapp_seen = _core_tools.set_whatsapp_seen
delegate_subtask = _core_tools.delegate_subtask
generate_image = _core_tools.generate_image
list_directory = _core_tools.list_directory
search_files = _core_tools.search_files
rename_file = _core_tools.rename_file
delete_file = _core_tools.delete_file
http_request = _core_tools.http_request
get_datetime = _core_tools.get_datetime
run_diagnosis = _core_tools.run_diagnosis
ask_ai_simple = _core_tools.ask_ai_simple
search_in_files = _core_tools.search_in_files
_dispatch_sub_tool = _core_tools._dispatch_sub_tool
TOOLS_DESCRIPTION = _core_tools.TOOLS_DESCRIPTION
ask_user = _core_tools.ask_user
confirm = _core_tools.confirm

__all__ = [
    'notify',
    'toast',
    'dialog',
    'tts_speak',
    'get_battery_status',
    'get_wifi_scan_info',
    'get_clipboard',
    'set_clipboard',
    'get_telephony_device_info',
    'vibrate',
    'get_volume_info',
    'set_volume',
    'toggle_torch',
    'get_location',
    'set_brightness',
    'get_sms_messages',
    'send_sms',
    'get_contacts',
    'get_contact_by_id',
    'list_sensors',
    'get_sensor_data',
    'start_sensor_stream',
    'set_wallpaper',
    'get_wallpaper',
    'take_photo',
    'record_video',
    'get_camera_info',
    'start_recording',
    'stop_recording',
    'record_audio',
    'pick_file',
    'share_file',
    'share_text',
    # Core tools
    'log_write',
    'wa_log_write',
    'build_memory_block',
    'save_memory',
    'retrieve_memory',
    'read_file',
    'write_file',
    'index_files',
    'web_scrape',
    'sleep_mode',
    'intermediate_print',
    'send_whatsapp_message',
    'get_whatsapp_status',
    'get_whatsapp_chats',
    'get_pending_whatsapp_messages',
    'fetch_whatsapp_chat_history',
    'set_whatsapp_busy_mode',
    'get_whatsapp_report',
    'set_whatsapp_user_profile',
    'archive_whatsapp_chat',
    'search_whatsapp_chat',
    'get_whatsapp_group_participants',
    'get_whatsapp_contact_info',
    'react_to_whatsapp_message',
    'download_whatsapp_media',
    'schedule_whatsapp_message',
    'silence_whatsapp_contact',
    'set_whatsapp_seen',
    'delegate_subtask',
    'generate_image',
    'list_directory',
    'search_files',
    'rename_file',
    'delete_file',
    'http_request',
    'get_datetime',
    'run_diagnosis',
    'ask_ai_simple',
    'search_in_files',
    'TOOLS_DESCRIPTION',
    'ask_user',
    'confirm',
]