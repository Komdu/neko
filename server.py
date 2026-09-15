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
from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse

import settings as neko_settings

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
<p class="sub">Нажми и говори &mdash; отпущу &mdash; и получишь ответ
&nbsp;&nbsp;|&nbsp;&nbsp;<a href="/settings" style="color:#58a6ff">⚙ Настройки</a></p>

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


# =====================================================================
#  WebUI «Настройки»: конфиг ESP32 + ассистента + env-секреты + OTA
# =====================================================================
SETTINGS_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Настройки · Умная колонка</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;
  display:flex;flex-direction:column;align-items:center;padding:32px 16px}
h1{font-size:24px;margin-bottom:4px;text-align:center}
.top{a font-weight:700;}
.nav{width:100%;max-width:680px;display:flex;gap:12px;margin-bottom:24px}
.nav a{color:#58a6ff;text-decoration:none;font-size:14px}
.form{width:100%;max-width:680px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:14px;text-transform:uppercase;color:#58a6ff;margin-bottom:12px;letter-spacing:.5px}
.row{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px;align-items:center}
.row label{width:180px;font-size:12px;color:#8b949e;flex-shrink:0}
.row input{flex:1;min-width:200px;background:#0d1117;border:1px solid #30363d;border-radius:6px;
  padding:8px 10px;color:#e6edf3;font-size:14px;font-family:monospace}
.row input[type=range]{flex:1;min-width:200px}
.row .hint{font-size:11px;color:#484f58;width:100%}
.row .btn{flex:0 0 auto}
.btn{background:#238636;border:none;color:#fff;border-radius:6px;padding:8px 14px;
  font-size:13px;cursor:pointer;font-weight:600}
.btn:hover{background:#2ea043}
.btn.gray{background:#30363d}.btn.gray:hover{background:#3d444d}
.btn.red{background:#da3633}.btn.red:hover{background:#eb5a57}
.btn:disabled{opacity:.5;cursor:default}
.msg{padding:8px 12px;border-radius:6px;font-size:13px;margin-bottom:12px;display:none}
.msg.ok{background:#12291a;border:1px solid #238636;color:#7ee787;display:block;white-space:pre-wrap}
.msg.err{background:#2d1215;border:1px solid #da3633;color:#ffa198;display:block;white-space:pre-wrap}
.sec{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sec input{flex:1;min-width:180px}
.sec .chk{flex:0 0 auto;display:flex;gap:4px;align-items:center;font-size:11px;color:#8b949e}
pre.log{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px;margin-top:10px;
  font-size:11px;line-height:1.4;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-all;display:none}
.badge{font-size:11px;border-radius:4px;padding:2px 6px;margin-left:6px}
.badge.ok{background:#12291a;color:#7ee787}.badge.no{background:#2d1215;color:#ffa198}
.title-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;width:100%;max-width:680px}
</style>
</head>
<body>
<div class="title-row">
  <h1>⚙ Настройки «Нэко»</h1>
  <div class="nav"><a href="/">🎙 Голос</a><a href="/settings">⚙ Настройки</a></div>
</div>
<div id="authbox"></div>
<div class="msg" id="msg"></div>

<div class="form" id="form" style="display:none">
 <div class="card">
  <h2>Прошивка ESP32</h2>
  <div class="row"><label>WiFi SSID</label><input data-k="ssid" data-s="esp32"></div>
  <div class="row"><label>WiFi пароль</label><input data-k="wifi_password" data-s="esp32" type="password">
    <button class="btn gray" onclick="gen(['wifi_password'])">случайный</button></div>
  <div class="row"><label>Статический IP</label><input data-k="static_ip" data-s="esp32">
    <button class="btn gray" onclick="checkIp()">проверить занятость</button></div>
  <div class="row"><label>TTS-токен (AUTH_TOKEN)</label><input data-k="auth_token" data-s="esp32" type="password">
    <button class="btn gray" onclick="gen(['auth_token'])">сгенерировать</button></div>
  <div class="row"><label>OTA-пароль</label><input data-k="ota_password" data-s="esp32" type="password">
    <button class="btn gray" onclick="gen(['ota_password'])">сгенерировать</button></div>
  <div class="row"><span class="hint">Кнопка «проверить занятость» — быстрая проверка (ping + TCP 80/443) адреса в поле IP, по умолчанию 192.168.0.200. Если занят — впиши другой и сохрани.</span></div>
 </div>

 <div class="card">
  <h2>Ассистент</h2>
  <div class="row"><label>LLM URL</label><input data-k="llm_url" data-s="assistant"></div>
  <div class="row"><label>LLM модель</label><input data-k="llm_model" data-s="assistant"></div>
  <div class="row"><label>Wake-слова</label><input data-k="wake_words" data-s="assistant"></div>
  <div class="row"><label>Порог wake (0..1)</label><input data-k="wake_ratio" data-s="assistant" type="number" step="0.01" min="0" max="1"></div>
  <div class="row"><label>Слушать, сек</label><input data-k="max_listen_s" data-s="assistant" type="number" step="0.5" min="1" max="30"></div>
  <div class="row"><label>Громкость (0..1)</label><input data-k="volume" data-s="assistant" type="number" step="0.05" min="0" max="1"></div>
  <div class="row"><label>Аудио-порт</label><input data-k="esp32_port" data-s="assistant" type="number"></div>
  <div class="row"><label>Управл. порт</label><input data-k="esp32_ctrl_port" data-s="assistant" type="number"></div>
  <div class="row"><span class="hint">Применяется к ассистенту после перезапуска (./run.sh).</span></div>
 </div>

 <div class="card">
  <h2>Секреты (env-файлы)</h2>
  <div id="envrows"></div>
  <div class="row"><span class="hint">Токены показываются как «установлен». Поле пустым = не менять, галочка «очистить» удаляет ключ.</span></div>
 </div>

 <div class="row" style="justify-content:space-between">
   <button class="btn" onclick="save(false)">Сохранить</button>
   <button class="btn" onclick="save(true)">Сохранить и перепрошить (OTA)</button>
 </div>

 <div class="card">
  <h2>Лог прошивки</h2>
  <button class="btn gray" onclick="flashStatus()">обновить</button>
  <span id="fstat" style="font-size:12px;color:#8b949e;margin-left:10px"></span>
  <pre class="log" id="flog"></pre>
 </div>
</div>

<script>
let PASS=localStorage.getItem('neko_pass')||'', CFG=null;

const $=s=>document.querySelector(s);
function msg(t,ok){const el=$('#msg');el.className='msg '+(ok?'ok':'err');el.textContent=t;}
function esc(s){return (s||'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hdr(){return {'Content-Type':'application/json','X-Settings-Pass':PASS};}
async function api(method,url,body){
  const r=await fetch(url,{method,headers:hdr(),body:body?JSON.stringify(body):undefined});
  if(r.status===428){initAuth();throw new Error('password_not_set');}
  if(r.status===403){setAuth();throw new Error('bad_password');}
  return r.json();
}

function initAuth(){
  $('#authbox').innerHTML=`<div class="card"><h2>Задать пароль WebUI</h2>
    <div class="row"><label>Пароль (мин. 4 символа)</label><input id="newp" type="password"></div>
    <button class="btn" onclick="initPass()">Задать</button></div>`;
}
async function initPass(){
  const p=$('#newp').value;
  const r=await fetch('/api/init_password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:p})});
  const j=await r.json();
  if(r.ok&&j.ok){PASS=p;localStorage.setItem('neko_pass',p);$('#authbox').innerHTML='';load();}
  else msg('Пароль: '+(j.error||'ошибка'),false);
}
function setAuth(){
  $('#authbox').innerHTML=`<div class="card"><h2>Требуется пароль</h2>
    <div class="row"><label>Пароль WebUI</label><input id="setp" type="password"></div>
    <button class="btn" onclick="tryPass()">Войти</button></div>`;
}
async function tryPass(){PASS=$('#setp').value;localStorage.setItem('neko_pass',PASS);$('#authbox').innerHTML='';load();}

async function load(){
  try{
    CFG=await api('GET','/api/settings');
    $('#form').style.display='';
    for(const s of ['esp32','assistant']){
      for(const el of document.querySelectorAll(`input[data-s="${s}"]`)){
        const k=el.dataset.k;
        let v=CFG[s][k];
        if(CFG[s]['has_'+k]===true){el.placeholder=CFG[s]['has_'+k]?'установлен':'не задан';el.value='';}
        else el.value=v!==undefined?v:'';
      }
    }
    $('#envrows').innerHTML=CFG.env.map(e=>`
      <div class="sec" style="margin-bottom:8px">
        <label style="flex:0 0 auto;width:230px;font-size:12px;color:#8b949e">${esc(e.key)}
          ${e.set?`<span class="badge ok">установлен</span>`:`<span class="badge no">нет</span>`}</label>
        <input data-env="${e.key}" placeholder="${e.set?('установлен'):'не задан'}" type="${e.secret?'password':'text'}">
        <span class="chk"><input type="checkbox" data-clear="${e.key}">очистить</span>
      </div>`).join('');
  }catch(e){ if(e.message!=='password_not_set'&&e.message!=='bad_password') msg('Ошибка: '+e.message,false); }
}

function collect(){
  const out={esp32:{},assistant:{},env:{}};
  for(const s of ['esp32','assistant']){
    for(const el of document.querySelectorAll(`input[data-s="${s}"]`)){
      const k=el.dataset.k,v=el.value.trim();
      if(v!=='')out[s][k]=v;
    }
  }
  for(const el of document.querySelectorAll('input[data-env]')){
    const k=el.dataset.env,v=el.value.trim();
    if(v!=='')out.env[k]=v;
  }
  for(const el of document.querySelectorAll('input[data-clear]')){
    if(el.checked)out.env[el.dataset.clear]='__CLEAR__';
  }
  return out;
}

async function save(flash){
  try{
    const j=await api('POST','/api/settings',collect());
    msg('Сохранено:\n'+(j.notes||[]).join('\n'),true);
    if(flash){
      const f=await api('POST','/api/esp32/flash',{});
      msg((j.notes||[]).join('\n')+'\n\n'+f.message,true);
      flashStatus();setInterval(flashStatus,1500);
    }
  }catch(e){ msg('Ошибка: '+e.message,false); }
}

async function gen(keys){
  try{
    const j=await api('POST','/api/esp32/gen_token',{keys});
    for(const k of keys){const el=document.querySelector(`input[data-k="${k}"]`);if(el)el.value=j[k];}
  }catch(e){msg('Ошибка: '+e.message,false);}
}

async function checkIp(){
  const ip=document.querySelector('input[data-k="static_ip"]').value.trim()||'192.168.0.200';
  try{
    const j=await api('GET','/api/esp32/ipcheck?ip='+encodeURIComponent(ip));
    msg(ip+' — '+(j.busy?('ЗАНЯТ'+ (j.hint?': '+j.hint:'')):'свободен'), !j.busy);
  }catch(e){msg('Ошибка: '+e.message,false);}
}

async function flashStatus(){
  try{
    const s=await api('GET','/api/esp32/flash_status');
    $('#fstat').textContent=s.running?'…прошивка идёт':('exit='+(s.exit===null?'?':s.exit));
    const fl=$('#flog');fl.style.display='';fl.textContent=s.log||'';
  }catch(e){}
}
setInterval(()=>{if(CFG)flashStatus();},4000);
load();
</script>
</body>
</html>"""


def _authorize(xpass):
    if not neko_settings.webui_password():
        raise HTTPException(428, "Пароль WebUI не задан — открой /settings и задай")
    if not neko_settings.check_password(xpass or ""):
        raise HTTPException(403, "неверный пароль WebUI")


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return SETTINGS_HTML


@app.get("/api/settings")
def api_settings_get(x_settings_pass: str | None = Header(default=None)):
    _authorize(x_settings_pass)
    return neko_settings.masked_view(neko_settings.load())


@app.post("/api/settings")
def api_settings_set(x_settings_pass: str | None = Header(default=None),
                     payload: dict = Body(...)):
    _authorize(x_settings_pass)
    return {"ok": True, "notes": neko_settings.save(payload)}


@app.post("/api/init_password")
def api_init_password(payload: dict = Body(...)):
    ok, text = neko_settings.set_password(payload.get("password", ""))
    if not ok:
        raise HTTPException(400, text)
    return {"ok": True, "message": text}


@app.post("/api/esp32/gen_token")
def api_gen_token(x_settings_pass: str | None = Header(default=None),
                  payload: dict = Body(default={"keys": ["auth_token"]})):
    _authorize(x_settings_pass)
    out = {}
    for k in payload.get("keys", []):
        if k == "auth_token":
            out["auth_token"] = neko_settings.gen_token()
        elif k == "ota_password":
            out["ota_password"] = neko_settings.gen_token(10)
        elif k == "wifi_password":
            out["wifi_password"] = neko_settings.gen_token(8)
    return out


@app.get("/api/esp32/ipcheck")
def api_ipcheck(ip: str = "", x_settings_pass: str | None = Header(default=None)):
    _authorize(x_settings_pass)
    suggested = "192.168.0.200"
    busy = neko_settings.host_busy(ip or suggested)
    if not ip:
        ip = suggested
    return {"ip": ip, "busy": busy, "hint": (
        "адрес занят — введи другой (например, 192.168.0.201)" if busy
        else "адрес свободен")}


@app.post("/api/esp32/flash")
def api_flash(x_settings_pass: str | None = Header(default=None),
              payload: dict = Body(default={})):
    _authorize(x_settings_pass)
    ok, text = neko_settings.flash_start()
    if not ok:
        raise HTTPException(409, text)
    return {"ok": True, "message": text}


@app.get("/api/esp32/flash_status")
def api_flash_status(x_settings_pass: str | None = Header(default=None)):
    _authorize(x_settings_pass)
    return neko_settings.flash_status()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        ssl_certfile="/etc/ssl/speaker-server.crt",
        ssl_keyfile="/etc/ssl/speaker-server.key",
    )