from machine import I2C, Pin, UART
import time
import network
import socket
import machine
import sys
try:
    import ntptime
except:
    ntptime = None
try:
    import urequests
except:
    urequests = None

# I2C LCD (PCF8574 backpack)
I2C_SCL = 9
I2C_SDA = 8
LCD_ADDR = 0x27  # change to 0x3F if your backpack uses that

# RS485 (Modbus RTU) PT100 reader
RS485_TX_PIN = 0  # GPIO0 -> RS485 DI
RS485_RX_PIN = 1  # GPIO1 -> RS485 RO
RS485_BAUD = 9600
RS485_SLAVE = 1
RS485_FUNC = 0x03  # 0x03=holding registers, 0x04=input registers
RS485_REG = 0x0000
RS485_COUNT = 1

# Pulse counter (GPIO20)
PULSE_PIN = 20

# Wi-Fi credentials (from esp32-pzem-counter-v0.py)
DEFAULT_SSID = "TP-Link_5B9A"
DEFAULT_PASS = "97180937"
wifi_ssid = DEFAULT_SSID
wifi_pass = DEFAULT_PASS
wifi_mode = "dhcp"
wifi_ip = ""
wifi_gateway = ""
wifi_subnet = ""
CONFIG_FILE = "esp32c3_config.txt"
COUNTER_FILE = "counter_data.txt"
device_mac = "unknown"
counter_divider = 10
counter_send_divider = 10  # send counter data every N pulses (for upload)
counter_enabled = True
rs485_enabled = True
UPLOAD_HOST = "137.184.86.182"
UPLOAD_COUNTER_PATH = "iot2026/smart01/insert2C.php"
UPLOAD_TEMP_PATH = "iot2026/smart01/insertT.php"
UPLOAD_TEMP_INTERVAL_MS = 60_000  # default 60s
UPLOAD_DEVICE_ID = "smart01"
UPLOAD_PDID = "PO-001"
KFACTOR = 100  # percent (50-200)

# Timezone offset seconds (UTC+7 default)
TIME_OFFSET = 7 * 3600

EN = 0x04      # Enable bit
RS = 0x01      # Register select
BACKLIGHT = 0x08

# Global handles
i2c = None
uart = None
sock = None
ntp_synced = False
last_send_ms = 0
last_counter_send_ms = 0
last_send_status = "Never"
pulse_count = 0
pulse_accm = 0
pulse_cpm = 0           # displayed CPM (prev full minute, or first-minute live)
pulse_window_start = 0  # ms timestamp of current minute window
pulse_window_pulses = 0 # pulses counted in current minute window
pulse_cpm_prev = 0      # last completed minute CPM
has_prev_cpm = False
divider_counter = 0
counter_save_pending = False
counter_send_accum = 0
counter_send_pending = False
lcd_page = 0
lcd_page_timer = 0


# ---------------- LCD helpers ----------------
def _lcd_write4(bits, mode=0):
    data = mode | (bits & 0xF0) | BACKLIGHT
    i2c.writeto(LCD_ADDR, bytes([data | EN]))
    time.sleep_us(500)
    i2c.writeto(LCD_ADDR, bytes([data]))
    time.sleep_us(100)


def _lcd_write_byte(bits, mode=0):
    _lcd_write4(bits, mode)
    _lcd_write4(bits << 4, mode)


def lcd_cmd(cmd):
    _lcd_write_byte(cmd, 0)


def lcd_write_char(ch):
    _lcd_write_byte(ord(ch), RS)


def lcd_print_at(row, text):
    addr = 0x80 if row == 0 else 0xC0
    lcd_cmd(addr)
    s = str(text)
    if len(s) < 16:
        s = s + (" " * (16 - len(s)))
    else:
        s = s[:16]
    for ch in s:
        lcd_write_char(ch)


def lcd_init():
    time.sleep_ms(50)
    for _ in range(3):
        _lcd_write4(0x30)
        time.sleep_ms(5)
    _lcd_write4(0x20)
    time.sleep_ms(5)
    lcd_cmd(0x28)  # function set: 4-bit, 2 line, 5x8 dots
    lcd_cmd(0x0C)  # display on, cursor off
    lcd_cmd(0x06)  # entry mode set
    lcd_cmd(0x01)  # clear
    time.sleep_ms(5)


# ---------------- RS485 / Modbus helpers ----------------
def modbus_crc(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def rs485_init():
    global uart
    uart = UART(1, baudrate=RS485_BAUD, bits=8, parity=None, stop=1,
                tx=RS485_TX_PIN, rx=RS485_RX_PIN, timeout=1000)
    print("RS485 ready on UART1 TX={}, RX={}, baud={}".format(RS485_TX_PIN, RS485_RX_PIN, RS485_BAUD))


def read_pt100_temp():
    if not rs485_enabled:
        return None
    req = bytearray([
        RS485_SLAVE,
        RS485_FUNC,
        (RS485_REG >> 8) & 0xFF,
        RS485_REG & 0xFF,
        (RS485_COUNT >> 8) & 0xFF,
        RS485_COUNT & 0xFF,
    ])
    crc = modbus_crc(req)
    req.append(crc & 0xFF)
    req.append((crc >> 8) & 0xFF)

    uart.read()  # clear
    uart.write(req)
    time.sleep_ms(120)

    resp = uart.read()
    if not resp or len(resp) < 7:
        raise RuntimeError("no/short response")
    if resp[0] != RS485_SLAVE or resp[1] != RS485_FUNC:
        raise RuntimeError("bad header {}".format(resp[:2]))
    byte_count = resp[2]
    if byte_count < 2:
        raise RuntimeError("byte_count {}".format(byte_count))
    expected = 3 + byte_count + 2
    if len(resp) < expected:
        raise RuntimeError("len {} < {}".format(len(resp), expected))
    data = resp[3:3 + byte_count]
    recv_crc = resp[3 + byte_count] | (resp[3 + byte_count + 1] << 8)
    calc_crc = modbus_crc(resp[:3 + byte_count])
    if recv_crc != calc_crc:
        raise RuntimeError("crc mismatch recv=0x%04X calc=0x%04X" % (recv_crc, calc_crc))
    raw = (data[0] << 8) | data[1]
    return raw / 10.0  # °C


# ---------------- Pulse counter (GPIO20) ----------------
def _pulse_irq(pin):
    global pulse_count, pulse_accm, divider_counter, counter_save_pending, pulse_window_pulses, counter_send_accum, counter_send_pending
    if not counter_enabled:
        return
    pulse_count += 1
    pulse_accm += 1
    pulse_window_pulses += 1
    divider_counter += 1
    counter_send_accum += 1
    if counter_send_accum >= counter_send_divider:
        counter_send_pending = True
        counter_send_accum = 0
    print("divider= ", divider_counter)
    print("counter= ", pulse_count, " accm= ", pulse_accm, "cpm: ", pulse_cpm)
    if divider_counter >= counter_divider:
        print("Divider hit: {} pulses (accm={})".format(divider_counter, pulse_accm))
        divider_counter = 0
        counter_save_pending = True

def pulse_init():
    global pulse_window_start
    if not counter_enabled:
        return
    pin = Pin(PULSE_PIN, Pin.IN, Pin.PULL_DOWN)
    pin.irq(trigger=Pin.IRQ_RISING, handler=_pulse_irq)
    pulse_window_start = time.ticks_ms()


# ---------------- Wi-Fi + HTTP helpers ----------------
def set_wifi(ssid, password, mode=None, ip=None, gateway=None, subnet=None):
    global wifi_ssid, wifi_pass, wifi_mode, wifi_ip, wifi_gateway, wifi_subnet
    wifi_ssid = ssid or wifi_ssid
    wifi_pass = password or wifi_pass
    if mode in ("dhcp", "static"):
        wifi_mode = mode
    if ip is not None:
        wifi_ip = ip
    if gateway is not None:
        wifi_gateway = gateway
    if subnet is not None:
        wifi_subnet = subnet
    save_config()


def connect_wifi(ssid=None, password=None, timeout_s=15):
    if ssid is None:
        ssid = wifi_ssid
    if password is None:
        password = wifi_pass
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)
    if wifi_mode == "static":
        if wifi_ip:
            gw = wifi_gateway or wifi_ip
            mask = wifi_subnet or "255.255.255.0"
            wlan.ifconfig((wifi_ip, mask, gw, gw))
        else:
            # missing static IP -> fallback to DHCP
            wlan.ifconfig(("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0"))
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        start = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) > timeout_s * 1000:
                raise RuntimeError("Wi-Fi connection timed out")
            time.sleep_ms(200)
    ip = wlan.ifconfig()[0]
    try:
        mac = wlan.config("mac")
        if mac:
            global device_mac
            device_mac = ":".join(["{:02X}".format(b) for b in mac])
    except Exception:
        pass
    print("Wi-Fi connected, IP:", ip)
    return ip


def wifi_rssi():
    try:
        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            return wlan.status("rssi")
    except Exception:
        pass
    return None


def create_server(ip):
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)
    s.settimeout(0.05)
    print("HTTP server on http://{}:80".format(ip))
    return s


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}
<title>ESP32-C3 PT100</title>
<style>
*{{box-sizing:border-box;}}
body{{font-family:Arial,sans-serif;background:radial-gradient(120% 120% at 20% 20%,#1e3a8a 0,#0f172a 45%,#0b1224 100%);color:#e2e8f0;margin:0;padding:0;}}
.shell{{max-width:760px;margin:0 auto;padding:20px;}}
.tabs{{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;}}
.tab{{padding:10px 14px;border-radius:10px;background:rgba(255,255,255,0.06);color:#cbd5e1;text-decoration:none;border:1px solid rgba(255,255,255,0.08);transition:all .2s;}}
.tab.active{{background:#0ea5e9;color:#0b1224;border-color:#38bdf8;box-shadow:0 10px 30px rgba(56,189,248,0.25);}}
.card{{background:rgba(15,23,42,0.8);padding:18px 20px;border-radius:14px;box-shadow:0 15px 40px rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.05);}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;}}
.pill{{padding:10px 12px;border-radius:10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);}}
.pill strong{{display:block;color:#38bdf8;margin-bottom:4px;}}
code, .mono{{font-family:SFMono-Regular,Consolas,monospace;background:#0b1224;padding:2px 6px;border-radius:6px;}}
form{{display:flex;flex-direction:column;gap:10px;margin-top:10px;}}
input{{padding:10px 12px;border-radius:10px;border:1px solid rgba(255,255,255,0.2);background:rgba(255,255,255,0.08);color:#e2e8f0;}}
button{{padding:10px 12px;border:none;border-radius:10px;background:#22d3ee;color:#0b1224;font-weight:700;cursor:pointer;box-shadow:0 10px 25px rgba(34,211,238,0.35);}}
small{{color:#94a3b8;}}
.switch{{position:relative;display:inline-block;width:54px;height:28px;}}
.switch input{{opacity:0;width:0;height:0;}}
.slider{{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#475569;border-radius:14px;transition:.2s;}}
.slider:before{{position:absolute;content:'';height:22px;width:22px;left:3px;top:3px;background:white;border-radius:50%;transition:.2s;}}
input:checked + .slider{{background:#0ea5e9;}}
input:checked + .slider:before{{transform:translateX(26px);}}
</style></head>
<body>
<div class="shell">
  <div class="tabs">
    <a class="tab {dash_active}" href="/">Dashboard</a>
    <a class="tab {set_active}" href="/settings">Settings</a>
    <a class="tab {upload_active}" href="/upload">Upload</a>
  </div>
  <div class="card">
    {content}
  </div>
</div>
</body></html>"""


def render_page(get_state_fn, tab="dashboard", note=""):
    status, temp, err, ts, send_status, ip, synced, pcount, paccm, pcpm, c_enabled, r_enabled = get_state_fn()
    refresh = '<meta http-equiv="refresh" content="10">' if tab == "dashboard" else ""
    if tab == "settings":
        content = """
        <h2>Wi-Fi Settings</h2>
        <form method="POST" action="/settings">
          <label style="display:flex;align-items:center;gap:10px;">
            <span>DHCP</span>
            <label class="switch">
              <input type="checkbox" name="mode" value="static" {static_checked}>
              <span class="slider"></span>
            </label>
            <span>Static</span>
          </label>
          <input name="ssid" placeholder="SSID" value="{ssid}">
          <input name="password" placeholder="Password" value="{pwd}">
          <input name="ip" placeholder="Static IP" value="{ip}">
          <input name="gateway" placeholder="Gateway" value="{gw}">
          <input name="subnet" placeholder="Subnet mask" value="{mask}">
          <input name="divider" placeholder="Counter divider" value="{divider}">
          <h3>RS485 Settings</h3>
          <label>Slave address (1-247)</label>
          <input name="rs485_slave" placeholder="Slave address (1-247)" value="{rs_slave}">
          <label>Function (3=holding, 4=input)</label>
          <input name="rs485_func" placeholder="Function (3=holding,4=input)" value="{rs_func}">
          <label>Start register</label>
          <input name="rs485_reg" placeholder="Start register (e.g., 0)" value="{rs_reg}">
          <label>Register count</label>
          <input name="rs485_count" placeholder="Register count" value="{rs_count}">
          <label style="display:flex;align-items:center;gap:10px;">
            <span>Counter</span>
            <label class="switch">
              <input type="checkbox" name="counter_on" value="1" {counter_checked}>
              <span class="slider"></span>
            </label>
          </label>
          <label style="display:flex;align-items:center;gap:10px;">
            <span>RS485-PT100</span>
            <label class="switch">
              <input type="checkbox" name="rs485_on" value="1" {rs485_checked}>
              <span class="slider"></span>
            </label>
          </label>
          <button type="submit">Save (reboot to apply)</button>
        </form>
        <form method="POST" action="/reset_counter" style="margin-top:10px;">
          <button type="submit" style="background:#f59e0b;color:#0b1224;">Reset Counter</button>
        </form>
        <form method="POST" action="/reset_accm" style="margin-top:10px;">
          <button type="submit" style="background:#fb7185;color:#0b1224;">Reset Accm</button>
        </form>
        <form method="POST" action="/reset" style="margin-top:10px;">
          <button type="submit" style="background:#ef4444;color:#0b1224;">Reset Device</button>
        </form>
        <p><small>{note}</small></p>
        <p><small>MAC: {mac}</small></p>
        """.format(
            ssid=wifi_ssid,
            pwd=wifi_pass,
            ip=wifi_ip,
            gw=wifi_gateway,
            mask=wifi_subnet,
            dhcp_checked="checked" if wifi_mode != "static" else "",
            static_checked="checked" if wifi_mode == "static" else "",
            note=note or "",
            mac=device_mac,
            divider=counter_divider,
            rs_slave=RS485_SLAVE,
            rs_func=RS485_FUNC,
            rs_reg=RS485_REG,
            rs_count=RS485_COUNT,
            counter_checked="checked" if counter_enabled else "",
            rs485_checked="checked" if rs485_enabled else ""
        )
    elif tab == "upload":
        content = """
        <h2>Upload Targets</h2>
        <form method="POST" action="/upload">
          <label>Host</label>
          <input name="upload_host" placeholder="Host" value="{host}">
          <label>Counter path</label>
          <input name="upload_counter_path" placeholder="iot2026/smart01/insert2C.php" value="{cpath}">
          <label>Temp path</label>
          <input name="upload_temp_path" placeholder="iot2026/smart01/insertT.php" value="{tpath}">
          <label>Device ID (devid)</label>
          <input name="upload_device_id" placeholder="smart01" value="{devid}">
          <label>Production Order ID (pdid)</label>
          <input name="upload_pdid" placeholder="PO-001" value="{pdid}">
          <label>kfactor (50-200, percent)</label>
          <input name="upload_kfactor" placeholder="100" value="{kfactor}">
          <label>Temp interval (seconds)</label>
          <select name="upload_temp_interval">
            <option value="60000" {int60}>60</option>
            <option value="120000" {int120}>120</option>
            <option value="180000" {int180}>180</option>
            <option value="300000" {int300}>300</option>
          </select>
          <label>Counter send divider (pulses)</label>
          <input name="upload_counter_div" placeholder="10" value="{cdiv}">
          <p style="font-size:12px;color:#94a3b8;">Counter payload: devid=&lt;id&gt;&amp;qty=&lt;qty&gt;&amp;accm=&lt;accm&gt;&amp;cpm=&lt;cpm&gt;<br>Temp payload: devid=&lt;id&gt;&amp;temp=&lt;value&gt;&amp;kfactor=&lt;k&gt;</p>
          <button type="submit">Save Upload Settings</button>
        </form>
        <p><small>{note}</small></p>
        """.format(
            host=UPLOAD_HOST,
            cpath=UPLOAD_COUNTER_PATH,
            tpath=UPLOAD_TEMP_PATH,
            devid=UPLOAD_DEVICE_ID,
            pdid=UPLOAD_PDID,
            kfactor=KFACTOR,
            int60="selected" if UPLOAD_TEMP_INTERVAL_MS == 60_000 else "",
            int120="selected" if UPLOAD_TEMP_INTERVAL_MS == 120_000 else "",
            int180="selected" if UPLOAD_TEMP_INTERVAL_MS == 180_000 else "",
            int300="selected" if UPLOAD_TEMP_INTERVAL_MS == 300_000 else "",
            cdiv=counter_send_divider,
            note=note or ""
        )
    else:
        content = """
        <h2>PT100 RS485</h2>
        <div class="grid">
          <div class="pill"><strong>Status</strong><span class="mono">{status}</span></div>
          <div class="pill"><strong>Temp</strong><span class="mono">{temp}</span></div>
          <div class="pill"><strong>Last error</strong><span class="mono">{err}</span></div>
          <div class="pill"><strong>Updated</strong><span class="mono">{ts}</span></div>
          <div class="pill"><strong>Send</strong><span class="mono">{send}</span></div>
          <div class="pill"><strong>IP</strong><span class="mono">{ip}</span></div>
          <div class="pill"><strong>NTP</strong><span class="mono">{synced}</span></div>
          <div class="pill"><strong>Pulse count</strong><span class="mono">{pcount}</span></div>
          <div class="pill"><strong>Pulse accm</strong><span class="mono">{paccm}</span></div>
          <div class="pill"><strong>CPM</strong><span class="mono">{pcpm}</span></div>
          <div class="pill"><strong>Counter</strong><span class="mono">{cstat}</span></div>
          <div class="pill"><strong>RS485</strong><span class="mono">{rstat}</span></div>
        </div>
        """.format(status=status, temp=temp, err=err, ts=ts, send=send_status, ip=ip, synced=("yes" if synced else "no"),
                   pcount=pcount, paccm=paccm, pcpm=pcpm,
                   cstat="on" if c_enabled else "off",
                   rstat="on" if r_enabled else "off")
    return HTML.format(
        refresh=refresh,
        dash_active="active" if tab == "dashboard" else "",
        set_active="active" if tab == "settings" else "",
        upload_active="active" if tab == "upload" else "",
        content=content
    )


def handle_http_once(sock, get_state_fn):
    global counter_divider, counter_send_divider, divider_counter, pulse_count, pulse_accm, counter_save_pending, counter_enabled, rs485_enabled, RS485_SLAVE, RS485_FUNC, RS485_REG, RS485_COUNT
    global pulse_window_start, pulse_window_pulses, counter_send_accum, counter_send_pending
    global UPLOAD_HOST, UPLOAD_COUNTER_PATH, UPLOAD_TEMP_PATH, UPLOAD_TEMP_INTERVAL_MS, UPLOAD_DEVICE_ID, UPLOAD_PDID, KFACTOR
    try:
        client, _ = sock.accept()
    except OSError:
        return False
    try:
        req = client.recv(512)
        if not req:
            return False
        req_line = req.split(b"\r\n", 1)[0]
        # capture any body bytes that arrived with the first read
        header_split = req.find(b"\r\n\r\n")
        initial_body = b""
        content_length = 0
        if header_split != -1:
            headers_part = req[:header_split].decode("utf-8", "ignore")
            for hline in headers_part.split("\r\n"):
                if hline.lower().startswith("content-length:"):
                    try:
                        content_length = int(hline.split(":", 1)[1].strip())
                    except:
                        content_length = 0
            initial_body = req[header_split + 4:]
        print("Request:", req_line)
        is_settings = b"/settings" in req_line or b"tab=settings" in req_line
        is_upload = b"/upload" in req_line or b"tab=upload" in req_line
        method = b"GET"
        if req_line:
            parts = req_line.split()
            if len(parts) >= 1:
                method = parts[0]
        body = ""
        if method == b"POST":
            # read rest for form data (honor content-length)
            rest = initial_body
            remaining = max(0, content_length - len(initial_body))
            while remaining > 0:
                chunk = client.recv(min(remaining, 2048))
                if not chunk:
                    break
                rest += chunk
                remaining -= len(chunk)
            if b"/reset_counter" in req_line:
                pulse_count = 0
                divider_counter = 0
                save_counters()
                body = render_page(get_state_fn, tab="settings", note="Counter reset & saved.")
                response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}".format(
                    len(body), body
                )
                client.send(response)
                return True
            if b"/reset_accm" in req_line:
                pulse_accm = 0
                pulse_count = 0
                divider_counter = 0
                save_counters()
                body = render_page(get_state_fn, tab="settings", note="Accumulation+Counter reset & saved.")
                response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}".format(
                    len(body), body
                )
                client.send(response)
                return True
            if b"/reset" in req_line:
                client.send(b"HTTP/1.1 302 Found\r\nLocation: /\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nRedirecting...")
                client.close()
                machine.reset()
                return True
            if b"/upload" in req_line:
                try:
                    payload = rest.split(b"\r\n\r\n", 1)[1]
                except IndexError:
                    payload = rest
                params = {}
                for pair in payload.split(b"&"):
                    if b"=" in pair:
                        k, v = pair.split(b"=", 1)
                        params[k.decode()] = v.decode().replace("+", " ")
                host_val = params.get("upload_host", "").strip()
                cpath_val = params.get("upload_counter_path", "").strip()
                tpath_val = params.get("upload_temp_path", "").strip()
                temp_interval_val = params.get("upload_temp_interval", "").strip()
                counter_div_val = params.get("upload_counter_div", "").strip()
                device_val = params.get("upload_device_id", "").strip()
                pdid_val = params.get("upload_pdid", "").strip()
                kfactor_val = params.get("upload_kfactor", "").strip()
                # normalize encoded slashes if present
                cpath_val = cpath_val.replace("%252F", "/").replace("%2F", "/")
                tpath_val = tpath_val.replace("%252F", "/").replace("%2F", "/")
                print("Upload POST -> host:", host_val, "cpath:", cpath_val, "tpath:", tpath_val,
                      "devid:", device_val, "pdid:", pdid_val, "kfactor:", kfactor_val,
                      "temp_int:", temp_interval_val, "ctr_div:", counter_div_val)
                if host_val:
                    UPLOAD_HOST = host_val
                if cpath_val:
                    UPLOAD_COUNTER_PATH = cpath_val
                if tpath_val:
                    UPLOAD_TEMP_PATH = tpath_val
                if device_val:
                    UPLOAD_DEVICE_ID = device_val
                if pdid_val:
                    UPLOAD_PDID = pdid_val
                try:
                    UPLOAD_TEMP_INTERVAL_MS = int(temp_interval_val) if temp_interval_val else UPLOAD_TEMP_INTERVAL_MS
                except:
                    pass
                try:
                    counter_send_divider = int(counter_div_val) if counter_div_val else counter_send_divider
                except:
                    pass
                try:
                    if kfactor_val:
                        KFACTOR = int(kfactor_val)
                except:
                    pass
                save_config()
                body = render_page(get_state_fn, tab="upload", note="Upload settings saved.")
            elif b"ssid=" in rest:
                try:
                    payload = rest.split(b"\r\n\r\n", 1)[1]
                except IndexError:
                    payload = rest
                params = {}
                for pair in payload.split(b"&"):
                    if b"=" in pair:
                        k, v = pair.split(b"=", 1)
                        params[k.decode()] = v.decode().replace("+", " ")
                ssid_val = params.get("ssid", "").strip()
                pass_val = params.get("password", "").strip()
                mode_val = params.get("mode", "").strip()  # "static" when slider checked, "" when DHCP
                ip_val = params.get("ip", "").strip()
                gw_val = params.get("gateway", "").strip()
                mask_val = params.get("subnet", "").strip()
                div_val = params.get("divider", "").strip()
                rs_slave_val = params.get("rs485_slave", "").strip()
                rs_func_val = params.get("rs485_func", "").strip()
                rs_reg_val = params.get("rs485_reg", "").strip()
                rs_count_val = params.get("rs485_count", "").strip()
                counter_on = params.get("counter_on", "").strip()
                rs485_on = params.get("rs485_on", "").strip()
                # If slider unchecked, mode_val empty -> force DHCP
                effective_mode = "static" if mode_val == "static" else "dhcp"
                try:
                    div_int = int(div_val) if div_val else counter_divider
                except:
                    div_int = counter_divider
                try:
                    slave_int = int(rs_slave_val) if rs_slave_val else RS485_SLAVE
                except:
                    slave_int = RS485_SLAVE
                try:
                    func_int = int(rs_func_val) if rs_func_val else RS485_FUNC
                except:
                    func_int = RS485_FUNC
                try:
                    reg_int = int(rs_reg_val) if rs_reg_val else RS485_REG
                except:
                    reg_int = RS485_REG
                try:
                    count_int = int(rs_count_val) if rs_count_val else RS485_COUNT
                except:
                    count_int = RS485_COUNT
                prev_counter_enabled = counter_enabled
                counter_enabled = True if counter_on == "1" else False
                rs485_enabled = True if rs485_on == "1" else False
                # apply wifi settings (keep existing if blank)
                ssid_use = ssid_val or wifi_ssid
                pass_use = pass_val or wifi_pass
                set_wifi(ssid_use, pass_use, effective_mode, ip_val or None, gw_val or None, mask_val or None)
                RS485_SLAVE = slave_int
                RS485_FUNC = func_int
                RS485_REG = reg_int
                RS485_COUNT = count_int
                counter_divider = div_int
                divider_counter = 0
                if counter_enabled and not prev_counter_enabled:
                    # re-init pulse IRQ when turning counter on at runtime
                    pulse_window_pulses = 0
                    pulse_window_start = time.ticks_ms()
                    pulse_init()
                body = render_page(get_state_fn, tab="settings", note="Saved. Use Reset Device to apply Wi-Fi/static changes.")
            else:
                body = render_page(get_state_fn, tab="settings", note="No data")
        else:
            if is_upload:
                body = render_page(get_state_fn, tab="upload")
            elif is_settings:
                body = render_page(get_state_fn, tab="settings")
            else:
                body = render_page(get_state_fn, tab="dashboard")
        response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}".format(
            len(body), body
        )
        client.send(response)
    except Exception as e:
        print("Client handling error:", e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
    finally:
        client.close()
    return True


def sync_time():
    global ntp_synced
    if not ntptime:
        return False
    try:
        ntptime.settime()
        ntp_synced = True
        print("NTP synced")
        return True
    except Exception as e:
        print("NTP sync failed:", e)
        return False


def fmt_datetime(ts=None):
    if ts is None:
        ts = time.time()
    ts += TIME_OFFSET
    tm = time.localtime(ts)
    return "{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(tm[1], tm[2], tm[3], tm[4], tm[5])


def send_temp(temp_c):
    global last_send_status
    if urequests is None:
        last_send_status = "urequests missing"
        return
    try:
        temp_path = UPLOAD_TEMP_PATH.replace("%2F", "/").replace("%252F", "/")
        url = "http://{}/{}?devid={}&temp={:.1f}&kfactor={}".format(
            UPLOAD_HOST, temp_path, UPLOAD_DEVICE_ID, temp_c, KFACTOR)
        print("Send temp ->", url)
        r = urequests.get(url)
        last_send_status = "OK " + str(r.status_code)
        try:
            print("Temp response:", r.text)
        except Exception:
            pass
        r.close()
    except Exception as e:
        last_send_status = "ERR " + str(e)


def send_counter(qty, accm, cpm):
    if urequests is None:
        return
    try:
        counter_path = UPLOAD_COUNTER_PATH.replace("%2F", "/").replace("%252F", "/")
        url = "http://{}/{}?devid={}&pdid={}&qty={}&accm={}&cpm={}".format(
            UPLOAD_HOST, counter_path, UPLOAD_DEVICE_ID, UPLOAD_PDID, qty, accm, cpm)
        print("Send counter ->", url)
        r = urequests.get(url)
        try:
            print("Counter response:", r.text)
        except Exception:
            pass
        r.close()
    except Exception as e:
        print("Send counter err:", e)


def save_counters():
    try:
        with open(COUNTER_FILE, "w") as f:
            f.write(str(pulse_count) + "\n")
            f.write(str(pulse_accm) + "\n")
            f.write(str(divider_counter) + "\n")
        return True
    except Exception as e:
        print("Save counters failed:", e)
        return False


def load_counters():
    global pulse_count, pulse_accm, divider_counter
    try:
        with open(COUNTER_FILE, "r") as f:
            lines = f.read().splitlines()
        if len(lines) >= 1:
            pulse_count = int(lines[0])
        if len(lines) >= 2:
            pulse_accm = int(lines[1])
        if len(lines) >= 3:
            divider_counter = int(lines[2])
        print("Counters loaded:", pulse_count, pulse_accm, divider_counter)
    except Exception as e:
        print("No counters loaded (starting fresh):", e)

def save_config():
    global wifi_mode, wifi_ssid, wifi_pass, wifi_ip, wifi_gateway, wifi_subnet, counter_divider, counter_enabled, rs485_enabled
    global RS485_SLAVE, RS485_FUNC, RS485_REG, RS485_COUNT
    global UPLOAD_HOST, UPLOAD_COUNTER_PATH, UPLOAD_TEMP_PATH, UPLOAD_TEMP_INTERVAL_MS, counter_send_divider, UPLOAD_DEVICE_ID, UPLOAD_PDID, KFACTOR
    try:
        print("Saving config...")
        print("kfactor= ",KFACTOR, " PDID= ", UPLOAD_PDID)
        with open(CONFIG_FILE, "w") as f:
            f.write((wifi_mode or "") + "\n")
            f.write((wifi_ssid or "") + "\n")
            f.write((wifi_pass or "") + "\n")
            f.write((wifi_ip or "") + "\n")
            f.write((wifi_gateway or "") + "\n")
            f.write((wifi_subnet or "") + "\n")
            f.write(str(counter_divider) + "\n")
            f.write("1\n" if counter_enabled else "0\n")
            f.write("1\n" if rs485_enabled else "0\n")
            f.write(str(RS485_SLAVE) + "\n")
            f.write(str(RS485_FUNC) + "\n")
            f.write(str(RS485_REG) + "\n")
            f.write(str(RS485_COUNT) + "\n")
            f.write(UPLOAD_HOST + "\n")
            f.write(UPLOAD_COUNTER_PATH + "\n")
            f.write(UPLOAD_TEMP_PATH + "\n")
            f.write(str(UPLOAD_TEMP_INTERVAL_MS) + "\n")
            f.write(str(counter_send_divider) + "\n")
            f.write(UPLOAD_DEVICE_ID + "\n")
            f.write(UPLOAD_PDID + "\n")
            f.write(str(KFACTOR) + "\n")
        return True
    except Exception as e:
        print("Save config failed:", e)
        return False


def load_config():
    global wifi_mode, wifi_ssid, wifi_pass, wifi_ip, wifi_gateway, wifi_subnet, counter_divider, counter_enabled, rs485_enabled
    global RS485_SLAVE, RS485_FUNC, RS485_REG, RS485_COUNT
    global UPLOAD_HOST, UPLOAD_COUNTER_PATH, UPLOAD_TEMP_PATH, UPLOAD_TEMP_INTERVAL_MS, counter_send_divider, UPLOAD_DEVICE_ID, UPLOAD_PDID, KFACTOR
    try:
        print("Loading config >>>>>:")
        with open(CONFIG_FILE, "r") as f:
            lines = f.read().splitlines()
        if len(lines) >= 1:
            wifi_mode = lines[0].strip() or "dhcp"
        if len(lines) >= 2:
            wifi_ssid = lines[1].strip() or DEFAULT_SSID
        if len(lines) >= 3:
            wifi_pass = lines[2].strip() or DEFAULT_PASS
        if len(lines) >= 4:
            wifi_ip = lines[3].strip()
        if len(lines) >= 5:
            wifi_gateway = lines[4].strip()
        if len(lines) >= 6:
            wifi_subnet = lines[5].strip()
        if len(lines) >= 7:
            try:
                counter_divider = int(lines[6].strip())
            except:
                counter_divider = 10
        if len(lines) >= 8:
            counter_enabled = (lines[7].strip() == "1")
        if len(lines) >= 9:
            rs485_enabled = (lines[8].strip() == "1")
        if len(lines) >= 10:
            try:
                RS485_SLAVE = int(lines[9].strip())
            except:
                RS485_SLAVE = 1
        if len(lines) >= 11:
            try:
                RS485_FUNC = int(lines[10].strip())
            except:
                RS485_FUNC = 0x03
        if len(lines) >= 12:
            try:
                RS485_REG = int(lines[11].strip())
            except:
                RS485_REG = 0
        if len(lines) >= 13:
            try:
                RS485_COUNT = int(lines[12].strip())
            except:
                RS485_COUNT = 1
        if len(lines) >= 14:
            UPLOAD_HOST = lines[13].strip() or UPLOAD_HOST
        if len(lines) >= 15:
            UPLOAD_COUNTER_PATH = (lines[14].strip() or UPLOAD_COUNTER_PATH).replace("%252F", "/").replace("%2F", "/")
        if len(lines) >= 16:
            UPLOAD_TEMP_PATH = (lines[15].strip() or UPLOAD_TEMP_PATH).replace("%252F", "/").replace("%2F", "/")
        if len(lines) >= 17:
            try:
                UPLOAD_TEMP_INTERVAL_MS = int(lines[16].strip())
            except:
                UPLOAD_TEMP_INTERVAL_MS = 60_000
        if len(lines) >= 18:
            try:
                counter_send_divider = int(lines[17].strip())
            except:
                counter_send_divider = 10
        if len(lines) >= 19:
            UPLOAD_DEVICE_ID = lines[18].strip() or UPLOAD_DEVICE_ID
        if len(lines) >= 20:
            UPLOAD_PDID = lines[19].strip() or UPLOAD_PDID
        if len(lines) >= 21:
            try:
                KFACTOR = int(lines[20].strip())
            except:
                KFACTOR = 100
        print("Config loaded:", wifi_mode, wifi_ssid, wifi_ip, wifi_gateway, wifi_subnet,UPLOAD_PDID,KFACTOR)
    except Exception as e:
        print("No config loaded (using defaults):", e)


def main():
    global i2c, sock, last_send_ms, last_counter_send_ms, pulse_cpm, pulse_count, pulse_window_start, pulse_window_pulses, pulse_cpm_prev, has_prev_cpm, lcd_page, lcd_page_timer, counter_save_pending, counter_send_pending
    load_config()
    load_counters()
    pulse_window_start = time.ticks_ms()
    pulse_window_pulses = 0
    pulse_cpm_prev = 0
    has_prev_cpm = False
    counter_send_pending = False
    i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400000)
    lcd_init()
    rs485_init()
    pulse_init()

    try:
        ip = connect_wifi()
        sock = create_server(ip)
        if ntptime:
            sync_time()
    except Exception as exc:
        print("Startup error:", exc)
        lcd_print_at(0, "WiFi fail")
        lcd_print_at(1, str(exc)[:16])
        time.sleep(5)
        machine.reset()
        return

    latest_temp = None
    latest_err = ""
    last_ts = 0
    ip_cached = ip

    def get_state():
        return ("ok" if not latest_err else "error",
                "{:.1f} C".format(latest_temp) if latest_temp is not None else "N/A",
                latest_err,
                fmt_datetime(last_ts if last_ts else time.time()),
                last_send_status,
                ip_cached,
                ntp_synced,
                pulse_count,
                pulse_accm,
                pulse_cpm,
                counter_enabled,
                rs485_enabled)

    last_read = 0
    interval = 10_000  # ms
    lcd_print_at(0, fmt_datetime())
    lcd_print_at(1, "Temp init...")
    lcd_page_timer = time.ticks_ms()

    while True:
        now = time.ticks_ms()
        # pulse CPM update
        elapsed = time.ticks_diff(now, pulse_window_start)
        if elapsed < 0:
            elapsed = 0
        if elapsed >= 60_000:
            # close the window, compute CPM from that minute, show it during next minute
            if elapsed == 0:
                elapsed = 1
            pulse_cpm_prev = int(pulse_window_pulses * 60_000 / elapsed)
            pulse_cpm = pulse_cpm_prev
            has_prev_cpm = True
            print("Pulse window done: pulses={} accm={} cpm={}".format(pulse_window_pulses, pulse_accm, pulse_cpm))
            pulse_window_start = now
            pulse_window_pulses = 0
        else:
            # during the current minute, show live CPM only for the first minute; afterward show last full minute
            if not has_prev_cpm:
                live_elapsed = elapsed if elapsed > 0 else 1
                pulse_cpm = int(pulse_window_pulses * 60_000 / live_elapsed) if pulse_window_pulses > 0 else 0
            else:
                pulse_cpm = pulse_cpm_prev

        if counter_save_pending:
            if save_counters():
                counter_save_pending = False

        if time.ticks_diff(now, last_read) >= interval:
            try:
                temp_c = read_pt100_temp()
                if temp_c is None and not rs485_enabled:
                    latest_err = "RS485 disabled"
                    latest_temp = None
                    if lcd_page == 0:
                        lcd_print_at(0, fmt_datetime(last_ts if last_ts else time.time()))
                        lcd_print_at(1, "RS485 disabled")
                elif temp_c is None:
                    latest_err = "No data"
                    latest_temp = None
                    if lcd_page == 0:
                        lcd_print_at(0, fmt_datetime(last_ts if last_ts else time.time()))
                        lcd_print_at(1, "Temp N/A")
                else:
                    adjusted_temp = temp_c * KFACTOR / 100.0
                    latest_temp = adjusted_temp
                    latest_err = ""
                    last_ts = time.time()
                    if lcd_page == 0:
                        lcd_print_at(0, fmt_datetime(last_ts))
                        lcd_print_at(1, "{:6.1f} C".format(adjusted_temp))
                    print("Temp raw: {:.1f} C adj: {:.1f} (k={})".format(temp_c, adjusted_temp, KFACTOR))
                    # periodic send
                    if rs485_enabled and time.ticks_diff(now, last_send_ms) >= UPLOAD_TEMP_INTERVAL_MS:
                        send_temp(adjusted_temp)
                        last_send_ms = now
            except Exception as e:
                latest_err = str(e)
                last_ts = time.time()
                if lcd_page == 0:
                    lcd_print_at(0, fmt_datetime(last_ts))
                    lcd_print_at(1, "Err:{}".format(str(e)[:10]))
                print("Read error:", e)
            last_read = now

        # send counter upload when pending (triggered every counter_send_divider pulses)
        if counter_send_pending and counter_enabled:
            try:
                send_counter(pulse_count, pulse_accm, pulse_cpm)
            except Exception as e:
                print("Counter upload error:", e)
            counter_send_pending = False
            last_counter_send_ms = now

        # LCD page toggle every 2 seconds
        if time.ticks_diff(now, lcd_page_timer) >= 2000:
            lcd_page_timer = now
            lcd_page = (lcd_page + 1) % 3  # three pages: temp, counter, wifi
            if lcd_page == 0:
                lcd_print_at(0, fmt_datetime(last_ts if last_ts else time.time()))
                if rs485_enabled and latest_temp is not None:
                    lcd_print_at(1, "{:6.1f} C".format(latest_temp))
                elif rs485_enabled and latest_temp is None:
                    lcd_print_at(1, "Temp N/A")
                else:
                    lcd_print_at(1, "                ")  # RS485 disabled: show only datetime on page 0
            elif lcd_page == 1:
                lcd_print_at(0, "Q:{:4d} CPM:{:4d}".format(pulse_count, pulse_cpm))
                if counter_enabled:
                    lcd_print_at(1, "Accm:{:6d}".format(pulse_accm))
                else:
                    lcd_print_at(1, "Counter disabled")
            else:
                sig = wifi_rssi()
                lcd_print_at(0, "IP {}".format(ip_cached or "0.0.0.0"))
                if sig is None:
                    lcd_print_at(1, "RSSI: N/A")
                else:
                    lcd_print_at(1, "RSSI:{:4d} dBm".format(sig))

        try:
            served = handle_http_once(sock, get_state)
            if not served:
                time.sleep_ms(20)
        except Exception as e:
            print("HTTP server error:", e)
            time.sleep_ms(200)


if __name__ == "__main__":
    main()


