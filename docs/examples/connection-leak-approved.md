# Incident inc-ebc4fa1a959d: Connections close to max_connections

| | |
|---|---|
| Alert | `PostgresConnectionSaturation` (1 delivery) |
| Runbook | `connection-saturation` 1.0.0 |
| Outcome | **resolved** |
| Opened | 2026-11-07T13:11:10.115480+00:00 |
| Closed | 2026-11-07T13:11:11.436392+00:00 |
| Time to resolved | 1.3s |
| Labels | alertname=PostgresConnectionSaturation, datname=target, instance=eval |

## What was found

- pid 6452 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6453 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6454 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6455 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6456 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6457 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6458 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- pid 6459 (leaky-worker) idle for 3s while connections are at 60%
  - query: `SELECT 1`
- ... and 47 more (leaky-worker x47)

## What was done

| Action | Target | Step | Approved by | Result |
|---|---|---|---|---|
| `terminate_backend` | pid 6452 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6452) |
| `terminate_backend` | pid 6453 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6453) |
| `terminate_backend` | pid 6454 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6454) |
| `terminate_backend` | pid 6455 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6455) |
| `terminate_backend` | pid 6456 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6456) |
| `terminate_backend` | pid 6457 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6457) |
| `terminate_backend` | pid 6458 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6458) |
| `terminate_backend` | pid 6459 | close idle connections of the application holding most of them | eval-approver | done: pg_terminate_backend(6459) |
| ... 47 more | | | | terminate_backend done x47 |

## Timeline

- `13:11:10.115` **opened**: PostgresConnectionSaturation opened an incident
- `13:11:10.132` **diagnosis**: attempt 1: connection_saturation found 55 finding(s)
- `13:11:10.135` **plan**: rule planner proposed 55 action(s): 0 allowed, 55 need approval, 0 denied
- `13:11:10.140` **approval**: eval-approver approved 55 action(s)
- `13:11:10.150` **action**: terminate_backend on s6452-3f6af968: done (pg_terminate_backend(6452))
- `13:11:10.157` **action**: terminate_backend on s6453-099adea8: done (pg_terminate_backend(6453))
- `13:11:10.164` **action**: terminate_backend on s6454-ee212514: done (pg_terminate_backend(6454))
- `13:11:10.169` **action**: terminate_backend on s6455-fef7e90f: done (pg_terminate_backend(6455))
- `13:11:10.176` **action**: terminate_backend on s6456-fc6e46bb: done (pg_terminate_backend(6456))
- `13:11:10.179` **action**: terminate_backend on s6457-e54308f7: done (pg_terminate_backend(6457))
- `13:11:10.187` **action**: terminate_backend on s6458-131e23de: done (pg_terminate_backend(6458))
- `13:11:10.193` **action**: terminate_backend on s6459-38b33b76: done (pg_terminate_backend(6459))
- `13:11:10.196` **action**: terminate_backend on s6460-43d0871d: done (pg_terminate_backend(6460))
- `13:11:10.202` **action**: terminate_backend on s6461-2161564c: done (pg_terminate_backend(6461))
- `13:11:10.206` **action**: terminate_backend on s6462-f0d73a6b: done (pg_terminate_backend(6462))
- `13:11:10.215` **action**: terminate_backend on s6463-090f2b92: done (pg_terminate_backend(6463))
- `13:11:10.221` **action**: terminate_backend on s6464-94d8d4b6: done (pg_terminate_backend(6464))
- `13:11:10.228` **action**: terminate_backend on s6465-cc3873a7: done (pg_terminate_backend(6465))
- `13:11:10.232` **action**: terminate_backend on s6466-05b096fc: done (pg_terminate_backend(6466))
- `13:11:10.240` **action**: terminate_backend on s6467-41a33ffc: done (pg_terminate_backend(6467))
- `13:11:10.243` **action**: terminate_backend on s6468-c0cdbc60: done (pg_terminate_backend(6468))
- `13:11:10.250` **action**: terminate_backend on s6469-6a8fd6fd: done (pg_terminate_backend(6469))
- `13:11:10.257` **action**: terminate_backend on s6470-1031b716: done (pg_terminate_backend(6470))
- `13:11:10.260` **action**: terminate_backend on s6471-aaae75d4: done (pg_terminate_backend(6471))
- `13:11:10.268` **action**: terminate_backend on s6472-c27e5780: done (pg_terminate_backend(6472))
- `13:11:10.275` **action**: terminate_backend on s6473-d0c8c0c3: done (pg_terminate_backend(6473))
- `13:11:10.278` **action**: terminate_backend on s6474-d2eace37: done (pg_terminate_backend(6474))
- `13:11:10.283` **action**: terminate_backend on s6475-69935d0d: done (pg_terminate_backend(6475))
- `13:11:10.289` **action**: terminate_backend on s6476-0d49068a: done (pg_terminate_backend(6476))
- `13:11:10.293` **action**: terminate_backend on s6477-d79c417b: done (pg_terminate_backend(6477))
- `13:11:10.300` **action**: terminate_backend on s6478-e50019d7: done (pg_terminate_backend(6478))
- `13:11:10.305` **action**: terminate_backend on s6479-e40bde9e: done (pg_terminate_backend(6479))
- `13:11:10.311` **action**: terminate_backend on s6480-2a058151: done (pg_terminate_backend(6480))
- `13:11:10.315` **action**: terminate_backend on s6481-b9693e38: done (pg_terminate_backend(6481))
- `13:11:10.320` **action**: terminate_backend on s6482-09d29281: done (pg_terminate_backend(6482))
- `13:11:10.324` **action**: terminate_backend on s6483-ee01d63c: done (pg_terminate_backend(6483))
- `13:11:10.329` **action**: terminate_backend on s6484-826e49ec: done (pg_terminate_backend(6484))
- `13:11:10.333` **action**: terminate_backend on s6485-a9927f6a: done (pg_terminate_backend(6485))
- `13:11:10.337` **action**: terminate_backend on s6486-0d4099c4: done (pg_terminate_backend(6486))
- `13:11:10.341` **action**: terminate_backend on s6487-459b6966: done (pg_terminate_backend(6487))
- `13:11:10.345` **action**: terminate_backend on s6488-529943ee: done (pg_terminate_backend(6488))
- `13:11:10.349` **action**: terminate_backend on s6489-ee0cb782: done (pg_terminate_backend(6489))
- `13:11:10.355` **action**: terminate_backend on s6490-4c1d60f8: done (pg_terminate_backend(6490))
- `13:11:10.360` **action**: terminate_backend on s6491-d11cec16: done (pg_terminate_backend(6491))
- `13:11:10.365` **action**: terminate_backend on s6492-390f00f4: done (pg_terminate_backend(6492))
- `13:11:10.369` **action**: terminate_backend on s6493-a9700068: done (pg_terminate_backend(6493))
- `13:11:10.373` **action**: terminate_backend on s6494-b275e621: done (pg_terminate_backend(6494))
- `13:11:10.377` **action**: terminate_backend on s6495-aaaa7d39: done (pg_terminate_backend(6495))
- `13:11:10.381` **action**: terminate_backend on s6496-7a2b5fa8: done (pg_terminate_backend(6496))
- `13:11:10.385` **action**: terminate_backend on s6497-33d54ca2: done (pg_terminate_backend(6497))
- `13:11:10.391` **action**: terminate_backend on s6498-5ccc381d: done (pg_terminate_backend(6498))
- `13:11:10.395` **action**: terminate_backend on s6499-4a7d9ef2: done (pg_terminate_backend(6499))
- `13:11:10.400` **action**: terminate_backend on s6500-0f1725a0: done (pg_terminate_backend(6500))
- `13:11:10.404` **action**: terminate_backend on s6501-ac80f912: done (pg_terminate_backend(6501))
- `13:11:10.408` **action**: terminate_backend on s6502-6c3eb0ac: done (pg_terminate_backend(6502))
- `13:11:10.413` **action**: terminate_backend on s6503-54fd9c10: done (pg_terminate_backend(6503))
- `13:11:10.417` **action**: terminate_backend on s6504-c33c4999: done (pg_terminate_backend(6504))
- `13:11:10.421` **action**: terminate_backend on s6505-f1b8916f: done (pg_terminate_backend(6505))
- `13:11:10.425` **action**: terminate_backend on s6506-d505bf9d: done (pg_terminate_backend(6506))
- `13:11:11.435` **verify**: verified: the detector finds nothing

## Follow-up

- Put a pooler (PgBouncer) in front of the application or reduce its pool size.
- Set idle_session_timeout for the application role.

## Runbook guidance

Only idle connections are closed, and only for applications holding a large share of all
connections; that is almost always a pool that is too large or a connection leak. The
policy asks for approval when many sessions would be closed at once.
