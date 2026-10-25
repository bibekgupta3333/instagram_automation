# 30 · Pond Studio API contract v1

Local-first single-owner FastAPI app. `/api` JSON; same-origin static frontend served at `/`.
No publishing endpoints or automatic human approval. Made-for-kids and AI disclosure remain true.
The app is a creator tool for adults; original Plinkety Pond IP only.

## Project (persisted JSON)

```
{id, name, topic, created_at, updated_at, revision, made_for_kids:true, ai_disclosure:true,
 characters:[{id,name,species:"otter"|"rabbit"|"bear"|"frog",color:"#RRGGBB",accessory:"scarf"|"bow"|"none",description,asset_id:null|string}],
 scenes:[{id,title,duration:1..15,narration,caption,background:"pond"|"meadow"|"bedtime"|"studio",motion:"bounce"|"sway"|"float",character_ids:[string],camera:"wide"|"close"}],
 audio:{asset_id:null|string,mode:"guide"|"uploaded",volume:0..1},
 review:{originality:false,rights:false,child_safety:false,lyrics:false,visuals:false},
 format:"landscape"|"portrait", resolution:"draft"|"final"}
```

Max 24 scenes, total 120 seconds, max 8 characters. Always validate IDs, strings and limits.
A project save increments revision. Editing invalidates export readiness of older renders.

## Routes

- `GET /api/health` → `{status:"ok"}`
- `GET /api/system` → `{platform,free_disk_gb,pressure:"green"|"yellow"|"red"|"unknown",active_job:null|string,providers:[{id,label,available,reason}],auth_required:false|true}`. No secrets.
- `GET /api/projects` → `{projects:[Project]}`
- `POST /api/projects` body `{name?,topic?,template?:"bubbles"|"bedtime"|"counting"}` → Project seeded with original draft characters/scenes, 15 seconds.
- `GET /api/projects/{id}` → Project
- `PUT /api/projects/{id}` body Project → Project; reject stale revision with 409.
- `DELETE /api/projects/{id}` → `{ok:true}`; reject active job.
- `POST /api/projects/{id}/plan` body `{topic:string,template:string}` → Project with deterministic original storyboard; label template-based, not LLM.
- `POST /api/projects/{id}/assets` multipart `file`, `kind=image|audio`, `rights_confirmed=true` → `{id,name,kind,url}`; only raster PNG/JPEG/WebP (no SVG uploads), WAV/MP3, bounded files. Project audio uses returned id.
- `GET /api/projects/{id}/assets/{asset_id}` → inline validated owned asset. No filesystem path in client.
- `GET /api/projects/{id}/characters/{character_id}/preview` → PNG of saved local illustration or selected generated image.
- `POST /api/projects/{id}/jobs` body `{kind:"render"|"character",character_id?:string,provider?:"illustration"|"mflux"|"diffusers",format?:"landscape"|"portrait",resolution?:"draft"|"final"}` → Job.
- `GET /api/jobs/{id}` → Job; `GET /api/jobs?project_id=ID` → `{jobs:[Job]}`
- `POST /api/jobs/{id}/cancel` → Job. Queued cancellation immediate, running cancellation terminates owned worker group.
- `GET /api/jobs/{id}/video` → inline MP4 (range supported)
- `GET /api/jobs/{id}/download` → attachment MP4
- `GET /api/jobs/{id}/poster` → PNG
- `GET /api/jobs/{id}/character` → generated PNG, for character jobs; backend attaches asset to character only if project revision unchanged.
- `GET /api/jobs/{id}/package` → ZIP of MP4, poster, description, metadata, saved project, captions and checklist. Metadata includes SHA-256 media hashes, approved=false, published=false, and private YouTube target; status is draft or review-ready. Preview bundle always allowed; metadata says draft until review complete AND uploaded real audio. No approvals or posting. Character jobs do not get package.

Job: `{id,project_id,kind,status:"queued"|"running"|"succeeded"|"failed"|"cancelled",progress:0..100,message,error:null|string,created_at,updated_at,project_revision,format,resolution,video_url:null|string,download_url:null|string,poster_url:null|string,character_url:null|string,package_url:null|string,guide_audio:true|false,review_ready:false|true}`.
Errors: HTTP error with `{detail:string}`. GET response includes no host filesystem paths/secret values.

## Worker contract

Backend writes a project snapshot and request JSON to app-owned job directory, then supervisor runs:
`python -m studio.web.worker --project PROJECT_JSON --request REQUEST_JSON --out JOB_DIR`.
Request contains kind/provider/character_id/format/resolution and `assets_dir` (trusted server path).
No user supplied executables or shell strings. Worker updates `progress.json` atomically:
`{progress:int,message:string}`. Success writes `result.json` with `{kind,guide_audio,seconds,width,height, ...}`;
fixed filenames `video.mp4`, `poster.png`, `captions.srt`, `character.png`, `metadata.json`.
Renderer must not start a new session for ffmpeg children: supervisor owns process group.

`studio.web.supervisor.run_supervised(command:list[str], *, cwd:Path, log_path:Path, cancel_event:threading.Event, timeout_seconds:int=900) -> dict`
returns `{returncode,reason,resource_stopped,cancelled}`; only returns after child tree stopped, or raises a fatal cleanup exception. Supervisor takes existing repo `output/.guarded-run.lock` (or explicit data-root lock on EC2), enforces 10GB disk floor, samples pressure (Mac) / Linux available-memory + cgroup every second. Limit CPU pools=3, no runtime downloads. No nested guards.

Default app data `output/web-studio`; configurable `STUDIO_WEB_DATA_DIR`. API binds loopback by default. Non-loopback refuses startup without `STUDIO_WEB_TOKEN` of >=24 characters. Frontend unlock via bearer token held only in sessionStorage. Downloads use authenticated fetch→Blob; video/poster assets may use same-origin HttpOnly session cookie created by `POST /api/session` body `{token}` (constant-time validation), delete session via `DELETE /api/session`. Mutating calls reject foreign Origin. Cookie SameSite=Strict, path=/, token never goes in URL. EC2 TLS/reverse proxy and private access required.

Shared helper: `studio.web.renderer.character_preview(character:dict) -> bytes` returns original procedural PNG (512 square), used by character preview endpoint. Pure CPU, no models. For model/uploaded character assets the backend serves the validated owned raster instead. Worker request also includes trusted `asset_paths:{asset_id:absolute_local_path}` built by backend, never accepted from clients.
