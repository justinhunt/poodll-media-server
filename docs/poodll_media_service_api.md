# Poodll Media Service — Client Integration Guide

This document describes how to integrate with the LiveKit-based media recording and transcription service from any client application (PHP, JavaScript, iOS, Android, etc.).

---

## Architecture Overview

```
Your Backend (PHP/etc.)
    │
    │  1. Validate poodlltoken with CloudPoodll → get apiusername
    │  2. Generate JWT with session metadata + apiusername
    │  3. Return { token, url, s3BaseUrl } to client
    ▼
LiveKit Server (wss://media-eu.poodll.com:7880)
    │
    │  4. Client connects with token, publishes audio/video tracks
    ▼
Worker Agent (Python)
    │
    ├── 5a. Streams live transcript segments via Data Channel (during session)
    ├── 5b. On session end: sends TRANSCRIPT_COMPLETE signal via Data Channel
    ├── 5c. Triggers LiveKit Egress → saves .mp4 or .mp3 to S3
    ├── 5d. Runs Whisper STT → saves .vtt, .txt, .json to S3
    └── 5e. [Optional] POSTs RECORDING_READY to a configured callback URL
```

---

## Step 1: Token Generation (Your Backend)

Your backend must:
1. Call the CloudPoodll REST API to validate the user's `poodlltoken` and retrieve `apiusername`
2. Generate a LiveKit JWT containing a `metadata` JSON payload with session configuration
3. Return `{ token, url, s3BaseUrl }` to the client

### CloudPoodll Authentication

> [!NOTE]
> Authentication responses are **cached in Redis** with a 60-minute TTL for success and 60-second TTL for failure — so repeated calls with the same `(poodlltoken, appid, parent)` tuple incur no CloudPoodll round-trip.

```python
params = urllib.parse.urlencode({
    "wstoken":            poodlltoken,
    "wsfunction":         "local_cpapi_fetch_authenticated",
    "moodlewsrestformat": "json",
    "appid":              appid,     # e.g. "mod_minilesson"
    "parent":             parent,    # e.g. "https://mymoodle.com"
    "mediatype":          mediatype  # "audio" or "video"
})
```

**Response:**
```json
{
  "authenticated": 1,
  "apiusername":   "justinmoodle",
  "message":       ""
}
```

| Response field | Type | Description |
|---|---|---|
| `authenticated` | int | `1` = valid, `0` = rejected |
| `apiusername` | string | The API username associated with the token |
| `message` | string | Empty on success; failure reason on rejection (e.g. `"subscription expired"`, `"appid not authorised"`) |

If `authenticated` is `0`, do not issue a token. The `message` should be surfaced to the user.

### Token Structure

```python
import json
from livekit import api

metadata = json.dumps({
    # Core session options
    "video":      True,       # bool  — publish camera track? mp4 if true, mp3 if false
    "res":        "1080p",    # str   — "720p" or "1080p"
    "transcribe": True,       # bool  — run Whisper STT?
    "record":     True,       # bool  — save media + transcripts to S3 via Egress?
    "word_ts":    True,       # bool  — include word-level timestamps in .json?
    "lang":       "en",       # str   — "en", "ja", "ru" (or any Whisper language code)

    # Poodll-specific routing fields
    "region":      "eu-west-1",              # str — AWS region (official code or friendly name)
    "expiredays":  "180",                    # str — valid values: 1,3,7,30,90,180,365,730,9999
    "owner":       "student_username",       # str — used in S3 path
    "poodlltoken": "<user_poodll_token>",    # str — already validated above
    "parent":      "https://demo.poodll.com",# str — the client site URL
    "appid":       "mod_minilesson",         # str — passed to CloudPoodll auth
    "apiusername": "justinmoodle",           # str — from CloudPoodll auth response

    # Optional
    "targetsentence":      "cat, sat, mat",  # str|null — vocabulary hint for Whisper
    "recording_ready_url": ""               # str — HTTP callback URL (leave empty if behind firewall)
})

grant = api.VideoGrants(
    room_join=True,
    room="your-room-name",
    can_publish=True,
    can_publish_data=True,
    can_subscribe=True
)

token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
    .with_identity(unique_user_id) \
    .with_name(display_name) \
    .with_metadata(metadata) \
    .with_grants(grant) \
    .to_jwt()
```

> [!IMPORTANT]
> Include `apiusername` in the metadata. The Worker Agent reads it directly from the JWT — it does **not** make a second CloudPoodll API call. Validation happens once, on your backend.

### Making the Token Request (JS Client → Your Backend)

Your JS client sends a `GET` request to your backend's token endpoint. All session configuration is passed as **query parameters**:

```javascript
const params = new URLSearchParams({
    // Core session options
    video:          true,
    res:            '1080p',
    lang:           'en',
    transcribe:     true,
    record:         true,
    word_ts:        true,

    // Poodll routing
    poodlltoken:    'user_token',
    parent:         'https://mymoodle.com',
    owner:          'student_username',
    appid:          'mod_minilesson',   // ← your Moodle plugin id
    region:         'us-east-1',
    expiredays:     '180',

    // Optional: vocabulary hint for Whisper
    targetsentence: 'cat, sat, mat'
});

const response = await fetch(`https://poodllmediaserver.com/token?${params}`);

if (!response.ok) {
    // 401 — auth rejected by CloudPoodll
    const { error, message } = await response.json();
    showError(message); // e.g. "subscription expired" or "appid not authorised"
    return;
}

const { token, url, s3BaseUrl } = await response.json();
```

### Token Endpoint Response

| Field | Type | Description |
|---|---|---|
| `token` | string | The LiveKit **JWT** — used to authenticate the room connection |
| `url` | string | The LiveKit **WebSocket address** to connect to (e.g. `wss://media-eu.poodll.com:7880`) |
| `s3BaseUrl` | string | Pre-computed S3 path prefix for this session. Append `poodllfile{audioTrackSid}.{ext}` to get the full media URL |

**Example response:**
```json
{
  "token":     "eyJhbGciOiJIUzI1NiJ9...",
  "url":       "wss://media-eu.poodll.com:7880",
  "s3BaseUrl": "https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/"
}
```

> [!NOTE]
> The `token` and `url` are **two separate things**. The token is your auth credential — pass both to `room.connect(url, token)`. The `url` is the LiveKit WebSocket server address.

---

## Step 2: Session Configuration Reference

All `metadata` fields are optional. Defaults are shown below.

### Core Options

| Field | Type | Default | Description |
|---|---|---|---|
| `video` | bool | `false` | Publish camera track. Egress produces `.mp4` if true, `.mp3` if false. |
| `res` | string | `"720p"` | Video resolution for Egress. `"720p"` or `"1080p"`. |
| `transcribe` | bool | `true` | Run Whisper STT. |
| `record` | bool | `true` | Trigger Egress to save media + transcripts to S3. |
| `word_ts` | bool | `true` | Include word-level timestamps in the `.json` output. |
| `lang` | string | `"en"` | Language hint for Whisper (`"en"`, `"ja"`, `"ru"`, etc). Skips auto-detection. |

### Poodll Routing Fields

| Field | Type | Description |
|---|---|---|
| `region` | string | AWS region — official code **or** friendly name (see table below) |
| `expiredays` | string | Retention period path component. Valid values: `1, 3, 7, 30, 90, 180, 365, 730, 9999`. Defaults to `365` if invalid. |
| `owner` | string | Username of the recording owner |
| `poodlltoken` | string | User's Poodll token — validated by backend, embedded in JWT for reference |
| `parent` | string | URL of the client site (e.g. `"https://mymoodle.com"`) |
| `appid` | string | Moodle plugin identifier (e.g. `"mod_minilesson"`) |
| `apiusername` | string | Returned by CloudPoodll auth — embed so the agent can build the S3 path |
| `targetsentence` | string \| null | Optional vocabulary/context hint passed to Whisper as `initial_prompt`. |
| `recording_ready_url` | string | Optional HTTP callback URL. If set, agent POSTs `RECORDING_READY` there after S3 upload. Leave empty if server is behind a firewall. |

### Region Codes

Both official AWS region codes and friendly names are accepted:

| Friendly name | Official code |
|---|---|
| `tokyo` (default) | `ap-northeast-1` |
| `useast1` | `us-east-1` |
| `dublin` | `eu-west-1` |
| `london` | `eu-west-2` |
| `frankfurt` | `eu-central-1` |
| `sydney` | `ap-southeast-2` |
| `singapore` | `ap-southeast-1` |
| `mumbai` | `ap-south-1` |
| `capetown` | `af-south-1` |
| `bahrain` | `me-south-1` |
| `saopaulo` | `sa-east-1` |
| `ottawa` | `ca-central-1` |
| `ningxia` | `cn-northwest-1` |

> [!NOTE]
> S3 bucket names and URLs always use the **official region code**, regardless of which form was passed in.

### Behaviour Matrix

| `transcribe` | `record` | Result |
|---|---|---|
| `true` | `true` | Live transcript segments streamed + `TRANSCRIPT_COMPLETE` signal + media + `.vtt/.txt/.json` saved to S3 |
| `true` | `false` | Live transcript segments streamed + `TRANSCRIPT_COMPLETE` signal — nothing written to S3 |
| `false` | `true` | Media file saved to S3, no transcription |
| `false` | `false` | Connect only |

---

### `targetsentence` — Vocabulary Context for Whisper

Whisper accepts an optional `initial_prompt` parameter which biases its beam search toward vocabulary and phrasing present in the prompt. This is useful for:

- **Heavily accented speakers**: helps Whisper decode uncommon phoneme combinations for domain-specific words
- **Listen & Repeat tasks**: when you know the target phrase and want technical terms, proper nouns, or foreign words transcribed correctly

#### How it works

The prompt is prepended to Whisper's context window as simulated prior speech. It is a **soft bias** — not a hard constraint. Whisper will still transcribe what it hears; it just weights vocabulary from the prompt more heavily when audio is ambiguous.

#### Recommended usage — vocabulary hint pattern

Instead of passing the full target sentence, pass key vocabulary words. This provides the domain signal without pulling the transcript toward the exact phrasing:

```json
// Full sentence (higher risk of over-correction on near-misses)
"targetsentence": "The cat sat on the mat"

// Vocabulary hint (safer — helps with content words, not sentence shape)
"targetsentence": "cat, sat, mat"
```

#### Behaviour by scenario

| Student says | Target sentence | With `targetsentence` | Outcome |
|---|---|---|---|
| Target with heavy accent | "The cat sat on the mat" | `"cat, sat, mat"` | ✅ Correct transcript |
| Near-miss (1 word wrong) | "The cat sat on the mat" | Full sentence | ⚠️ May over-correct to target |
| Clearly wrong | "The dog climbed the ladder" | Full sentence | ✅ Transcribes actual speech |
| Correct | "The cat sat on the mat" | Full sentence | ✅ Correct transcript |

> [!WARNING]
> Avoid passing the full target sentence for **error-detection tasks** (listen & repeat grading). A near-miss like "sat on the **map**" may be corrected to "mat" by the prompt — masking the student's actual error. Use vocabulary hints or omit `targetsentence` entirely in those contexts.

---

## Step 3: Client Connection (JavaScript SDK)

```html
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
```

### Connect and Publish

```javascript
const lk = window.LivekitClient;

// Module-level state — reset before each session
let sessionTranscripts = [];          // accumulates live transcript segments
let resolveTranscriptComplete = null; // signalled when agent finishes processing

// 1. Fetch token from your backend
const { token, url, s3BaseUrl } = await fetch('/your-token-endpoint').then(r => r.json());

// 2. Reset per-session state
sessionTranscripts = [];
resolveTranscriptComplete = null;

// 3. Create room and attach handler BEFORE connecting
const room = new lk.Room({
    audioCaptureDefaults: {
        autoGainControl: true,
        echoCancellation: true,
        noiseSuppression: true,
    }
});

room.on(lk.RoomEvent.DataReceived, (payload) => {
    const str = new TextDecoder('utf-8').decode(payload);

    // JSON control messages (e.g. TRANSCRIPT_COMPLETE)
    try {
        const msg = JSON.parse(str);
        if (msg.type === 'TRANSCRIPT_COMPLETE') {
            if (resolveTranscriptComplete) {
                resolveTranscriptComplete(msg.fullTranscript || '');
                resolveTranscriptComplete = null;
            }
            return;
        }
        return;
    } catch (_) { /* not JSON — fall through */ }

    // Live transcript segment: "participantIdentity:text"
    const sep = str.indexOf(':');
    if (sep !== -1) {
        const speaker = str.substring(0, sep);
        const text    = str.substring(sep + 1).trim();
        if (text) sessionTranscripts.push(text); // accumulate for timeout fallback
        showLiveTranscript(speaker, text);
    }
});

// 4. Connect and publish microphone
await room.connect(url, token);
await room.localParticipant.setMicrophoneEnabled(true);

// 5. Capture audio track SID — used to construct S3 filenames
const audioPub      = Array.from(room.localParticipant.audioTrackPublications.values())[0];
const audioTrackSid = audioPub?.trackSid;
const hasVideo      = false; // match your session config
const ext           = hasVideo ? 'mp4' : 'mp3';
const mediaFile     = `poodllfile${audioTrackSid}.${ext}`;

// Pre-compute S3 URLs (valid once upload completes, ~10–30s after session end)
const mediaUrl = `${s3BaseUrl}${mediaFile}`;      // e.g. ...poodllfileXYZ.mp3
const vttUrl   = `${s3BaseUrl}${mediaFile}.vtt`;   // e.g. ...poodllfileXYZ.mp3.vtt

// 6. Optionally publish camera
if (hasVideo) {
    await room.localParticipant.setCameraEnabled(true);
}
```

---

## Step 4: Stopping the Session

When the user stops recording, **do not disconnect immediately**. Instead:
1. Stop mic/camera tracks (signals the agent to flush remaining audio through Whisper)
2. Wait for the `TRANSCRIPT_COMPLETE` data signal or an 8-second timeout
3. Disconnect — you now have the full transcript

```javascript
async function stopSession() {
    statusDiv.textContent = '⏳ Finalising transcript…';

    // 1. Stop tracks — signals the agent that recording has ended
    room.localParticipant.trackPublications.forEach(pub => {
        if (pub.track) { pub.track.stop(); pub.track.detach(); }
    });

    // 2. Wait for TRANSCRIPT_COMPLETE or 8s timeout
    const signalPromise  = new Promise(resolve => { resolveTranscriptComplete = resolve; });
    const timeoutPromise = new Promise(resolve => setTimeout(() => resolve(null), 8000));
    const fullTranscript = await Promise.race([signalPromise, timeoutPromise]);

    // 3. Disconnect from LiveKit
    await room.disconnect();

    // 4. Use signal transcript; fall back to assembled live segments if timed out
    const finalTranscript = (fullTranscript != null)
        ? fullTranscript
        : (sessionTranscripts.join(' ') || null);

    sessionTranscripts = [];

    // 5. All information is ready:
    //    mediaUrl       — S3 media URL (upload still completing; accessible in ~10–30s)
    //    vttUrl         — S3 VTT URL
    //    finalTranscript — complete session transcript
    displayResult(mediaUrl, vttUrl, finalTranscript);
}
```

### `TRANSCRIPT_COMPLETE` Data Message

Sent by the Worker Agent via the Reliable Data Channel once all audio has been processed.

```json
{
  "type":           "TRANSCRIPT_COMPLETE",
  "trackSid":       "TR_AMXXc4aLv73k5V",
  "fullTranscript": "The cat sat on the mat."
}
```

| Field | Description |
|---|---|
| `type` | Always `"TRANSCRIPT_COMPLETE"` |
| `trackSid` | The audio track SID for this session |
| `fullTranscript` | Complete, ordered transcript for the entire session |

> [!NOTE]
> `TRANSCRIPT_COMPLETE` arrives **before** the S3 upload completes — typically 2–5 seconds after track stop on GPU. The media/VTT files may not be on S3 yet; design your UI to enable playback after a short delay or with retry logic.

---

## Step 5: Optional HTTP Callback (recording_ready_url)

If your server is **publicly accessible** (not behind a firewall), you can opt in to receive a `RECORDING_READY` HTTP POST once the S3 upload is complete.

Set `recording_ready_url` in the JWT metadata:

```python
"recording_ready_url": "https://mymoodle.com/local/poodll/recording_ready.php"
```

**Payload:**

```json
{
  "type":           "RECORDING_READY",
  "trackSid":       "TR_AMXXc4aLv73k5V",
  "mediaUrl":       "https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3",
  "vttUrl":         "https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3.vtt",
  "hasVideo":       false,
  "fullTranscript": "The cat sat on the mat."
}
```

> [!WARNING]
> Most Moodle sites are behind firewalls — leave `recording_ready_url` empty for those. The `TRANSCRIPT_COMPLETE` signal (Step 4) is the primary transcript delivery mechanism and requires no incoming HTTP reach.

---

## Step 6: S3 Output Files

### S3 Path Format

```
CP/{expiredays}/{apiusername}/{sitehost}/{owner}/poodllfile{audioTrackSid}.{ext}
```

`sitehost` is derived from the `parent` URL — hostname only:
- `https://demo.poodll.com` → `demo.poodll.com`
- `https://localhost:8051` → `localhost`

### S3 URL Format (varies by region)

| Region | URL format |
|---|---|
| `us-east-1` | `https://s3.amazonaws.com/{bucket}/{key}` |
| `ap-northeast-1` (tokyo) | `https://s3-ap-northeast-1.amazonaws.com/{bucket}/{key}` |
| `af-south-1`, `me-south-1` | `https://{bucket}.s3.{region}.amazonaws.com/{key}` |
| `cn-northwest-1` | `https://{bucket}.s3.{region}.amazonaws.com.cn/{key}` |
| All others | `https://s3-{region}.amazonaws.com/{bucket}/{key}` |

> [!TIP]
> You never need to construct these URLs manually. The token endpoint returns `s3BaseUrl` with the correct format for the session's region. Just append `poodllfile{audioTrackSid}.{ext}`.

### Example URLs (eu-west-1, audio session)

```
Media: https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3
VTT:   https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3.vtt
TXT:   https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3.txt
JSON:  https://s3-eu-west-1.amazonaws.com/poodll-audioprocessing-out-eu-west-1/CP/180/justinmoodle/demo.poodll.com/student1/poodllfileTR_AMXXc4aLv73k5V.mp3.json
```

### File Descriptions

| File | Condition | Description |
|---|---|---|
| `.mp4` | `video=true`, `record=true` | Muxed audio + video, H264 |
| `.mp3` | `video=false`, `record=true` | Audio only |
| `.mp4.vtt` / `.mp3.vtt` | `transcribe=true`, `record=true` | WebVTT subtitles, time-synced to media |
| `.mp4.txt` / `.mp3.txt` | `transcribe=true`, `record=true` | Plain text transcript |
| `.mp4.json` / `.mp3.json` | `word_ts=true`, `record=true` | AWS Transcribe-compatible JSON with word timestamps |

---

## Step 7: Playback with Subtitles

Always use a `<video>` element — only `<video>` renders WebVTT subtitle tracks in browsers.

```javascript
function createPlayer(mediaUrl, vttUrl, hasVideo) {
    const video = document.createElement('video');
    video.controls = true;
    video.crossOrigin = 'anonymous'; // required for cross-origin VTT from S3

    if (!hasVideo) {
        // Audio-only: use a slim video element so subtitles still render
        video.style.height = '80px';
        video.poster = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E";
    }

    const source = document.createElement('source');
    source.src = mediaUrl;
    source.type = hasVideo ? 'video/mp4' : 'audio/mpeg';
    video.appendChild(source);

    if (vttUrl) {
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = 'Transcript';
        track.srclang = 'en';
        track.src = vttUrl;
        track.default = true;
        video.appendChild(track);
    }

    return video;
}
```

---

## Environment Variables

### Token Server

| Variable | Description |
|---|---|
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret (≥ 32 bytes) |
| `LIVEKIT_URL` | Internal WebSocket URL (e.g. `ws://livekit-server:7880`) |
| `LIVEKIT_PUBLIC_URL` | Public WSS URL returned to clients (e.g. `wss://media-eu.poodll.com:7880`) |
| `CLOUDPOODLL_URL` | Base URL of the CloudPoodll server (e.g. `https://cloud.poodll.com`) |
| `REDIS_URL` | Redis connection URL for auth caching (e.g. `redis://redis:6379`) |
| `AUTH_CACHE_TTL_OK` | Seconds to cache a successful auth response (default: `3600`) |
| `AUTH_CACHE_TTL_FAIL` | Seconds to cache a failed auth response (default: `60`) |
| `MEDIA_DOMAIN` | Public domain for this deployment — used by Caddy for TLS |

### Worker Agent

| Variable | Description |
|---|---|
| `LIVEKIT_URL` | Internal WebSocket URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `AWS_ACCESS_KEY_ID` | AWS access key with S3 write permissions |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_REGION` | Default AWS region (for boto3 client init) |
| `RECORDING_READY_URL` | Server-wide HTTP callback URL (optional; per-session `recording_ready_url` metadata takes precedence) |

---

## Quick Integration Checklist

- [ ] Call CloudPoodll auth API → get `apiusername`
- [ ] Generate JWT with metadata including `apiusername` and all Poodll routing fields
- [ ] Return `{ token, url, s3BaseUrl }` to client
- [ ] Client: reset `sessionTranscripts = []` before connecting
- [ ] Client: register `DataReceived` handler **before** `room.connect()`
- [ ] Client: connect room, enable mic, capture `audioTrackSid`
- [ ] Client: pre-compute `mediaUrl = s3BaseUrl + 'poodllfile' + audioTrackSid + '.mp3'`
- [ ] On stop: stop tracks → await `TRANSCRIPT_COMPLETE` or 8s timeout → disconnect
- [ ] Use `<video crossOrigin="anonymous">` with `<track>` for subtitle playback
- [ ] Ensure S3 bucket has CORS `GET` policy for your app's origin

### Example S3 CORS Policy

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET"],
    "AllowedOrigins": ["https://your-app-domain.com"],
    "ExposeHeaders": []
  }
]
```
