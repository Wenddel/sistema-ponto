import sqlite3
import json
import os
import io
import hashlib
import re
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ===================== CONFIGURACOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = 8000
ADMIN_USUARIO = "admin"
ADMIN_SENHA = "3223ronte"
sessoes_admin = {}
tentativas_login = {}
acessos_funcionarios = {}
os.makedirs("static", exist_ok=True)

CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()"
}

def sanitizar_texto(texto, max_len=500):
    if not texto: return ""
    texto = str(texto).strip()
    texto = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', texto)
    if len(texto) > max_len: texto = texto[:max_len]
    return texto

def verificar_rate_limit(ip, max_tentativas=5, janela_segundos=300):
    agora = datetime.now()
    if ip in tentativas_login:
        info = tentativas_login[ip]
        if (agora - info["ultima_tentativa"]).total_seconds() > janela_segundos:
            tentativas_login[ip] = {"tentativas": 1, "ultima_tentativa": agora}
            return True
        if info["tentativas"] >= max_tentativas: return False
        info["tentativas"] += 1
        info["ultima_tentativa"] = agora
    else:
        tentativas_login[ip] = {"tentativas": 1, "ultima_tentativa": agora}
    return True

def obter_ip_cliente(handler):
    x_forwarded = handler.headers.get("X-Forwarded-For", "")
    if x_forwarded: return x_forwarded.split(",")[0].strip()
    return handler.client_address[0]

def criar_logo_padrao():
    caminho_logo = os.path.join("static", "logo.png")
    if os.path.exists(caminho_logo):
        print(f"[LOGO] Encontrada: {caminho_logo} ({os.path.getsize(caminho_logo)} bytes)")
        return True
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (256, 256), color='#667eea')
        draw = ImageDraw.Draw(img)
        draw.ellipse([48, 48, 208, 208], fill='white', outline='#764ba2', width=3)
        try:
            font_grande = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
            font_pequena = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        except:
            font_grande = ImageFont.load_default()
            font_pequena = ImageFont.load_default()
        draw.text((100, 70), "♥", fill='#f44336', font=font_grande)
        draw.text((78, 170), "CLINICA", fill='#667eea', font=font_pequena)
        draw.text((70, 192), "PONTO", fill='#667eea', font=font_pequena)
        img.save(caminho_logo, "PNG")
        print(f"[LOGO] Padrao criada: {caminho_logo}")
        return True
    except ImportError:
        print("[LOGO] Pillow nao instalado.")
        return False
    except Exception as e:
        print(f"[LOGO] Erro: {e}")
        return False

DB_NOME = "ponto.db"

def get_db():
    conn = sqlite3.connect(DB_NOME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS funcionarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        cpf TEXT UNIQUE NOT NULL,
        horario_entrada TEXT DEFAULT '08:00:00',
        horario_saida_almoco TEXT DEFAULT '12:00:00',
        horario_retorno_almoco TEXT DEFAULT '13:00:00',
        horario_saida TEXT DEFAULT '18:00:00'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS registros_ponto (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        funcionario_id INTEGER NOT NULL,
        data_hora TEXT NOT NULL,
        tipo TEXT NOT NULL,
        atrasado INTEGER DEFAULT 0,
        minutos_atraso INTEGER DEFAULT 0,
        minutos_banco_horas INTEGER DEFAULT 0,
        justificativa TEXT DEFAULT '',
        ip_dispositivo TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        horario_acesso TEXT DEFAULT '',
        FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS acessos_dispositivos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        funcionario_id INTEGER,
        cpf TEXT NOT NULL,
        data_hora_acesso TEXT NOT NULL,
        ip_dispositivo TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        tipo_acesso TEXT DEFAULT 'pagina_inicial',
        FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE SET NULL
    )""")
    for coluna, tipo in [("minutos_atraso","INTEGER DEFAULT 0"),("minutos_banco_horas","INTEGER DEFAULT 0"),("justificativa","TEXT DEFAULT ''"),("ip_dispositivo","TEXT DEFAULT ''"),("user_agent","TEXT DEFAULT ''"),("horario_acesso","TEXT DEFAULT ''")]:
        try:
            conn.execute(f"ALTER TABLE registros_ponto ADD COLUMN {coluna} {tipo}")
            print(f"[MIGRACAO] Coluna {coluna} adicionada")
        except: pass
    conn.commit()
    conn.close()

init_db()
criar_logo_padrao()

def formatar_cpf(cpf):
    return ''.join(filter(str.isdigit, str(cpf)))

def verificar_atraso(hora_registro, horario_padrao):
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        return t_r > t_p
    except: return False

def calcular_minutos(hora1, hora2):
    try:
        h1 = hora1.split(":"); h2 = hora2.split(":")
        t1 = int(h1[0])*3600 + int(h1[1])*60 + (int(h1[2]) if len(h1)>2 else 0)
        t2 = int(h2[0])*3600 + int(h2[1])*60 + (int(h2[2]) if len(h2)>2 else 0)
        return abs(t1-t2)//60
    except: return 0

def calcular_banco_horas(tipo, hora_registro, func):
    try:
        h_r = hora_registro.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        if tipo == "ENTRADA":
            h_p = func["horario_entrada"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r < t_p: return (t_p-t_r)//60
        elif tipo == "SAIDA_ALMOCO":
            h_p = func["horario_saida_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r > t_p: return (t_r-t_p)//60
        elif tipo == "RETORNO_ALMOCO":
            h_p = func["horario_retorno_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r < t_p: return (t_p-t_r)//60
        elif tipo == "SAIDA":
            h_p = func["horario_saida"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r > t_p: return (t_r-t_p)//60
        return 0
    except: return 0

def obter_ultimo_registro(funcionario_id, data_str):
    conn = get_db()
    ultimo = conn.execute("SELECT * FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ? ORDER BY data_hora DESC LIMIT 1", (funcionario_id, data_str)).fetchone()
    conn.close()
    return ultimo

def verificar_registro_duplicado(funcionario_id, data_str, tipo):
    conn = get_db()
    existe = conn.execute("SELECT id FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ? AND tipo = ? LIMIT 1", (funcionario_id, data_str, tipo)).fetchone()
    conn.close()
    return existe is not None

def verificar_sequencia_valida(ultimo_tipo, novo_tipo):
    sequencia = {None:["ENTRADA"],"ENTRADA":["SAIDA_ALMOCO","SAIDA"],"SAIDA_ALMOCO":["RETORNO_ALMOCO"],"RETORNO_ALMOCO":["SAIDA"],"SAIDA":["ENTRADA"]}
    proximos = sequencia.get(ultimo_tipo, ["ENTRADA"])
    if novo_tipo in proximos: return True, ""
    msgs = {"ENTRADA":"Voce ja registrou ENTRADA hoje. Proximo: SAIDA ALMOCO ou SAIDA.","SAIDA_ALMOCO":"Voce ja registrou SAIDA ALMOCO. Proximo: RETORNO ALMOCO.","RETORNO_ALMOCO":"Voce ja registrou RETORNO ALMOCO. Proximo: SAIDA.","SAIDA":"Voce ja registrou SAIDA hoje. Nova ENTRADA so amanha."}
    return False, msgs.get(ultimo_tipo, "Registro nao permitido agora.")

def gerar_sessao():
    return hashlib.sha256(os.urandom(64)).hexdigest()

def limpar_sessoes_expiradas():
    agora = datetime.now()
    for token in [t for t,e in sessoes_admin.items() if e <= agora]: del sessoes_admin[token]
    for cpf in [c for c,i in acessos_funcionarios.items() if i["expira"] <= agora]: del acessos_funcionarios[cpf]

def verificar_login(handler):
    limpar_sessoes_expiradas()
    try:
        for cookie in handler.headers.get("Cookie","").split(";"):
            cookie = cookie.strip()
            if cookie.startswith("sessao_admin="):
                token = cookie.replace("sessao_admin=","").strip()
                expira = sessoes_admin.get(token)
                if expira and expira > datetime.now(): return True
    except: pass
    return False

def registrar_acesso_dispositivo(cpf, funcionario_id, ip, user_agent, tipo_acesso="pagina_inicial"):
    try:
        conn = get_db()
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO acessos_dispositivos (funcionario_id, cpf, data_hora_acesso, ip_dispositivo, user_agent, tipo_acesso) VALUES (?, ?, ?, ?, ?, ?)", (funcionario_id, cpf, agora, ip, user_agent[:500], tipo_acesso))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"[ERRO ACESSO] {e}")
        return False

def responder_json(handler, dados, status=200, cookies_extra=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    for k,v in CABECALHOS_SEGURANCA.items(): handler.send_header(k,v)
    if cookies_extra:
        for c in cookies_extra: handler.send_header("Set-Cookie", c)
    handler.end_headers()
    handler.wfile.write(json.dumps(dados, ensure_ascii=False).encode("utf-8"))

def responder_html(handler, conteudo, status=200, cookies_extra=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    for k,v in CABECALHOS_SEGURANCA.items(): handler.send_header(k,v)
    if cookies_extra:
        for c in cookies_extra: handler.send_header("Set-Cookie", c)
    handler.end_headers()
    handler.wfile.write(conteudo.encode("utf-8"))

TIPOS_REGISTRO = {
    "ENTRADA": {"label":"ENTRADA","cor":"#4CAF50","icone":"✅"},
    "SAIDA_ALMOCO": {"label":"SAIDA ALMOCO","cor":"#ff9800","icone":"🍽️"},
    "RETORNO_ALMOCO": {"label":"RETORNO ALMOCO","cor":"#2196F3","icone":"↩️"},
    "SAIDA": {"label":"SAIDA","cor":"#f44336","icone":"🚪"}
}

# ===================== ESTILOS E COMPONENTES COMUNS =====================
ESTILO_RODAPE_WELL = """
.rodape-well { margin-top: 22px; padding: 12px; background: linear-gradient(135deg, rgba(102,126,234,0.08) 0%, rgba(240,147,251,0.08) 100%); border-radius: 12px; border: 1px solid rgba(102,126,234,0.15); text-align: center; }
.rodape-content { display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 12px; color: #667eea; }
.rodape-icone { font-size: 16px; animation: pulse-well 2s infinite; }
.rodape-texto { font-weight: 600; }
.rodape-versao { background: linear-gradient(135deg, #667eea, #f093fb, #4facfe); color: white; padding: 3px 10px; border-radius: 12px; font-size: 10px; font-weight: bold; }
@keyframes pulse-well { 0%,100%{transform:scale(1);opacity:1} 50%{transform:scale(1.25);opacity:0.7} }
"""

RODAPE_WELL = """
<div class="rodape-well">
  <div class="rodape-content">
    <span class="rodape-icone">⚡</span>
    <span class="rodape-texto">Desenvolvido por <strong>WELL</strong></span>
    <span class="rodape-versao">v3.0 SECURE</span>
  </div>
</div>
"""

ESTILOS_5D = """
.btn-3d { position: relative; border: none; border-radius: 14px; color: white; font-weight: bold; cursor: pointer; overflow: hidden; transform-style: preserve-3d; transition: all 0.3s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow: 0 6px 0 rgba(0,0,0,0.18), 0 10px 25px rgba(0,0,0,0.22), inset 0 2px 0 rgba(255,255,255,0.4), inset 0 -2px 0 rgba(0,0,0,0.08); }
.btn-3d::before { content:''; position:absolute; top:0; left:-100%; width:100%; height:100%; background:linear-gradient(90deg,transparent,rgba(255,255,255,0.35),transparent); transition:left 0.6s ease; }
.btn-3d:hover::before { left:100%; }
.btn-3d:hover { transform: translateY(-4px); box-shadow: 0 10px 0 rgba(0,0,0,0.18), 0 18px 35px rgba(0,0,0,0.28), inset 0 2px 0 rgba(255,255,255,0.4); }
.btn-3d:active { transform: translateY(2px); box-shadow: 0 2px 0 rgba(0,0,0,0.18), 0 4px 10px rgba(0,0,0,0.18), inset 0 2px 0 rgba(255,255,255,0.4); }
.card-3d { background: white; border-radius: 22px; position: relative; transform-style: preserve-3d; transition: all 0.5s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow: 0 25px 50px rgba(0,0,0,0.15), 0 10px 20px rgba(0,0,0,0.08), inset 0 1px 0 rgba(255,255,255,0.9); }
.card-3d::before { content:''; position:absolute; top:0; left:0; right:0; height:5px; border-radius:22px 22px 0 0; background:linear-gradient(90deg,#667eea,#f093fb,#f5576c,#4facfe,#43e97b,#667eea); background-size:300% 100%; animation:arco-iris 6s linear infinite; }
@keyframes arco-iris { 0%{background-position:0% 50%} 100%{background-position:300% 50%} }
.input-moderno { width:100%; padding:16px 20px; border:3px solid #e8e8e8; border-radius:14px; font-size:16px; transition:all 0.3s ease; background:#fafafa; box-shadow:inset 0 2px 5px rgba(0,0,0,0.04); }
.input-moderno:focus { border-color:#667eea; background:white; outline:none; box-shadow:inset 0 2px 5px rgba(0,0,0,0.04), 0 0 0 5px rgba(102,126,234,0.12), 0 5px 20px rgba(102,126,234,0.18); transform:translateY(-2px); }
@keyframes entrar-cima { from{opacity:0;transform:translateY(-30px) scale(0.95)} to{opacity:1;transform:translateY(0) scale(1)} }
.animar-entrar { animation: entrar-cima 0.6s cubic-bezier(0.175,0.885,0.32,1.275) forwards; }
.fundo-animado { background:linear-gradient(-45deg,#667eea,#764ba2,#f093fb,#4facfe,#43e97b); background-size:400% 400%; animation:gradiente-fundo 15s ease infinite; }
@keyframes gradiente-fundo { 0%{background-position:0% 50%} 50%{background-position:100% 50%} 100%{background-position:0% 50%} }
.particulas { position:fixed; top:0; left:0; width:100%; height:100%; pointer-events:none; overflow:hidden; z-index:0; }
.particula { position:absolute; width:10px; height:10px; background:rgba(255,255,255,0.18); border-radius:50%; animation:flutuar 20s infinite linear; }
@keyframes flutuar { 0%{transform:translateY(100vh) rotate(0deg);opacity:0} 10%{opacity:1} 90%{opacity:1} 100%{transform:translateY(-100px) rotate(720deg);opacity:0} }
"""

SCRIPT_PARTICULAS = """
<script>
(function(){
  const p = document.createElement('div');
  p.className = 'particulas';
  p.id = 'particulas-dinamicas';
  document.body.appendChild(p);
  for(let i=0;i<18;i++){
    const el = document.createElement('div');
    el.className = 'particula';
    el.style.left = Math.random()*100+'%';
    el.style.animationDelay = Math.random()*20+'s';
    el.style.animationDuration = (14+Math.random()*14)+'s';
    const tam = 5+Math.random()*14;
    el.style.width = tam+'px'; el.style.height = tam+'px';
    p.appendChild(el);
  }
})();
</script>
"""

# ===================== HTML - LOGIN ADMIN =====================
def gerar_html_login():
    ts = str(int(datetime.now().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔐 Login Admin - Sistema de Ponto</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; }
body { min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; position:relative; overflow:hidden; }
""" + ESTILOS_5D + ESTILO_RODAPE_WELL + """
.wrapper { position:relative; z-index:1; width:100%; max-width:420px; }
.login-box { padding:40px 35px; width:100%; }
.login-box.animar-entrar { animation-delay:0.1s; }
.logo-container { display:flex; justify-content:center; margin-bottom:20px; }
.logo { max-width:110px; max-height:110px; border-radius:20px; box-shadow:0 10px 30px rgba(0,0,0,0.2); }
.logo-fallback { width:110px; height:110px; border-radius:20px; background:linear-gradient(135deg,#667eea,#764ba2); display:flex; align-items:center; justify-content:center; color:white; font-size:44px; box-shadow:0 10px 30px rgba(102,126,234,0.4); }
.login-box h1 { color:#333; margin-bottom:8px; font-size:26px; text-align:center; background:linear-gradient(135deg,#667eea,#764ba2); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
.sub { color:#888; margin-bottom:28px; text-align:center; font-size:14px; }
.btn-entrar { width:100%; padding:16px; margin-top:18px; font-size:16px; background:linear-gradient(135deg,#667eea,#764ba2); }
.mensagem { padding:14px; border-radius:12px; margin-top:16px; text-align:center; font-size:14px; font-weight:bold; display:none; }
.erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; display:block; border:1px solid #ef9a9a; }
.links { margin-top:18px; display:flex; justify-content:space-between; }
.links a { color:#667eea; text-decoration:none; font-size:13px; font-weight:500; transition:all 0.3s; }
.links a:hover { color:#764ba2; text-decoration:underline; transform:translateX(3px); }
</style>
</head>
<body class="fundo-animado">
<div class="wrapper">
<div class="login-box card-3d animar-entrar">
<div class="logo-container">
<img src="/static/logo.png?t=""" + ts + """" alt="Logo" class="logo" onerror="this.outerHTML='<div class=\\'logo-fallback\\'>🏥</div>'">
</div>
<h1>🔐 Login Admin</h1>
<p class="sub">Acesso ao Painel Administrativo</p>
<input type="text" id="usuario" class="input-moderno" placeholder="Usuário" autocomplete="username">
<input type="password" id="senha" class="input-moderno" placeholder="Senha" autocomplete="current-password">
<button class="btn-3d btn-entrar" onclick="logar()">🚀 ENTRAR</button>
<div class="mensagem" id="mensagem"></div>
<div class="links"><a href="/">← Voltar ao Ponto</a></div>
""" + RODAPE_WELL + """
</div>
</div>
""" + SCRIPT_PARTICULAS + """
<script>
document.getElementById('senha').addEventListener('keypress',function(e){if(e.key==='Enter')logar();});
document.getElementById('usuario').addEventListener('keypress',function(e){if(e.key==='Enter')document.getElementById('senha').focus();});
document.getElementById('usuario').focus();
async function logar(){
  const u=document.getElementById('usuario').value.trim();
  const s=document.getElementById('senha').value;
  const m=document.getElementById('mensagem');
  if(!u||!s){m.textContent='Preencha usuário e senha!';m.className='mensagem erro';return;}
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({usuario:u,senha:s})});
    if(r.ok)window.location.href='/admin';
    else{const e=await r.json();m.textContent=e.detail||'Erro';m.className='mensagem erro';}
  }catch(e){m.textContent='Erro de conexão!';m.className='mensagem erro';}
}
</script>
</body>
</html>"""

# ===================== HTML - PAGINA PRINCIPAL (ENTRADA CPF) =====================
def gerar_html_ponto():
    ts = str(int(datetime.now().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📱 Sistema de Ponto - Clínica</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; }
body { min-height:100vh; display:flex; align-items:center; justify-content:center; padding:15px; position:relative; overflow-x:hidden; }
""" + ESTILOS_5D + ESTILO_RODAPE_WELL + """
.wrapper { position:relative; z-index:1; width:100%; max-width:440px; }
.container { padding:35px 30px; width:100%; }
.container.animar-entrar { animation-delay:0.1s; }
.logo-container { display:flex; justify-content:center; margin-bottom:18px; }
.logo { max-width:130px; max-height:130px; border-radius:22px; box-shadow:0 15px 40px rgba(0,0,0,0.2); }
.logo-fallback { width:130px; height:130px; border-radius:22px; background:linear-gradient(135deg,#667eea,#764ba2); display:flex; align-items:center; justify-content:center; color:white; font-size:52px; box-shadow:0 15px 40px rgba(102,126,234,0.4); }
.container h1 { color:#333; font-size:28px; margin-bottom:5px; text-align:center; background:linear-gradient(135deg,#667eea,#f093fb,#4facfe); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
.subtitulo { color:#888; font-size:14px; margin-bottom:25px; text-align:center; }
.data-hora { background:linear-gradient(135deg,#e8f0fe,#f3e8ff); color:#667eea; padding:15px; border-radius:14px; font-weight:bold; font-size:14px; margin-bottom:25px; text-align:center; border:2px solid rgba(102,126,234,0.2); box-shadow:0 5px 15px rgba(102,126,234,0.12); }
.input-moderno { margin:10px 0; text-align:center; font-size:18px; letter-spacing:2px; }
.btn-acessar { width:100%; padding:18px; margin-top:18px; font-size:17px; background:linear-gradient(135deg,#43e97b,#38f9d7); }
.info-func { margin-top:15px; padding:14px; border-radius:12px; font-size:14px; font-weight:bold; text-align:center; display:none; }
.info-ok { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#2e7d32; display:block; border:1px solid #a5d6a7; }
.info-err { background:linear-gradient(135deg,#fff3e0,#ffe0b2); color:#e65100; display:block; border:1px solid #ffcc80; }
.mensagem { padding:14px; border-radius:12px; margin-top:16px; font-size:14px; font-weight:bold; text-align:center; display:none; }
.erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; display:block; border:1px solid #ef9a9a; }
.admin-link { margin-top:20px; padding-top:16px; border-top:2px dashed #eee; text-align:center; }
.admin-link a { color:#888; font-size:12px; text-decoration:none; transition:all 0.3s; }
.admin-link a:hover { color:#667eea; font-size:13px; }
.dica { margin-top:14px; padding:10px; background:linear-gradient(135deg,#fff8e1,#ffecb3); border-radius:10px; font-size:11px; color:#f57f17; text-align:center; border:1px solid #ffe082; }
</style>
</head>
<body class="fundo-animado">
<div class="wrapper">
<div class="container card-3d animar-entrar">
<div class="logo-container">
<img src="/static/logo.png?t=""" + ts + """" alt="Logo" class="logo" onerror="this.outerHTML='<div class=\\'logo-fallback\\'>🏥</div>'">
</div>
<h1>Sistema de Ponto</h1>
<p class="subtitulo">Clínica - Controle de Funcionários</p>
<div class="data-hora" id="dataHora">Carregando...</div>
<input type="text" id="cpf" class="input-moderno" placeholder="Digite seu CPF (apenas números)" maxlength="11" inputmode="numeric">
<div class="info-func" id="infoFunc"></div>
<button class="btn-3d btn-acessar" onclick="acessar()">🔓 ACESSAR MEU PAINEL</button>
<div class="mensagem" id="mensagem"></div>
<div class="dica">🔒 Seus dados estão protegidos. Acesso registrado por dispositivo.</div>
<div class="admin-link"><a href="/admin">🔐 Acesso Administrador</a></div>
""" + RODAPE_WELL + """
</div>
</div>
""" + SCRIPT_PARTICULAS + """
<script>
function atualizarDH(){
  const o={weekday:'long',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'};
  document.getElementById('dataHora').textContent='🕐 '+new Date().toLocaleDateString('pt-BR',o);
}
setInterval(atualizarDH,1000); atualizarDH();
const cpfInp=document.getElementById('cpf');
cpfInp.addEventListener('input',function(){this.value=this.value.replace(/\\D/g,'');});
cpfInp.addEventListener('keypress',function(e){if(e.key==='Enter')acessar();});
cpfInp.addEventListener('blur',async function(){
  const c=this.value.replace(/\\D/g,''); const inf=document.getElementById('infoFunc');
  if(c.length===11){
    try{const r=await fetch('/api/buscar/'+c);const d=await r.json();
      if(d.encontrado){inf.textContent='👤 '+d.nome;inf.className='info-func info-ok';}
      else{inf.textContent='⚠️ CPF NÃO cadastrado! Contate o RH.';inf.className='info-func info-err';}
    }catch(e){inf.style.display='none';}
  }else inf.style.display='none';
});
async function acessar(){
  const c=cpfInp.value.replace(/\\D/g,''); const m=document.getElementById('mensagem');
  if(!c||c.length!==11){m.textContent='Digite um CPF válido com 11 números!';m.className='mensagem erro';return;}
  try{
    const r=await fetch('/api/funcionario/acessar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:c})});
    if(r.ok)window.location.href='/funcionario?cpf='+c;
    else{const e=await r.json();m.textContent=e.detail||'Erro';m.className='mensagem erro';}
  }catch(e){m.textContent='Erro de conexão!';m.className='mensagem erro';}
}
</script>
</body>
</html>"""

# ===================== HTML - PAINEL DO FUNCIONARIO =====================
def gerar_html_funcionario():
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>👤 Painel do Funcionário</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; }
body { min-height:100vh; padding:15px; position:relative; overflow-x:hidden; }
""" + ESTILOS_5D + ESTILO_RODAPE_WELL + """
.wrapper { position:relative; z-index:1; max-width:480px; margin:0 auto; }
.card-topo { padding:25px; margin-bottom:20px; }
.card-topo.animar-entrar { animation-delay:0.1s; }
.voltar { display:inline-flex; align-items:center; gap:5px; color:#667eea; text-decoration:none; font-size:13px; font-weight:bold; margin-bottom:15px; padding:6px 12px; background:rgba(102,126,234,0.1); border-radius:20px; transition:all 0.3s; }
.voltar:hover { background:rgba(102,126,234,0.2); transform:translateX(-3px); }
.foto-func { width:72px; height:72px; border-radius:50%; background:linear-gradient(135deg,#667eea,#f093fb); display:flex; align-items:center; justify-content:center; color:white; font-size:30px; font-weight:bold; margin:0 auto 12px; box-shadow:0 10px 25px rgba(102,126,234,0.4); border:3px solid white; }
.nome-func { text-align:center; font-size:20px; color:#333; margin-bottom:5px; }
.cpf-func { text-align:center; color:#888; font-size:13px; margin-bottom:16px; }
.status-wrapper { text-align:center; margin-bottom:15px; }
.status-acesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#2e7d32; padding:10px 16px; border-radius:25px; font-size:12px; font-weight:bold; display:inline-block; }
.horarios-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:18px; }
.horario-item { background:linear-gradient(135deg,#f5f7fa,#e4e8ec); padding:10px; border-radius:10px; text-align:center; }
.horario-label { font-size:10px; color:#888; text-transform:uppercase; font-weight:bold; }
.horario-valor { font-size:14px; color:#333; font-weight:bold; margin-top:3px; }
.data-hora { background:linear-gradient(135deg,#e8f0fe,#f3e8ff); color:#667eea; padding:14px; border-radius:14px; font-weight:bold; font-size:13px; margin-bottom:20px; text-align:center; border:2px solid rgba(102,126,234,0.2); }
.card-botoes { padding:25px; margin-bottom:20px; }
.card-botoes h2 { font-size:16px; color:#333; margin-bottom:18px; text-align:center; }
.botoes { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.botoes button { padding:20px 10px; font-size:13px; }
.icone-btn { font-size:28px; display:block; margin-bottom:6px; }
.btn-entrada { background:linear-gradient(135deg,#4CAF50,#66bb6a,#43a047); }
.btn-almoco { background:linear-gradient(135deg,#ff9800,#ffb74d,#f57c00); }
.btn-retorno { background:linear-gradient(135deg,#2196F3,#64b5f6,#1976D2); }
.btn-saida { background:linear-gradient(135deg,#f44336,#ef5350,#d32f2f); }
.mensagem { padding:16px; border-radius:14px; margin-top:18px; font-size:14px; font-weight:bold; white-space:pre-line; line-height:1.6; display:none; text-align:center; }
.sucesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#1b5e20; display:block; border:1px solid #a5d6a7; }
.erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; display:block; border:1px solid #ef9a9a; }
.banco-horas { background:linear-gradient(135deg,#e0f7fa,#b2ebf2); color:#006064; display:block; border:1px solid #80deea; }
.modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.6); backdrop-filter:blur(5px); z-index:1000; align-items:center; justify-content:center; padding:20px; }
.modal-overlay.ativo { display:flex; }
.modal-box { background:white; border-radius:22px; padding:30px; width:100%; max-width:420px; box-shadow:0 30px 70px rgba(0,0,0,0.4); animation:entrar-cima 0.4s cubic-bezier(0.175,0.885,0.32,1.275); }
.modal-box h2 { color:#f44336; font-size:20px; margin-bottom:10px; }
.modal-box p { color:#555; font-size:14px; margin-bottom:15px; line-height:1.5; }
.modal-box .info-atraso { background:linear-gradient(135deg,#fff3e0,#ffe0b2); padding:14px; border-radius:12px; margin-bottom:15px; font-weight:bold; color:#e65100; font-size:15px; border:1px solid #ffcc80; }
.modal-box textarea { width:100%; padding:14px; border:3px solid #e8e8e8; border-radius:12px; font-size:14px; resize:vertical; min-height:90px; font-family:'Segoe UI',Arial,sans-serif; transition:all 0.3s; }
.modal-box textarea:focus { border-color:#667eea; outline:none; box-shadow:0 0 0 5px rgba(102,126,234,0.12); }
.modal-botoes { display:flex; gap:10px; margin-top:18px; }
.modal-botoes button { flex:1; padding:14px; border:none; border-radius:12px; font-weight:bold; cursor:pointer; font-size:14px; transition:all 0.3s; }
.btn-cancelar { background:linear-gradient(135deg,#e0e0e0,#bdbdbd); color:#333; }
.btn-confirmar { background:linear-gradient(135deg,#667eea,#764ba2); color:white; }
.disp-info { margin-top:15px; padding:12px; background:linear-gradient(135deg,#f3e5f5,#e1bee7); border-radius:12px; font-size:11px; color:#6a1b9a; text-align:center; border:1px solid #ce93d8; }
</style>
</head>
<body class="fundo-animado">
<div class="wrapper">
<div class="card-topo card-3d animar-entrar">
<a href="/" class="voltar">← Trocar usuário</a>
<div class="foto-func" id="fotoFunc">👤</div>
<h2 class="nome-func" id="nomeFunc">Carregando...</h2>
<p class="cpf-func" id="cpfFunc">CPF: ---</p>
<div class="status-wrapper"><span class="status-acesso">✅ Sessão segura ativa</span></div>
<div class="horarios-grid" id="horariosInfo"></div>
</div>
<div class="card-botoes card-3d animar-entrar" style="animation-delay:0.2s">
<div class="data-hora" id="dataHora">Carregando...</div>
<h2>🎯 Selecione o Registro</h2>
<div class="botoes">
<button class="btn-3d btn-entrada" onclick="registrar('ENTRADA')"><span class="icone-btn">✅</span>ENTRADA</button>
<button class="btn-3d btn-almoco" onclick="registrar('SAIDA_ALMOCO')"><span class="icone-btn">🍽️</span>SAÍDA ALMOÇO</button>
<button class="btn-3d btn-retorno" onclick="registrar('RETORNO_ALMOCO')"><span class="icone-btn">↩️</span>RETORNO ALMOÇO</button>
<button class="btn-3d btn-saida" onclick="registrar('SAIDA')"><span class="icone-btn">🚪</span>SAÍDA</button>
</div>
<div class="mensagem" id="mensagem"></div>
<div class="disp-info" id="dispInfo">📡 Dispositivo registrado com segurança</div>
</div>
""" + RODAPE_WELL + """
</div>
<div class="modal-overlay" id="modalJust">
<div class="modal-box">
<h2>⚠️ Atenção!</h2>
<p id="modalTexto"></p>
<div class="info-atraso" id="modalInfo"></div>
<p style="font-weight:bold;color:#333;margin-bottom:8px;">📝 Informe a justificativa:</p>
<textarea id="justificativa" placeholder="Descreva o motivo detalhadamente..."></textarea>
<div class="modal-botoes">
<button class="btn-cancelar" onclick="fecharModal()">Cancelar</button>
<button class="btn-confirmar" onclick="confirmar()">✅ Confirmar</button>
</div>
</div>
</div>
""" + SCRIPT_PARTICULAS + """
<script>
const QR="CLINICA_PONTO_2024";
let pendente=null;
const prm=new URLSearchParams(window.location.search);
const CPF=prm.get('cpf')||'';
function atualizarDH(){
  const o={weekday:'long',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'};
  document.getElementById('dataHora').textContent='🕐 '+new Date().toLocaleDateString('pt-BR',o);
}
setInterval(atualizarDH,1000); atualizarDH();
async function carregar(){
  if(!CPF){window.location.href='/';return;}
  try{
    const r=await fetch('/api/buscar/'+CPF);const d=await r.json();
    if(!d.encontrado){window.location.href='/';return;}
    document.getElementById('nomeFunc').textContent=d.nome;
    document.getElementById('cpfFunc').textContent='CPF: '+CPF.replace(/(\\d{3})(\\d{3})(\\d{3})(\\d{2})/,'$1.$2.$3-$4');
    document.getElementById('fotoFunc').textContent=d.nome.charAt(0).toUpperCase();
    document.getElementById('horariosInfo').innerHTML=
      '<div class="horario-item"><div class="horario-label">Entrada</div><div class="horario-valor">🕐 '+d.horario_entrada+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída Almoço</div><div class="horario-valor">🍽️ '+d.horario_saida_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Retorno</div><div class="horario-valor">↩️ '+d.horario_retorno_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída</div><div class="horario-valor">🚪 '+d.horario_saida+'</div></div>';
  }catch(e){window.location.href='/';}
}
carregar();
async function registrar(tipo){
  if(!CPF)return;
  try{
    const r=await fetch('/api/verificar_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:CPF,tipo:tipo,qr_code:QR})});
    const d=await r.json();
    if(!r.ok){mostrar(d.detail||'Erro','erro');return;}
    if(d.precisa_justificativa){
      pendente={cpf:CPF,tipo:tipo};
      document.getElementById('modalTexto').textContent=d.mensagem_justificativa;
      document.getElementById('modalInfo').textContent='⏱️ '+d.info_atraso;
      document.getElementById('justificativa').value='';
      document.getElementById('modalJust').classList.add('ativo');
      document.getElementById('justificativa').focus();
    }else await executar(CPF,tipo,'');
  }catch(e){mostrar('Erro de conexão!','erro');}
}
function fecharModal(){document.getElementById('modalJust').classList.remove('ativo');pendente=null;}
async function confirmar(){
  if(!pendente)return;
  const j=document.getElementById('justificativa').value.trim();
  if(!j){alert('Informe a justificativa!');document.getElementById('justificativa').focus();return;}
  fecharModal(); await executar(pendente.cpf,pendente.tipo,j);
}
async function executar(cpf,tipo,just){
  try{
    const r=await fetch('/api/bater_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:cpf,tipo:tipo,qr_code:QR,justificativa:just})});
    const d=await r.json();
    if(r.ok){let t='sucesso';if(d.mensagem.includes('Banco'))t='banco-horas';mostrar(d.mensagem,t);}
    else mostrar(d.detail||'Erro','erro');
  }catch(e){mostrar('Erro de conexão!','erro');}
}
function mostrar(texto,tipo){const m=document.getElementById('mensagem');m.textContent=texto;m.className='mensagem '+tipo;setTimeout(()=>m.className='mensagem',10000);}
document.getElementById('modalJust').addEventListener('click',function(e){if(e.target===this)fecharModal();});
</script>
</body>
</html>"""

# ===================== HTML - PAINEL ADMIN =====================
def gerar_html_admin():
    ts = str(int(datetime.now().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚙️ Painel Administrativo</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; }
body { background:#f0f2f5; min-height:100vh; }
""" + ESTILOS_5D + ESTILO_RODAPE_WELL + """
.header { background:linear-gradient(135deg,#667eea,#764ba2,#f093fb); color:white; padding:18px 25px; text-align:center; position:relative; box-shadow:0 8px 30px rgba(102,126,234,0.35); }
.header-content { display:flex; align-items:center; justify-content:center; gap:15px; }
.logo-header { width:48px; height:48px; border-radius:12px; box-shadow:0 4px 15px rgba(0,0,0,0.25); }
.logo-header-fallback { width:48px; height:48px; border-radius:12px; background:rgba(255,255,255,0.2); display:flex; align-items:center; justify-content:center; font-size:24px; backdrop-filter:blur(5px); }
.header h1 { font-size:22px; text-shadow:0 2px 8px rgba(0,0,0,0.2); }
.logout { position:absolute; right:20px; top:50%; transform:translateY(-50%); background:rgba(255,255,255,0.2); padding:9px 18px; border-radius:25px; cursor:pointer; font-size:13px; border:1px solid rgba(255,255,255,0.35); backdrop-filter:blur(5px); transition:all 0.3s; font-weight:bold; }
.logout:hover { background:rgba(255,255,255,0.35); transform:translateY(-50%) scale(1.05); }
.container { max-width:1250px; margin:25px auto; padding:0 20px; }
.tabs { display:flex; gap:6px; margin-bottom:20px; flex-wrap:wrap; }
.tab { padding:12px 20px; background:#dde2e8; border:none; border-radius:12px 12px 0 0; cursor:pointer; font-weight:bold; font-size:13px; color:#555; transition:all 0.3s; }
.tab:hover { background:#cfd6de; transform:translateY(-2px); }
.tab.ativo { background:white; color:#667eea; box-shadow:0 -4px 15px rgba(0,0,0,0.08); }
.painel { background:white; border-radius:0 18px 18px 18px; padding:28px; box-shadow:0 10px 40px rgba(0,0,0,0.08); display:none; }
.painel.ativo { display:block; animation:entrar-cima 0.4s ease; }
h2 { color:#333; margin-bottom:22px; font-size:21px; background:linear-gradient(135deg,#667eea,#764ba2); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
input, select, textarea { width:100%; padding:12px; margin:8px 0; border:2px solid #e8e8e8; border-radius:10px; font-size:14px; font-family:'Segoe UI',Arial,sans-serif; transition:all 0.3s; background:#fafafa; }
input:focus, select:focus, textarea:focus { border-color:#667eea; background:white; outline:none; box-shadow:0 0 0 4px rgba(102,126,234,0.1); }
button { padding:12px 22px; background:linear-gradient(135deg,#667eea,#764ba2); color:white; border:none; border-radius:10px; cursor:pointer; font-weight:bold; font-size:14px; margin:5px 5px 5px 0; transition:all 0.3s; box-shadow:0 4px 12px rgba(102,126,234,0.25); }
button:hover { transform:translateY(-2px); box-shadow:0 7px 18px rgba(102,126,234,0.35); }
button:active { transform:translateY(0); }
.btn-success { background:linear-gradient(135deg,#4CAF50,#66bb6a); box-shadow:0 4px 12px rgba(76,175,80,0.3); }
.btn-danger { background:linear-gradient(135deg,#f44336,#ef5350); box-shadow:0 4px 12px rgba(244,67,54,0.3); }
.btn-warning { background:linear-gradient(135deg,#ff9800,#ffb74d); box-shadow:0 4px 12px rgba(255,152,0,0.3); }
.btn-small { padding:6px 12px; font-size:12px; border-radius:8px; }
table { width:100%; border-collapse:collapse; margin-top:15px; display:block; overflow-x:auto; }
th, td { padding:11px 13px; text-align:left; border-bottom:1px solid #eee; font-size:13px; white-space:nowrap; }
th { background:linear-gradient(135deg,#f8f9fa,#eef2f7); font-weight:bold; color:#555; }
.atrasado { color:#f44336; font-weight:bold; }
.banco-horas { color:#0c5460; font-weight:bold; }
.tipo-entrada { color:#4CAF50; font-weight:bold; }
.tipo-saida-almoco { color:#ff9800; font-weight:bold; }
.tipo-retorno-almoco { color:#2196F3; font-weight:bold; }
.tipo-saida { color:#f44336; font-weight:bold; }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:22px; }
@media(max-width:700px){.grid-2{grid-template-columns:1fr}}
.mensagem { padding:14px; border-radius:12px; margin:12px 0; display:none; font-weight:bold; font-size:14px; }
.sucesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#1b5e20; display:block; border:1px solid #a5d6a7; }
.erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; display:block; border:1px solid #ef9a9a; }
.qr-info { background:linear-gradient(135deg,#e3f2fd,#bbdefb); padding:16px; border-radius:12px; margin:15px 0; word-break:break-all; border:1px solid #90caf9; }
.card { background:linear-gradient(135deg,#fafafa,#f5f7fa); padding:18px; border-radius:14px; margin:12px 0; border-left:4px solid #667eea; box-shadow:0 3px 10px rgba(0,0,0,0.05); }
.horarios-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.info-box { background:linear-gradient(135deg,#fff8e1,#ffecb3); border-left:4px solid #ffc107; padding:14px; border-radius:8px; margin:15px 0; font-size:13px; color:#856404; }
.justificativa-cell { max-width:180px; font-size:11px; color:#666; font-style:italic; overflow:hidden; text-overflow:ellipsis; }
.minutos-cell { color:#f44336; font-weight:bold; }
.banco-cell { color:#0c5460; font-weight:bold; }
.fim-semana { background-color:#fffde7 !important; }
.fim-semana td { color:#e65100; }
.filtros { display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap; align-items:center; }
.filtros input, .filtros select { margin:0; width:auto; min-width:150px; }
label { font-size:13px; color:#555; font-weight:bold; display:block; margin-top:8px; }
</style>
</head>
<body>
<div class="header">
<div class="header-content">
<img src="/static/logo.png?t=""" + ts + """" alt="Logo" class="logo-header" onerror="this.outerHTML='<div class=\\'logo-header-fallback\\'>🏥</div>'">
<h1>⚙️ Painel Administrativo</h1>
</div>
<div class="logout" onclick="sair()">🚪 Sair</div>
</div>
<div class="container">
<div class="tabs">
<button class="tab ativo" onclick="abrir('cadastro',this)">👤 Cadastrar</button>
<button class="tab" onclick="abrir('funcionarios',this)">📋 Funcionários</button>
<button class="tab" onclick="abrir('registros',this)">📊 Registros</button>
<button class="tab" onclick="abrir('relatorios',this)">📄 Relatórios PDF</button>
<button class="tab" onclick="abrir('qrcode',this)">📱 QR Code</button>
<button class="tab" onclick="abrir('acessos',this)">📡 Acessos</button>
<button class="tab" onclick="abrir('config',this)">🔧 Configurações</button>
</div>
<div id="cadastro" class="painel ativo">
<h2>Cadastrar Novo Funcionário</h2>
<div class="mensagem" id="msgCad"></div>
<label>Nome Completo:</label>
<input type="text" id="nome" placeholder="Ex: Maria da Silva">
<label>CPF (apenas números):</label>
<input type="text" id="cpfCad" placeholder="Ex: 12345678900" maxlength="11" inputmode="numeric">
<div class="horarios-grid">
<div><label>Horário de Entrada:</label><input type="text" id="hEntrada" value="08:00:00"></div>
<div><label>Saída para Almoço:</label><input type="text" id="hSaidaAlmoco" value="12:00:00"></div>
<div><label>Retorno do Almoço:</label><input type="text" id="hRetornoAlmoco" value="13:00:00"></div>
<div><label>Horário de Saída:</label><input type="text" id="hSaida" value="18:00:00"></div>
</div>
<button class="btn-success" onclick="cadastrar()">💾 Salvar Cadastro</button>
</div>
<div id="funcionarios" class="painel">
<h2>Funcionários Cadastrados</h2>
<button onclick="carregarFuncs()">🔄 Atualizar Lista</button>
<table><thead><tr><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Saída Almoço</th><th>Retorno</th><th>Saída</th><th>Ação</th></tr></thead><tbody id="tbodyFunc"></tbody></table>
</div>
<div id="registros" class="painel">
<h2>Todos os Registros de Ponto</h2>
<div class="filtros">
<input type="text" id="filtroNome" placeholder="Filtrar por nome..." oninput="carregarRegs()">
<select id="filtroTipo" onchange="carregarRegs()">
<option value="">Todos os tipos</option>
<option value="ENTRADA">ENTRADA</option>
<option value="SAIDA_ALMOCO">SAÍDA ALMOÇO</option>
<option value="RETORNO_ALMOCO">RETORNO ALMOÇO</option>
<option value="SAIDA">SAÍDA</option>
</select>
<button onclick="carregarRegs()">🔄 Atualizar</button>
</div>
<table><thead><tr><th>Funcionário</th><th>CPF</th><th>Data</th><th>Hora</th><th>Tipo</th><th>Atrasado</th><th>Min.</th><th>Banco</th><th>Justificativa</th><th>IP</th></tr></thead><tbody id="tbodyReg"></tbody></table>
</div>
<div id="relatorios" class="painel">
<h2>Gerar Relatórios em PDF</h2>
<div class="grid-2">
<div class="card">
<h3 style="margin:10px 0;color:#555;">📄 Relatório Geral do Mês</h3>
<label>Mês/Ano:</label>
<input type="month" id="mesAno">
<button class="btn-success" onclick="gerarGeral()">⬇️ Baixar PDF Geral</button>
</div>
<div class="card">
<h3 style="margin:10px 0;color:#555;">👤 Relatório por Funcionário</h3>
<select id="selFunc"></select>
<label>Mês/Ano:</label>
<input type="month" id="mesAnoFunc">
<button class="btn-warning" onclick="gerarInd()">⬇️ Baixar PDF</button>
</div>
</div>
</div>
<div id="qrcode" class="painel">
<h2>📱 QR Code do Sistema</h2>
<div class="qr-info"><p><strong>URL Local:</strong></p><p id="urlLocal" style="font-weight:bold;color:#1565c0;"></p></div>
<button onclick="gerarQR()">🔄 Gerar/Atualizar QR Code</button>
<div id="qrImg" style="margin-top:20px;"></div>
</div>
<div id="acessos" class="painel">
<h2>📡 Registros de Acesso de Dispositivos</h2>
<button onclick="carregarAcessos()">🔄 Atualizar</button>
<table><thead><tr><th>Data/Hora</th><th>CPF</th><th>Funcionário</th><th>IP</th><th>Dispositivo</th><th>Tipo</th></tr></thead><tbody id="tbodyAcessos"></tbody></table>
</div>
<div id="config" class="painel">
<h2>🔧 Configurações</h2>
<div class="card">
<h3 style="margin-bottom:10px;">🖼️ Logo da Clínica</h3>
<p style="font-size:14px;line-height:1.6;">Coloque sua logo em <strong>static/logo.png</strong> (formato PNG).<br>Se não aparecer, pressione <strong>Ctrl+F5</strong>.</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🔐 Credenciais de Acesso</h3>
<p style="font-size:14px;"><strong>Usuário:</strong> admin<br><strong>Senha:</strong> 3223ronte</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🛡️ Segurança</h3>
<p style="font-size:14px;line-height:1.6;">
• Rate limiting contra brute force<br>
• Cabeçalhos de segurança anti-XSS<br>
• Sanitização de todas as entradas<br>
• Registro de IP e dispositivo em cada acesso<br>
• Cookies HttpOnly e SameSite
</p>
</div>
""" + RODAPE_WELL + """
</div>
</div>
</div>
<script>
const h=new Date();const ma=h.toISOString().slice(0,7);
document.getElementById('mesAno').value=ma;document.getElementById('mesAnoFunc').value=ma;
document.getElementById('urlLocal').textContent=window.location.origin+'/';
document.getElementById('cpfCad').addEventListener('input',function(){this.value=this.value.replace(/\\D/g,'');});
function abrir(n,btn){
  document.querySelectorAll('.painel').forEach(p=>p.classList.remove('ativo'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('ativo'));
  document.getElementById(n).classList.add('ativo');btn.classList.add('ativo');
  if(n==='funcionarios')carregarFuncs();
  if(n==='registros')carregarRegs();
  if(n==='relatorios')carregarSel();
  if(n==='acessos')carregarAcessos();
}
function sair(){document.cookie='sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';window.location.href='/admin';}
function msg(id,texto,tipo){const e=document.getElementById(id);e.textContent=texto;e.className='mensagem '+tipo;setTimeout(()=>e.className='mensagem',4000);}
async function cadastrar(){
  const d={nome:document.getElementById('nome').value.trim(),cpf:document.getElementById('cpfCad').value.replace(/\\D/g,''),horario_entrada:document.getElementById('hEntrada').value.trim()||'08:00:00',horario_saida_almoco:document.getElementById('hSaidaAlmoco').value.trim()||'12:00:00',horario_retorno_almoco:document.getElementById('hRetornoAlmoco').value.trim()||'13:00:00',horario_saida:document.getElementById('hSaida').value.trim()||'18:00:00'};
  if(!d.nome||!d.cpf){msg('msgCad','Preencha nome e CPF!','erro');return;}
  if(d.cpf.length!==11){msg('msgCad','CPF deve ter 11 dígitos!','erro');return;}
  const r=await fetch('/api/funcionarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  if(r.ok){msg('msgCad','✅ Funcionário cadastrado!','sucesso');document.getElementById('nome').value='';document.getElementById('cpfCad').value='';}
  else{const e=await r.json();msg('msgCad','❌ '+(e.detail||'Erro'),'erro');}
}
async function carregarFuncs(){
  const r=await fetch('/api/funcionarios');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const tb=document.getElementById('tbodyFunc');
  if(d.length===0){tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:#999;padding:20px;">Nenhum cadastrado.</td></tr>';return;}
  tb.innerHTML=d.map(f=>'<tr><td>'+f.id+'</td><td>'+f.nome+'</td><td>'+f.cpf+'</td><td><strong>'+f.horario_entrada+'</strong></td><td>'+f.horario_saida_almoco+'</td><td>'+f.horario_retorno_almoco+'</td><td><strong>'+f.horario_saida+'</strong></td><td><button class="btn-danger btn-small" onclick="excluir('+f.id+')">Excluir</button></td></tr>').join('');
}
async function excluir(id){if(confirm('Tem CERTEZA? Todos os registros serão APAGADOS!')){const r=await fetch('/api/funcionarios/'+id,{method:'DELETE'});if(r.status===401){window.location.href='/admin';return;}carregarFuncs();}}
async function carregarRegs(){
  const r=await fetch('/api/registros');if(r.status===401){window.location.href='/admin';return;}
  let d=await r.json();
  const fn=document.getElementById('filtroNome').value.toLowerCase();
  const ft=document.getElementById('filtroTipo').value;
  if(fn)d=d.filter(x=>x.nome.toLowerCase().includes(fn));
  if(ft)d=d.filter(x=>x.tipo===ft);
  const tb=document.getElementById('tbodyReg');
  if(d.length===0){tb.innerHTML='<tr><td colspan="10" style="text-align:center;color:#999;padding:20px;">Nenhum registro.</td></tr>';return;}
  tb.innerHTML=d.map(function(r){
    const ct='tipo-'+r.tipo.toLowerCase().replace(/_/g,'-');
    const cf=r.dia_semana>=5?' fim-semana':'';
    const mh=r.minutos_atraso>0?'<span class="minutos-cell">'+r.minutos_atraso+'</span>':'-';
    const bh=r.minutos_banco_horas>0?'<span class="banco-cell">+'+r.minutos_banco_horas+'</span>':'-';
    const jt=r.justificativa?'<span class="justificativa-cell" title="'+r.justificativa.replace(/"/g,'&quot;')+'">'+r.justificativa+'</span>':'<span style="color:#ccc;">-</span>';
    const ip=r.ip_dispositivo||'<span style="color:#ccc;">-</span>';
    return '<tr class="'+cf+'"><td>'+r.nome+'</td><td>'+r.cpf+'</td><td>'+r.data+'</td><td>'+r.hora+'</td><td class="'+ct+'">'+r.tipo_formatado+'</td><td class="'+(r.atrasado?'atrasado':'')+'">'+(r.atrasado?'⚠️ SIM':'✅ NÃO')+'</td><td>'+mh+'</td><td>'+bh+'</td><td>'+jt+'</td><td style="font-size:11px;color:#888;">'+ip+'</td></tr>';
  }).join('');
}
async function carregarSel(){
  const r=await fetch('/api/funcionarios');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const s=document.getElementById('selFunc');
  if(d.length===0){s.innerHTML='<option value="">Cadastre funcionários primeiro</option>';return;}
  s.innerHTML=d.map(f=>'<option value="'+f.id+'">'+f.nome+' ('+f.cpf+')</option>').join('');
}
function gerarGeral(){const m=document.getElementById('mesAno').value;if(!m){alert('Selecione o mês!');return;}window.open('/api/pdf/geral?mes='+m,'_blank');}
function gerarInd(){const i=document.getElementById('selFunc').value;const m=document.getElementById('mesAnoFunc').value;if(!i||!m){alert('Preencha todos os campos!');return;}window.open('/api/pdf/funcionario/'+i+'?mes='+m,'_blank');}
async function gerarQR(){
  const r=await fetch('/api/gerar_qrcode');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();
  if(d.detail)document.getElementById('qrImg').innerHTML='<p style="color:#f44336;">❌ '+d.detail+'</p>';
  else document.getElementById('qrImg').innerHTML='<img src="'+d.caminho+'?t='+Date.now()+'" style="max-width:250px;border:3px solid #ddd;border-radius:14px;box-shadow:0 8px 25px rgba(0,0,0,0.15);">';
}
async function carregarAcessos(){
  const r=await fetch('/api/acessos');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const tb=document.getElementById('tbodyAcessos');
  if(d.length===0){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#999;padding:20px;">Nenhum acesso registrado.</td></tr>';return;}
  tb.innerHTML=d.map(a=>'<tr><td>'+a.data_hora+'</td><td>'+a.cpf+'</td><td>'+(a.nome||'<span style="color:#999;">-</span>')+'</td><td style="font-size:11px;color:#555;">'+a.ip+'</td><td style="font-size:10px;color:#888;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="'+(a.user_agent||'').replace(/"/g,'&quot;')+'">'+(a.user_agent||'-')+'</td><td>'+a.tipo_acesso+'</td></tr>').join('');
}
</script>
</body>
</html>"""

# ===================== SERVIDOR HTTP =====================
class ServidorPonto(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")
    
    def do_GET(self):
        url = urlparse(self.path)
        caminho = url.path
        params = parse_qs(url.query)
        
        if caminho == "/" or caminho == "/index.html":
            responder_html(self, gerar_html_ponto())
            return
        
        if caminho == "/funcionario":
            responder_html(self, gerar_html_funcionario())
            return
        
        if caminho == "/admin":
            if verificar_login(self):
                responder_html(self, gerar_html_admin())
            else:
                responder_html(self, gerar_html_login())
            return
        
        if caminho == "/login":
            responder_html(self, gerar_html_login())
            return
        
        if caminho.startswith("/static/"):
            nome_arquivo = caminho.replace("/static/", "").split("?")[0]
            caminho_completo = os.path.join("static", nome_arquivo)
            if os.path.exists(caminho_completo):
                self.send_response(200)
                if nome_arquivo.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                elif nome_arquivo.endswith(".jpg") or nome_arquivo.endswith(".jpeg"):
                    self.send_header("Content-Type", "image/jpeg")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
                with open(caminho_completo, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
            return
        
        if caminho.startswith("/api/buscar/"):
            cpf = formatar_cpf(caminho.replace("/api/buscar/", ""))
            if len(cpf) != 11:
                responder_json(self, {"encontrado": False})
                return
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                conn.close()
                if func:
                    responder_json(self, {
                        "encontrado": True, 
                        "nome": func["nome"],
                        "horario_entrada": func["horario_entrada"],
                        "horario_saida_almoco": func["horario_saida_almoco"],
                        "horario_retorno_almoco": func["horario_retorno_almoco"],
                        "horario_saida": func["horario_saida"]
                    })
                else:
                    responder_json(self, {"encontrado": False})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        rotas_admin = ["/api/funcionarios", "/api/registros", "/api/gerar_qrcode", "/api/pdf/geral", "/api/logout", "/api/acessos"]
        precisa_login = (caminho in rotas_admin or caminho.startswith("/api/funcionarios/") or caminho.startswith("/api/pdf/funcionario/"))
        
        if precisa_login and not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado. Faça login."}, status=401)
            return
        
        if caminho == "/api/funcionarios":
            try:
                conn = get_db()
                funcs = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
                conn.close()
                resultado = [{
                    "id": f["id"], "nome": f["nome"], "cpf": f["cpf"],
                    "horario_entrada": f["horario_entrada"],
                    "horario_saida_almoco": f["horario_saida_almoco"],
                    "horario_retorno_almoco": f["horario_retorno_almoco"],
                    "horario_saida": f["horario_saida"]
                } for f in funcs]
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/registros":
            try:
                conn = get_db()
                regs = conn.execute("""
                    SELECT r.*, f.nome, f.cpf FROM registros_ponto r 
                    JOIN funcionarios f ON r.funcionario_id = f.id 
                    ORDER BY r.data_hora DESC
                """).fetchall()
                conn.close()
                resultado = []
                dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
                for r in regs:
                    dh = datetime.strptime(r["data_hora"], "%Y-%m-%d %H:%M:%S")
                    resultado.append({
                        "nome": r["nome"], "cpf": r["cpf"],
                        "data": dh.strftime("%d/%m/%Y"), "hora": dh.strftime("%H:%M:%S"),
                        "tipo": r["tipo"], "tipo_formatado": r["tipo"].replace("_", " "),
                        "atrasado": bool(r["atrasado"]),
                        "minutos_atraso": r["minutos_atraso"] or 0,
                        "minutos_banco_horas": r["minutos_banco_horas"] or 0,
                        "justificativa": r["justificativa"] or "",
                        "ip_dispositivo": r["ip_dispositivo"] or "",
                        "dia_semana": dh.weekday()
                    })
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/acessos":
            try:
                conn = get_db()
                acs = conn.execute("""
                    SELECT a.*, f.nome FROM acessos_dispositivos a 
                    LEFT JOIN funcionarios f ON a.funcionario_id = f.id 
                    ORDER BY a.data_hora_acesso DESC LIMIT 200
                """).fetchall()
                conn.close()
                resultado = []
                for a in acs:
                    dh = datetime.strptime(a["data_hora_acesso"], "%Y-%m-%d %H:%M:%S")
                    resultado.append({
                        "data_hora": dh.strftime("%d/%m/%Y %H:%M:%S"),
                        "cpf": a["cpf"], "nome": a["nome"],
                        "ip": a["ip_dispositivo"] or "",
                        "user_agent": a["user_agent"] or "",
                        "tipo_acesso": a["tipo_acesso"] or ""
                    })
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/logout":
            responder_json(self, {"status": "ok"}, cookies_extra=['sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;'])
            return
        
        if caminho == "/api/gerar_qrcode":
            try:
                import qrcode
                host = self.headers.get("Host", f"localhost:{PORTA}")
                url = f"http://{host}/"
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                img.save("static/qrcode_empresa.png")
                responder_json(self, {"status": "ok", "caminho": "/static/qrcode_empresa.png", "url": url})
            except ImportError:
                responder_json(self, {"detail": "Instale: pip install qrcode pillow"}, status=500)
            except Exception as e:
                responder_json(self, {"detail": f"Erro QR: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/pdf/geral":
            mes = params.get("mes", [""])[0]
            if not mes:
                responder_json(self, {"detail": "Informe o mês"}, status=400)
                return
            try:
                pdf_bytes = gerar_pdf_geral(mes)
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f"attachment; filename=Relatorio_Geral_{mes}.pdf")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
                self.wfile.write(pdf_bytes.getvalue())
            except ImportError:
                responder_html(self, "<h1>Erro</h1><p>Instale: pip install reportlab</p>")
            except Exception as e:
                responder_json(self, {"detail": f"Erro PDF: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/pdf/funcionario/"):
            func_id = int(caminho.replace("/api/pdf/funcionario/", ""))
            mes = params.get("mes", [""])[0]
            if not mes:
                responder_json(self, {"detail": "Informe o mês"}, status=400)
                return
            try:
                pdf_bytes = gerar_pdf_individual(func_id, mes)
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f"attachment; filename=Relatorio_Func_{func_id}_{mes}.pdf")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
                self.wfile.write(pdf_bytes.getvalue())
            except ImportError:
                responder_html(self, "<h1>Erro</h1><p>Instale: pip install reportlab</p>")
            except Exception as e:
                responder_json(self, {"detail": f"Erro PDF: {str(e)}"}, status=500)
            return
        
        self.send_response(404)
        for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
        self.end_headers()
    
    def do_POST(self):
        url = urlparse(self.path)
        caminho = url.path
        
        tamanho = int(self.headers.get("Content-Length", 0))
        if tamanho > 100000:
            responder_json(self, {"detail": "Requisição muito grande"}, status=413)
            return
        
        corpo = self.rfile.read(tamanho).decode("utf-8")
        
        try:
            dados = json.loads(corpo) if corpo else {}
        except:
            dados = {}
        
        ip_cliente = obter_ip_cliente(self)
        user_agent = sanitizar_texto(self.headers.get("User-Agent", ""), 500)
        
        if caminho == "/api/login":
            if not verificar_rate_limit(ip_cliente):
                responder_json(self, {"detail": "Muitas tentativas. Aguarde alguns minutos."}, status=429)
                return
            
            usuario = sanitizar_texto(dados.get("usuario", ""), 50)
            senha = sanitizar_texto(dados.get("senha", ""), 100)
            
            if usuario == ADMIN_USUARIO and senha == ADMIN_SENHA:
                token = gerar_sessao()
                sessoes_admin[token] = datetime.now() + timedelta(hours=8)
                cookie = f"sessao_admin={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
                tentativas_login.pop(ip_cliente, None)
                print(f"[LOGIN OK] Admin de {ip_cliente}")
                responder_json(self, {"status": "ok", "mensagem": "Login realizado"}, cookies_extra=[cookie])
            else:
                print(f"[LOGIN FALHA] {ip_cliente} user={usuario}")
                responder_json(self, {"detail": "Usuário ou senha incorretos!"}, status=401)
            return
        
        if caminho == "/api/funcionario/acessar":
            cpf = formatar_cpf(dados.get("cpf", ""))
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido! 11 dígitos."}, status=400)
                return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if not func:
                    conn.close()
                    responder_json(self, {"detail": "CPF não cadastrado!"}, status=404)
                    return
                
                func_id = func["id"]
                conn.close()
                
                registrar_acesso_dispositivo(cpf, func_id, ip_cliente, user_agent, "login_funcionario")
                acessos_funcionarios[cpf] = {
                    "funcionario_id": func_id,
                    "ip": ip_cliente,
                    "expira": datetime.now() + timedelta(hours=2)
                }
                
                print(f"[ACESSO FUNC] {func['nome']} | IP: {ip_cliente}")
                responder_json(self, {"status": "ok", "nome": func["nome"]})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/verificar_ponto":
            cpf = formatar_cpf(dados.get("cpf", ""))
            tipo = dados.get("tipo", "ENTRADA")
            qr_code = dados.get("qr_code", "")
            
            if qr_code != SEGREDO_QR:
                responder_json(self, {"detail": "QR Code inválido!"}, status=400)
                return
            if tipo not in TIPOS_REGISTRO:
                responder_json(self, {"detail": "Tipo inválido!"}, status=400)
                return
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido! 11 dígitos."}, status=400)
                return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if not func:
                    conn.close()
                    responder_json(self, {"detail": "CPF não cadastrado!"}, status=404)
                    return
                
                agora = datetime.now()
                data_str = agora.strftime("%Y-%m-%d")
                hora_str = agora.strftime("%H:%M:%S")
                
                ultimo = obter_ultimo_registro(func["id"], data_str)
                ultimo_tipo = ultimo["tipo"] if ultimo else None
                
                valido, msg_erro = verificar_sequencia_valida(ultimo_tipo, tipo)
                if not valido:
                    conn.close()
                    responder_json(self, {"detail": "⛔ " + msg_erro}, status=400)
                    return
                
                if verificar_registro_duplicado(func["id"], data_str, tipo):
                    conn.close()
                    responder_json(self, {"detail": f"⛔ {TIPOS_REGISTRO[tipo]['label']} JÁ registrada hoje! Não é permitido duplicar."}, status=400)
                    return
                
                atrasado = 0
                minutos = 0
                msg_just = ""
                info = ""
                
                if tipo == "ENTRADA":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"]) else 0
                    if atrasado:
                        minutos = calcular_minutos(hora_str, func["horario_entrada"])
                        msg_just = f"Atraso na ENTRADA. Horário padrão: {func['horario_entrada']}."
                        info = f"Atraso de {minutos} minuto(s)"
                elif tipo == "SAIDA_ALMOCO":
                    minutos_antes = calcular_minutos(func["horario_saida_almoco"], hora_str)
                    if not verificar_atraso(hora_str, func["horario_saida_almoco"]) and minutos_antes >= 30:
                        minutos = minutos_antes
                        msg_just = f"Saída para almoço com {minutos_antes} min de antecedência. Padrão: {func['horario_saida_almoco']}."
                        info = f"Antecedência de {minutos_antes} min"
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"]) else 0
                    if atrasado:
                        minutos = calcular_minutos(hora_str, func["horario_retorno_almoco"])
                        msg_just = f"Atraso no RETORNO. Padrão: {func['horario_retorno_almoco']}."
                        info = f"Atraso de {minutos} minuto(s)"
                elif tipo == "SAIDA":
                    atrasado = 1 if verificar_atraso(func["horario_saida"], hora_str) else 0
                    if atrasado:
                        minutos = calcular_minutos(func["horario_saida"], hora_str)
                        msg_just = f"SAÍDA ANTECIPADA. Padrão: {func['horario_saida']}."
                        info = f"Antecipada em {minutos} minuto(s)"
                
                conn.close()
                responder_json(self, {
                    "precisa_justificativa": (minutos > 0),
                    "mensagem_justificativa": msg_just,
                    "info_atraso": info,
                    "minutos_atraso": minutos,
                    "atrasado": atrasado
                })
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/bater_ponto":
            cpf = formatar_cpf(dados.get("cpf", ""))
            tipo = dados.get("tipo", "ENTRADA")
            qr_code = dados.get("qr_code", "")
            justificativa = sanitizar_texto(dados.get("justificativa", ""), 500)
            
            if qr_code != SEGREDO_QR:
                responder_json(self, {"detail": "QR Code inválido!"}, status=400)
                return
            if tipo not in TIPOS_REGISTRO:
                responder_json(self, {"detail": "Tipo inválido!"}, status=400)
                return
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido!"}, status=400)
                return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if not func:
                    conn.close()
                    responder_json(self, {"detail": "CPF não cadastrado!"}, status=404)
                    return
                
                agora = datetime.now()
                data_str = agora.strftime("%Y-%m-%d")
                data_hora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
                hora_str = agora.strftime("%H:%M:%S")
                horario_acesso = agora.strftime("%Y-%m-%d %H:%M:%S")
                
                ultimo = obter_ultimo_registro(func["id"], data_str)
                ultimo_tipo = ultimo["tipo"] if ultimo else None
                
                valido, msg_erro = verificar_sequencia_valida(ultimo_tipo, tipo)
                if not valido:
                    conn.close()
                    responder_json(self, {"detail": "⛔ " + msg_erro}, status=400)
                    return
                
                if verificar_registro_duplicado(func["id"], data_str, tipo):
                    conn.close()
                    responder_json(self, {"detail": f"⛔ {TIPOS_REGISTRO[tipo]['label']} JÁ registrada hoje!"}, status=400)
                    return
                
                atrasado = 0
                minutos_atraso = 0
                
                if tipo == "ENTRADA":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"]) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_entrada"])
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"]) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_retorno_almoco"])
                elif tipo == "SAIDA":
                    atrasado = 1 if verificar_atraso(func["horario_saida"], hora_str) else 0
                    if atrasado: minutos_atraso = calcular_minutos(func["horario_saida"], hora_str)
                
                minutos_banco = calcular_banco_horas(tipo, hora_str, func)
                
                conn.execute("""
                    INSERT INTO registros_ponto 
                    (funcionario_id, data_hora, tipo, atrasado, minutos_atraso, minutos_banco_horas, justificativa, ip_dispositivo, user_agent, horario_acesso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (func["id"], data_hora_str, tipo, atrasado, minutos_atraso, minutos_banco, justificativa, ip_cliente, user_agent, horario_acesso))
                conn.commit()
                conn.close()
                
                tipo_info = TIPOS_REGISTRO[tipo]
                msg = f"{tipo_info['icone']} {tipo_info['label']} registrada!\n"
                msg += f"👤 {func['nome']}\n📅 {agora.strftime('%d/%m/%Y')}\n⏰ {hora_str}"
                if atrasado: msg += f"\n⚠️ Atraso: {minutos_atraso} min"
                if minutos_banco > 0: msg += f"\n⏱️ Banco de horas: +{minutos_banco} min"
                if justificativa: msg += f"\n📝 Justificativa registrada"
                
                print(f"[PONTO] {func['nome']} | {tipo} | {hora_str} | IP:{ip_cliente}")
                responder_json(self, {"mensagem": msg})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401)
            return
        
        if caminho == "/api/funcionarios":
            nome = sanitizar_texto(dados.get("nome", ""), 150)
            cpf = formatar_cpf(dados.get("cpf", ""))
            
            if not nome or not cpf:
                responder_json(self, {"detail": "Preencha nome e CPF!"}, status=400)
                return
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF deve ter 11 dígitos!"}, status=400)
                return
            
            h_entrada = sanitizar_texto(dados.get("horario_entrada", "08:00:00"), 20) or "08:00:00"
            h_saida_almoco = sanitizar_texto(dados.get("horario_saida_almoco", "12:00:00"), 20) or "12:00:00"
            h_retorno_almoco = sanitizar_texto(dados.get("horario_retorno_almoco", "13:00:00"), 20) or "13:00:00"
            h_saida = sanitizar_texto(dados.get("horario_saida", "18:00:00"), 20) or "18:00:00"
            
            try:
                conn = get_db()
                existe = conn.execute("SELECT id FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if existe:
                    conn.close()
                    responder_json(self, {"detail": "CPF já cadastrado!"}, status=400)
                    return
                
                conn.execute("""
                    INSERT INTO funcionarios (nome, cpf, horario_entrada, horario_saida_almoco, horario_retorno_almoco, horario_saida)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (nome, cpf, h_entrada, h_saida_almoco, h_retorno_almoco, h_saida))
                conn.commit()
                
                novo_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                conn.close()
                
                print(f"[CADASTRO] {nome} | CPF: {cpf}")
                responder_json(self, {"status": "ok", "id": novo_id})
            except Exception as e:
                responder_json(self, {"detail": f"Erro BD: {str(e)}"}, status=500)
            return
        
        responder_json(self, {"detail": "Rota não encontrada"}, status=404)
    
    def do_DELETE(self):
# ===================== GERAÇÃO DE PDF =====================
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401)
            return
        
        url = urlparse(self.path)
        caminho = url.path
        
        if caminho.startswith("/api/funcionarios/"):
            try:
                func_id = int(caminho.replace("/api/funcionarios/", ""))
                conn = get_db()
                conn.execute("DELETE FROM registros_ponto WHERE funcionario_id = ?", (func_id,))
                conn.execute("DELETE FROM funcionarios WHERE id = ?", (func_id,))
                conn.commit()
                conn.close()
                print(f"[EXCLUSAO] Funcionário ID: {func_id}")
                responder_json(self, {"status": "ok"})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        responder_json(self, {"detail": "Rota não encontrada"}, status=404)

# ===================== FUNÇÕES DE PDF =====================
def desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors):
    c.setFillColor(colors.HexColor("#667eea"))
    c.rect(0, altura - 80, largura, 80, fill=True, stroke=False)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, altura - 50, "RELATORIO DE FOLHA PONTO")
    c.setFont("Helvetica", 10)
    c.drawString(40, altura - 68, "Mes/Ano: " + mes)
    
    y = altura - 110
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, "Funcionario: " + func["nome"])
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "CPF: " + func["cpf"])
    c.drawString(200, y, "Entrada: " + func["horario_entrada"])
    c.drawString(340, y, "Saida Almoco: " + func["horario_saida_almoco"])
    y -= 14
    c.drawString(200, y, "Retorno: " + func["horario_retorno_almoco"])
    c.drawString(340, y, "Saida: " + func["horario_saida"])
    y -= 25
    
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(colors.HexColor("#f0f0f0"))
    c.rect(40, y - 14, largura - 80, 18, fill=True, stroke=False)
    c.setFillColor(colors.black)
    c.drawString(45, y - 9, "DATA")
    c.drawString(100, y - 9, "HORA")
    c.drawString(155, y - 9, "TIPO")
    c.drawString(240, y - 9, "ATRASO")
    c.drawString(290, y - 9, "MIN.")
    c.drawString(340, y - 9, "BANCO")
    c.drawString(400, y - 9, "DIA")
    c.drawString(440, y - 9, "JUSTIFICATIVA")
    y -= 32
    
    c.setFont("Helvetica", 7)
    contagem = {"ENTRADA": 0, "SAIDA_ALMOCO": 0, "RETORNO_ALMOCO": 0, "SAIDA": 0}
    total_atrasos = 0
    total_min_atraso = 0
    total_banco_horas = 0
    
    for reg in registros:
        if y < 100:
            c.showPage()
            y = altura - 50
            c.setFont("Helvetica", 7)
        
        dh = datetime.strptime(reg["data_hora"], "%Y-%m-%d %H:%M:%S")
        data = dh.strftime("%d/%m/%Y")
        hora = dh.strftime("%H:%M:%S")
        dia_semana = dias_semana[dh.weekday()]
        tipo_fmt = reg["tipo"].replace("_", " ")
        
        if dh.weekday() >= 5:
            c.setFillColor(colors.HexColor("#fff3cd"))
            c.rect(40, y - 2, largura - 80, 12, fill=True, stroke=False)
            c.setFillColor(colors.black)
        
        c.drawString(45, y, data)
        c.drawString(100, y, hora)
        
        if reg["tipo"] == "ENTRADA": c.setFillColor(colors.HexColor("#4CAF50"))
        elif reg["tipo"] == "SAIDA_ALMOCO": c.setFillColor(colors.HexColor("#ff9800"))
        elif reg["tipo"] == "RETORNO_ALMOCO": c.setFillColor(colors.HexColor("#2196F3"))
        else: c.setFillColor(colors.HexColor("#f44336"))
        
        contagem[reg["tipo"]] += 1
        c.drawString(155, y, tipo_fmt)
        c.setFillColor(colors.black)
        
        if reg["atrasado"]:
            c.setFillColor(colors.HexColor("#f44336"))
            c.drawString(240, y, "SIM")
            c.setFillColor(colors.black)
            total_atrasos += 1
        else:
            c.drawString(240, y, "Nao")
        
        min_atraso = reg["minutos_atraso"] or 0
        if min_atraso > 0:
            c.setFillColor(colors.HexColor("#f44336"))
            c.drawString(290, y, str(min_atraso) + "m")
            c.setFillColor(colors.black)
            total_min_atraso += min_atraso
        else:
            c.drawString(290, y, "-")
        
        min_banco = reg["minutos_banco_horas"] or 0
        if min_banco > 0:
            c.setFillColor(colors.HexColor("#0c5460"))
            c.drawString(340, y, "+" + str(min_banco) + "m")
            c.setFillColor(colors.black)
            total_banco_horas += min_banco
        else:
            c.drawString(340, y, "-")
        
        c.drawString(400, y, dia_semana[:3])
        
        justificativa = reg["justificativa"] or ""
        if justificativa:
            c.setFillColor(colors.HexColor("#666666"))
            if len(justificativa) > 45: justificativa = justificativa[:42] + "..."
            c.drawString(440, y, justificativa)
            c.setFillColor(colors.black)
        
        y -= 13
    
    if y < 200:
        c.showPage()
        y = altura - 50
    
    y -= 10
    c.setFillColor(colors.HexColor("#f5f5f5"))
    c.rect(40, y - 100, largura - 80, 110, fill=True, stroke=False)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y - 15, "RESUMO DO MES:")
    c.setFont("Helvetica", 9)
    c.drawString(50, y - 35, "Total: " + str(len(registros)) + " registros")
    c.drawString(180, y - 35, "Entradas: " + str(contagem["ENTRADA"]))
    c.drawString(310, y - 35, "Saida Almoco: " + str(contagem["SAIDA_ALMOCO"]))
    c.drawString(50, y - 50, "Retornos: " + str(contagem["RETORNO_ALMOCO"]))
    c.drawString(180, y - 50, "Saidas: " + str(contagem["SAIDA"]))
    c.drawString(310, y - 50, "Atrasos: " + str(total_atrasos))
    c.drawString(50, y - 65, "Min. atrasados: " + str(total_min_atraso) + " min")
    
    c.setFillColor(colors.HexColor("#0c5460"))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(180, y - 65, "Banco: +" + str(total_banco_horas) + " min")
    c.setFillColor(colors.black)
    
    y -= 115
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "_______________________________________________________")
    y -= 15
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Assinatura do Funcionario: ___________________________")
    y -= 15
    c.drawString(40, y, "Data: ____/____/__________")
    
    y -= 40
    c.setFont("Helvetica-Bold", 10)
    c.drawString(300, y, "_______________________________________________________")
    y -= 15
    c.setFont("Helvetica", 9)
    c.drawString(300, y, "Assinatura do Responsavel: ___________________________")
    y -= 15
    c.drawString(300, y, "Data: ____/____/__________")
    
    c.showPage()

def gerar_pdf_geral(mes):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    
    conn = get_db()
    funcionarios = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    largura, altura = A4
    dias_semana = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]
    
    for func in funcionarios:
        registros = conn.execute("""
            SELECT * FROM registros_ponto 
            WHERE funcionario_id = ? AND strftime('%Y-%m', data_hora) = ?
            ORDER BY data_hora
        """, (func["id"], mes)).fetchall()
        desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors)
    
    conn.close()
    c.save()
    buffer.seek(0)
    return buffer

def gerar_pdf_individual(func_id, mes):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    
    conn = get_db()
    func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (func_id,)).fetchone()
    
    if not func:
        conn.close()
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        c.drawString(100, 400, "Funcionario nao encontrado")
        c.save()
        buffer.seek(0)
        return buffer
    
    registros = conn.execute("""
        SELECT * FROM registros_ponto 
        WHERE funcionario_id = ? AND strftime('%Y-%m', data_hora) = ?
        ORDER BY data_hora
    """, (func_id, mes)).fetchall()
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    largura, altura = A4
    dias_semana = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]
    
    desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors)
    
    conn.close()
    c.save()
    buffer.seek(0)
    return buffer

# ===================== INICIAR SERVIDOR =====================
if __name__ == "__main__":
    print("=" * 65)
    print("   SISTEMA DE PONTO v3.0 SECURE - FUNCIONANDO!")
    print("=" * 65)
    print(f"Pagina inicial (CPF):   http://localhost:{PORTA}")
    print(f"Painel Funcionario:     http://localhost:{PORTA}/funcionario")
    print(f"Login Admin:            http://localhost:{PORTA}/admin")
    print(f"Usuario: {ADMIN_USUARIO} | Senha: {ADMIN_SENHA}")
    print("=" * 65)
    print("NOVAS FUNCIONALIDADES v3.0:")
    print("  - Tela de login separada para funcionario")
    print("  - Registro de IP e dispositivo em cada acesso")
    print("  - Rate limiting contra brute force")
    print("  - Anti-duplicata (mesmo tipo nao pode 2x no dia)")
    print("  - Justificativa flexivel para qualquer horario")
    print("  - Design moderno com efeitos 5D e animacoes")
    print("  - Desenvolvido por WELL")
    print("=" * 65)
    print(f"Acesso WI-FI: http://SEU_IP:{PORTA}")
    print("=" * 65)
    print("\nServidor rodando... Ctrl+C para parar.\n")
    
    try:
        servidor = HTTPServer(("0.0.0.0", PORTA), ServidorPonto)
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor parado.")
        servidor.server_close()
