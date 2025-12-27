Item model:esp32c3-06-01
PCB file: esp32c3-newbox-2.pcb
On device file structure:
- boot.py
- counter_data.txt
- esp32ce_config.txt
- main.py
Features:
Web UI
•	Tabs: Dashboard, Settings, Upload.
•	Dashboard: Shows RS485 status, temperature, last error/time, last send status, IP/NTP, pulse count/accumulation/CPM, and counter/RS485 on/off states (@esp32c3-rs485-pt100.py#454-474).
•	Settings tab:
•	Wi Fi config (DHCP/static IP/gateway/subnet, SSID/password).
•	RS485 read parameters (slave ID, function code, start register, register count).
•	Toggles to enable/disable Counter and RS485-PT100.
•	Buttons: Reset Counter, Reset Accumulation, Reset Device (@esp32c3-rs485-pt100.py#360-389).
•	Upload tab:
•	Host, Counter path, Temp path.
•	Device ID (devid), Production Order ID (pdid), kfactor (percent).
•	Temperature send interval choices (60/120/180/300 seconds).
•	Counter send divider (pulses per send).
•	Payload hints for counter and temp uploads (@esp32c3-rs485-pt100.py#410-438).
Persistence
•	All settings (Wi Fi, RS485 params, counter/RS485 toggles, upload host/paths, devid, pdid, kfactor, temp interval, counter divider) are saved to esp32c3_config.txt and reloaded on boot.
•	POST handler uses Content-Length to read full form data and assigns globals, so kfactor/pdid/devid persist correctly.
Runtime behavior
•	Counter: Pulse IRQ on GPIO20; enable/disable via toggle. Divider controls when counter data is sent.
•	RS485-PT100: Reads holding/input registers per configured slave/func/reg/count; enable/disable via toggle.
•	kfactor: Adjusts temperature before display/send (temp_raw * kfactor / 100).
•	Upload sends:
•	Temp: http://<host>/<temp_path>?devid=<id>&temp=<adj_temp>&kfactor=<k>.
•	Counter: http://<host>/<counter_path>?devid=<id>&pdid=<pdid>&qty=<qty>&accm=<accm>&cpm=<cpm>.
•	Paths are normalized to replace %2F/%252F with /.
•	Intervals: Temp send interval is selectable; counter send uses divider.
•	Debug: Serial prints for upload URLs and HTTP responses; logs POST-parsed values.
LCD pages (cycles every 2s)
1.	Date/time + adjusted temperature (or blank if RS485 disabled).
2.	Counter: Q, CPM, Accumulation (or “Counter disabled”).
3.	Wi Fi: IP address and RSSI.
Other controls
•	Reset counter and accumulation via buttons.
•	Device reset via button.
•	NTP sync status shown on dashboard; IP shown on dashboard and LCD Wi Fi page.
