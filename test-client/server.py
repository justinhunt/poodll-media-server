import http.server
import socketserver
import os
import json
import uuid
import hashlib
import datetime
import urllib.request
import urllib.parse
import urllib.error
from urllib.parse import urlparse, parse_qs
from livekit import api
try:
    import redis as redis_lib
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

PORT = 8000

SESSION_SERVER = os.getenv('MEDIA_DOMAIN', os.getenv('AWS_REGION', 'local'))


def log_session_event(event: str, **fields):
    """Emit a structured JSON session log line to stdout."""
    record = {
        "event":     event,
        "server":    SESSION_SERVER,
        "timestamp": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    record.update(fields)
    print(f"[SESSION] {json.dumps(record)}", flush=True)


CLOUDPOODLL_URL = os.getenv("CLOUDPOODLL_URL", "https://cloud.poodll.com")
S3_BUCKET       = os.getenv("S3_BUCKET", "poodll-temp-us-east-1")
AWS_REGION      = os.getenv("AWS_REGION", "us-east-1")
REDIS_URL       = os.getenv("REDIS_URL", "redis://redis:6379")

# Cache TTLs (seconds)
AUTH_CACHE_TTL_OK   = int(os.getenv("AUTH_CACHE_TTL_OK",   "3600"))  # 60 min for success
AUTH_CACHE_TTL_FAIL = int(os.getenv("AUTH_CACHE_TTL_FAIL", "60"))    #  1 min for failure

# In-memory store for completed recordings (used by the /recordings poll endpoint)
completed_recordings = []

# Redis client (initialised lazily on first use)
_redis_client = None

def _get_redis():
    """Return a Redis client, or None if Redis is unavailable."""
    global _redis_client
    if not REDIS_AVAILABLE:
        return None
    if _redis_client is None:
        try:
            _redis_client = redis_lib.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
            _redis_client.ping()  # verify connection
            print("[cache] Redis connected")
        except Exception as e:
            print(f"[cache] Redis unavailable, caching disabled: {e}")
            _redis_client = None
    return _redis_client

def _cache_key(poodlltoken, appid, parent):
    """Composite cache key: cpauth:<sha256(token)>:<appid>:<hostname>"""
    # Hash the raw token so it doesn't sit in plain text as a Redis key
    token_hash = hashlib.sha256(poodlltoken.encode()).hexdigest()[:16]
    hostname   = derive_site_domain(parent)
    return f"cpauth:{token_hash}:{appid}:{hostname}"

def _cache_get(poodlltoken, appid, parent):
    """Return cached (authenticated, apiusername, message) or None on miss."""
    rc = _get_redis()
    if rc is None:
        return None
    try:
        raw = rc.get(_cache_key(poodlltoken, appid, parent))
        if raw:
            data = json.loads(raw)
            print(f"[cache] HIT for appid={appid} parent={parent}")
            return data['authenticated'], data['apiusername'], data['message']
    except Exception as e:
        print(f"[cache] Redis read error: {e}")
    return None

def _cache_set(poodlltoken, appid, parent, authenticated, apiusername, message):
    """Store result in Redis with appropriate TTL."""
    rc = _get_redis()
    if rc is None:
        return
    try:
        ttl = AUTH_CACHE_TTL_OK if authenticated else AUTH_CACHE_TTL_FAIL
        payload = json.dumps({'authenticated': authenticated, 'apiusername': apiusername, 'message': message})
        rc.setex(_cache_key(poodlltoken, appid, parent), ttl, payload)
        print(f"[cache] SET appid={appid} parent={parent} authenticated={authenticated} ttl={ttl}s")
    except Exception as e:
        print(f"[cache] Redis write error: {e}")



def derive_site_domain(parent_url):
    """Extract hostname from a URL, stripping protocol and port."""
    if not parent_url:
        return 'unknown'
    try:
        return urlparse(parent_url).hostname or 'unknown'
    except Exception:
        return 'unknown'


# ---------------------------------------------------------------------------
# Region resolution & S3 URL construction (mirrors PHP fetch_s3_root logic)
# ---------------------------------------------------------------------------
_FRIENDLY_TO_REGION = {
    'dublin':    'eu-west-1',
    'sydney':    'ap-southeast-2',
    'useast1':   'us-east-1',
    'frankfurt': 'eu-central-1',
    'london':    'eu-west-2',
    'saopaulo':  'sa-east-1',
    'mumbai':    'ap-south-1',
    'singapore': 'ap-southeast-1',
    'capetown':  'af-south-1',
    'bahrain':   'me-south-1',
    'ottawa':    'ca-central-1',
    'ningxia':   'cn-northwest-1',
    'tokyo':     'ap-northeast-1',
}
_DEFAULT_BUCKET_REGIONS = {'ap-northeast-1'}
_VALID_EXPIRY_DAYS = {1, 3, 7, 30, 90, 180, 365, 730, 9999}

BUCKET_AUDIO = 'poodll-audioprocessing-out'
BUCKET_VIDEO = 'poodll-videoprocessing-out'

def resolve_region(region_input):
    """Return (official_region, audio_bucket, video_bucket)."""
    raw      = (region_input or '').strip().lower()
    official = _FRIENDLY_TO_REGION.get(raw, raw) or 'ap-northeast-1'
    if official in _DEFAULT_BUCKET_REGIONS:
        return official, BUCKET_AUDIO, BUCKET_VIDEO
    return official, f"{BUCKET_AUDIO}-{official}", f"{BUCKET_VIDEO}-{official}"

def resolve_expiry_days(expiredays):
    """Normalise expiry days; default 365 for invalid values."""
    try:
        days = int(expiredays)
    except (TypeError, ValueError):
        return 365
    return days if days in _VALID_EXPIRY_DAYS else 365

def build_s3_url(official_region, bucket, key):
    """Build the correct public S3 URL for an object key (mirrors PHP fetch_s3_root)."""
    if official_region in ('af-south-1', 'me-south-1'):
        return f"https://{bucket}.s3.{official_region}.amazonaws.com/{key}"
    elif official_region == 'cn-northwest-1':
        return f"https://{bucket}.s3.{official_region}.amazonaws.com.cn/{key}"
    elif official_region == 'us-east-1':
        return f"https://s3.amazonaws.com/{bucket}/{key}"
    else:
        return f"https://s3-{official_region}.amazonaws.com/{bucket}/{key}"

def fetch_s3_base_url(region, mediatype, expiredays, apiusername, parent, owner):
    """Build the full S3 base URL for a session (equivalent to PHP fetch_s3_root).
    Returns a URL ending in '/' to which the filename is appended by the client.
    e.g. https://s3.amazonaws.com/poodll-audioprocessing-out-us-east-1/CP/180/user/demo.poodll.io/owner/
    """

    official, audio_bucket, video_bucket = resolve_region(region)
    bucket = video_bucket if mediatype == 'video' else audio_bucket
    days   = resolve_expiry_days(expiredays)
    host   = derive_site_domain(parent)
    cp_path = f"CP/{days}/{apiusername}/{host}/{owner}/"
    return build_s3_url(official, bucket, cp_path)


def validate_poodll_token(poodlltoken, appid='', parent='', mediatype='audio'):
    """Validate poodlltoken against CloudPoodll, with Redis caching.
    Returns (authenticated: bool, apiusername: str|None, message: str)
    """
    if not poodlltoken:
        return False, None, 'No token provided'

    # --- Cache check ---
    cached = _cache_get(poodlltoken, appid, parent)
    if cached is not None:
        return cached

    # --- Live CloudPoodll request ---
    try:
        params = urllib.parse.urlencode({
            "wstoken":            poodlltoken,
            "wsfunction":         "local_cpapi_fetch_authenticated",
            "moodlewsrestformat": "json",
            "appid":              appid,
            "parent":             parent,
            "mediatype":          mediatype
        }).encode('utf-8')
        req = urllib.request.Request(
            f"{CLOUDPOODLL_URL}/webservice/rest/server.php",
            data=params,
            method="POST",
            headers={"User-Agent": "PoodllMediaServer/1.0"}
        )
        try:
            resp_obj = urllib.request.urlopen(req, timeout=10)
            body = resp_obj.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')

        data          = json.loads(body)
        authenticated = bool(data.get("authenticated", False))
        apiusername   = data.get("apiusername") if authenticated else None
        message       = data.get("message", '')

        # --- Store in cache ---
        _cache_set(poodlltoken, appid, parent, authenticated, apiusername, message)

        return authenticated, apiusername, message
    except Exception as e:
        print(f"[server] CloudPoodll auth error: {e}")
        # Do NOT cache network/parse errors — let the next request retry
        return False, None, str(e)



class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress noisy access logs

    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/token':
            query = parse_qs(parsed_path.query)
            api_key = os.getenv("LIVEKIT_API_KEY", "devkey")
            api_secret = os.getenv("LIVEKIT_API_SECRET", "secret")

            # Core session settings
            video_enabled = query.get('video', ['false'])[0] == 'true'
            resolution    = query.get('res', ['720p'])[0]
            word_ts       = query.get('word_ts', ['true'])[0] == 'true'
            lang          = query.get('lang', ['en'])[0]
            transcribe    = query.get('transcribe', ['true'])[0] == 'true'
            record        = query.get('record', ['true'])[0] == 'true'

            # New Poodll fields
            region      = query.get('region', ['us-east-1'])[0]
            expiredays  = query.get('expiredays', ['180'])[0]
            owner       = query.get('owner', [''])[0]
            poodlltoken = query.get('poodlltoken', [''])[0]
            parent      = query.get('parent', [''])[0]
            appid       = query.get('appid', [''])[0]
            mediatype   = 'video' if video_enabled else 'audio'

            # Validate poodlltoken with CloudPoodll if provided
            apiusername = None
            auth_t0     = datetime.datetime.utcnow()
            if poodlltoken:
                authenticated, apiusername, auth_message = validate_poodll_token(
                    poodlltoken, appid=appid, parent=parent, mediatype=mediatype
                )
                auth_ms = round((datetime.datetime.utcnow() - auth_t0).total_seconds() * 1000)
                log_session_event(
                    "TOKEN_REQUEST",
                    authenticated = authenticated,
                    apiusername   = apiusername,
                    appid         = appid,
                    sitehost      = derive_site_domain(parent),
                    region        = region,
                    owner         = owner,
                    mediatype     = mediatype,
                    latencyMs     = auth_ms,
                    message       = auth_message or '',
                )
                if not authenticated:
                    self.send_response(401)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "error":   "Authentication failed",
                        "message": auth_message or "Invalid poodlltoken"
                    }).encode())
                    print(f"[server] Rejected: {auth_message}")
                    return
                print(f"[server] Authenticated as: {apiusername}")


            # Derive site domain for S3 path construction
            sitedomainname = derive_site_domain(parent)

            # Optional: vocabulary context hint for Whisper (initial_prompt)
            targetsentence = query.get('targetsentence', [''])[0]

            metadata = json.dumps({
                "video":          video_enabled,
                "res":            resolution,
                "word_ts":        word_ts,
                "lang":           lang,
                "transcribe":     transcribe,
                "record":         record,
                "region":         region,
                "expiredays":     expiredays,
                "owner":          owner,
                "poodlltoken":    poodlltoken,
                "parent":         parent,
                "appid":          appid,
                "apiusername":    apiusername,
                "targetsentence": targetsentence or None
            })

            identity = f"test-user-{uuid.uuid4().hex[:6]}"
            grant = api.VideoGrants(
                room_join=True, room="stt-test-room",
                can_publish=True, can_publish_data=True, can_subscribe=True
            )
            token = api.AccessToken(api_key, api_secret) \
                .with_identity(identity) \
                .with_name("Test User") \
                .with_metadata(metadata) \
                .with_grants(grant)

            # Build the base S3 URL (everything except the filename)
            # Client appends poodllfile{trackSid}.mp3 / .mp4 etc.
            if apiusername and owner and expiredays and parent:
                s3_base_url = fetch_s3_base_url(
                    region, mediatype, expiredays, apiusername, parent, owner
                )
            else:
                # Legacy fallback for sessions without full Poodll metadata
                official, audio_bucket, video_bucket = resolve_region(region)
                bucket = video_bucket if video_enabled else audio_bucket
                s3_base_url = build_s3_url(official, bucket, "recordings/")

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "token":       token.to_jwt(),
                "url":         os.getenv("LIVEKIT_PUBLIC_URL", "ws://127.0.0.1:7880"),
                # Client appends poodllfile{audioSid}.{ext} to this to get the full media URL
                "s3BaseUrl":   s3_base_url
            }).encode())

        elif parsed_path.path == '/recordings':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(completed_recordings).encode())

        else:
            super().do_GET()

    def do_POST(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/recording-ready':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                completed_recordings.append(data)
                print(f"[server] Recording ready: {data.get('mediaUrl')}")
            except Exception as e:
                print(f"[server] Failed to parse recording data: {e}")

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        else:
            self.send_response(404)
            self.end_headers()


print(f"Starting server at port {PORT}")
print(f"CloudPoodll URL: {CLOUDPOODLL_URL}")
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()
