"""Journey membership — which trips belong to a journey.

A journey holds every trip of its vehicle whose start lies in the journey's
days (date_from..date_to, whole days) — also trips detected long after the
journey was created — except the ones removed by hand
(journey_trip_excluded). ``journey_trips`` stays the materialised membership
so every reader just joins that table.

``sync_journey_trips()`` tops it up. It runs wherever trips appear
(detection, CSV/GPX import, manual entry) so a journey planned in advance
fills itself while the app is running, and once more before a journey is
shown so a journey never displays a stale list — even if the trips were
written by a path that predates this module.
"""

import logging

log = logging.getLogger("triplog.journeys")

# Whole days: a trip starting at 06:30 on the departure day belongs to the
# journey even when date_from carries a later clock time.
IN_RANGE = ("t.device = j.device AND t.start_time >= substr(j.date_from, 1, 10) "
            "AND t.start_time < date(substr(j.date_to, 1, 10), '+1 day')")

_FILL_SQL = f"""INSERT OR IGNORE INTO journey_trips (journey_id, trip_id)
    SELECT j.id, t.id FROM journeys j JOIN trips t ON {IN_RANGE}
     WHERE {{where}}
       AND NOT EXISTS (SELECT 1 FROM journey_trip_excluded x
                        WHERE x.journey_id = j.id AND x.trip_id = t.id)"""


def sync_journey_trips(db, device=None, journey_id=None):
    """Link every trip in range that was not removed by hand. Commits.

    Scope: one journey (``journey_id``), every journey of one vehicle
    (``device``) or — with neither given — all journeys. A no-op when there
    is nothing to add, so it is cheap enough to call after each detector run.
    """
    if journey_id is not None:
        where, args = "j.id = ?", (journey_id,)
    elif device is not None:
        where, args = "j.device = ?", (device,)
    else:
        where, args = "1", ()
    n = db.execute(_FILL_SQL.format(where=where), args).rowcount
    db.commit()
    if n > 0:
        log.info("Journeys: %d trip(s) added automatically (device=%s, journey=%s)",
                 n, device or "*", journey_id or "*")
        # the journeys' list pictures are stale now: render new ones in the
        # background, so the list never waits for them
        try:
            from blueprints.journeys import thumbs_im_hintergrund
            ids = [r[0] for r in db.execute(f"SELECT j.id FROM journeys j WHERE {where}", args)]
            thumbs_im_hintergrund(ids)
        except Exception:
            log.exception("Journeys: thumbnail pre-render could not be started")
    return n


def sync_journeys_for_trips(db, trips):
    """Auto-fill after new trips were written: one pass per vehicle touched.

    Never lets a journey problem break trip detection or an import — the
    page-view sync would pick the trips up later anyway.
    """
    devices = {t.get("device") for t in trips if t.get("device")}
    total = 0
    for dev in devices:
        try:
            total += sync_journey_trips(db, device=dev)
        except Exception:
            log.exception("Journeys: auto-fill failed for device %s", dev)
    return total


def repair_orphan_journey_devices(db):
    """Re-point journeys whose vehicle tag matches nothing. Commits.

    ``journeys.device`` is the InfluxDB tag of the vehicle that was active when
    the journey was created — and when that vehicle had no tag yet, the guess
    from ``config.INFLUX_DEVICE`` was stored instead. As soon as the real tag
    arrives (setup, a vehicle edit, a GPX import deriving it), such a journey
    matches neither its vehicle nor any trip: it drops out of the journeys list
    and the auto-fill can never find a trip for it — exactly the case where
    trips had to be ticked by hand.

    Only unambiguous, lossless cases are touched: exactly one vehicle carries a
    tag (so the target is clear) and no trip carries the journey's own tag (so
    the journey cannot be holding trips of another vehicle).
    """
    devices = [r["device"] for r in db.execute(
        "SELECT DISTINCT device FROM vehicles WHERE device IS NOT NULL AND TRIM(device) <> ''")]
    if len(devices) != 1:
        return 0
    target = devices[0]
    n = db.execute(
        """UPDATE journeys SET device = ?
            WHERE device <> ?
              AND NOT EXISTS (SELECT 1 FROM trips t WHERE t.device = journeys.device)""",
        (target, target),
    ).rowcount
    db.commit()
    if n > 0:
        log.info("Journeys: %d journey(s) re-pointed to the only vehicle (device=%s)", n, target)
    return n
