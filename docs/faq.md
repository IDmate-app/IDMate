# FAQ

[← README](../README.md)

**Who is IDmate for?**
Owners of EVs without a usable vendor API (VW ID.3/4/5/7, ID. Buzz and other CAN/OBD2-readable cars) who want TeslaMate-style insight, self-hosted. Also company-car drivers who need a trip log and a monthly charging statement.

**Does it work with a Tesla?**
Yes, via Home Assistant or the TeslaMate import — but for a Tesla alone, [TeslaMate](https://github.com/teslamate-org/teslamate) is the natural choice.

**Do I need the IDTelemetry stick?**
For live telemetry (position, SoC, power, energy counter) yes. Charge tracking alone works with Home Assistant, and other cars can feed telemetry through Home Assistant too.

**Is my data sent to a cloud?**
No. Everything runs on your server. MQTT payloads are AES-256 encrypted.

**Why is my state of health different from what the car shows?**
The car's BMS value is an estimate that is recalibrated from time to time (often back to 100 %). IDmate measures the energy that actually goes into the battery per % SoC. The BMS value is shown next to it for comparison.

**Why do the capacity points scatter?**
The displayed SoC is only about ±1 % exact. Over a 40 % charge that is ±3 % capacity. Trust the median and the trend, not single points.

**Printing is blocked — why?**
The monthly statement is checked against the meter. Settings → Gaps shows what is missing or counted twice.

**Can I bring my TeslaMate history?**
Yes, see [TeslaMate import](teslamate-import.md).

**Why two databases?**
InfluxDB for the high-rate telemetry, SQLite for everything relational.

**Is it a fork of TeslaMate?**
No, no shared code — just the same idea.

**ABRP?**
The stick emulates a BLE ELM327 dongle; ABRP pairs with it directly.
