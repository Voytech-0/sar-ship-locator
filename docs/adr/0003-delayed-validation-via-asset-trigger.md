# Validate Detections after the AIS archive lag, triggered by Assets

Status: accepted

Danish historical AIS is published as daily files about 3 days late (checked 2026-10-17: the file for 14 Oct existed, 15 and 16 Oct did not). So Detections are stored as soon as a Scene is processed, and Validation runs later in a separate `validation` DAG. It is triggered by an Airflow Asset when AIS covering the Scene's acquisition time (plus a margin) has been loaded.

## Considered Options

- **Live AIS recorder (AISStream.io)**: no lag, but no history before deployment. Kept as a possible fallback behind the `AisSource` interface, and the Dutch Region was rejected because no free Dutch AIS archive exists.
- **Single DAG that waits for AIS**: holds a Scene unfinished for days and hides the Detections.

## Consequences

A Scene has a status (`detected` then `validated`) with Postgres as the source of truth. Any Scene within the archive's range can be validated retroactively, which also provides free backfill of labelled training data.
