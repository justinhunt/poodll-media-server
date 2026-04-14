import os
import asyncio
import logging
import datetime
import numpy as np
import boto3
import json
import httpx
from urllib.parse import urlparse
from livekit import rtc, api
from faster_whisper import WhisperModel
import torchaudio
import torch

logging.basicConfig(level=logging.INFO)

# Identifies this server in session logs (set to MEDIA_DOMAIN in production)
SESSION_SERVER = os.getenv('MEDIA_DOMAIN', os.getenv('AWS_REGION', 'local'))


def log_session_event(event: str, **fields):
    """Emit a structured JSON session log line to stdout.
    Each regional server writes its own log stream — identified by SESSION_SERVER.
    These lines can be shipped to CloudWatch Logs via the CW agent.
    """
    record = {
        "event":     event,
        "server":    SESSION_SERVER,
        "timestamp": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    record.update(fields)
    print(f"[SESSION] {json.dumps(record)}", flush=True)


# AWS Setup
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
    region_name=os.getenv('AWS_REGION', 'us-east-1')
)
S3_BUCKET = os.getenv('S3_BUCKET', 'poodll-temp-us-east-1')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

model = None


def derive_site_domain(parent_url):
    """Extract just the hostname from a URL, stripping protocol and port.
    e.g. 'https://demo.poodll.io'  -> 'demo.poodll.io'
    'https://localhost:8051'   -> 'localhost'
    """

    if not parent_url:
        return 'unknown'
    try:
        return urlparse(parent_url).hostname or 'unknown'
    except Exception:
        return 'unknown'

# ---------------------------------------------------------------------------
# Whisper Model Configuration
# ---------------------------------------------------------------------------
WHISPER_MODEL_SIZE   = os.getenv('WHISPER_MODEL_SIZE', 'small')
WHISPER_DEVICE       = os.getenv('WHISPER_DEVICE', 'auto')
WHISPER_COMPUTE_TYPE = os.getenv('WHISPER_COMPUTE_TYPE', 'auto')

# Resolve 'auto' values
if WHISPER_DEVICE == 'auto':
    WHISPER_DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

if WHISPER_COMPUTE_TYPE == 'auto':
    # float16 is standard for GPU, int8 is faster/low-mem for CPU
    WHISPER_COMPUTE_TYPE = 'float16' if WHISPER_DEVICE == 'cuda' else 'int8'

# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------
# Maps friendly names → official AWS region codes
_FRIENDLY_TO_REGION = {
    'dublin':     'eu-west-1',
    'sydney':     'ap-southeast-2',
    'useast1':    'us-east-1',
    'frankfurt':  'eu-central-1',
    'london':     'eu-west-2',
    'saopaulo':   'sa-east-1',
    'mumbai':     'ap-south-1',
    'singapore':  'ap-southeast-1',
    'capetown':   'af-south-1',
    'bahrain':    'me-south-1',
    'ottawa':     'ca-central-1',
    'ningxia':    'cn-northwest-1',
    'tokyo':      'ap-northeast-1',
}

# Regions that use the un-suffixed default bucket (tokyo / ap-northeast-1 only)
_DEFAULT_BUCKET_REGIONS = {'ap-northeast-1'}

def resolve_region(region_input):
    """Resolve a region string (friendly or official) to
    (official_region, audio_bucket, video_bucket).

    Friendly names like 'tokyo' are normalised to their official code.
    All regions get a suffixed bucket EXCEPT ap-northeast-1 (tokyo),
    which uses the un-suffixed legacy bucket name.
    """
    raw = (region_input or '').strip().lower()
    official = _FRIENDLY_TO_REGION.get(raw, raw) or 'ap-northeast-1'

    if official in _DEFAULT_BUCKET_REGIONS:
        audio_bucket = 'poodll-audioprocessing-out'
        video_bucket = 'poodll-videoprocessing-out'
    else:
        audio_bucket = f'poodll-audioprocessing-out-{official}'
        video_bucket = f'poodll-videoprocessing-out-{official}'

    return official, audio_bucket, video_bucket


# Valid expiry day values (mirrors PHP switch-case list; default 365)
_VALID_EXPIRY_DAYS = {1, 3, 7, 30, 90, 180, 365, 730, 9999}

def resolve_expiry_days(expiredays):
    """Normalise expiry days to a valid value, defaulting to 365."""
    try:
        days = int(expiredays)
    except (TypeError, ValueError):
        return 365
    return days if days in _VALID_EXPIRY_DAYS else 365


def build_s3_url(official_region, bucket, key):
    """Build the correct public S3 URL for a given region, bucket and object key.

    URL format varies by region (mirrors PHP fetch_s3_root logic):
      us-east-1        → https://s3.amazonaws.com/{bucket}/{key}
      ap-northeast-1   → https://s3-ap-northeast-1.amazonaws.com/{bucket}/{key}
      af-south-1 /
      me-south-1       → https://{bucket}.s3.{region}.amazonaws.com/{key}
      cn-northwest-1   → https://{bucket}.s3.{region}.amazonaws.com.cn/{key}
      all others       → https://s3-{region}.amazonaws.com/{bucket}/{key}
    """
    if official_region in ('af-south-1', 'me-south-1'):
        return f"https://{bucket}.s3.{official_region}.amazonaws.com/{key}"
    elif official_region == 'cn-northwest-1':
        return f"https://{bucket}.s3.{official_region}.amazonaws.com.cn/{key}"
    elif official_region == 'us-east-1':
        return f"https://s3.amazonaws.com/{bucket}/{key}"
    else:
        # ap-northeast-1 (tokyo) and all other standard regions
        return f"https://s3-{official_region}.amazonaws.com/{bucket}/{key}"


def build_s3_path(cfg, audio_track_sid, extension):
    """Build the S3 object key for a recording (path within the bucket).
    Format: CP/{expiredays}/{apiusername}/{sitehost}/{owner}/poodllfile{sid}.{ext}
    Falls back to legacy 'recordings/{sid}.{ext}' if Poodll fields are absent.
    """
    apiusername = cfg.get('apiusername')
    owner       = cfg.get('owner')
    expiredays  = cfg.get('expiredays')
    parent      = cfg.get('parent')

    if apiusername and owner and expiredays and parent:
        days     = resolve_expiry_days(expiredays)     # normalise to valid value
        site     = derive_site_domain(parent)
        filename = f"poodllfile{audio_track_sid}"
        base     = f"CP/{days}/{apiusername}/{site}/{owner}/{filename}"
    else:
        # Legacy fallback (test sessions without full metadata)
        base = f"recordings/{audio_track_sid}"

    return base, f"{base}.{extension}"


async def load_model_bg():
    global model
    if model is None:
        logging.info(f"Starting background model load: {WHISPER_MODEL_SIZE} on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE})...")
        if WHISPER_DEVICE == 'cuda' and not torch.cuda.is_available():
            logging.warning("WHISPER_DEVICE=cuda requested but CUDA not available! Falling back to CPU.")
            device, compute = 'cpu', 'int8'
        else:
            device, compute = WHISPER_DEVICE, WHISPER_COMPUTE_TYPE

        loop = asyncio.get_event_loop()
        def _load():
            return WhisperModel(WHISPER_MODEL_SIZE, device=device, compute_type=compute)
        model = await loop.run_in_executor(None, _load)
        logging.info(f"Background model load complete ({WHISPER_MODEL_SIZE}/{WHISPER_DEVICE}).")


def format_txt(transcripts):
    transcripts.sort(key=lambda x: x["start"])
    return " ".join([t["text"] for t in transcripts])


def format_vtt(transcripts):
    transcripts.sort(key=lambda x: x["start"])
    lines = ["WEBVTT\n"]
    for t in transcripts:
        start_ms = int(t["start"] * 1000)
        end_ms = int(t["end"] * 1000)
        start_fmt = f"{start_ms//3600000:02d}:{(start_ms//60000)%60:02d}:{(start_ms//1000)%60:02d}.{start_ms%1000:03d}"
        end_fmt = f"{end_ms//3600000:02d}:{(end_ms//60000)%60:02d}:{(end_ms//1000)%60:02d}.{end_ms%1000:03d}"
        lines.append(f"{start_fmt} --> {end_fmt}\n{t['text']}\n")
    return "\n".join(lines)


def format_aws_json(transcripts, track_sid):
    transcripts.sort(key=lambda x: x["start"])
    full_text = " ".join([t["text"] for t in transcripts])
    items = []
    for segment in transcripts:
        for w in segment.get("words", []):
            items.append({
                "start_time": f"{w['start']:.3f}",
                "end_time": f"{w['end']:.3f}",
                "type": "pronunciation",
                "alternatives": [{"confidence": f"{w.get('probability', 1.0):.2f}", "content": w["word"].strip()}]
            })
    return json.dumps({"jobName": track_sid, "results": {"transcripts": [{"transcript": full_text}], "items": items}, "status": "COMPLETED"}, indent=4)


async def upload_and_broadcast(room, transcripts, track_sid, word_ts_enabled, has_video, cfg):
    """Upload transcript files + media to S3. Returns (media_url, s3_ok)."""
    # --- 1. Signal the client that transcription is complete (before slow S3 ops) ---
    full_transcript = format_txt(transcripts) if transcripts else ""
    try:
        signal = json.dumps({
            "type":          "TRANSCRIPT_COMPLETE",
            "trackSid":      track_sid,
            "fullTranscript": full_transcript
        })
        res = room.local_participant.publish_data(signal.encode('utf-8'), reliable=True)
        if asyncio.iscoroutine(res): await res
        logging.info(f"Sent TRANSCRIPT_COMPLETE for {track_sid}")
    except Exception as e:
        logging.warning(f"Failed to send TRANSCRIPT_COMPLETE: {e}")

    # --- 2. Resolve the correct bucket from the session's region metadata ---
    official_region, audio_bucket, video_bucket = resolve_region(cfg.get('region'))
    bucket = video_bucket if has_video else audio_bucket
    logging.info(f"[S3] Region: {cfg.get('region')!r} → {official_region}, bucket: {bucket}")

    extension = "mp4" if has_video else "mp3"
    base_key, media_key = build_s3_path(cfg, track_sid, extension)
    vtt_key  = f"{media_key}.vtt"
    txt_key  = f"{media_key}.txt"
    logging.info(f"Finalizing S3 upload for {track_sid} -> s3://{bucket}/{media_key}")

    txt_content = format_txt(transcripts)
    vtt_content = format_vtt(transcripts)

    media_url = None
    s3_ok     = False
    try:
        s3_client.put_object(Bucket=bucket, Key=txt_key, Body=txt_content.encode('utf-8'))
        s3_client.put_object(Bucket=bucket, Key=vtt_key, Body=vtt_content.encode('utf-8'))
        if word_ts_enabled:
            json_content = format_aws_json(transcripts, track_sid)
            json_key = f"{media_key}.json"
            s3_client.put_object(Bucket=bucket, Key=json_key, Body=json_content.encode('utf-8'))

        media_url = build_s3_url(official_region, bucket, media_key)
        vtt_url   = build_s3_url(official_region, bucket, vtt_key)
        s3_ok     = True

        # --- 3. Optional HTTP callback (only fires if a URL is configured) ---
        callback_url = cfg.get('recording_ready_url') or os.getenv('RECORDING_READY_URL', '')
        if callback_url:
            cb_msg = {
                "type":           "RECORDING_READY",
                "trackSid":       track_sid,
                "mediaUrl":       media_url,
                "vttUrl":         vtt_url,
                "hasVideo":       has_video,
                "fullTranscript": full_transcript
            }
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(callback_url, json=cb_msg, timeout=5.0)
                logging.info(f"Posted RECORDING_READY to {callback_url}")
            except Exception as e:
                logging.warning(f"RECORDING_READY callback failed ({callback_url}): {e}")

    except Exception as e:
        logging.error(f"S3 upload failed: {e}")

    return media_url, s3_ok


async def start_track_egress(audio_track_sid=None, video_track_sid=None, resolution='1080p', cfg=None):
    try:
        url = os.getenv("LIVEKIT_URL", "http://livekit-server:7880")
        api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
        api_secret = os.getenv("LIVEKIT_API_SECRET", "secret")

        primary_sid = audio_track_sid or video_track_sid
        if not primary_sid:
            return

        has_video  = bool(video_track_sid)
        extension  = "mp4" if has_video else "mp3"
        cfg        = cfg or {}

        # Resolve bucket from session region (same logic as upload_and_broadcast)
        official_region, audio_bucket, video_bucket = resolve_region(cfg.get('region'))
        bucket = video_bucket if has_video else audio_bucket

        _, full_key = build_s3_path(cfg, primary_sid, extension)
        preset = api.EncodingOptionsPreset.H264_1080P_30 if resolution == '1080p' else api.EncodingOptionsPreset.H264_720P_30

        logging.info(f"Triggering Egress ({extension}/{resolution}) -> s3://{bucket}/{full_key}")

        lk_api = api.LiveKitAPI(url, api_key, api_secret)
        egress_req = api.TrackCompositeEgressRequest(
            room_name="stt-test-room",
            audio_track_id=audio_track_sid,
            video_track_id=video_track_sid,
            preset=preset,
            file=api.EncodedFileOutput(
                file_type=api.EncodedFileType.MP3 if not has_video else api.EncodedFileType.MP4,
                filepath=full_key,
                disable_manifest=True,
                s3=api.S3Upload(
                    access_key=os.getenv('AWS_ACCESS_KEY_ID', ''),
                    secret=os.getenv('AWS_SECRET_ACCESS_KEY', ''),
                    region=official_region,
                    bucket=bucket
                )
            )
        )
        await lk_api.egress.start_track_composite_egress(egress_req)
        await lk_api.aclose()
    except Exception as e:
        logging.error(f"Egress trigger failed: {e}")



async def run_inference_and_send(room, participant_id, frames, sample_rate, chunk_start_sec, transcript_list, word_ts_enabled, lang='en', initial_prompt=None):
    pcm_data = b"".join(frames)
    audio_np = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0

    if sample_rate != 16000:
        audio_tensor = torch.from_numpy(audio_np).unsqueeze(0)
        resampler = torchaudio.transforms.Resample(sample_rate, 16000)
        audio_np = resampler(audio_tensor).squeeze(0).numpy()

    loop = asyncio.get_event_loop()
    def _infer():
        global model
        if model is None: model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_np, beam_size=5, language=lang, vad_filter=True, word_timestamps=word_ts_enabled, initial_prompt=initial_prompt or None)
        res = []
        for s in segments:
            words = []
            if hasattr(s, 'words') and s.words:
                for w in s.words:
                    words.append({"word": w.word, "start": w.start, "end": w.end, "probability": w.probability})
            res.append((s.start, s.end, s.text, words))
        return res

    try:
        result_segments = await loop.run_in_executor(None, _infer)
        text_parts = []
        for start_offset, end_offset, text, words in result_segments:
            text_parts.append(text)
            processed_words = [{"word": w["word"], "start": chunk_start_sec + w["start"], "end": chunk_start_sec + w["end"], "probability": w["probability"]} for w in words]
            transcript_list.append({"start": chunk_start_sec + start_offset, "end": chunk_start_sec + end_offset, "text": text.strip(), "words": processed_words})

        full_text = " ".join(text_parts).strip()
        if full_text:
            logging.info(f"Transcript for {participant_id}: {full_text}")
            try:
                res = room.local_participant.publish_data(f"{participant_id}:{full_text}".encode('utf-8'), reliable=True)
                if asyncio.iscoroutine(res): await res
            except Exception: pass
    except Exception as e:
        logging.error(f"Inference error: {e}")


async def process_audio_track(room, track, participant, word_ts_enabled, has_video, lang='en', record=True, cfg=None, initial_prompt=None):
    logging.info(f"Processing audio track {track.sid} (Word TS: {word_ts_enabled}, HasVideo: {has_video}, Lang: {lang}, Record: {record}, Prompt: {bool(initial_prompt)})")
    cfg = cfg or {}
    session_start = datetime.datetime.utcnow()

    log_session_event(
        "SESSION_START",
        trackSid    = track.sid,
        apiusername = cfg.get('apiusername'),
        owner       = cfg.get('owner'),
        sitehost    = derive_site_domain(cfg.get('parent')),
        region      = cfg.get('region'),
        lang        = lang,
        transcribe  = cfg.get('transcribe', True),
        record      = record,
        hasVideo    = has_video,
        appid       = cfg.get('appid'),
    )

    audio_stream = rtc.AudioStream(track)
    all_frame_bytes, current_speech_bytes = [], []
    silence_frames, speech_frames, is_speaking, sample_rate, total_audio_time = 0, 0, False, None, 0.0
    track_inference_tasks, transcript_list = [], []
    SILENCE_THRESHOLD = 0.005

    try:
        async for frame_event in audio_stream:
            frame = frame_event.frame
            if sample_rate is None: sample_rate = frame.sample_rate
            duration_sec = frame.samples_per_channel / frame.sample_rate if frame.samples_per_channel else 0.01
            total_audio_time += duration_sec
            f_bytes = bytes(frame.data)
            all_frame_bytes.append(f_bytes)

            audio_np = np.frombuffer(f_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if np.mean(np.abs(audio_np)) > SILENCE_THRESHOLD:
                if not is_speaking:
                    is_speaking = True
                    backtrack = min(30, len(all_frame_bytes))
                    current_speech_bytes = all_frame_bytes[-backtrack:-1] if backtrack > 0 else []
                    chunk_start_sec = max(0.0, total_audio_time - (backtrack * duration_sec))
                silence_frames = 0
                current_speech_bytes.append(f_bytes)
            else:
                silence_frames += 1
                if is_speaking:
                    current_speech_bytes.append(f_bytes)
                    if silence_frames > 80:
                        is_speaking = False
                        if len(current_speech_bytes) > 50:
                            task = asyncio.create_task(run_inference_and_send(room, participant.identity, list(current_speech_bytes), sample_rate, chunk_start_sec, transcript_list, word_ts_enabled, lang, initial_prompt))
                            track_inference_tasks.append(task)
                        current_speech_bytes = []
    except asyncio.CancelledError:
        logging.info(f"Track {track.sid} cancelled.")
    finally:
        if track_inference_tasks: await asyncio.gather(*track_inference_tasks, return_exceptions=True)

        media_url = None
        s3_ok     = False
        if transcript_list and record:
            media_url, s3_ok = await upload_and_broadcast(
                room, transcript_list, track.sid, word_ts_enabled, has_video, cfg
            )
        elif transcript_list:
            logging.info(f"Transcription complete for {track.sid} but record=False, skipping S3 upload.")
        elif record:
            # No speech detected — still signal client so it doesn't hang
            try:
                signal = json.dumps({"type": "TRANSCRIPT_COMPLETE", "trackSid": track.sid, "fullTranscript": ""})
                res = room.local_participant.publish_data(signal.encode('utf-8'), reliable=True)
                if asyncio.iscoroutine(res): await res
            except Exception: pass

        full_text = " ".join(t['text'] for t in transcript_list).strip()
        duration  = (datetime.datetime.utcnow() - session_start).total_seconds()

        log_session_event(
            "SESSION_END",
            trackSid               = track.sid,
            apiusername            = cfg.get('apiusername'),
            owner                  = cfg.get('owner'),
            sitehost               = derive_site_domain(cfg.get('parent')),
            region                 = cfg.get('region'),
            lang                   = lang,
            transcribe             = cfg.get('transcribe', True),
            record                 = record,
            hasVideo               = has_video,
            appid                  = cfg.get('appid'),
            durationSec            = round(total_audio_time, 1),
            wallTimeSec            = round(duration, 1),
            inferenceCount         = len(track_inference_tasks),
            transcriptSegmentCount = len(transcript_list),
            fullTranscript         = full_text or None,
            s3MediaUrl             = media_url,
            s3UploadOk             = s3_ok,
        )



async def main():
    asyncio.create_task(load_model_bg())
    url, api_key, api_secret = os.getenv("LIVEKIT_URL"), os.getenv("LIVEKIT_API_KEY"), os.getenv("LIVEKIT_API_SECRET")
    room = rtc.Room()
    participant_tracks, track_tasks = {}, {}

    @room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        pid = participant.identity
        if pid not in participant_tracks:
            cfg = {
                "video": False, "res": "720p", "word_ts": True,
                "lang": "en", "transcribe": True, "record": True,
                # Poodll fields (all optional)
                "region": None, "expiredays": None, "owner": None,
                "poodlltoken": None, "parent": None, "apiusername": None,
                "appid": None, "targetsentence": None
            }
            try: cfg.update(json.loads(participant.metadata))
            except: pass
            participant_tracks[pid] = {'audio': None, 'video': None, 'video_event': asyncio.Event(), 'cfg': cfg}
            logging.info(f"New participant {pid} config: apiusername={cfg.get('apiusername')}, owner={cfg.get('owner')}, parent={cfg.get('parent')}")

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            participant_tracks[pid]['audio'] = track.sid
            p_cfg = participant_tracks[pid]['cfg']
            if p_cfg.get('transcribe', True):
                track_tasks[track.sid] = asyncio.create_task(process_audio_track(
                    room, track, participant,
                    p_cfg['word_ts'],
                    p_cfg['video'],
                    p_cfg.get('lang', 'en'),
                    p_cfg.get('record', True),
                    p_cfg,
                    p_cfg.get('targetsentence') or None
                ))
            else:
                logging.info(f"Transcription disabled for {pid}, skipping audio processing.")
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            participant_tracks[pid]['video'] = track.sid
            participant_tracks[pid]['video_event'].set()

        if (track.kind == rtc.TrackKind.KIND_AUDIO and not participant_tracks[pid]['video']) or \
           (track.kind == rtc.TrackKind.KIND_VIDEO and not participant_tracks[pid]['audio']):
            if not participant_tracks[pid]['cfg'].get('record', True):
                logging.info(f"Record=False for {pid}, skipping Egress trigger.")
            else:
                p_cfg = participant_tracks[pid]['cfg']
                async def trigger():
                    t = participant_tracks[pid]
                    if t['cfg']['video']:
                        logging.info(f"Video expected for {pid} - waiting up to 3s...")
                        try:
                            await asyncio.wait_for(t['video_event'].wait(), timeout=3.0)
                            logging.info(f"Video track arrived for {pid}: {t['video']}")
                        except asyncio.TimeoutError:
                            logging.warning(f"Video track did NOT arrive in 3s for {pid} - falling back to audio-only")
                    await start_track_egress(t['audio'], t['video'], t['cfg']['res'], t['cfg'])
                asyncio.create_task(trigger())

    @room.on("track_unpublished")
    def on_track_unpublished(pub, p):
        if pub.sid in track_tasks: track_tasks[pub.sid].cancel()

    @room.on("participant_disconnected")
    def on_participant_disconnected(p):
        for pub in p.track_publications.values():
            if pub.sid in track_tasks: track_tasks[pub.sid].cancel()

    grant = api.VideoGrants(
        room_join=True,
        room="stt-test-room",
        can_publish=True,
        can_publish_data=True,
        can_subscribe=True
    )

    await room.connect(url, api.AccessToken(api_key, api_secret)
        .with_identity("stt-agent")
        .with_grants(grant)
        .to_jwt())
    logging.info("Connected!")
    while True: await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
