const connectBtn = document.getElementById('connectBtn');
const disconnectBtn = document.getElementById('disconnectBtn');
const statusDiv = document.getElementById('status');
const transcriptBox = document.getElementById('transcript');
const videoToggle = document.getElementById('videoToggle');
const transcribeToggle = document.getElementById('transcribeToggle');
const recordToggle = document.getElementById('recordToggle');
const wordTsToggle = document.getElementById('wordTsToggle');
const resSelect = document.getElementById('resSelect');
const langSelect = document.getElementById('langSelect');
// Poodll fields
const poodlltokenInput    = document.getElementById('poodlltokenInput');
const appidInput          = document.getElementById('appidInput');
const parentInput         = document.getElementById('parentInput');
const ownerInput          = document.getElementById('ownerInput');
const regionInput         = document.getElementById('regionInput');
const expiredaysInput     = document.getElementById('expiredaysInput');
const targetsentenceInput = document.getElementById('targetsentenceInput');
const videoContainer = document.getElementById('videoContainer');
const localVideo = document.getElementById('localVideo');
const recordingsGallery = document.getElementById('recordingsGallery');
const recordingsList = document.getElementById('recordingsList');
const playbackArea = document.getElementById('playbackArea');
const mediaPlayerWrapper = document.getElementById('mediaPlayerWrapper');

let room = null;
let currentSession = null;
let sessionTranscripts = [];         // live transcript segments accumulated this session
let resolveTranscriptComplete = null; // set during disconnect wait

function addMessage(speaker, text) {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message';
    msgDiv.innerHTML = `<span class="speaker">${speaker}:</span><span>${text}</span>`;
    transcriptBox.appendChild(msgDiv);
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

async function connect() {
    connectBtn.disabled = true;
    statusDiv.textContent = "Fetching token...";

    try {
        const video = videoToggle.checked;
        const transcribe = transcribeToggle.checked;
        const record = recordToggle.checked;
        const res = resSelect.value;
        const word_ts = wordTsToggle.checked;
        const lang = langSelect.value;
        // Poodll fields
        const poodlltoken    = poodlltokenInput.value.trim();
        const appid          = appidInput.value.trim();
        const parent         = parentInput.value.trim();
        const owner          = ownerInput.value.trim();
        const region         = regionInput.value.trim() || 'us-east-1';
        const expiredays     = expiredaysInput.value.trim() || '180';
        const targetsentence = targetsentenceInput.value.trim();

        const params = new URLSearchParams({
            video, res, word_ts, lang, transcribe, record,
            poodlltoken, appid, parent, owner, region, expiredays, targetsentence
        });
        const response = await fetch(`/token?${params}`);
        const tokenData = await response.json();
        if (!response.ok) {
            // Show the human-readable message from CloudPoodll if available
            const reason = tokenData.message || tokenData.error || `HTTP ${response.status}`;
            throw new Error(reason);
        }

        // s3BaseUrl is the full path prefix, e.g:
        // https://bucket.s3.region.amazonaws.com/cp/180/user/demo.poodll.io/owner/

        // or the legacy: https://bucket.s3.region.amazonaws.com/recordings/
        const s3BaseUrl = tokenData.s3BaseUrl;
        
        statusDiv.textContent = "Connecting to LiveKit...";
        
        // Ensure LivekitClient is available from the UMD bundle
        const lk = window.LivekitClient;
        if (!lk) {
            throw new Error("LivekitClient library not loaded");
        }

        room = new lk.Room({
            audioCaptureDefaults: {
                autoGainControl: true,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        // Reset session state for new connection
        sessionTranscripts = [];
        resolveTranscriptComplete = null;

        // Listen for data messages from the agent
        room.on(lk.RoomEvent.DataReceived, (payload) => {
            const str = new TextDecoder('utf-8').decode(payload);
            console.log('[DataReceived]', str.substring(0, 120));

            // Try structured JSON message first
            try {
                const msg = JSON.parse(str);
                if (msg.type === 'TRANSCRIPT_COMPLETE') {
                    console.log('[TRANSCRIPT_COMPLETE] received, fullTranscript:', msg.fullTranscript?.substring(0, 80));
                    if (resolveTranscriptComplete) {
                        resolveTranscriptComplete(msg.fullTranscript || '');
                        resolveTranscriptComplete = null;
                    }
                    return;
                }
                return; // other JSON message types — ignore for now
            } catch (_) { /* not JSON — fall through */ }

            // Legacy colon-separated format: "participant_id:transcript text"
            const sep = str.indexOf(':');
            if (sep !== -1) {
                const speaker = str.substring(0, sep);
                const text    = str.substring(sep + 1).trim();
                if (text) sessionTranscripts.push(text);
                addMessage(speaker, text);
            } else {
                addMessage('System', str);
            }
        });

        // Use the websocket URL from the token endpoint (or default to localhost)
        const wkUrl = tokenData.url || 'ws://localhost:7880';
        await room.connect(tokenData.url, tokenData.token);
        
        statusDiv.textContent = "Connected. Activating microphone...";
        
        // Publish microphone
        await room.localParticipant.setMicrophoneEnabled(true);

        // Capture audio track SID to build S3 URLs
        const audioPub = Array.from(room.localParticipant.audioTrackPublications.values())[0];
        const audioSid = audioPub ? audioPub.trackSid : null;
        const hasVideo  = videoToggle.checked;
        const ext       = hasVideo ? 'mp4' : 'mp3';
        const willTranscribe = transcribeToggle.checked;
        const willRecord     = recordToggle.checked;

        // The server pre-computed the base URL - just append the filename
        if (audioSid && willRecord) {
            const filename  = `poodllfile${audioSid}`;
            const mediaFile = `${filename}.${ext}`;         // e.g. poodllfileABC.mp3
            currentSession = {
                trackSid: audioSid,
                mediaUrl: `${s3BaseUrl}${mediaFile}`,
                vttUrl:   willTranscribe ? `${s3BaseUrl}${mediaFile}.vtt` : null,  // e.g. poodllfileABC.mp3.vtt
                hasVideo:  hasVideo
            };
            console.log('[Session] Recording will be at:', currentSession.mediaUrl);
        } else if (audioSid) {
            console.log('[Session] Recording disabled - no gallery entry will be added.');
        }
        
        // Publish camera if requested
        if (videoToggle.checked) {
            console.log(`Publishing camera at ${resSelect.value}...`);
            statusDiv.textContent = "Activating camera...";
            videoContainer.style.display = 'flex';
            
            const resolution = resSelect.value === '1080p' ? lk.VideoPresets.h1080.resolution : lk.VideoPresets.h720.resolution;
            
            try {
                await room.localParticipant.setCameraEnabled(true, { resolution });
                console.log("Camera enabled with requested resolution.");
            } catch (cameraErr) {
                console.warn("Failed to start camera with specific resolution, falling back to defaults:", cameraErr);
                statusDiv.textContent = "Camera HQ failed, trying defaults...";
                await room.localParticipant.setCameraEnabled(true);
                console.log("Camera enabled with default resolution.");
            }
            
            // Find the video track to attach
            const videoPub = Array.from(room.localParticipant.videoTrackPublications.values())[0];
            if (videoPub && videoPub.track) {
                videoPub.track.attach(localVideo);
                console.log("Video track attached to preview.");
            }
            statusDiv.textContent = "Audio & Video active. Start recording!";
        } else {
            videoContainer.style.display = 'none';
            statusDiv.textContent = "Microphone active. Start speaking locally!";
        }
        
        connectBtn.style.display = 'none';
        disconnectBtn.disabled = false;

    } catch (e) {
        console.error(e);
        statusDiv.textContent = `Error: ${e.message}`;
        connectBtn.disabled = false;
    }
}

async function disconnect() {
    disconnectBtn.disabled = true;
    statusDiv.textContent = '⏳ Finalising transcript…';

    // 1. Stop all tracks — this signals the agent to flush remaining audio
    if (room) {
        room.localParticipant.trackPublications.forEach(pub => {
            if (pub.track) { pub.track.stop(); pub.track.detach(); }
        });
    }

    // 2. Wait for TRANSCRIPT_COMPLETE from the agent, or 8s timeout
    const signalPromise  = new Promise(resolve => { resolveTranscriptComplete = resolve; });
    const timeoutPromise = new Promise(resolve => setTimeout(() => resolve(null), 8000));
    const fullTranscript = await Promise.race([signalPromise, timeoutPromise]);

    if (fullTranscript === null) {
        console.warn('[Disconnect] TRANSCRIPT_COMPLETE timed out — assembling from streamed segments');
    }

    // 3. Fully disconnect from LiveKit
    if (room) await room.disconnect();

    // 4. Final transcript: prefer server signal; fall back to client-assembled segments
    const finalTranscript = (fullTranscript != null)
        ? fullTranscript
        : (sessionTranscripts.join(' ') || null);

    // Reset session accumulators
    resolveTranscriptComplete = null;
    sessionTranscripts = [];

    statusDiv.textContent = 'Disconnected';
    videoContainer.style.display = 'none';
    connectBtn.style.display = 'inline-block';
    connectBtn.disabled = false;

    // 5. Add gallery entry — transcript is complete, no polling needed
    if (currentSession) {
        const session = { ...currentSession, fullTranscript: finalTranscript };
        currentSession = null;
        addRecordingToGallery(session);
    }
}


function addRecordingToGallery(data) {
    recordingsGallery.style.display = 'block';

    // Check if an entry for this trackSid already exists (RECORDING_READY updating a placeholder)
    const existingItem = document.getElementById(`gallery-item-${data.trackSid}`);
    if (existingItem) {
        // Merge fullTranscript into the card
        const transcriptEl = existingItem.querySelector('.transcript-text');
        if (transcriptEl && data.fullTranscript) {
            transcriptEl.textContent = data.fullTranscript;
            transcriptEl.style.color = '#e2e8f0';
        }
        const statusEl = existingItem.querySelector('.processing-status');
        if (statusEl) statusEl.remove();
        // Re-enable the Play button with the complete data payload
        const btnEl = existingItem.querySelector('button');
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.onclick = () => playRecording(data);
        }
        return;
    }

    // Build a new gallery entry (called immediately on disconnect - no fullTranscript yet)
    const time = new Date().toLocaleTimeString();
    const item = document.createElement('div');
    item.id = `gallery-item-${data.trackSid}`;
    item.style.cssText = "background: #1e293b; padding: 0.75rem; border-radius: 8px; border: 1px solid #334155; margin-bottom: 0.25rem;";

    const hasTranscript = !!data.fullTranscript;
    item.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.4rem;">
            <div>
                <div style="font-weight:600;">${data.hasVideo ? '📹 Video' : '📻 Audio'} Session</div>
                <div style="font-size:0.8rem; color:#94a3b8;">${time} · ${data.trackSid}</div>
            </div>
            <button onclick='playRecording(${JSON.stringify(data)})'
                    style="padding:0.4rem 0.8rem; font-size:0.8rem;">
                Play
            </button>
        </div>
        ${!hasTranscript ? '<div class="processing-status" style="font-size:0.8rem;color:#64748b;margin-bottom:0.3rem;">⏳ Processing transcript…</div>' : ''}
        <div class="transcript-text"
             style="font-size:0.85rem; color:${hasTranscript ? '#e2e8f0' : '#475569'};
                    max-height:4rem; overflow:hidden; text-overflow:ellipsis;">
            ${hasTranscript ? data.fullTranscript : 'Transcript will appear here when processing is complete.'}
        </div>
    `;
    recordingsList.prepend(item);
}

window.playRecording = function(data) {
    playbackArea.style.display = 'block';
    playbackArea.scrollIntoView({ behavior: 'smooth' });
    mediaPlayerWrapper.innerHTML = '';
    
    // Always use <video> so subtitle tracks render.
    // For audio-only, we give it a dark background poster to prevent the
    // browser from falling back to a compact audio widget.
    const media = document.createElement('video');
    media.controls = true;
    media.autoplay = true;
    media.crossOrigin = "anonymous";
    media.style.width = "100%";
    media.style.borderRadius = "8px";
    media.style.display = "block";
    
    if (!data.hasVideo) {
        // Tiny transparent 1x1 poster keeps the video element in "video" rendering mode
        media.poster = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E";
        media.style.height = "80px";
        media.style.backgroundColor = "#1e293b";
    }
    
    const source = document.createElement('source');
    source.src = data.mediaUrl;
    source.type = data.hasVideo ? 'video/mp4' : 'audio/mpeg';
    media.appendChild(source);
    
    if (data.vttUrl) {
        const track = document.createElement('track');
        track.kind = "subtitles";
        track.label = "English";
        track.srclang = "en";
        track.src = data.vttUrl;
        track.default = true;
        media.appendChild(track);
    }
    
    mediaPlayerWrapper.appendChild(media);
    
    // Subtitles area below the player for audio sessions
    if (!data.hasVideo) {
        const subBox = document.createElement('div');
        subBox.id = 'subtitleDisplay';
        subBox.style.cssText = "min-height: 3rem; padding: 0.5rem; margin-top: 0.5rem; background: #0f172a; border-radius: 6px; color: #f8fafc; font-size: 1rem; text-align: center;";
        subBox.textContent = "Subtitles will appear here";
        mediaPlayerWrapper.appendChild(subBox);
        
        // Mirror subtitle cue to subBox
        media.addEventListener('loadedmetadata', () => {
            const tracks = media.textTracks;
            if (tracks.length > 0) {
                tracks[0].mode = 'hidden'; // hide native rendering, mirror manually
                tracks[0].addEventListener('cuechange', () => {
                    const cue = tracks[0].activeCues[0];
                    subBox.textContent = cue ? cue.text : '';
                });
            }
        });
    }
}

connectBtn.addEventListener('click', connect);
disconnectBtn.addEventListener('click', disconnect);
