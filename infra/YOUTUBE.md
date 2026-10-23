# YouTube private uploads (user setup)

**Decision (2026-10-23):** code uploads videos to the user's YouTube channel as **private**, with
title, description, tags, thumbnail, AI disclosure and an accurate made-for-kids setting. **The
user reviews each video in YouTube Studio and clicks Publish.** No code path makes a video public
or unlisted: `studio/publish/youtube.py` rejects any other privacy and re-checks what YouTube
reports back. Code: `studio/publish/youtube.py`, gate: `studio/publish/gate.py`, CLI:
`scripts/youtube_upload.py`.

## 1. One-time Google setup (the user does this)

1. Go to <https://console.cloud.google.com/>, create a project (for example `studio-uploads`).
   Use a personal Google account, not an employer account.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen:** user type *External*, add yourself under **Test
   users**. While the app is in *Testing*, refresh tokens expire after 7 days, so re-run `auth`
   weekly or publish the consent screen (unverified is fine for your own account).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →** application type
   **Desktop app**. Download the JSON.
5. Store it outside the repo with tight permissions, e.g.
   `mkdir -p ~/.config/studio && mv ~/Downloads/client_secret_*.json ~/.config/studio/youtube-client.json && chmod 600 ~/.config/studio/youtube-client.json`.
6. Add to `.env` (git-ignored): `YOUTUBE_CLIENT_SECRETS_PATH=~/.config/studio/youtube-client.json`.
   Optional: `YOUTUBE_TOKEN_PATH` (default `~/.config/studio/youtube-token.json`).
7. Install the libraries (proposed optional extra; until it lands, `uv pip install
   google-api-python-client google-auth-oauthlib`).
8. Run `uv run --no-sync python scripts/youtube_upload.py auth`. A browser opens (loopback
   redirect on 127.0.0.1, PKCE); sign in as the channel owner and choose the channel. The token
   is cached with mode 600. Add `--playlists` if you want `--playlist` uploads (that asks for the
   broader `youtube` scope; plain uploads and thumbnails need only `youtube.upload`).

Never paste the client JSON or the token into chat, logs or the repo.

## 2. Per video

```bash
# 1. Write RUN_DIR/meta.json: title (≤100 chars), description (≤5000 bytes), tags (≤500 chars
#    total), thumbnail (1280x720 JPG/PNG, ≤2 MB recommended, API max 50 MB), category_id
#    "24" Entertainment or "27" Education, video "9x16" (Shorts) or "16x9" (long-form).
# 2. Watch the whole video, then HUMAN approval (interactive; type the shown code):
uv run --no-sync python scripts/youtube_upload.py approve output/kids/plinkety-pond-ep01 --by "Your Name"
# 3. Check the exact API requests (no network):
uv run --no-sync python scripts/youtube_upload.py upload output/kids/plinkety-pond-ep01 \
    --brand plinkety-pond --meta output/kids/plinkety-pond-ep01/meta.json --dry-run
# 4. Upload (private), then open the printed Studio link, review, click Publish:
uv run --no-sync python scripts/youtube_upload.py upload output/kids/plinkety-pond-ep01 \
    --brand plinkety-pond --meta output/kids/plinkety-pond-ep01/meta.json [--playlist PL...]
uv run --no-sync python scripts/youtube_upload.py status
```

- Approval binds to the sha256 of the video, the thumbnail and the reviewed metadata (title,
  description, tags, category, audience). Any edit or re-render voids it. Re-running the kids
  tool rewrites `run.json`, which also drops the approval. Pipeline jobs (`job.json`) use
  `studio approve JOB_ID --by NAME` instead.
- Idempotent per content hash (`youtube_uploads` table in `output/ledger.sqlite`). A re-run
  returns the existing video id and only retries a failed thumbnail or playlist step. An
  interrupted upload stays `pending` and blocks retries (`NeedsReconcile`): check YouTube Studio
  first so you never get a duplicate.
- **`--schedule ISO`** (optional, e.g. `2026-11-01T09:00:00Z`) sets `status.publishAt`. YouTube
  only accepts it on a private, never-published video, and **YouTube then makes the video public
  at that time automatically**. Leave it off (the default) to publish by hand.

## 3. Audit and quota (verified on developers.google.com, 2026-10-23)

- **Audit:** historically, videos uploaded by projects that had not passed the YouTube API
  Services compliance audit were locked to private. The current `videos.insert` reference says
  unverified projects are "not restricted to private viewing mode". It makes no difference
  here, because uploads are private by decision. Apply for the audit
  (<https://support.google.com/youtube/contact/yt_api_form>) only if you need more quota.
- **Quota:** `videos.insert` now has its **own "Video Uploads" bucket: 1 unit per call, 100
  calls/day** by default (the old "1,600 of 10,000 units" figure is outdated). `thumbnails.set`
  is ~50 units and `playlistItems.insert` 50 units from the default 10,000 units/day bucket.
- Uploads count against the channel's own daily upload limits too. Unverified channels (no phone
  verification) are limited to 15-minute videos and cannot set custom thumbnails. Verify the
  channel at <https://www.youtube.com/verify>.

## 4. Made-for-kids rules (doc 26)

- Only a brand pack with `made_for_kids: true` (currently `plinkety-pond`) may set
  `selfDeclaredMadeForKids=true`. Kids videos are **YouTube only** (no Instagram/Facebook), and
  private uploads only. A kids brand can never be uploaded as "not made for kids", and adult
  brands always stay `made_for_kids=false`. Mislabelling either way breaks COPPA/YouTube policy.
- On made-for-kids videos YouTube switches off, among others: **comments**, the **notification
  bell**, **personalised ads** (ads are contextual only, so expect lower RPM), the Miniplayer,
  cards and end screens, Save to playlist / Watch later, live chat, memberships and merch.
  Plan descriptions and thumbnails without end screens or subscribe prompts.
- AI disclosure: `containsSyntheticMedia=true` on every upload.
- In YouTube Studio before publishing, check: Audience shows "Yes, it's made for kids", the
  altered/synthetic content box is ticked, the thumbnail has no clickbait, and the video plays
  through without visual drift or lyric errors.
