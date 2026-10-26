"""Ladebeleg -> externe Ladung (1.2.143-dev).

Asked for: "Ich möchte einen automatischen Import, wenn ich den Screenshot
uploade – am besten nur über Copy & Paste. Kosten, Energie, Start, Ende
importieren und automatisch bestätigen." beleg.py reads the text of the
receipt (OCR or pasted text) tolerantly; the route returns the fields; the
form saves by itself when kWh, start and end are there. These tests pin the
parser to real layouts (E.ON Drive as Tesseract read it, Ionity, Tesla in
English), the route, the duplicate guard and the page wiring. The OCR
itself runs only where tesseract is installed (the image, not the parser)."""

import io
import shutil

import pytest

import beleg

EON_OCR = """19:217 al! 56 ED
<
Ladedetails
SE*EFC*E221770 / 21316
Brogatan 141, 89435 Örnsköldsvik
A, er
Veckefjärdens GC \\|
Överhörnäs en
=Google N
E.ON Drive Infrastructure Sweden (main CPO
E.ON Drive Infrastructure GmbH DE*UFC)
Ladevorgangsreferenz
10507686
Status Ladevorgang
Abgeschlossen und validiert
kWh geladen Kosten inkl. MwSt
44,78 24,63 €
kwh EI) EUR C4
Starten
Gesamtzeit
So 27 Sept 2026 =
13:13
Beenden Oh 30m
So 27 Sept 2026
13:43
Bewerten Sie Ihr Ladeerlebnis"""


class TestParser:
    def test_eon_receipt_as_tesseract_reads_it(self):
        f = beleg.felder_aus_text(EON_OCR)
        assert f["schema"] == "uta" and f["schema_name"] == "UTA App" and f["auto"] is True
        assert f["kwh"] == 44.78 and f["cost_total"] == 24.63 and f["currency"] == "EUR"
        assert f["start"] == "2026-09-27 13:13" and f["end"] == "2026-09-27 13:43"
        assert f["station"] == "SE*EFC*E221770" and f["ref"] == "10507686"
        assert f["address"] == "Brogatan 141, 89435 Örnsköldsvik"
        assert f["operator"] == "E.ON"          # one name per brand (EVSE SE*EFC)
        assert f["fehlt"] == []
        # The phone's status-bar time (19:21) never becomes the start.
        assert "19:21" not in (f["start"] or "")

    def test_status_bar_and_blank_lines_as_the_real_screenshot_has_them(self):
        # Tesseract on the grey-scaled original: "5G" plus battery became "56 €",
        # and a blank line sits between the labels and the numbers.
        text = EON_OCR.replace("19:217 al! 56 ED", "19:214 al 56 €").replace(
            "kWh geladen Kosten inkl. MwSt\n44,78", "kwh geladen Kosten inkl. MwSt\n\n44,78")
        f = beleg.felder_aus_text(text)
        assert f["kwh"] == 44.78 and f["cost_total"] == 24.63 and f["fehlt"] == []

    def test_tesla_receipt_start_plus_duration_and_price_per_kwh(self):
        # Tesseract on the dark Tesla app: no end time, "Sitzungszeit 14 Min.",
        # and a tariff "0,44 €/kWh" that must not pass as the cost.
        text = ("19:26 4 ul) 56 EI\n< Ladevorgang\nHannover, Germany - Altwar...\nJuni 14, 2026 - 11:14 AM R\n"
                "Supercharging ©\nSitzung bewerten\nEinzelheiten zur Sitzung\nLadegebühren\n"
                "20,3111 kWh @ 0,44 €/kWh 8,93 €\nZugeführte Energie 20,3111 kWh\nSitzungszeit 14 Min.\n"
                "Rechnung\n& Ladevorgangsdaten\nStandort\n27 Hannoversche StraBe\nHannover Niedersachsen Germany\n30916\n")
        f = beleg.felder_aus_text(text + "ee u\n")
        assert f["schema"] == "tesla" and f["auto"] is True
        assert f["kwh"] == 20.3111 and f["cost_total"] == 8.93
        assert (f["start"], f["end"], f["duration_min"]) == ("2026-06-14 11:14", "2026-06-14 11:28", 14)
        assert f["operator"] == "Tesla" and f["location_name"] == "Hannover, Germany"
        assert f["address"] == "27 Hannoversche StraBe, Hannover Niedersachsen Germany, 30916"
        assert f["fehlt"] == []

    def test_unknown_layout_is_a_suggestion_never_saved_by_itself(self):
        f = beleg.felder_aus_text("IONITY Ladevorgang\n27.09.2026, 13:13 – 13:43 Uhr\n44,78 kWh\n24,63 EUR")
        assert f["schema"] == "generisch" and f["schema_name"] is None and f["auto"] is False
        assert f["fehlt"] == []          # complete, but still a suggestion

    def test_schema_detection_needs_the_labels_not_the_numbers(self):
        assert beleg.schema_erkennen("Ladedetails\nkWh geladen\n44,78").id == "uta"
        assert beleg.schema_erkennen("Ladevorgang\nEinzelheiten zur Sitzung\nZugeführte Energie 20 kWh\nSitzungszeit 14 Min.").id == "tesla"
        assert beleg.schema_erkennen("Charging session\nSession details\nEnergy added 20 kWh\nSession time 14 min").id == "tesla"
        assert beleg.schema_erkennen("44,78 kWh 24,63 € 27.09.2026 13:13") is None

    def test_known_schema_with_a_missing_value_names_it_and_does_not_save(self):
        f = beleg.felder_aus_text("Ladedetails\nLadevorgangsreferenz\n12345\nkWh geladen Kosten inkl. MwSt\n44,78 24,63 €\nStarten\nSo 27 Sept 2026\n13:13")
        assert f["schema"] == "uta" and f["start"] == "2026-09-27 13:13" and f["end"] is None
        assert f["fehlt"] == ["Ende"] and f["auto"] is False

    def test_uta_cost_survives_tesseracts_reading_order(self):
        """Norlys/Maribo receipt (3 Oct 2026): 26,93 kWh for 12,79 €. The amount
        must be found whatever lands between label and value, and must never
        be the kWh figure."""
        kopf = "Ladedetails\nDK*NOR*E3733649 / 4606331\nEuropavej 7, 4930 Maribo\nNorlys\n"
        fuss = "Starten\nSa 03 Okt 2026\n15:42\nBeenden\nSa 03 Okt 2026\n15:53\n"
        for mitte in (
            "kWh geladen Kosten inkl. MwSt\n26,93\n12,79 €\nkWh EUR\n",           # values on two lines
            "kWh geladen\n26,93\nkWh\nKosten inkl. MwSt\n12,79\nEUR\n",           # € sign dropped
            "kWh geladen\n26,93\nkWh\nKosten inkl. MwSt\n12,79 e\nEUR\n",         # € read as e
            "Kosten inkl. MwSt\nkWh geladen\n26,93\n12,79 €\n",                  # labels first
            "Kosten inkl. MwSt\nEuropavej 7, 4930 Maribo\n12,79 €\nkWh geladen\n26,93\n",   # address in between
            "Ladevorgangsreferenz\ndb376bf3-02e8-4f0a-8ae2-6b90b2b91ba7\nKosten inkl. MwSt\n\n\n12,79 €\nkWh geladen\n26,93\n",
        ):
            f = beleg.felder_aus_text(kopf + mitte + fuss)
            assert f["schema"] == "uta", mitte
            assert (f["kwh"], f["cost_total"], f["currency"]) == (26.93, 12.79, "EUR"), (mitte, f)

    def test_a_seven_read_as_a_slash_is_not_nine_euros(self):
        """Tesseract at one scale: "26,93 12,/9€" for 26,93 kWh and 12,79 €.
        The 9 in front of the sign is a fragment, not the price."""
        text = ("Ladedetails\nNorlys\nLadevorgangsreferenz\n4606331\nkWh geladen Kosten inkl. MwSt\n"
                "26,93 12,/9€\nkWh EI) EUR C4\nStarten\nSa 03 Okt 2026\n15:42\nBeenden\nSa 03 Okt 2026\n15:53\n")
        f = beleg.felder_aus_text(text)
        assert (f["kwh"], f["cost_total"]) == (26.93, 12.79)
        # and when the repair cannot apply, no fragment becomes the price
        f = beleg.felder_aus_text(text.replace("12,/9€", "12,?9€"))
        assert f["kwh"] == 26.93 and f["cost_total"] is None

    def test_duration_spellings(self):
        for dauer, minuten in (("1 Std. 5 Min.", 65), ("0h 30m", 30), ("Oh 30m", 30), ("1 h 5 min", 65),
                               ("45 minutes", 45), ("2 Stunden", 120)):
            f = beleg.felder_aus_text(f"Start 27.09.2026 13:13\nDauer {dauer}\n10 kWh\n5 €")
            assert f["duration_min"] == minuten, dauer
            assert f["end"] == ("2026-09-27 " + f"{13 + minuten // 60:02d}:{13 + minuten % 60:02d}"
                                if 13 + minuten % 60 < 60 else f["end"]), dauer
        # With a real end time the duration is not needed and not applied.
        f = beleg.felder_aus_text("27.09.2026 13:13\n27.09.2026 13:43\nGesamtzeit 0h 30m\n10 kWh")
        assert f["end"] == "2026-09-27 13:43" and f["duration_min"] is None

    def test_operator_names_from_real_ocr(self):
        # what Tesseract made of the operator line on real UTA receipts
        for roh, name in (("Tr: SLA", "Tesla"), ("eon", "E.ON"), ("K Lataus", "K-Lataus"),
                          ("E.ON Drive Infrastructure Sweden", "E.ON"), ("Shell Recharge", "Shell Recharge"),
                          ("Recharge", "Recharge"), ("IONITY GmbH", "IONITY"), ("Gartenhaus", "Gartenhaus"),
                          ("oogle", None), ("Google & L", None), ("© Google", None), ("ab", None)):
            assert beleg.betreiber_name(roh) == name, roh

    def test_evse_operator_code_beats_the_ocr_line(self):
        text = EON_OCR.replace("E.ON Drive Infrastructure Sweden", "oogle").replace("SE*EFC*E221770", "DE*TSL*E001BFX")
        assert beleg.felder_aus_text(text)["operator"] == "Tesla"
        assert beleg.betreiber_aus_evse("DK*NOR*E3733649 / 4606331") == "Norlys"
        assert beleg.betreiber_aus_evse("XX*ZZZ*E1") is None

    def test_operator_is_a_word_not_a_part_of_one(self):
        f = beleg.felder_aus_text("Mercedes me Charge\n27.09.2026 13:13\n27.09.2026 13:43\n30 kWh\nTotal 12,50 €")
        assert f["operator"] not in ("Mer", "Total", "TotalEnergies")

    def test_known_operator_from_the_database_wins(self):
        f = beleg.felder_aus_text(EON_OCR, bekannte=["Ionity", "E.ON Drive"])
        assert f["operator"] == "E.ON Drive" and f["location_name"] == "E.ON Drive"

    def test_single_date_with_a_time_range(self):
        f = beleg.felder_aus_text("IONITY Ladevorgang\nHamburg Stillhorn Ost\n27.09.2026, 13:13 – 13:43 Uhr\n"
                                  "Geladene Energie 44,78 kWh\nGesamtbetrag 24,63 EUR")
        assert (f["start"], f["end"], f["kwh"], f["cost_total"]) == ("2026-09-27 13:13", "2026-09-27 13:43", 44.78, 24.63)
        assert f["operator"] == "IONITY"

    def test_english_receipt_with_am_pm_and_thousands(self):
        f = beleg.felder_aus_text("Tesla Supercharger\nSep 27, 2026 1:13 PM - 1:43 PM\nEnergy 44.78 kWh\nTotal €24.63\n"
                                  "Odometer 12,345 km")
        assert (f["start"], f["end"]) == ("2026-09-27 13:13", "2026-09-27 13:43")
        assert f["kwh"] == 44.78 and f["cost_total"] == 24.63
        assert f["address"] is None          # a date line is not an address

    def test_foreign_currency_goes_to_the_note_not_the_cost_field(self):
        f = beleg.felder_aus_text("Recharge\n27.09.2026 13:13\n27.09.2026 13:43\n30,5 kWh\n289,00 kr")
        assert f["kwh"] == 30.5 and f["cost_total"] is None and f["currency"] == "SEK"
        assert "289.00 SEK" in f["note"]

    def test_missing_pieces_are_named_not_guessed(self):
        f = beleg.felder_aus_text("EnBW mobility+\n27.09.2026\n44,78 kWh\n24,63 €",
                                  beschriftung={"start": "Startzeit", "end": "Endzeit"})
        assert f["start"] is None and f["fehlt"] == ["Startzeit", "Endzeit"]
        assert beleg.felder_aus_text("nur Text ohne Zahlen")["fehlt"] == ["kWh", "Start", "Ende"]

    def test_end_before_start_is_swapped(self):
        f = beleg.felder_aus_text("Ende 27.09.2026 13:43\nStart 27.09.2026 13:13\n10 kWh")
        assert (f["start"], f["end"]) == ("2026-09-27 13:13", "2026-09-27 13:43")


@pytest.fixture
def client(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) "
               "VALUES (1, 'admin', 'x', 1, 1)")
    db.execute("INSERT INTO operators (name) VALUES ('E.ON Drive')")
    db.commit()
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        yield c


def test_route_reads_pasted_text_and_knows_the_operators(client):
    d = client.post("/api/charge/external/beleg", json={"text": EON_OCR}).get_json()
    f = d["felder"]
    assert f["kwh"] == 44.78 and f["start"] == "2026-09-27 13:13" and f["operator"] == "E.ON Drive"
    assert f["fehlt"] == [] and d["text"].startswith("19:21")


def test_route_names_missing_fields_in_the_ui_language(client):
    d = client.post("/api/charge/external/beleg", json={"text": "44,78 kWh"}).get_json()
    assert d["felder"]["fehlt"] and "kWh" not in d["felder"]["fehlt"]
    assert client.post("/api/charge/external/beleg", json={"text": "  "}).status_code == 400


def test_route_ocr_from_a_rendered_receipt_image(client):
    if not shutil.which("tesseract"):
        r = client.post("/api/charge/external/beleg",
                        data={"file": (io.BytesIO(b"nicht wirklich ein Bild"), "b.png")},
                        content_type="multipart/form-data")
        assert r.status_code == 503 and "tesseract" in r.get_json()["error"]
        pytest.skip("tesseract fehlt – OCR selbst nicht prüfbar")
    from PIL import Image, ImageDraw, ImageFont
    bild = Image.new("L", (1000, 700), 255)
    zeichnen = ImageDraw.Draw(bild)
    try:
        schrift = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
    except OSError:
        schrift = ImageFont.load_default()
    y = 30
    for zeile in ("Ladedetails", "IONITY Hamburg Stillhorn", "Start 27.09.2026 13:13",
                  "Ende 27.09.2026 13:43", "Energie 44,78 kWh", "Betrag 24,63 EUR"):
        zeichnen.text((40, y), zeile, fill=0, font=schrift)
        y += 100
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    puffer.seek(0)
    r = client.post("/api/charge/external/beleg", data={"file": (puffer, "beleg.png")},
                    content_type="multipart/form-data")
    f = r.get_json()["felder"]
    assert r.status_code == 200
    assert f["kwh"] == 44.78 and f["cost_total"] == 24.63
    assert (f["start"], f["end"]) == ("2026-09-27 13:13", "2026-09-27 13:43")


def test_route_ocr_reads_a_dark_receipt(client):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract fehlt")
    from PIL import Image, ImageDraw, ImageFont
    bild = Image.new("L", (1000, 600), 20)
    zeichnen = ImageDraw.Draw(bild)
    try:
        schrift = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
    except OSError:
        schrift = ImageFont.load_default()
    y = 30
    for zeile in ("Ladevorgang", "Juni 14, 2026 - 11:14 AM", "Supercharging",
                  "Zugefuehrte Energie 20,31 kWh", "Sitzungszeit 14 Min.", "Ladegebuehren 8,93 EUR"):
        zeichnen.text((40, y), zeile, fill=235, font=schrift)
        y += 90
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    puffer.seek(0)
    f = client.post("/api/charge/external/beleg", data={"file": (puffer, "dunkel.png")},
                    content_type="multipart/form-data").get_json()["felder"]
    assert f["kwh"] == 20.31 and f["cost_total"] == 8.93
    assert (f["start"], f["end"]) == ("2026-06-14 11:14", "2026-06-14 11:28")


def test_saving_keeps_the_location_name_and_refuses_the_same_receipt_twice(client, db):
    daten = {"vehicle_plate": "EV1", "kwh": 44.78, "cost_total": 24.63,
             "timestamp": "2026-09-27 13:13", "end_time": "2026-09-27 13:43",
             "location_name": "E.ON Drive", "note": "SE*EFC*E221770"}
    assert client.post("/api/charge/external", json=daten).get_json() == {"ok": True, "session_number": "ext"}
    nochmal = client.post("/api/charge/external", json=daten).get_json()
    assert nochmal["ok"] and nochmal["duplicate"]
    rows = db.execute("SELECT location_name, note FROM charge_sessions WHERE is_external = 1").fetchall()
    assert len(rows) == 1 and rows[0]["location_name"] == "E.ON Drive" and rows[0]["note"] == "SE*EFC*E221770"
    # A different charge at nearly the same time is not a duplicate.
    anders = dict(daten, kwh=12.0)
    assert "duplicate" not in client.post("/api/charge/external", json=anders).get_json()


def test_pages_wire_paste_file_and_clipboard(client, db):
    js = client.get("/static/beleg.js").get_data(as_text=True)
    for stueck in ("/api/charge/external/beleg", "navigator.clipboard.read", 'addEventListener("paste"',
                   "if (f.auto) {", "window.belegAusZwischenablage", "window.belegDateiGewaehlt"):
        assert stueck in js, stueck
    # Erfassen: Felder des Neu-Formulars, Standortname mit ins Speichern.
    html = client.get("/charges").get_data(as_text=True)
    for stueck in ('id="extBelegText"', 'id="extBelegDatei"', 'data-onclick="belegAusZwischenablage"',
                   "IDmateBeleg.einrichten({", "start: 'extTimestampStart'", "location_name: _extLocationName",
                   "UTA App, Tesla App", 'id="beleg-texte"'):
        assert stueck in html, stueck
    # Bearbeiten: die automatisch erkannte externe Ladung öffnen, Beleg drüber.
    db.execute("INSERT INTO charge_sessions (is_external, vehicle_plate, start_time, end_time, total_kwh, note) "
               "VALUES (1, 'EV1', '2026-09-27T13:10:00', '2026-09-27T13:50:00', 40.0, 'Automatisch erkannt (SoC 35%->80%)')")
    db.commit()
    sid = db.execute("SELECT id FROM charge_sessions WHERE is_external = 1").fetchone()["id"]
    detail = client.get(f"/charges/{sid}").get_data(as_text=True)
    for stueck in ('id="extBelegText"', "IDmateBeleg.einrichten({", "start: 'extStart'", "kwh: 'extKwh'",
                   "speichern: () => saveExternal()", "_belegOperator", "UTA App, Tesla App"):
        assert stueck in detail, stueck
    # Ein Wallbox-Eintrag (nicht extern) bekommt den Beleg nicht.
    db.execute("INSERT INTO charge_sessions (is_external, vehicle_plate, start_time, end_time, total_kwh) "
               "VALUES (0, 'EV1', '2026-09-26T20:00:00', '2026-09-26T23:00:00', 20.0)")
    db.commit()
    wid = db.execute("SELECT id FROM charge_sessions WHERE is_external = 0").fetchone()["id"]
    assert 'id="extBelegText"' not in client.get(f"/charges/{wid}").get_data(as_text=True)


def test_migration_cleans_stored_operator_names(app_module, db):
    import logging
    import migrations
    for op, note in (("oogle", "Automatisch erkannt · oogle · DE*TSL*E001BFX · a4eba0016d1f"),
                     ("Google & L", "Automatisch erkannt · Google & L"),
                     ("eon", "Automatisch erkannt · eon · Ref 10918"),
                     ("Tr: SLA", "Tr: SLA · SE*TSL*E1B96MF"), ("IONITY", None)):
        db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, operator, note) "
                   "VALUES ('EV1', '2026-09-26T10:00:00', 1, 30, ?, ?)", (op, note))
    migrations.m071_operator_names(db, logging.getLogger("t"))
    assert [r[0] for r in db.execute("SELECT operator FROM charge_sessions ORDER BY id")] == \
        ["Tesla", None, "E.ON", "Tesla", "IONITY"]


def test_charge_stats_count_only_external_providers(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'a', 'x', 1, 1)")
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home) VALUES ('Zuhause', 53.8, 10.3, 1)")
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, operator, location_name) "
               "VALUES ('EV1', '2026-09-01T10:00:00', 1, 30, 'Zuhause', 'Zuhause'), "
               "('EV1', '2026-09-02T10:00:00', 1, 40, 'IONITY', 'Raststätte')")
    db.commit()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        assert c.get("/api/charge/stats").get_json()["operators"] == {"IONITY": 1}


def test_charge_stats_take_the_provider_of_the_location(app_module, db):
    db.execute("INSERT INTO vehicles (id, plate, device) VALUES (1, 'EV1', 'dev1')")
    db.execute("INSERT INTO users (id, username, password_hash, is_admin, active_vehicle_id) VALUES (1, 'a', 'x', 1, 1)")
    db.execute("INSERT INTO operators (id, name) VALUES (6, 'Tesla')")
    db.execute("INSERT INTO charge_locations (name, lat, lon, is_home, operator_id) VALUES ('SuC Braak', 53.6, 10.2, 0, 6)")
    db.execute("INSERT INTO charge_sessions (vehicle_plate, start_time, is_external, total_kwh, operator, location_name) "
               "VALUES ('EV1', '2026-10-16T11:15:00', 1, 50, 'oogle', 'SuC Braak'), "
               "('EV1', '2026-10-17T11:15:00', 1, 30, 'IONITY', 'unbekannter Ort')")
    db.commit()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        with c.session_transaction() as s:
            s["_user_id"] = "1"
        assert c.get("/api/charge/stats").get_json()["operators"] == {"Tesla": 1, "IONITY": 1}
