import io
import os
import subprocess
import time

import numpy as np
import requests
import soundfile as sf
import torch
import torchaudio
import uvicorn
import whisper
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse

SAMPLE_RATE = 16000
LLM_URL = os.getenv("LLM_URL", "http://localhost:1234/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-7b-instruct")
LLM_TIMEOUT = 60

TTS_MODEL_ID = "v5_5_ru"
TTS_SPEAKER = "kseniya"
TTS_SAMPLE_RATE = 48000

SYSTEM_PROMPT = (
    "Ты — умная колонка для дома. Отвечай кратко и по делу, 1-3 предложения, "
    "обычной разговорной речью, без смайликов и форматирования. Всегда говори по-русски."
)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {DEVICE}")

print("Loading Whisper small...")
stt_model = whisper.load_model("small", device=DEVICE)

print(f"Loading Silero {TTS_MODEL_ID}/{TTS_SPEAKER}...")
tts_model, _ = torch.hub.load(
    repo_or_dir="snakers4/silero-models",
    model="silero_tts",
    language="ru",
    model_id=TTS_MODEL_ID,
    speaker=TTS_MODEL_ID,
)

print("Loading Silero VAD...")
vad_model, _vad_utils = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
(
    get_speech_timestamps,
    _save_audio,
    _read_audio,
    _vad_iterator,
    collect_chunks,
) = _vad_utils

app = FastAPI(title="Smart Speaker Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBUI_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Умная колонка</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;
  display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:40px 16px}
h1{font-size:28px;margin-bottom:8px}
.sub{color:#8b949e;margin-bottom:40px}
.mic-btn{
  width:140px;height:140px;border-radius:50%;border:4px solid #30363d;
  background:#161b22;color:#e6edf3;font-size:18px;cursor:pointer;
  transition:all .15s;user-select:none;position:relative}
.mic-btn:hover{border-color:#58a6ff;background:#1c2533}
.mic-btn.recording{border-color:#f85149;background:#2a1215;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(248,81,73,.5)}50%{box-shadow:0 0 0 16px rgba(248,81,73,0)}}
.mic-btn:disabled{opacity:.5;cursor:default}
.status{margin-top:24px;font-size:14px;color:#8b949e;min-height:24px;text-align:center}
.panel{width:100%;max-width:560px;margin-top:32px}
.block{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;margin-bottom:14px}
.block .label{font-size:11px;text-transform:uppercase;color:#8b949e;margin-bottom:6px;letter-spacing:.5px}
.block .text{font-size:15px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.block .text.empty{color:#484f58;font-style:italic}
audio{width:100%;margin-top:8px}
</style>
</head>
<body>
<h1>Умная колонка</h1>
<p class="sub">Нажми и говори &mdash; отпущу &mdash; и получишь ответ</p>

<button class="mic-btn" id="btn">🎙 Микрофон</button>
<div class="status" id="status"></div>

<div class="panel">
  <div class="block" id="stt-block">
    <div class="label">Вы сказали</div>
    <div class="text empty" id="stt-text">&mdash;</div>
  </div>
  <div class="block" id="chat-block">
    <div class="label">Ответ</div>
    <div class="text empty" id="chat-text">&mdash;</div>
  </div>
  <div class="block" id="audio-block">
    <div class="label">Голос ответа</div>
    <audio id="audio" controls></audio>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const btn=$('#btn'), status=$('#status'), sttText=$('#stt-text'), chatText=$('#chat-text'),
      audio=$('#audio');
let rec=null, chunks=[], audioCtx=null, isRecording=false;

function setStatus(t){ status.textContent=t||''; }
function show(el,txt){ el.textContent=txt||'—'; el.classList.toggle('empty',!txt); }

async function process(blob){
  try{
    btn.disabled=true;
    setStatus('Распознаю...');
    const form=new FormData();
    form.append('file', blob, 'audio.webm');
    const sttResp=await fetch('/stt',{method:'POST',body:form});
    if(!sttResp.ok){const e=await sttResp.text(); throw new Error('STT: '+e);}
    const stt=await sttResp.json();
    show(sttText, stt.text);
    if(!stt.text){setStatus('Не разобрал, попробуйте ещё раз'); return;}
    setStatus('Думаю...');
    const chatResp=await fetch('/chat?'+new URLSearchParams({prompt:stt.text}),{method:'POST'});
    const chat=await chatResp.json();
    show(chatText, chat.text);
    setStatus('Говорю...');
    const ttsResp=await fetch('/tts?'+new URLSearchParams({text:chat.text}),{method:'POST'});
    const wav=await ttsResp.blob();
    const url=URL.createObjectURL(wav);
    audio.src=url;
    audio.play();
    setStatus('Готово');
  }catch(e){
    setStatus('Ошибка: '+e.message);
  }finally{
    btn.disabled=false;
  }
}

function stopRecording(){
  if(!rec||rec.state==='inactive') return;
  rec.stop();
}

btn.addEventListener('mousedown',async()=>{
  if(isRecording){stopRecording();return;}
  sttText.textContent='—'; sttText.classList.add('empty');
  chatText.textContent='—'; chatText.classList.add('empty');
  audio.removeAttribute('src');
  chunks=[];
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  rec=new MediaRecorder(stream,{mimeType:'audio/webm;codecs=opus'});
  rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
  rec.onstop=async()=>{
    stream.getTracks().forEach(t=>t.stop());
    isRecording=false; btn.classList.remove('recording'); btn.textContent='🎙 Микрофон';
    await process(new Blob(chunks,{type:'audio/webm'}));
  };
  rec.start();
  isRecording=true; btn.classList.add('recording'); btn.textContent='⏹ Стоп';
  setStatus('Слушаю...');
});
btn.addEventListener('mouseup',()=>{if(isRecording)stopRecording();});
btn.addEventListener('touchstart',e=>{e.preventDefault(); btn.dispatchEvent(new MouseEvent('mousedown'))},{passive:false});
btn.addEventListener('touchend',e=>{e.preventDefault(); btn.dispatchEvent(new MouseEvent('mouseup'));});
document.addEventListener('keydown',e=>{if(e.code==='Space'&&!e.repeat){e.preventDefault(); btn.dispatchEvent(new MouseEvent(isRecording?'mouseup':'mousedown'));}});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return WEBUI_HTML


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE}


@app.post("/stt")
def stt(file: UploadFile = File(...)):
    raw = file.file.read()
    if raw[:4] != b"RIFF":
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", "pipe:0", "-f", "wav", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1", "pipe:1"],
            input=raw, capture_output=True, timeout=15,
        )
        if proc.returncode != 0 or len(proc.stdout) < 100:
            raise HTTPException(400, f"ffmpeg conversion failed: {proc.stderr[-200:].decode(errors='replace')}")
        raw = proc.stdout
    y, sr = sf.read(io.BytesIO(raw), dtype="float32")

    if y.ndim > 1:
        y = y.mean(axis=1)

    if sr != SAMPLE_RATE:
        t = torch.from_numpy(y)
        y = torchaudio.functional.resample(t, sr, SAMPLE_RATE).numpy()

    t0 = time.time()
    audio_t = torch.from_numpy(y)
    segments = get_speech_timestamps(
        audio_t,
        vad_model,
        sampling_rate=SAMPLE_RATE,
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=600,
        speech_pad_ms=250,
    )
    if not segments:
        return {"text": "", "latency_s": round(time.time() - t0, 2)}

    clipped = collect_chunks(segments, audio_t).numpy()
    if len(clipped) < SAMPLE_RATE * 0.3:
        return {"text": "", "latency_s": round(time.time() - t0, 2)}

    result = stt_model.transcribe(
        clipped,
        language="ru",
        fp16=False,
        condition_on_previous_text=False,
    )
    elapsed = time.time() - t0

    text = result["text"].strip()
    if result.get("no_speech_prob", 0) > 0.5 and not text:
        text = ""
    if len(text.split()) > 40:
        text = ""

    return {"text": text, "latency_s": round(elapsed, 2)}


@app.post("/chat")
def chat(prompt: str = ""):
    if not prompt.strip():
        raise HTTPException(400, "empty prompt")

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
        "stream": False,
    }

    try:
        resp = requests.post(LLM_URL, json=payload, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"LM Studio error: {e}")

    answer = resp.json()["choices"][0]["message"]["content"].strip()
    return {"text": answer}


@app.post("/tts")
def tts(text: str = ""):
    if not text.strip():
        raise HTTPException(400, "empty text")

    audio = tts_model.apply_tts(
        text=text.strip(),
        speaker=TTS_SPEAKER,
        sample_rate=TTS_SAMPLE_RATE,
    )
    if isinstance(audio, list):
        audio = torch.cat(audio)

    buf = io.BytesIO()
    sf.write(buf, audio.cpu().numpy(), TTS_SAMPLE_RATE, format="WAV")
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav")


@app.post("/talk")
def talk(file: UploadFile = File(...)):
    text = stt(file)
    answer = chat(text["text"])
    return tts(answer["text"])


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        ssl_certfile="/etc/ssl/speaker-server.crt",
        ssl_keyfile="/etc/ssl/speaker-server.key",
    )