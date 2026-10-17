# Prometheus metrics

Exposed by `pipeline-api` at `/metrics`. Prefix `sar_`. Label values come from fixed enums, never from free text (no `scene_id` labels).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `sar_jobs_total` | counter | `stage`, `status` (`succeeded`/`failed`) | Finished Jobs |
| `sar_job_duration_seconds` | histogram | `stage` | Job run time |
| `sar_jobs_in_flight` | gauge | `stage` | Jobs `queued` or `running` |
| `sar_scenes` | gauge | `region_id`, `status` | Scenes currently in each status |
| `sar_scenes_discovered_total` | counter | `region_id` | New Scenes registered |
| `sar_detections_total` | counter | `region_id`, `detector_name`, `shore` | Detections written |
| `sar_detections_dropped_on_land_total` | counter | `region_id`, `detector_name` | Candidates discarded as on land |
| `sar_validations_total` | counter | `region_id`, `label` | Validation outcomes |
| `sar_ais_positions_loaded_total` | counter | `region_id` | AIS Positions loaded |
| `sar_ais_lag_days` | gauge | `region_id` | Days between today and the newest loaded AIS day |
| `sar_last_scene_acquired_timestamp_seconds` | gauge | `region_id` | Acquisition time of the newest Scene (drives the "no new Scene in 7 days" check) |
| `sar_source_requests_total` | counter | `source` (`cdse`/`dma`), `outcome` (`ok`/`error`) | Calls to external data sources |
| `sar_bytes_downloaded_total` | counter | `source` | Bytes fetched from external sources |

Plus the default HTTP request metrics of the web framework, under their library's standard names.
