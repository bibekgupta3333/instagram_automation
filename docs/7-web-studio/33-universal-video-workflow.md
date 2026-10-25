# 33 · Universal prompt-to-video workflow

Research and contract review: **2026-10-25**. This is the product and implementation
plan for a general creator beside Pond Studio's nursery editor. The canonical schema
is [`creator_models.py`](../../studio/web/creator_models.py); route and ownership
agreements are in [doc 32](32-creator-contract.md). This document does not extend
either contract. Provider evidence and commercial limitations are in
[doc 34](34-generation-research.md).

## 1. Scope and evidence

The intended experience is idea → editable plan → generation → playback and download.
It supports original stories, explainers and visual experiments beyond nursery songs.
The user can start with just an idea, style and length; the planner proposes an
original cast automatically. Optional brief character descriptions override proposed
identities. The service handles provider
selection, clip assembly and progress. A style choice is creative direction, not a
promise of identity preservation, native HD, factual accuracy or professional quality.

At this docs review, the canonical creator schema, SQLite Store, creator planner and
creator API were read directly after the code graph returned no matching web
definitions. The planner/API arrived during parallel work; media and creator frontend
were not yet present in the inspected listing. Behavior below describes the agreed
design unless explicitly identified as inspected code, not a verified running app.
The existing nursery app has historical offline checks, but its changelog records a
live render refused at yellow pressure. Those checks do not establish creator success.
No creator end-to-end generation, paid call, weights download or EC2 deployment was
performed for this documentation.

## 2. Inputs, plan and outputs

| Contract item | Accepted input or promised contract artifact |
| --- | --- |
| `VideoBrief.idea` | 5–4,000 characters describing the video |
| `style` | `watercolor-anime`, `anime`, `cartoon`, `cinematic`, `live-action`, `motion-graphics` |
| `audience`, `platforms` | `general` or `kids`; unique choices from YouTube, Instagram, Facebook; kids requires exactly `["youtube"]` |
| `duration`, `language` | 5–60 seconds in multiples of five; `en` only |
| `characters` | Zero to four original named character descriptions; no reference-upload field in this schema |
| `audio`, `title` | `ambient`, `silent`, or `narration`; optional title up to 120 characters |
| `VideoPlan` | Title, hook, optional `characters` (defaults to `[]`, at most four), one to twelve five-second shots, description, hashtags, and `planner="opencode"` or `"template"` |
| Each `VideoShot` | Number, five-second duration, visual prompt, optional caption and narration text |
| `CreationRender` | `mode="preview"` or `"cloud"`; `max_cost_usd` defaults to 0 and is bounded by the schema at 20 |
| Delivery | `video.mp4`, `poster.png`, `captions.srt`, `metadata.json`, `description.txt`; downloadable review ZIP |

The narration enum was confirmed in the schema on 2026-10-25; doc 32's original brief
summary still lists ambient/silent and needs a parent-owned wording update. A narration
string in a shot does not itself prove speech synthesis. The announced implementation
is automatic macOS system `say` or Linux `espeak-ng`, with no model downloads by the
app. This was not implemented in the inspected files. Linux binary/voice availability
and macOS installed voices must be checked; no automatic voice installation is
authorized. Missing capability must produce a visible explanation. Singing is absent.

Planning and save validation must ensure contiguous shot numbers and a shot count
whose total matches the brief. The Pydantic field bounds alone do not prove that
cross-object consistency. For example, a 15-second brief needs three five-second shots.
Captions and narration are separate: a subtitle file is not evidence of spoken audio.

Delivery targets portrait **1080×1920, 30 fps, H.264/AAC**. Silent output must be
identified as silent; an AAC stream carrying silence is not narration. Metadata must
retain actual provider source dimensions/frame rate and conversion history. FFmpeg
scaling to 1080p does not make a 480p source native HD; interpolation does not recover
true captured motion. Captions and labels are composed as overlays, never requested
inside generated images.

## 3. User journey

1. Open `/create`, unlock through the existing session flow when required, and enter
   the idea. Choose style, audience, platforms, duration and audio; optionally name
   original characters. No endpoint, model, GPU, workflow graph or API key belongs in
   this form. General projects must not inherit nursery defaults.
2. Save the brief and generate a plan, including automatically proposed original
   characters when the user supplied none. Display that cast for editing; optional
   brief cast takes precedence. The schema addition is confirmed; at the inspected
   planner snapshot, template auto-casting and effective proposed-cast injection into
   shot prompts still needed completion/verification. Identity conditioning remains
   textual, with no reference-image conditioning. Show a bounded planning spinner and retain
   edits after failure. If the free planner is unavailable, explicitly label the
   deterministic template plan and allow editing; do not label it model-generated.
3. Review the hook, shots, narration text and captions, then save. A stale revision
   gets a reload explanation instead of overwriting another change. An active job
   prevents replanning; older outputs remain associated with their saved revision.
4. Choose a clearly labelled **draft motion-graphics preview**, or cloud generation
   when an operator has enabled it and an accepted cost cap exists. Preview is an
   animatic even when the brief asks for anime or live-action. Configuration detection
   is not a successful provider test. With billing disabled, explain that cloud
   generation is unavailable and leave the saved plan usable.
5. Show queued/running progress and cancellation. On completion, play the MP4 and
   offer video and review-package downloads. Show resource stops, provider failures
   and uncertain remote billing honestly. A successful cloud job still needs review;
   all creator jobs retain `publishable=false`.

There is **no user budget decision yet**. The default zero cap and cloud-off policy
remain in effect. A future explicit budget decision must precede paid execution;
enabling billing is operator configuration, never a side effect of a render request.
Reading provider prices or saving a brief grants no spending authorization.

## 4. Creative presets and identity

| Style value | Original visual direction | Main review concern |
| --- | --- | --- |
| `watercolor-anime` | Soft painted environments, pigment texture, gentle fantasy lighting | Texture flicker and changing costume details |
| `anime` | Clean drawn contours, controlled shading, expressive original animation | Faces and limbs drifting between shots |
| `cartoon` | Clear shapes, readable silhouettes, simple expressive motion | Proportions and object permanence |
| `live-action` | Photorealistic synthetic environments and camera movement | Real-person resemblance, anatomy and misleading realism |
| `cinematic` | Deliberate framing, lighting, lenses and shot progression | Attractive shots without story continuity |
| `motion-graphics` | Original shapes, timed layouts and composited labels | Legibility, pacing and accurate diagrams |

These are application prompt presets, not separately trained models or licensed
franchises. Do not copy copyrighted characters, logos, supplied third-party assets or
recognizable voice/likeness. Generic style vocabulary does not establish asset rights.
Use the same character descriptions across shots, but say plainly that text alone
cannot lock identity. Fixed seeds are useful provenance, not an identity guarantee.
Reference images and trained adapters require a future reviewed contract and explicit
rights handling; the current brief accepts descriptions only.

## 5. Architecture and API compatibility

```mermaid
flowchart TD
  UI[Same-origin /create: brief and editable plan] --> API[/api/creator contract]
  API --> Planner[Optional free-only OpenCode planner or labelled template]
  API <--> DB[(Existing Store: studio.sqlite3)]
  API --> Queue[Creation jobs and saved revision snapshots]
  Queue --> Guard[One shared supervisor and resource lock]
  Guard --> Preview[Draft motion-graphics preview]
  Guard --> Gate[Cloud disabled by default: authorization and budget check]
  Gate --> Managed[Managed Wan clips: no local weights]
  Managed --> Journal[Provider IDs and bounded events in job directory]
  Preview --> Assemble[FFmpeg composition and available audio]
  Managed --> Assemble
  Journal -. proposed reconciliation .-> DB
  Assemble --> Files[MP4, poster, captions, metadata and review ZIP]
  Files --> Review[Playback and human review outside publishing]
```

All routes below are relative to `/api/creator`, exactly as doc 32 specifies:

| Action | Route and body |
| --- | --- |
| Inspect capabilities | `GET /status` |
| List/create | `GET /projects`; `POST /projects` with `VideoBrief` |
| Load/save | `GET /projects/{id}`; `PUT /projects/{id}` with `SaveCreation` |
| Plan | `POST /projects/{id}/plan` |
| Queue | `POST /projects/{id}/jobs` with `CreationRender` |
| Track/cancel | `GET /jobs?project_id=ID`; `GET /jobs/{id}`; `POST /jobs/{id}/cancel` |
| Retrieve artifacts | `GET /jobs/{id}/video`, `/download`, `/poster`, `/package` |

Session unlock uses existing `/api/session`. Do not add billing, upload, approval,
publishing or arbitrary-provider routes based on this plan. The worker invocation and
`kind="creation"` request dispatch remain the doc 32 agreement. Backend imports should
be lazy/injected so offline tests do not depend on other agents' unfinished modules.

## 6. SQLite tracking and crash recovery

The inspected Store uses SQLite WAL and a `documents(kind,id,body)` table. Doc 32
assigns `creation` to project records and `creation_job` to job records; job snapshots
bind a specific `project_revision`. These are the baseline persistence contract, not
new SQL tables. Files live in owned job directories, with database records tracking
their application-visible jobs. Keep one API process and the shared worker lock.

The inspected creator API additionally writes internal `creation_event` lifecycle
records and `creation_reservation` budget records. Reservations retain unknown spend
after dispatched work, including successful workers, pending reconciliation. These
are code observations, not tested billing correctness: current Store writes commit
individually, so grouping reservation and job updates atomically remains follow-up.

Provider prediction IDs/events are required in the job directory by doc 32. Persisting
provider IDs **also in SQLite**, and completing charge/budget event reconciliation,
remain internal follow-ups. Agree storage with the parent; do not add
fields to `Creation`, `CreationRender` or public Job responses. The event design should
record job/revision association, shot and attempt, provider request ID, submission
intent, acceptance, terminal outcome, estimate reservation/release, known charges and
unknown charges. These are semantic requirements, not invented API field names.

Reserve the whole job's conservative estimate transactionally before submission.
Persist submission intent before the network request and the returned ID immediately
after acceptance. The remote call and SQLite cannot share an atomic transaction:
timeout or process death between acceptance and recording creates uncertainty.
Mark that uncertainty for reconciliation; never retry a billable submission blindly
or claim exactly-once remote execution. Polling a known ID is different from creating
another prediction. Record actual cost only when supported by billing evidence;
otherwise retain an estimate and unresolved reservation.

Restart marks interrupted jobs failed with a reconciliation warning, without replay.
Cancellation or a local resource stop cannot guarantee remote cancellation or a refund.
The recovery process must account for a provider finishing after the worker stopped.
Back up SQLite and its sidecars consistently with job artifacts; backup/restore and
reservation races need offline tests before relying on the proposed ledger.

## 7. Production work still required

| Area | Remaining acceptance evidence |
| --- | --- |
| Integration | Contract-compatible mounts, lifecycle and UI; offline tests, then separately authorized live playback/export verification |
| Narration | Verify `say`/`espeak-ng` implementation and executable availability, duration fitting, audible output, caption alignment and failure messages; no voice cloning |
| Singing/music | Not supported by current brief audio modes; requires rights-reviewed composition, lyrics, singing synthesis, alignment, mixing and a deliberate contract extension |
| Upscaling | Record source resolution; evaluate licensed enhancement separately, without runtime downloads or calling resized output native HD |
| Character consistency | Original reference pack workflow, shot continuity review and measured failure rates; no identity guarantee |
| Budget/recovery | Atomic reservation tests, IDs and budget events in SQLite, uncertain-submit/cancel reconciliation and operator cost inspection |
| Safety/release | Originality, rights, factual and visual review, synthetic-media disclosure and accurate audience classification before any separate publishing process |

Preserve existing policy restrictions, including no expert impersonation or advice,
no real-person voice/likeness and the reviewed kids path restricted to private YouTube
upload with a human's final manual publish action. No creator API action approves or
publishes. Provider moderation is additional filtering, not publication approval.

The existing supervisor stops on yellow/red/unknown pressure and below the 10 GB disk
floor. No outer guard, runtime downloads, automatic model reinstall, or resource-limit
bypass is part of this design. This research authorizes none of those actions.
