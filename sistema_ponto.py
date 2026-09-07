# -*- coding: utf-8 -*-
"""
=================================================================
   SISTEMA DE PONTO v3.2 - CLÍNICA
=================================================================
   Desenvolvido por WELL
   Última atualização: 2026-09-07
   
   NOVAS FUNCIONALIDADES v3.2:
   ✅ Sistema de múltiplos administradores
   ✅ Painel de resumo visual dos funcionários
   ✅ Correção total das abas do painel admin
   ✅ Gerenciamento completo de admins (CRUD)
   ✅ Cards coloridos e divertidos com horários
=================================================================
"""
import sqlite3
import json
import os
import io
import hashlib
import re
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ===================== CONFIGURACAO FUSO HORARIO BRASILIA =====================
import os
os.environ["TZ"] = "America/Sao_Paulo"
try:
    import time
    time.tzset()
except:
    pass
try:
    from zoneinfo import ZoneInfo
    FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
except:
    FUSO_BRASILIA = None

def agora_brasilia():
    """Retorna datetime atual no fuso horário de Brasília (UTC-3)"""
    if FUSO_BRASILIA:
        return datetime.now(FUSO_BRASILIA).replace(tzinfo=None)
    return datetime.now()

# ===================== CONFIGURACOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = int(os.environ.get("PORT", 8000))
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
    agora = agora_brasilia()
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
    
    # Tabela de funcionários
    conn.execute("""CREATE TABLE IF NOT EXISTS funcionarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        cpf TEXT UNIQUE NOT NULL,
        horario_entrada TEXT DEFAULT '08:00:00',
        horario_saida_almoco TEXT DEFAULT '12:00:00',
        horario_retorno_almoco TEXT DEFAULT '13:00:00',
        horario_saida TEXT DEFAULT '18:00:00'
    )""")
    
    # Tabela de registros de ponto
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
    
    # Tabela de acessos de dispositivos
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
    
    # Tabela de solicitações pendentes
    conn.execute("""CREATE TABLE IF NOT EXISTS solicitacoes_pendentes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        funcionario_id INTEGER NOT NULL,
        cpf TEXT NOT NULL,
        data_hora_solicitacao TEXT NOT NULL,
        tipo TEXT NOT NULL,
        atrasado INTEGER DEFAULT 0,
        minutos_atraso INTEGER DEFAULT 0,
        justificativa TEXT DEFAULT '',
        status TEXT DEFAULT 'PENDENTE',
        ip_dispositivo TEXT DEFAULT '',
        user_agent TEXT DEFAULT '',
        data_hora_aprovacao TEXT DEFAULT '',
        admin_aprovador TEXT DEFAULT '',
        motivo_negacao TEXT DEFAULT '',
        FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
    )""")
    
    # Tabela de ADMINS (NOVO - múltiplos administradores)
    conn.execute("""CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT UNIQUE NOT NULL,
        senha TEXT NOT NULL,
        nome_completo TEXT DEFAULT '',
        criado_em TEXT DEFAULT '',
        ultimo_login TEXT DEFAULT ''
    )""")
    
    # Migrações de colunas se necessário
    for coluna, tipo in [
        ("minutos_atraso","INTEGER DEFAULT 0"),
        ("minutos_banco_horas","INTEGER DEFAULT 0"),
        ("justificativa","TEXT DEFAULT ''"),
        ("ip_dispositivo","TEXT DEFAULT ''"),
        ("user_agent","TEXT DEFAULT ''"),
        ("horario_acesso","TEXT DEFAULT ''")
    ]:
        try:
            conn.execute(f"ALTER TABLE registros_ponto ADD COLUMN {coluna} {tipo}")
            print(f"[MIGRACAO] Coluna {coluna} adicionada")
        except: pass
    
    conn.commit()
    conn.close()

init_db()
criar_logo_padrao()

# ===================== FUNÇÕES AUXILIARES =====================
def formatar_cpf(cpf):
    return ''.join(filter(str.isdigit, str(cpf)))

def verificar_atraso(hora_registro, horario_padrao, tolerancia_minutos=0):
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_segundos = tolerancia_minutos * 60
        return t_r > (t_p + tolerancia_segundos)
    except: return False

def calcular_minutos(hora1, hora2):
    try:
        h1 = hora1.split(":"); h2 = hora2.split(":")
        t1 = int(h1[0])*3600 + int(h1[1])*60 + (int(h1[2]) if len(h1)>2 else 0)
        t2 = int(h2[0])*3600 + int(h2[1])*60 + (int(h2[2]) if len(h2)>2 else 0)
        return abs(t1-t2)//60
    except: return 0


def verificar_na_tolerancia_antes(hora_registro, horario_padrao, tolerancia_minutos=5):
    """Verifica se o registro está dentro da tolerância ANTES do horário padrão.
    Retorna True se estiver entre (horario_padrao - tolerancia) e horario_padrao (exclusive).
    Ex: horario_padrao=08:00, tolerancia=5 → 07:55:00 até 07:59:59 retorna True"""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_segundos = tolerancia_minutos * 60
        return (t_p - tolerancia_segundos) <= t_r < t_p
    except: return False

def verificar_na_tolerancia_depois(hora_registro, horario_padrao, tolerancia_minutos=5):
    """Verifica se o registro está dentro da tolerância DEPOIS do horário padrão.
    Retorna True se estiver entre horario_padrao (exclusive) e (horario_padrao + tolerancia)."""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_segundos = tolerancia_minutos * 60
        return t_p < t_r <= (t_p + tolerancia_segundos)
    except: return False

def calcular_banco_horas(tipo, hora_registro, func):
    """Calcula banco de horas. NÃO conta banco se estiver dentro da tolerância de 5 minutos
    ANTES (para chegadas) ou DEPOIS (para saídas) do horário padrão."""
    try:
        h_r = hora_registro.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        tolerancia = 5 * 60  # 5 minutos em segundos
        
        if tipo == "ENTRADA":
            h_p = func["horario_entrada"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            # Só conta banco se chegar MAIS de 5 minutos antes
            if t_r < (t_p - tolerancia): return (t_p-t_r)//60
        elif tipo == "SAIDA_ALMOCO":
            h_p = func["horario_saida_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            # Só conta banco se sair MAIS de 5 minutos DEPOIS
            if t_r > (t_p + tolerancia): return (t_r-t_p)//60
        elif tipo == "RETORNO_ALMOCO":
            h_p = func["horario_retorno_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            # Só conta banco se chegar MAIS de 5 minutos antes
            if t_r < (t_p - tolerancia): return (t_p-t_r)//60
        elif tipo == "SAIDA":
            h_p = func["horario_saida"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            # Só conta banco se sair MAIS de 5 minutos DEPOIS
            if t_r > (t_p + tolerancia): return (t_r-t_p)//60
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

def hash_senha(senha):
    """Hash simples para senhas de admins adicionais"""
    return hashlib.sha256(senha.encode('utf-8')).hexdigest()

def verificar_credenciais_admin(usuario, senha):
    """
    Verifica credenciais: primeiro o admin padrão, depois os admins do banco.
    Retorna (True, nome_usuario) ou (False, None)
    """
    # Admin padrão (master)
    if usuario == ADMIN_USUARIO and senha == ADMIN_SENHA:
        return True, ADMIN_USUARIO
    
    # Admins do banco de dados
    try:
        conn = get_db()
        admin = conn.execute("SELECT * FROM admins WHERE usuario = ?", (usuario,)).fetchone()
        conn.close()
        if admin and admin["senha"] == hash_senha(senha):
            return True, admin["nome_completo"] or admin["usuario"]
    except:
        pass
    
    return False, None

def limpar_sessoes_expiradas():
    agora = agora_brasilia()
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
                if expira and expira > agora_brasilia(): return True
    except: pass
    return False

def registrar_acesso_dispositivo(cpf, funcionario_id, ip, user_agent, tipo_acesso="pagina_inicial"):
    try:
        conn = get_db()
        agora = agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")
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
    <span class="rodape-versao">v3.2</span>
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
    ts = str(int(agora_brasilia().timestamp()))
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
    ts = str(int(agora_brasilia().timestamp()))
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
cpfInp.addEventListener('input',function(){this.value=this.value.replace(/\\\\D/g,'');});
cpfInp.addEventListener('keypress',function(e){if(e.key==='Enter')acessar();});
cpfInp.addEventListener('blur',async function(){
  const c=this.value.replace(/\\\\D/g,''); const inf=document.getElementById('infoFunc');
  if(c.length===11){
    try{const r=await fetch('/api/buscar/'+c);const d=await r.json();
      if(d.encontrado){inf.textContent='👤 '+d.nome;inf.className='info-func info-ok';}
      else{inf.textContent='⚠️ CPF NÃO cadastrado! Contate o RH.';inf.className='info-func info-err';}
    }catch(e){inf.style.display='none';}
  }else inf.style.display='none';
});
async function acessar(){
  const c=cpfInp.value.replace(/\\\\D/g,''); const m=document.getElementById('mensagem');
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
.tela-bloqueio { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); backdrop-filter:blur(10px); z-index:2000; align-items:center; justify-content:center; padding:20px; }
.tela-bloqueio.ativa { display:flex; }
.bloqueio-box { background:white; border-radius:28px; padding:40px 30px; width:100%; max-width:420px; text-align:center; box-shadow:0 30px 80px rgba(244,67,54,0.4); animation:entrar-cima 0.5s cubic-bezier(0.175,0.885,0.32,1.275); }
.bloqueio-icone { width:100px; height:100px; border-radius:50%; background:linear-gradient(135deg,#f44336,#ff5722); display:flex; align-items:center; justify-content:center; font-size:50px; margin:0 auto 20px; box-shadow:0 10px 30px rgba(244,67,54,0.5); animation:pulse-bloqueio 2s infinite; }
@keyframes pulse-bloqueio { 0%,100%{transform:scale(1);box-shadow:0 10px 30px rgba(244,67,54,0.5)} 50%{transform:scale(1.08);box-shadow:0 15px 40px rgba(244,67,54,0.6)} }
.bloqueio-box h2 { color:#f44336; font-size:22px; margin-bottom:10px; }
.bloqueio-sub { color:#666; font-size:14px; margin-bottom:20px; line-height:1.6; }
.bloqueio-info { background:linear-gradient(135deg,#fff3e0,#ffe0b2); border:2px solid #ff9800; border-radius:14px; padding:15px; margin-bottom:20px; text-align:left; }
.bloqueio-info p { margin:5px 0; font-size:13px; color:#e65100; }
.bloqueio-info strong { color:#bf360c; }
.bloqueio-status { background:linear-gradient(135deg,#e3f2fd,#bbdefb); border-radius:14px; padding:15px; margin-bottom:15px; }
.bloqueio-status .status-texto { color:#1565c0; font-weight:bold; font-size:14px; }
.bloqueio-status .status-dots { display:inline-block; margin-left:8px; }
.bloqueio-status .status-dots span { display:inline-block; width:8px; height:8px; background:#1565c0; border-radius:50%; margin:0 2px; animation:dots 1.4s infinite; }
.bloqueio-status .status-dots span:nth-child(2){animation-delay:0.2s}
.bloqueio-status .status-dots span:nth-child(3){animation-delay:0.4s}
@keyframes dots { 0%,80%,100%{transform:scale(0);opacity:0.5} 40%{transform:scale(1);opacity:1} }
.bloqueio-aprovado { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); border:2px solid #4CAF50; border-radius:14px; padding:20px; margin-bottom:15px; }
.bloqueio-aprovado h3 { color:#2e7d32; font-size:18px; margin-bottom:8px; }
.bloqueio-aprovado p { color:#1b5e20; font-size:14px; }
.bloqueio-negado { background:linear-gradient(135deg,#ffebee,#ffcdd2); border:2px solid #f44336; border-radius:14px; padding:20px; margin-bottom:15px; }
.bloqueio-negado h3 { color:#b71c1c; font-size:18px; margin-bottom:8px; }
.bloqueio-negado p { color:#c62828; font-size:14px; }
.bloqueio-negado .motivo { background:white; padding:10px; border-radius:8px; margin-top:10px; font-style:italic; color:#555; font-size:13px; }
.btn-continuar { width:100%; padding:14px; background:linear-gradient(135deg,#667eea,#764ba2); color:white; border:none; border-radius:12px; font-weight:bold; font-size:15px; cursor:pointer; transition:all 0.3s; }
.btn-continuar:hover { transform:translateY(-2px); box-shadow:0 6px 20px rgba(102,126,234,0.4); }
.bloqueio-aviso { background:linear-gradient(135deg,#fffde7,#fff9c4); border:1px solid #fbc02d; border-radius:10px; padding:10px; font-size:11px; color:#f57f17; margin-top:15px; }

/* ========== TELA DE CONFIRMAÇÃO DE REGISTRO (BONITA) ========== */
.tela-confirmacao { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:linear-gradient(135deg,rgba(76,175,80,0.92),rgba(56,249,215,0.88),rgba(102,126,234,0.9)); backdrop-filter:blur(15px); z-index:3000; align-items:center; justify-content:center; padding:20px; }
.tela-confirmacao.ativa { display:flex; animation:fadeIn 0.4s ease; }
@keyframes fadeIn { from{opacity:0} to{opacity:1} }
.confirmacao-box { background:white; border-radius:32px; padding:45px 35px; width:100%; max-width:440px; text-align:center; box-shadow:0 40px 100px rgba(0,0,0,0.35); animation:confirmarEntrada 0.6s cubic-bezier(0.175,0.885,0.32,1.275); }
@keyframes confirmarEntrada { from{opacity:0;transform:scale(0.7) translateY(30px)} to{opacity:1;transform:scale(1) translateY(0)} }
.confirmacao-icone-grande { width:130px; height:130px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:65px; margin:0 auto 25px; animation:checkPop 0.7s cubic-bezier(0.175,0.885,0.32,1.5); box-shadow:0 15px 40px rgba(0,0,0,0.2); }
@keyframes checkPop { 0%{transform:scale(0) rotate(-30deg)} 60%{transform:scale(1.15) rotate(5deg)} 100%{transform:scale(1) rotate(0)} }
.confirmacao-titulo { font-size:28px; font-weight:bold; margin-bottom:8px; color:#333; }
.confirmacao-subtitulo { color:#888; font-size:14px; margin-bottom:25px; }
.confirmacao-nome { background:linear-gradient(135deg,#f5f7fa,#e8ecf1); padding:15px; border-radius:16px; margin-bottom:18px; }
.confirmacao-nome .label { font-size:11px; color:#888; text-transform:uppercase; font-weight:bold; letter-spacing:1px; }
.confirmacao-nome .valor { font-size:20px; font-weight:bold; color:#333; margin-top:4px; }
.confirmacao-dados { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:20px; }
.confirmacao-dado { background:#f8f9fa; padding:14px 10px; border-radius:14px; }
.confirmacao-dado .dado-label { font-size:10px; color:#888; text-transform:uppercase; font-weight:bold; }
.confirmacao-dado .dado-valor { font-size:16px; font-weight:bold; color:#333; margin-top:4px; }
.confirmacao-dado .dado-valor.pequeno { font-size:13px; }
.confirmacao-status { padding:16px; border-radius:16px; margin-bottom:22px; font-weight:bold; font-size:15px; color:white; }
.confirmacao-status.normal { background:linear-gradient(135deg,#4CAF50,#66bb6a); }
.confirmacao-status.tolerancia { background:linear-gradient(135deg,#ff9800,#ffb74d); }
.confirmacao-status.banco { background:linear-gradient(135deg,#00bcd4,#4dd0e1); }
.confirmacao-status.atrasado { background:linear-gradient(135deg,#f44336,#ef5350); }
.btn-confirmar-ok { width:100%; padding:18px; background:linear-gradient(135deg,#667eea,#764ba2); color:white; border:none; border-radius:16px; font-weight:bold; font-size:17px; cursor:pointer; transition:all 0.3s; box-shadow:0 8px 25px rgba(102,126,234,0.4); }
.btn-confirmar-ok:hover { transform:translateY(-3px); box-shadow:0 12px 35px rgba(102,126,234,0.5); }
.btn-confirmar-ok:active { transform:translateY(0); }
.confirmacao-hora-real { position:absolute; top:25px; right:25px; background:rgba(255,255,255,0.25); color:white; padding:10px 18px; border-radius:25px; font-weight:bold; font-size:14px; backdrop-filter:blur(10px); border:1px solid rgba(255,255,255,0.3); }
.confirmacao-decoracao { position:absolute; bottom:0; left:0; right:0; height:8px; background:linear-gradient(90deg,#4CAF50,#38f9d7,#667eea,#f093fb,#4CAF50); background-size:300% 100%; animation:arco-iris 4s linear infinite; border-radius:0 0 32px 32px; }

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
<!-- TELA DE BLOQUEIO - AGUARDANDO APROVAÇÃO ADMIN -->
<div class="tela-bloqueio" id="telaBloqueio">
  <div class="bloqueio-box" id="bloqueioConteudo">
    <div class="bloqueio-icone" id="bloqueioIcone">🔒</div>
    <h2 id="bloqueioTitulo">REGISTRO BLOQUEADO</h2>
    <p class="bloqueio-sub" id="bloqueioSub">Seu registro está fora da tolerância de 5 minutos e requer aprovação do administrador.</p>
    <div class="bloqueio-info" id="bloqueioInfo">
      <p><strong>Tipo:</strong> <span id="bloqTipo">-</span></p>
      <p><strong>Atraso:</strong> <span id="bloqAtraso">-</span> minuto(s)</p>
      <p><strong>Solicitado em:</strong> <span id="bloqHora">-</span></p>
    </div>
    <div class="bloqueio-status" id="bloqueioStatus">
      <span class="status-texto">⏳ Aguardando aprovação do administrador</span>
      <span class="status-dots"><span></span><span></span><span></span></span>
    </div>
    <div class="bloqueio-aviso">⚠️ Esta tela não pode ser fechada. O sistema verificará automaticamente a aprovação.</div>
  </div>
</div>

<!-- TELA DE CONFIRMAÇÃO DE REGISTRO - BONITA E ANIMADA -->
<div class="tela-confirmacao" id="telaConfirmacao">
  <div class="confirmacao-hora-real" id="confHoraReal">--:--:--</div>
  <div class="confirmacao-box">
    <div class="confirmacao-icone-grande" id="confIcone" style="background:linear-gradient(135deg,#4CAF50,#66bb6a);color:white;">✅</div>
    <h2 class="confirmacao-titulo" id="confTitulo">PONTO REGISTRADO!</h2>
    <p class="confirmacao-subtitulo" id="confSubtitulo">Registro efetuado com sucesso</p>
    <div class="confirmacao-nome">
      <div class="label">Funcionário</div>
      <div class="valor" id="confNome">---</div>
    </div>
    <div class="confirmacao-dados">
      <div class="confirmacao-dado">
        <div class="dado-label">Tipo</div>
        <div class="dado-valor pequeno" id="confTipo">---</div>
      </div>
      <div class="confirmacao-dado">
        <div class="dado-label">Horário</div>
        <div class="dado-valor" id="confHora">--:--:--</div>
      </div>
      <div class="confirmacao-dado">
        <div class="dado-label">Data</div>
        <div class="dado-valor pequeno" id="confData">--/--/----</div>
      </div>
      <div class="confirmacao-dado">
        <div class="dado-label">Padrão</div>
        <div class="dado-valor pequeno" id="confPadrao">--:--:--</div>
      </div>
    </div>
    <div class="confirmacao-status normal" id="confStatus">✅ No horário exato</div>
    <button class="btn-confirmar-ok" onclick="fecharTelaConfirmacao()">✓ CONFIRMAR</button>
    <div class="confirmacao-decoracao"></div>
  </div>
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
    document.getElementById('cpfFunc').textContent='CPF: '+CPF.replace(/(\\\\d{3})(\\\\d{3})(\\\\d{3})(\\\\d{2})/,'$1.$2.$3-$4');
    document.getElementById('fotoFunc').textContent=d.nome.charAt(0).toUpperCase();
    document.getElementById('horariosInfo').innerHTML=
      '<div class="horario-item"><div class="horario-label">Entrada</div><div class="horario-valor">🕐 '+d.horario_entrada+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída Almoço</div><div class="horario-valor">🍽️ '+d.horario_saida_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Retorno</div><div class="horario-valor">↩️ '+d.horario_retorno_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída</div><div class="horario-valor">🚪 '+d.horario_saida+'</div></div>';
  }catch(e){window.location.href='/';}
}
carregar();
let pollingId=null;
async function registrar(tipo){
  if(!CPF)return;
  try{
    const r=await fetch('/api/verificar_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:CPF,tipo:tipo,qr_code:QR})});
    const d=await r.json();
    if(!r.ok){mostrar(d.detail||'Erro','erro');return;}
    if(d.bloqueado){
      await solicitarAprovacao(tipo,d);
    }else if(d.precisa_justificativa){
      pendente={cpf:CPF,tipo:tipo};
      document.getElementById('modalTexto').textContent=d.mensagem_justificativa;
      document.getElementById('modalInfo').textContent='⏱️ '+d.info_atraso;
      document.getElementById('justificativa').value='';
      document.getElementById('modalJust').classList.add('ativo');
      document.getElementById('justificativa').focus();
    }else await executar(CPF,tipo,'');
  }catch(e){mostrar('Erro de conexão!','erro');}
}
async function solicitarAprovacao(tipo,dados){
  const just=prompt('📝 Descreva o motivo do atraso:\\n(esta informação será enviada ao administrador)');
  if(just===null)return;
  try{
    const r=await fetch('/api/solicitar_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:CPF,tipo:tipo,qr_code:QR,justificativa:just||'Não informado'})});
    const d=await r.json();
    if(!r.ok){mostrar(d.detail||'Erro','erro');return;}
    mostrarTelaBloqueio(tipo,dados.minutos_atraso||0);
    iniciarPolling();
  }catch(e){mostrar('Erro ao enviar solicitação!','erro');}
}
function mostrarTelaBloqueio(tipo,minutos){
  const tiposLabel={'ENTRADA':'✅ ENTRADA','SAIDA_ALMOCO':'🍽️ SAÍDA ALMOCO','RETORNO_ALMOCO':'↩️ RETORNO ALMOCO','SAIDA':'🚪 SAÍDA'};
  document.getElementById('bloqTipo').textContent=tiposLabel[tipo]||tipo;
  document.getElementById('bloqAtraso').textContent=minutos;
  document.getElementById('bloqHora').textContent=new Date().toLocaleTimeString('pt-BR');
  document.getElementById('telaBloqueio').classList.add('ativa');
  document.addEventListener('keydown',prevenirFechamento);
}
function prevenirFechamento(e){
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();}
}
function iniciarPolling(){
  pararPolling();
  pollingId=setInterval(verificarStatusSolicitacao,3000);
  verificarStatusSolicitacao();
}
function pararPolling(){
  if(pollingId){clearInterval(pollingId);pollingId=null;}
}
async function verificarStatusSolicitacao(){
  if(!CPF)return;
  try{
    const r=await fetch('/api/solicitacao_status/'+CPF);
    const d=await r.json();
    if(!d.encontrada)return;
    if(d.status==='APROVADA'){
      pararPolling();
      mostrarResultadoAprovacao(d);
    }else if(d.status==='NEGADA'){
      pararPolling();
      mostrarResultadoNegacao(d);
    }
  }catch(e){}
}
function mostrarResultadoAprovacao(d){
  document.getElementById('bloqueioIcone').textContent='✅';
  document.getElementById('bloqueioIcone').style.background='linear-gradient(135deg,#4CAF50,#66bb6a)';
  document.getElementById('bloqueioIcone').style.animation='none';
  document.getElementById('bloqueioTitulo').textContent='SOLICITAÇÃO APROVADA!';
  document.getElementById('bloqueioTitulo').style.color='#2e7d32';
  document.getElementById('bloqueioSub').textContent='O administrador aprovou seu registro. Ponto registrado com sucesso.';
  document.getElementById('bloqueioStatus').outerHTML=
    '<div class="bloqueio-aprovado"><h3>🎉 Ponto Registrado!</h3><p>Tipo: '+d.tipo_formatado+'<br>Horário: '+d.data_hora.split(' ')[1]+'</p></div>'+
    '<button class="btn-continuar" onclick="fecharTelaBloqueio()">Continuar</button>';
  document.removeEventListener('keydown',prevenirFechamento);
}
function mostrarResultadoNegacao(d){
  document.getElementById('bloqueioIcone').textContent='❌';
  document.getElementById('bloqueioIcone').style.background='linear-gradient(135deg,#f44336,#d32f2f)';
  document.getElementById('bloqueioIcone').style.animation='none';
  document.getElementById('bloqueioTitulo').textContent='SOLICITAÇÃO NEGADA';
  document.getElementById('bloqueioTitulo').style.color='#b71c1c';
  document.getElementById('bloqueioSub').textContent='O administrador negou sua solicitação de registro.';
  const motivo=d.motivo_negacao?'<div class="motivo">Motivo: '+d.motivo_negacao+'</div>':'';
  document.getElementById('bloqueioStatus').outerHTML=
    '<div class="bloqueio-negado"><h3>⚠️ Registro não efetuado</h3><p>Procure o administrador para mais informações.</p>'+motivo+'</div>'+
    '<button class="btn-continuar" onclick="fecharTelaBloqueio()">Entendido</button>';
  document.removeEventListener('keydown',prevenirFechamento);
}
function fecharTelaBloqueio(){
  document.getElementById('telaBloqueio').classList.remove('ativa');
  location.reload();
}
function fecharModal(){document.getElementById('modalJust').classList.remove('ativo');pendente=null;}
async function confirmar(){
  if(!pendente)return;
  const j=document.getElementById('justificativa').value.trim();
  if(!j){alert('Informe a justificativa!');document.getElementById('justificativa').focus();return;}
  fecharModal();
  
  if(pendente.solicitar){
    // É uma solicitação de aprovação para o admin
    try{
      const r=await fetch('/api/solicitar_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:pendente.cpf,tipo:pendente.tipo,qr_code:QR,justificativa:j||'Não informado'})});
      const d=await r.json();
      if(!r.ok){alert('❌ '+(d.detail||'Erro'));return;}
      // Buscar os dados de atraso para a tela de bloqueio
      try{
        const v=await fetch('/api/verificar_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:CPF,tipo:pendente.tipo,qr_code:QR})});
        const vd=await v.json();
        mostrarTelaBloqueio(pendente.tipo,vd.minutos_atraso||0);
        iniciarPolling();
      }catch(e){mostrarTelaBloqueio(pendente.tipo,0);iniciarPolling();}
    }catch(e){alert('❌ Erro ao enviar solicitação!');}
  }else{
    // É um registro normal com justificativa
    await executar(pendente.cpf,pendente.tipo,j);
  }
}
async function executar(cpf,tipo,just){
  try{
    const r=await fetch('/api/bater_ponto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:cpf,tipo:tipo,qr_code:QR,justificativa:just})});
    const d=await r.json();
    if(r.ok && d.registro_ok){
      mostrarTelaConfirmacao(d);
    }else{
      alert('❌ '+(d.detail||'Erro ao registrar ponto'));
    }
  }catch(e){alert('❌ Erro de conexão!');}
}
function mostrar(texto,tipo){
  // Função mantida para compatibilidade, mas agora usa alert para erros
  if(tipo==='erro'){alert('⚠️ '+texto);}
}

let intervaloHoraReal=null;
function mostrarTelaConfirmacao(d){
  document.getElementById('confIcone').textContent=d.tipo_icone||'✅';
  document.getElementById('confIcone').style.background='linear-gradient(135deg,'+d.tipo_cor+','+d.tipo_cor+')';
  document.getElementById('confTitulo').textContent=d.tipo_label+' REGISTRADA!';
  document.getElementById('confSubtitulo').textContent='Registro efetuado com sucesso';
  document.getElementById('confNome').textContent=d.nome;
  document.getElementById('confTipo').textContent=d.tipo_label;
  document.getElementById('confHora').textContent=d.hora;
  document.getElementById('confData').textContent=d.data;
  document.getElementById('confPadrao').textContent=d.horario_padrao;
  
  const statusEl=document.getElementById('confStatus');
  statusEl.textContent=d.status_texto;
  statusEl.className='confirmacao-status '+d.status_registro.toLowerCase();
  
  document.getElementById('telaConfirmacao').classList.add('ativa');
  
  // Atualizar hora real no canto
  atualizarHoraRealConf();
  if(intervaloHoraReal)clearInterval(intervaloHoraReal);
  intervaloHoraReal=setInterval(atualizarHoraRealConf,1000);
}
function atualizarHoraRealConf(){
  const el=document.getElementById('confHoraReal');
  if(el)el.textContent=new Date().toLocaleTimeString('pt-BR');
}
function fecharTelaConfirmacao(){
  document.getElementById('telaConfirmacao').classList.remove('ativa');
  if(intervaloHoraReal){clearInterval(intervaloHoraReal);intervaloHoraReal=null;}
}
document.getElementById('modalJust').addEventListener('click',function(e){if(e.target===this)fecharModal();});
</script>
</body>
</html>"""

# ===================== HTML - PAINEL ADMIN (CORRIGIDO E MELHORADO) =====================
def gerar_html_admin():
    ts = str(int(agora_brasilia().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚙️ Painel Administrativo v3.2</title>
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
.tab { padding:12px 18px; background:#dde2e8; border:none; border-radius:12px 12px 0 0; cursor:pointer; font-weight:bold; font-size:12px; color:#555; transition:all 0.3s; }
.tab:hover { background:#cfd6de; transform:translateY(-2px); }
.tab.ativo { background:white; color:#667eea; box-shadow:0 -4px 15px rgba(0,0,0,0.08); }
.painel { background:white; border-radius:0 18px 18px 18px; padding:28px; box-shadow:0 10px 40px rgba(0,0,0,0.08); display:none; }
.painel.ativo { display:block; animation:entrar-cima 0.4s ease; }
h2 { color:#333; margin-bottom:22px; font-size:21px; background:linear-gradient(135deg,#667eea,#764ba2); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
input, select, textarea { width:100%; padding:12px; margin:8px 0; border:2px solid #e0e0e0; border-radius:10px; font-size:14px; font-family:'Segoe UI',Arial,sans-serif; transition:all 0.3s; background:#fafafa; }
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
.tab-badge { position:relative; }
.badge-notificacao { position:absolute; top:-5px; right:-8px; background:#f44336; color:white; border-radius:50%; width:20px; height:20px; font-size:11px; display:flex; align-items:center; justify-content:center; font-weight:bold; box-shadow:0 2px 8px rgba(244,67,54,0.5); animation:pulse-badge 2s infinite; }
@keyframes pulse-badge { 0%,100%{transform:scale(1)} 50%{transform:scale(1.15)} }
.solic-card { background:linear-gradient(135deg,#fff,#fafafa); border:2px solid #e0e0e0; border-left:5px solid #ff9800; border-radius:14px; padding:18px; margin:12px 0; transition:all 0.3s; box-shadow:0 3px 10px rgba(0,0,0,0.05); }
.solic-card:hover { transform:translateX(4px); box-shadow:0 6px 20px rgba(0,0,0,0.1); }
.solic-card.atraso-alto { border-left-color:#f44336; background:linear-gradient(135deg,#fff,#fff5f5); }
.solic-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
.solic-nome { font-size:16px; font-weight:bold; color:#333; }
.solic-tipo { font-size:12px; padding:4px 10px; border-radius:20px; font-weight:bold; color:white; }
.tipo-entrada-bg { background:linear-gradient(135deg,#4CAF50,#66bb6a); }
.tipo-saida-almoco-bg { background:linear-gradient(135deg,#ff9800,#ffb74d); }
.tipo-retorno-almoco-bg { background:linear-gradient(135deg,#2196F3,#64b5f6); }
.tipo-saida-bg { background:linear-gradient(135deg,#f44336,#ef5350); }
.solic-detalhes { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; margin:10px 0; }
.solic-detalhe-item { background:#f5f5f5; padding:8px 10px; border-radius:8px; font-size:12px; }
.solic-detalhe-item strong { display:block; color:#888; font-size:10px; text-transform:uppercase; margin-bottom:2px; }
.solic-justificativa { background:linear-gradient(135deg,#fff8e1,#ffecb3); border-radius:10px; padding:10px 12px; margin:10px 0; font-size:13px; color:#e65100; border-left:3px solid #ff9800; }
.solic-acoes { display:flex; gap:10px; margin-top:12px; }
.solic-acoes button { flex:1; padding:10px; font-size:13px; }
.alerta-solic-vazio { text-align:center; padding:40px; color:#999; font-size:15px; }
.alerta-solic-vazio .icone-grande { font-size:50px; display:block; margin-bottom:15px; opacity:0.5; }
.motivo-input { width:100%; padding:10px; margin:8px 0; border:2px solid #e0e0e0; border-radius:8px; font-size:13px; resize:vertical; min-height:60px; }

/* ========== NOVOS ESTILOS: RESUMO FUNCIONÁRIOS ========== */
.resumo-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:20px; margin-top:20px; }
.func-card-resumo { background:white; border-radius:18px; padding:20px; position:relative; overflow:hidden; transition:all 0.4s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 8px 25px rgba(0,0,0,0.08); border:2px solid transparent; }
.func-card-resumo:hover { transform:translateY(-8px) scale(1.02); box-shadow:0 20px 50px rgba(102,126,234,0.2); border-color:#667eea; }
.func-card-resumo::before { content:''; position:absolute; top:0; left:0; right:0; height:6px; background:linear-gradient(90deg,#667eea,#f093fb,#4facfe,#43e97b); }
.func-avatar { width:60px; height:60px; border-radius:50%; background:linear-gradient(135deg,#667eea,#f093fb); display:flex; align-items:center; justify-content:center; color:white; font-size:26px; font-weight:bold; margin-bottom:12px; box-shadow:0 5px 15px rgba(102,126,234,0.3); }
.func-nome-resumo { font-size:16px; font-weight:bold; color:#333; margin-bottom:4px; }
.func-cpf-resumo { font-size:11px; color:#999; margin-bottom:15px; }
.func-horarios-resumo { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.func-horario-item { background:#f8f9fa; padding:8px; border-radius:8px; text-align:center; transition:all 0.3s; }
.func-card-resumo:hover .func-horario-item { background:linear-gradient(135deg,#f0f4ff,#f8f0ff); }
.func-horario-icone { font-size:16px; display:block; margin-bottom:2px; }
.func-horario-label { font-size:9px; color:#888; text-transform:uppercase; font-weight:bold; }
.func-horario-valor { font-size:12px; color:#333; font-weight:bold; margin-top:2px; }
.func-status-hoje { margin-top:12px; padding:8px 12px; border-radius:10px; text-align:center; font-size:12px; font-weight:bold; }
.status-ok { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#2e7d32; }
.status-pendente { background:linear-gradient(135deg,#fff3e0,#ffe0b2); color:#e65100; }
.resumo-estatisticas { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:15px; margin-bottom:25px; }
.stat-card { padding:18px; border-radius:14px; text-align:center; color:white; transition:all 0.3s; }
.stat-card:hover { transform:translateY(-4px); box-shadow:0 10px 25px rgba(0,0,0,0.2); }
.stat-card.roxo { background:linear-gradient(135deg,#667eea,#764ba2); }
.stat-card.verde { background:linear-gradient(135deg,#43e97b,#38f9d7); }
.stat-card.laranja { background:linear-gradient(135deg,#fa709a,#fee140); }
.stat-card.azul { background:linear-gradient(135deg,#4facfe,#00f2fe); }
.stat-numero { font-size:32px; font-weight:bold; display:block; }
.stat-label { font-size:12px; opacity:0.9; margin-top:4px; display:block; }

/* ========== NOVOS ESTILOS: GERENCIAR ADMINS ========== */
.admin-item { display:flex; justify-content:space-between; align-items:center; padding:15px; background:#fafafa; border-radius:12px; margin:10px 0; border-left:4px solid #667eea; transition:all 0.3s; }
.admin-item:hover { background:#f0f4ff; transform:translateX(4px); }
.admin-info { display:flex; align-items:center; gap:15px; }
.admin-avatar { width:45px; height:45px; border-radius:50%; background:linear-gradient(135deg,#667eea,#764ba2); display:flex; align-items:center; justify-content:center; color:white; font-weight:bold; font-size:18px; }
.admin-dados strong { display:block; color:#333; font-size:14px; }
.admin-dados span { color:#888; font-size:12px; }
.admin-master { background:linear-gradient(135deg,#fff3e0,#ffe0b2); border-left-color:#ff9800; }
.admin-master .admin-avatar { background:linear-gradient(135deg,#ff9800,#f57c00); }
</style>
</head>
<body>
<div class="header">
<div class="header-content">
<img src="/static/logo.png?t=""" + ts + """" alt="Logo" class="logo-header" onerror="this.outerHTML='<div class=\\'logo-header-fallback\\'>🏥</div>'">
<h1>⚙️ Painel Administrativo v3.2</h1>
</div>
<div class="logout" onclick="sair()">🚪 Sair</div>
</div>
<div class="container">
<div class="tabs">
<button class="tab ativo" onclick="abrirAba('resumo',this)">📊 Resumo</button>
<button class="tab" onclick="abrirAba('cadastro',this)">👤 Cadastrar</button>
<button class="tab" onclick="abrirAba('funcionarios',this)">📋 Funcionários</button>
<button class="tab" onclick="abrirAba('registros',this)">📊 Registros</button>
<button class="tab" onclick="abrirAba('relatorios',this)">📄 Relatórios PDF</button>
<button class="tab" onclick="abrirAba('qrcode',this)">📱 QR Code</button>
<button class="tab tab-badge" onclick="abrirAba('solicitacoes',this)" id="tabSolicitacoes">📨 Solicitações <span class="badge-notificacao" id="badgeSolic" style="display:none;">0</span></button>
<button class="tab" onclick="abrirAba('acessos',this)">📡 Acessos</button>
<button class="tab" onclick="abrirAba('admins',this)">👥 Admins</button>
<button class="tab" onclick="abrirAba('config',this)">🔧 Configurações</button>
</div>

<!-- ========== ABA: RESUMO (NOVA!) ========== -->
<div id="resumo" class="painel ativo">
<h2>📊 Resumo da Equipe</h2>
<div class="resumo-estatisticas" id="estatisticasResumo">
<div class="stat-card roxo"><span class="stat-numero" id="totalFuncs">0</span><span class="stat-label">Funcionários</span></div>
<div class="stat-card verde"><span class="stat-numero" id="totalRegistrosHoje">0</span><span class="stat-label">Registros Hoje</span></div>
<div class="stat-card laranja"><span class="stat-numero" id="totalAtrasos">0</span><span class="stat-label">Atrasos Hoje</span></div>
<div class="stat-card azul"><span class="stat-numero" id="totalSolicPend">0</span><span class="stat-label">Solicitações Pendentes</span></div>
</div>
<h3 style="color:#555;margin-bottom:10px;">👥 Horários da Equipe</h3>
<div class="resumo-grid" id="gridResumo">
<div style="grid-column:1/-1;text-align:center;padding:40px;color:#999;">
<div style="font-size:40px;margin-bottom:10px;opacity:0.5;">⏳</div>
Carregando dados dos funcionários...
</div>
</div>
</div>

<!-- ========== ABA: CADASTRO ========== -->
<div id="cadastro" class="painel">
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

<!-- ========== ABA: FUNCIONÁRIOS ========== -->
<div id="funcionarios" class="painel">
<h2>Funcionários Cadastrados</h2>
<button onclick="carregarFuncs()">🔄 Atualizar Lista</button>
<table><thead><tr><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Saída Almoço</th><th>Retorno</th><th>Saída</th><th>Ação</th></tr></thead><tbody id="tbodyFunc"></tbody></table>
</div>

<!-- ========== ABA: REGISTROS ========== -->
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

<!-- ========== ABA: RELATÓRIOS ========== -->
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

<!-- ========== ABA: QR CODE ========== -->
<div id="qrcode" class="painel">
<h2>📱 QR Code do Sistema</h2>
<div class="qr-info"><p><strong>URL Local:</strong></p><p id="urlLocal" style="font-weight:bold;color:#1565c0;"></p></div>
<button onclick="gerarQR()">🔄 Gerar/Atualizar QR Code</button>
<div id="qrImg" style="margin-top:20px;"></div>
</div>

<!-- ========== ABA: SOLICITAÇÕES ========== -->
<div id="solicitacoes" class="painel">
<h2>📨 Solicitações Pendentes de Aprovação</h2>
<div class="info-box">⚠️ Funcionários com atraso superior a 5 minutos precisam de sua aprovação para registrar o ponto. A tela do funcionário fica bloqueada até sua decisão.</div>
<button onclick="carregarSolicitacoes()">🔄 Atualizar Lista</button>
<div id="listaSolicitacoes"></div>
</div>

<!-- ========== ABA: ACESSOS ========== -->
<div id="acessos" class="painel">
<h2>📡 Registros de Acesso de Dispositivos</h2>
<button onclick="carregarAcessos()">🔄 Atualizar</button>
<table><thead><tr><th>Data/Hora</th><th>CPF</th><th>Funcionário</th><th>IP</th><th>Dispositivo</th><th>Tipo</th></tr></thead><tbody id="tbodyAcessos"></tbody></table>
</div>

<!-- ========== ABA: ADMINS (NOVA!) ========== -->
<div id="admins" class="painel">
<h2>👥 Gerenciar Administradores</h2>
<div class="info-box">💡 O usuário <strong>admin</strong> é o administrador MASTER e não pode ser excluído. Você pode criar admins adicionais abaixo.</div>
<div class="card" style="max-width:500px;">
<h3 style="margin-bottom:15px;color:#555;">➕ Adicionar Novo Admin</h3>
<div class="mensagem" id="msgAdmin"></div>
<label>Usuário (login):</label>
<input type="text" id="novoAdminUser" placeholder="Ex: rh.clinica">
<label>Nome Completo:</label>
<input type="text" id="novoAdminNome" placeholder="Ex: Recursos Humanos">
<label>Senha:</label>
<input type="password" id="novoAdminSenha" placeholder="Digite uma senha forte">
<button class="btn-success" onclick="cadastrarAdmin()">💾 Criar Admin</button>
</div>
<h3 style="margin-top:30px;color:#555;">📋 Administradores Cadastrados</h3>
<div id="listaAdmins"></div>
</div>

<!-- ========== ABA: CONFIGURAÇÕES ========== -->
<div id="config" class="painel">
<h2>🔧 Configurações</h2>
<div class="card">
<h3 style="margin-bottom:10px;">🖼️ Logo da Clínica</h3>
<p style="font-size:14px;line-height:1.6;">Coloque sua logo em <strong>static/logo.png</strong> (formato PNG).<br>Se não aparecer, pressione <strong>Ctrl+F5</strong>.</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🔐 Credenciais de Acesso Master</h3>
<p style="font-size:14px;"><strong>Usuário:</strong> admin<br><strong>Senha:</strong> 3223ronte</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">👥 Múltiplos Admins</h3>
<p style="font-size:14px;line-height:1.6;">Agora você pode criar administradores adicionais na aba <strong>👥 Admins</strong>. Cada admin tem seu próprio usuário e senha para acessar o painel.</p>
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
// ========== CONFIGURAÇÕES INICIAIS ==========
const h=new Date();const ma=h.toISOString().slice(0,7);
document.getElementById('mesAno').value=ma;
document.getElementById('mesAnoFunc').value=ma;
document.getElementById('urlLocal').textContent=window.location.origin+'/';
document.getElementById('cpfCad').addEventListener('input',function(){this.value=this.value.replace(/\\\\D/g,'');});

// ========== SISTEMA DE ABAS (CORRIGIDO!) ==========
function abrirAba(nomeId, botao){
  // Remove classe ativo de TODOS os painéis
  document.querySelectorAll('.painel').forEach(function(p){
    p.classList.remove('ativo');
  });
  // Remove classe ativo de TODAS as abas
  document.querySelectorAll('.tab').forEach(function(t){
    t.classList.remove('ativo');
  });
  // Ativa o painel selecionado
  document.getElementById(nomeId).classList.add('ativo');
  // Ativa o botão da aba
  botao.classList.add('ativo');
  // Carrega dados específicos conforme a aba
  if(nomeId==='resumo')carregarResumo();
  if(nomeId==='funcionarios')carregarFuncs();
  if(nomeId==='registros')carregarRegs();
  if(nomeId==='relatorios')carregarSel();
  if(nomeId==='acessos')carregarAcessos();
  if(nomeId==='solicitacoes')carregarSolicitacoes();
  if(nomeId==='admins')carregarAdmins();
}

// ========== POLLING PARA SOLICITAÇÕES ==========
let pollingAdmin=null;
function iniciarPollingAdmin(){
  pararPollingAdmin();
  pollingAdmin=setInterval(atualizarBadgeSolic,5000);
  atualizarBadgeSolic();
}
function pararPollingAdmin(){if(pollingAdmin){clearInterval(pollingAdmin);pollingAdmin=null;}}
async function atualizarBadgeSolic(){
  try{
    const r=await fetch('/api/solicitacoes_pendentes');
    if(r.status===401){window.location.href='/admin';return;}
    const d=await r.json();
    const badge=document.getElementById('badgeSolic');
    if(d.length>0){
      badge.style.display='flex';
      badge.textContent=d.length;
    }else{
      badge.style.display='none';
    }
  }catch(e){}
}
setTimeout(iniciarPollingAdmin,1000);

// ========== NOVA FUNÇÃO: CARREGAR RESUMO ==========
async function carregarResumo(){
  try{
    const r=await fetch('/api/funcionarios');
    if(r.status===401){window.location.href='/admin';return;}
    const funcs=await r.json();
    
    const r2=await fetch('/api/registros');
    let registros=[];
    if(r2.ok)registros=await r2.json();
    
    const r3=await fetch('/api/solicitacoes_pendentes');
    let solics=[];
    if(r3.ok)solics=await r3.json();
    
    // Estatísticas
    document.getElementById('totalFuncs').textContent=funcs.length;
    
    const hoje=new Date().toISOString().slice(0,10);
    const regsHoje=registros.filter(function(x){
      const partes=x.data.split('/');
      const dataReg=partes[2]+'-'+partes[1]+'-'+partes[0];
      return dataReg===hoje;
    });
    document.getElementById('totalRegistrosHoje').textContent=regsHoje.length;
    
    const atrasosHoje=regsHoje.filter(function(x){return x.atrasado;}).length;
    document.getElementById('totalAtrasos').textContent=atrasosHoje;
    document.getElementById('totalSolicPend').textContent=solics.length;
    
    // Grid de funcionários
    const grid=document.getElementById('gridResumo');
    if(funcs.length===0){
      grid.innerHTML='<div style="grid-column:1/-1;text-align:center;padding:40px;color:#999;"><div style="font-size:40px;margin-bottom:10px;opacity:0.5;">👥</div>Nenhum funcionário cadastrado ainda.</div>';
      return;
    }
    
    grid.innerHTML=funcs.map(function(f){
      const inicial=f.nome.charAt(0).toUpperCase();
      return '<div class="func-card-resumo">'+
        '<div class="func-avatar">'+inicial+'</div>'+
        '<div class="func-nome-resumo">'+f.nome+'</div>'+
        '<div class="func-cpf-resumo">CPF: '+f.cpf.replace(/(\\\\d{3})(\\\\d{3})(\\\\d{3})(\\\\d{2})/,'$1.$2.$3-$4')+'</div>'+
        '<div class="func-horarios-resumo">'+
          '<div class="func-horario-item"><span class="func-horario-icone">🌅</span><span class="func-horario-label">Entrada</span><span class="func-horario-valor">'+f.horario_entrada+'</span></div>'+
          '<div class="func-horario-item"><span class="func-horario-icone">🍽️</span><span class="func-horario-label">Almoço</span><span class="func-horario-valor">'+f.horario_saida_almoco+'</span></div>'+
          '<div class="func-horario-item"><span class="func-horario-icone">↩️</span><span class="func-horario-label">Retorno</span><span class="func-horario-valor">'+f.horario_retorno_almoco+'</span></div>'+
          '<div class="func-horario-item"><span class="func-horario-icone">🌙</span><span class="func-horario-label">Saída</span><span class="func-horario-valor">'+f.horario_saida+'</span></div>'+
        '</div>'+
        '<div class="func-status-hoje status-pendente">⏰ Horários definidos</div>'+
      '</div>';
    }).join('');
  }catch(e){
    document.getElementById('gridResumo').innerHTML='<div style="grid-column:1/-1;text-align:center;padding:40px;color:#f44336;">Erro ao carregar dados.</div>';
  }
}

// ========== SOLICITAÇÕES ==========
async function carregarSolicitacoes(){
  const r=await fetch('/api/solicitacoes_pendentes');
  if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();
  const lista=document.getElementById('listaSolicitacoes');
  if(d.length===0){
    lista.innerHTML='<div class="alerta-solic-vazio"><span class="icone-grande">✅</span>Nenhuma solicitação pendente no momento.</div>';
    return;
  }
  lista.innerHTML=d.map(function(s){
    const ct='tipo-'+s.tipo.toLowerCase().replace(/_/g,'-')+'-bg';
    const atrasoClasse=s.minutos_atraso>=15?'solic-card atraso-alto':'solic-card';
    const justificativa=s.justificativa?'<div class="solic-justificativa">📝 <strong>Justificativa:</strong> '+s.justificativa+'</div>':'';
    return '<div class="'+atrasoClasse+'" id="solic-'+s.id+'">'+
      '<div class="solic-header">'+
        '<div class="solic-nome">👤 '+s.nome+'</div>'+
        '<span class="solic-tipo '+ct+'">'+s.tipo_formatado+'</span>'+
      '</div>'+
      '<div class="solic-detalhes">'+
        '<div class="solic-detalhe-item"><strong>Data</strong>'+s.data+'</div>'+
        '<div class="solic-detalhe-item"><strong>Hora</strong>'+s.hora+'</div>'+
        '<div class="solic-detalhe-item"><strong>Atraso</strong><span style="color:#f44336;font-weight:bold;">'+s.minutos_atraso+' min</span></div>'+
        '<div class="solic-detalhe-item"><strong>CPF</strong>'+s.cpf+'</div>'+
      '</div>'+
      justificativa+
      '<div class="solic-acoes">'+
        '<button class="btn-success" onclick="aprovarSolic('+s.id+')">✅ ACEITAR ATRASO</button>'+
        '<button class="btn-danger" onclick="negarSolic('+s.id+')">❌ NEGAR</button>'+
      '</div>'+
    '</div>';
  }).join('');
}
async function aprovarSolic(id){
  if(!confirm('✅ Confirmar aprovação?\\n\\nO ponto será registrado para este funcionário.'))return;
  try{
    const r=await fetch('/api/solicitacoes/'+id+'/aprovar',{method:'POST',headers:{'Content-Type':'application/json'}});
    if(r.status===401){window.location.href='/admin';return;}
    const d=await r.json();
    if(r.ok){
      alert('✅ Solicitação aprovada! Ponto registrado.');
      carregarSolicitacoes();
      atualizarBadgeSolic();
      carregarResumo();
    }else alert('❌ Erro: '+(d.detail||'Erro ao aprovar'));
  }catch(e){alert('Erro de conexão!');}
}
async function negarSolic(id){
  const motivo=prompt('❌ Informe o motivo da negativa (será exibido ao funcionário):');
  if(motivo===null)return;
  try{
    const r=await fetch('/api/solicitacoes/'+id+'/negar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({motivo:motivo||'Não informado'})});
    if(r.status===401){window.location.href='/admin';return;}
    const d=await r.json();
    if(r.ok){
      alert('❌ Solicitação negada. O funcionário será notificado.');
      carregarSolicitacoes();
      atualizarBadgeSolic();
      carregarResumo();
    }else alert('❌ Erro: '+(d.detail||'Erro ao negar'));
  }catch(e){alert('Erro de conexão!');}
}

// ========== FUNÇÕES GERAIS ==========
function sair(){document.cookie='sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';window.location.href='/admin';}
function msg(id,texto,tipo){const e=document.getElementById(id);e.textContent=texto;e.className='mensagem '+tipo;setTimeout(()=>e.className='mensagem',4000);}

async function cadastrar(){
  const d={nome:document.getElementById('nome').value.trim(),cpf:document.getElementById('cpfCad').value.replace(/\\\\D/g,''),horario_entrada:document.getElementById('hEntrada').value.trim()||'08:00:00',horario_saida_almoco:document.getElementById('hSaidaAlmoco').value.trim()||'12:00:00',horario_retorno_almoco:document.getElementById('hRetornoAlmoco').value.trim()||'13:00:00',horario_saida:document.getElementById('hSaida').value.trim()||'18:00:00'};
  if(!d.nome||!d.cpf){msg('msgCad','Preencha nome e CPF!','erro');return;}
  if(d.cpf.length!==11){msg('msgCad','CPF deve ter 11 dígitos!','erro');return;}
  const r=await fetch('/api/funcionarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  if(r.ok){
    msg('msgCad','✅ Funcionário cadastrado!','sucesso');
    document.getElementById('nome').value='';
    document.getElementById('cpfCad').value='';
    carregarResumo();
  }else{const e=await r.json();msg('msgCad','❌ '+(e.detail||'Erro'),'erro');}
}

async function carregarFuncs(){
  const r=await fetch('/api/funcionarios');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const tb=document.getElementById('tbodyFunc');
  if(d.length===0){tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:#999;padding:20px;">Nenhum cadastrado.</td></tr>';return;}
  tb.innerHTML=d.map(f=>'<tr><td>'+f.id+'</td><td>'+f.nome+'</td><td>'+f.cpf+'</td><td><strong>'+f.horario_entrada+'</strong></td><td>'+f.horario_saida_almoco+'</td><td>'+f.horario_retorno_almoco+'</td><td><strong>'+f.horario_saida+'</strong></td><td><button class="btn-danger btn-small" onclick="excluir('+f.id+')">Excluir</button></td></tr>').join('');
}

async function excluir(id){if(confirm('Tem CERTEZA? Todos os registros serão APAGADOS!')){const r=await fetch('/api/funcionarios/'+id,{method:'DELETE'});if(r.status===401){window.location.href='/admin';return;}carregarFuncs();carregarResumo();}}

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

// ========== NOVAS FUNÇÕES: GERENCIAR ADMINS ==========
async function cadastrarAdmin(){
  const usuario=document.getElementById('novoAdminUser').value.trim();
  const nome=document.getElementById('novoAdminNome').value.trim();
  const senha=document.getElementById('novoAdminSenha').value;
  if(!usuario||!senha){msg('msgAdmin','Preencha usuário e senha!','erro');return;}
  try{
    const r=await fetch('/api/admins',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({usuario:usuario,nome_completo:nome,senha:senha})});
    if(r.status===401){window.location.href='/admin';return;}
    const d=await r.json();
    if(r.ok){
      msg('msgAdmin','✅ Admin criado com sucesso!','sucesso');
      document.getElementById('novoAdminUser').value='';
      document.getElementById('novoAdminNome').value='';
      document.getElementById('novoAdminSenha').value='';
      carregarAdmins();
    }else{msg('msgAdmin','❌ '+(d.detail||'Erro'),'erro');}
  }catch(e){msg('msgAdmin','Erro de conexão!','erro');}
}

async function carregarAdmins(){
  try{
    const r=await fetch('/api/admins');
    if(r.status===401){window.location.href='/admin';return;}
    const admins=await r.json();
    const lista=document.getElementById('listaAdmins');
    
    // Admin Master sempre primeiro
    let html='<div class="admin-item admin-master">'+
      '<div class="admin-info">'+
        '<div class="admin-avatar">👑</div>'+
        '<div class="admin-dados">'+
          '<strong>admin</strong>'+
          '<span>Administrador Master (padrão do sistema)</span>'+
        '</div>'+
      '</div>'+
      '<span style="background:#ff9800;color:white;padding:4px 12px;border-radius:20px;font-size:11px;font-weight:bold;">MASTER</span>'+
    '</div>';
    
    // Admins adicionais
    admins.forEach(function(a){
      const inicial=(a.nome_completo||a.usuario).charAt(0).toUpperCase();
      html+='<div class="admin-item" id="admin-'+a.id+'">'+
        '<div class="admin-info">'+
          '<div class="admin-avatar">'+inicial+'</div>'+
          '<div class="admin-dados">'+
            '<strong>'+a.usuario+'</strong>'+
            '<span>'+(a.nome_completo||'Sem nome')+'</span>'+
          '</div>'+
        '</div>'+
        '<button class="btn-danger btn-small" onclick="excluirAdmin('+a.id+',\\''+a.usuario+'\\')">🗑️ Excluir</button>'+
      '</div>';
    });
    
    if(admins.length===0){
      html+='<div style="text-align:center;padding:30px;color:#999;font-size:14px;">Nenhum admin adicional cadastrado.</div>';
    }
    
    lista.innerHTML=html;
  }catch(e){
    document.getElementById('listaAdmins').innerHTML='<p style="color:#f44336;">Erro ao carregar admins.</p>';
  }
}

async function excluirAdmin(id,usuario){
  if(!confirm('Tem CERTEZA que deseja excluir o admin "'+usuario+'"?\\n\\nEsta ação não pode ser desfeita.'))return;
  try{
    const r=await fetch('/api/admins/'+id,{method:'DELETE'});
    if(r.status===401){window.location.href='/admin';return;}
    if(r.ok){
      alert('✅ Admin excluído com sucesso!');
      carregarAdmins();
    }else{
      const d=await r.json();
      alert('❌ Erro: '+(d.detail||'Erro ao excluir'));
    }
  }catch(e){alert('Erro de conexão!');}
}

// Carregar resumo ao iniciar
setTimeout(carregarResumo,500);
</script>
</body>
</html>"""

# ===================== SERVIDOR HTTP =====================
class ServidorPonto(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        print(f"[{agora_brasilia().strftime('%H:%M:%S')}] {args[0]}")
    
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

        # Rota de Health Check para UptimeRobot
        if caminho == "/health" or caminho == "/healthz":
            try:
                conn = get_db()
                conn.execute("SELECT 1 FROM funcionarios LIMIT 1")
                conn.close()
                responder_json(self, {
                    "status": "ok",
                    "servico": "Sistema de Ponto v3.2",
                    "timestamp": agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),
                    "banco": "conectado"
                })
            except Exception as e:
                responder_json(self, {"status": "erro", "detalhe": str(e)}, status=500)
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
        
        # Rotas que precisam de login admin
        rotas_admin = [
            "/api/funcionarios", "/api/registros", "/api/gerar_qrcode", 
            "/api/pdf/geral", "/api/logout", "/api/acessos", 
            "/api/solicitacoes_pendentes", "/api/admins"
        ]
        
        precisa_login = (
            caminho in rotas_admin or 
            caminho.startswith("/api/funcionarios/") or 
            caminho.startswith("/api/pdf/funcionario/") or 
            caminho.startswith("/api/solicitacoes/") or
            caminho.startswith("/api/admins/")
        )
        
        # Rota para funcionário consultar status da sua solicitação (NÃO precisa de login admin)
        if caminho.startswith("/api/solicitacao_status/"):
            cpf = formatar_cpf(caminho.replace("/api/solicitacao_status/", ""))
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido"}, status=400)
                return
            try:
                conn = get_db()
                solic = conn.execute("""
                    SELECT s.*, f.nome FROM solicitacoes_pendentes s
                    JOIN funcionarios f ON s.funcionario_id = f.id
                    WHERE s.cpf = ? ORDER BY s.id DESC LIMIT 1
                """, (cpf,)).fetchone()
                conn.close()
                
                if solic:
                    responder_json(self, {
                        "encontrada": True,
                        "id": solic["id"],
                        "status": solic["status"],
                        "tipo": solic["tipo"],
                        "tipo_formatado": solic["tipo"].replace("_", " "),
                        "minutos_atraso": solic["minutos_atraso"] or 0,
                        "data_hora": solic["data_hora_solicitacao"],
                        "motivo_negacao": solic["motivo_negacao"] or "",
                        "data_aprovacao": solic["data_hora_aprovacao"] or ""
                    })
                else:
                    responder_json(self, {"encontrada": False})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if precisa_login and not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado. Faça login."}, status=401)
            return
        
        # ===== NOVA ROTA: Listar admins adicionais =====
        if caminho == "/api/admins":
            try:
                conn = get_db()
                admins = conn.execute("SELECT id, usuario, nome_completo, criado_em FROM admins ORDER BY id").fetchall()
                conn.close()
                resultado = [{
                    "id": a["id"],
                    "usuario": a["usuario"],
                    "nome_completo": a["nome_completo"],
                    "criado_em": a["criado_em"]
                } for a in admins]
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/solicitacoes_pendentes":
            try:
                conn = get_db()
                solics = conn.execute("""
                    SELECT s.*, f.nome, f.horario_entrada, f.horario_saida_almoco, 
                           f.horario_retorno_almoco, f.horario_saida
                    FROM solicitacoes_pendentes s
                    JOIN funcionarios f ON s.funcionario_id = f.id
                    WHERE s.status = 'PENDENTE'
                    ORDER BY s.data_hora_solicitacao ASC
                """).fetchall()
                conn.close()
                resultado = []
                for s in solics:
                    dh = datetime.strptime(s["data_hora_solicitacao"], "%Y-%m-%d %H:%M:%S")
                    resultado.append({
                        "id": s["id"],
                        "funcionario_id": s["funcionario_id"],
                        "nome": s["nome"],
                        "cpf": s["cpf"],
                        "data": dh.strftime("%d/%m/%Y"),
                        "hora": dh.strftime("%H:%M:%S"),
                        "tipo": s["tipo"],
                        "tipo_formatado": s["tipo"].replace("_", " "),
                        "atrasado": bool(s["atrasado"]),
                        "minutos_atraso": s["minutos_atraso"] or 0,
                        "justificativa": s["justificativa"] or "",
                        "ip": s["ip_dispositivo"] or "",
                        "horario_entrada": s["horario_entrada"],
                        "horario_saida": s["horario_saida"]
                    })
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
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
                        "cpf": a["cpf"],
                        "nome": a["nome"],
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
                proto = self.headers.get("X-Forwarded-Proto", "http")
                url = f"{proto}://{host}/"
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
            
            login_ok, nome_admin = verificar_credenciais_admin(usuario, senha)
            
            if login_ok:
                token = gerar_sessao()
                sessoes_admin[token] = agora_brasilia() + timedelta(hours=8)
                cookie = f"sessao_admin={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
                tentativas_login.pop(ip_cliente, None)
                print(f"[LOGIN OK] Admin '{nome_admin}' de {ip_cliente}")
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
                    "expira": agora_brasilia() + timedelta(hours=2)
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
                
                agora = agora_brasilia()
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
                tolerancia = 5  # minutos
                
                # NOVA LÓGICA:
                # - Até 5 minutos ANTES do horário: tolerância, registra NORMAL, SEM banco de horas
                # - Horário exato: normal
                # - 1 minuto ou mais DEPOIS: ATRASO = BLOQUEADO (para chegadas: ENTRADA, RETORNO_ALMOCO)
                # - Para saídas (SAIDA_ALMOCO, SAIDA): sair mais de 5 min ANTES = BLOQUEADO
                
                if tipo == "ENTRADA":
                    # Verifica se chegou DEPOIS do horário (qualquer atraso, mesmo 1 min)
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"], tolerancia_minutos=0) else 0
                    if atrasado:
                        minutos = calcular_minutos(hora_str, func["horario_entrada"])
                        msg_just = f"Atraso na ENTRADA. Horário padrão: {func['horario_entrada']}. Qualquer atraso requer aprovação do administrador."
                        info = f"Atraso de {minutos} minuto(s)"
                elif tipo == "SAIDA_ALMOCO":
                    # Verifica se saiu MAIS de 5 minutos ANTES do horário
                    minutos_antes = calcular_minutos(func["horario_saida_almoco"], hora_str)
                    saiu_cedo_demais = (minutos_antes > tolerancia) and not verificar_atraso(hora_str, func["horario_saida_almoco"], tolerancia_minutos=0)
                    if saiu_cedo_demais:
                        atrasado = 1
                        minutos = minutos_antes
                        msg_just = f"Saída para almoço com {minutos_antes} min de antecedência (acima da tolerância de {tolerancia} min). Padrão: {func['horario_saida_almoco']}."
                        info = f"Antecedência de {minutos_antes} min"
                elif tipo == "RETORNO_ALMOCO":
                    # Verifica se chegou DEPOIS do horário (qualquer atraso, mesmo 1 min)
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"], tolerancia_minutos=0) else 0
                    if atrasado:
                        minutos = calcular_minutos(hora_str, func["horario_retorno_almoco"])
                        msg_just = f"Atraso no RETORNO DO ALMOÇO. Horário padrão: {func['horario_retorno_almoco']}. Qualquer atraso requer aprovação do administrador."
                        info = f"Atraso de {minutos} minuto(s)"
                elif tipo == "SAIDA":
                    # Verifica se saiu MAIS de 5 minutos ANTES do horário
                    minutos_antes = calcular_minutos(func["horario_saida"], hora_str)
                    saiu_cedo_demais = (minutos_antes > tolerancia) and not verificar_atraso(hora_str, func["horario_saida"], tolerancia_minutos=0)
                    if saiu_cedo_demais:
                        atrasado = 1
                        minutos = minutos_antes
                        msg_just = f"SAÍDA ANTECIPADA. Horário padrão: {func['horario_saida']}. Sair mais de {tolerancia} min antes requer aprovação do administrador."
                        info = f"Antecipada em {minutos} minuto(s)"
                
                conn.close()
                bloqueado = (atrasado == 1)
                responder_json(self, {
                    "precisa_justificativa": (minutos > 0 and not bloqueado),
                    "bloqueado": bloqueado,
                    "mensagem_justificativa": msg_just,
                    "info_atraso": info,
                    "minutos_atraso": minutos,
                    "atrasado": atrasado
                })
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/solicitar_ponto":
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
                
                agora = agora_brasilia()
                data_str = agora.strftime("%Y-%m-%d")
                data_hora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
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
                    responder_json(self, {"detail": f"⛔ {TIPOS_REGISTRO[tipo]['label']} JÁ registrada hoje!"}, status=400)
                    return
                
                solic_existente = conn.execute("""
                    SELECT id FROM solicitacoes_pendentes 
                    WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora_solicitacao) = ? 
                    AND tipo = ? AND status = 'PENDENTE' LIMIT 1
                """, (func["id"], data_str, tipo)).fetchone()
                
                if solic_existente:
                    conn.close()
                    responder_json(self, {
                        "status": "ja_existe",
                        "solicitacao_id": solic_existente["id"],
                        "mensagem": "Já existe uma solicitação pendente de aprovação."
                    })
                    return
                
                atrasado = 0
                minutos_atraso = 0
                tolerancia = 5
                if tipo == "ENTRADA":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"], tolerancia_minutos=0) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_entrada"])
                elif tipo == "SAIDA_ALMOCO":
                    minutos_antes = calcular_minutos(func["horario_saida_almoco"], hora_str)
                    saiu_cedo_demais = (minutos_antes > tolerancia) and not verificar_atraso(hora_str, func["horario_saida_almoco"], tolerancia_minutos=0)
                    if saiu_cedo_demais:
                        atrasado = 1
                        minutos_atraso = minutos_antes
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"], tolerancia_minutos=0) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_retorno_almoco"])
                elif tipo == "SAIDA":
                    minutos_antes = calcular_minutos(func["horario_saida"], hora_str)
                    saiu_cedo_demais = (minutos_antes > tolerancia) and not verificar_atraso(hora_str, func["horario_saida"], tolerancia_minutos=0)
                    if saiu_cedo_demais:
                        atrasado = 1
                        minutos_atraso = minutos_antes
                
                conn.execute("""
                    INSERT INTO solicitacoes_pendentes 
                    (funcionario_id, cpf, data_hora_solicitacao, tipo, atrasado, minutos_atraso, 
                     justificativa, status, ip_dispositivo, user_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDENTE', ?, ?)
                """, (func["id"], cpf, data_hora_str, tipo, atrasado, minutos_atraso, 
                      justificativa, ip_cliente, user_agent))
                conn.commit()
                
                solic_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                conn.close()
                
                tipo_info = TIPOS_REGISTRO[tipo]
                print(f"[SOLICITAÇÃO #{solic_id}] {func['nome']} | {tipo} | Atraso: {minutos_atraso}min | IP:{ip_cliente}")
                
                responder_json(self, {
                    "status": "solicitado",
                    "solicitacao_id": solic_id,
                    "mensagem": f"Solicitação enviada para aprovação do administrador."
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
                
                agora = agora_brasilia()
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
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"], tolerancia_minutos=5) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_entrada"])
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"], tolerancia_minutos=5) else 0
                    if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_retorno_almoco"])
                elif tipo == "SAIDA":
                    atrasado = 1 if verificar_atraso(func["horario_saida"], hora_str, tolerancia_minutos=5) else 0
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
                msg = f"{tipo_info['icone']} {tipo_info['label']} registrada!\\n"
                msg += f"👤 {func['nome']}\\n📅 {agora.strftime('%d/%m/%Y')}\\n⏰ {hora_str}"
                if atrasado: msg += f"\\n⚠️ Atraso: {minutos_atraso} min"
                if minutos_banco > 0: msg += f"\\n⏱️ Banco de horas: +{minutos_banco} min"
                if justificativa: msg += f"\\n📝 Justificativa registrada"
                
                print(f"[PONTO] {func['nome']} | {tipo} | {hora_str} | IP:{ip_cliente}")
                responder_json(self, {"mensagem": msg})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        # ===== ROTAS QUE PRECISAM DE LOGIN ADMIN =====
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401)
            return
        
        # ===== NOVA ROTA: Criar admin =====
        if caminho == "/api/admins":
            usuario = sanitizar_texto(dados.get("usuario", ""), 50)
            nome_completo = sanitizar_texto(dados.get("nome_completo", ""), 100)
            senha = dados.get("senha", "")
            
            if not usuario or not senha:
                responder_json(self, {"detail": "Preencha usuário e senha!"}, status=400)
                return
            
            if usuario.lower() == ADMIN_USUARIO.lower():
                responder_json(self, {"detail": "Este usuário é o admin master e já existe!"}, status=400)
                return
            
            try:
                conn = get_db()
                existe = conn.execute("SELECT id FROM admins WHERE usuario = ?", (usuario,)).fetchone()
                if existe:
                    conn.close()
                    responder_json(self, {"detail": "Usuário já existe!"}, status=400)
                    return
                
                criado_em = agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("""
                    INSERT INTO admins (usuario, senha, nome_completo, criado_em)
                    VALUES (?, ?, ?, ?)
                """, (usuario, hash_senha(senha), nome_completo, criado_em))
                conn.commit()
                
                novo_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                conn.close()
                
                print(f"[ADMIN CRIADO] Usuário: {usuario} | Nome: {nome_completo}")
                responder_json(self, {"status": "ok", "id": novo_id, "usuario": usuario})
            except Exception as e:
                responder_json(self, {"detail": f"Erro BD: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/solicitacoes/") and caminho.endswith("/aprovar"):
            try:
                solic_id = int(caminho.replace("/api/solicitacoes/", "").replace("/aprovar", ""))
                conn = get_db()
                solic = conn.execute("SELECT * FROM solicitacoes_pendentes WHERE id = ?", (solic_id,)).fetchone()
                
                if not solic:
                    conn.close()
                    responder_json(self, {"detail": "Solicitação não encontrada"}, status=404)
                    return
                
                if solic["status"] != "PENDENTE":
                    conn.close()
                    responder_json(self, {"detail": f"Solicitação já foi {solic['status']}"}, status=400)
                    return
                
                func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (solic["funcionario_id"],)).fetchone()
                
                agora = agora_brasilia()
                data_hora_aprovacao = agora.strftime("%Y-%m-%d %H:%M:%S")
                
                data_hora_registro = solic["data_hora_solicitacao"]
                hora_str = data_hora_registro.split(" ")[1] if " " in data_hora_registro else data_hora_registro
                
                minutos_banco = calcular_banco_horas(solic["tipo"], hora_str, func)
                
                conn.execute("""
                    INSERT INTO registros_ponto 
                    (funcionario_id, data_hora, tipo, atrasado, minutos_atraso, minutos_banco_horas, 
                     justificativa, ip_dispositivo, user_agent, horario_acesso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (solic["funcionario_id"], data_hora_registro, solic["tipo"], 
                      solic["atrasado"], solic["minutos_atraso"], minutos_banco,
                      solic["justificativa"], solic["ip_dispositivo"], 
                      solic["user_agent"], data_hora_aprovacao))
                
                conn.execute("""
                    UPDATE solicitacoes_pendentes 
                    SET status = 'APROVADA', data_hora_aprovacao = ?, admin_aprovador = ?
                    WHERE id = ?
                """, (data_hora_aprovacao, ADMIN_USUARIO, solic_id))
                
                conn.commit()
                conn.close()
                
                print(f"[APROVADA #{solic_id}] {func['nome']} | {solic['tipo']} | Atraso: {solic['minutos_atraso']}min")
                responder_json(self, {"status": "ok", "mensagem": f"Solicitação aprovada! Ponto registrado para {func['nome']}."})
            except ValueError:
                responder_json(self, {"detail": "ID inválido"}, status=400)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/solicitacoes/") and caminho.endswith("/negar"):
            try:
                solic_id = int(caminho.replace("/api/solicitacoes/", "").replace("/negar", ""))
                motivo = sanitizar_texto(dados.get("motivo", ""), 500)
                
                conn = get_db()
                solic = conn.execute("SELECT * FROM solicitacoes_pendentes WHERE id = ?", (solic_id,)).fetchone()
                
                if not solic:
                    conn.close()
                    responder_json(self, {"detail": "Solicitação não encontrada"}, status=404)
                    return
                
                if solic["status"] != "PENDENTE":
                    conn.close()
                    responder_json(self, {"detail": f"Solicitação já foi {solic['status']}"}, status=400)
                    return
                
                func = conn.execute("SELECT nome FROM funcionarios WHERE id = ?", (solic["funcionario_id"],)).fetchone()
                agora = agora_brasilia()
                data_hora_aprovacao = agora.strftime("%Y-%m-%d %H:%M:%S")
                
                conn.execute("""
                    UPDATE solicitacoes_pendentes 
                    SET status = 'NEGADA', data_hora_aprovacao = ?, admin_aprovador = ?, motivo_negacao = ?
                    WHERE id = ?
                """, (data_hora_aprovacao, ADMIN_USUARIO, motivo, solic_id))
                
                conn.commit()
                conn.close()
                
                print(f"[NEGADA #{solic_id}] {func['nome']} | Motivo: {motivo[:50]}")
                responder_json(self, {"status": "ok", "mensagem": "Solicitação negada."})
            except ValueError:
                responder_json(self, {"detail": "ID inválido"}, status=400)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
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
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401)
            return
        
        url = urlparse(self.path)
        caminho = url.path
        
        # ===== NOVA ROTA: Excluir admin =====
        if caminho.startswith("/api/admins/"):
            try:
                admin_id = int(caminho.replace("/api/admins/", ""))
                conn = get_db()
                admin = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
                
                if not admin:
                    conn.close()
                    responder_json(self, {"detail": "Admin não encontrado"}, status=404)
                    return
                
                conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
                conn.commit()
                conn.close()
                
                print(f"[ADMIN EXCLUÍDO] ID: {admin_id} | Usuário: {admin['usuario']}")
                responder_json(self, {"status": "ok"})
            except ValueError:
                responder_json(self, {"detail": "ID inválido"}, status=400)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
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
    print("   🚀 SISTEMA DE PONTO v3.2 - FUNCIONANDO!")
    print("=" * 65)
    print(f"📱 Página do funcionário:  http://localhost:{PORTA}")
    print(f"👤 Painel Funcionário:     http://localhost:{PORTA}/funcionario")
    print(f"🔐 Login Admin:            http://localhost:{PORTA}/admin")
    print(f"👤 Usuário: {ADMIN_USUARIO}   |   Senha: {ADMIN_SENHA}")
    print("=" * 65)
    print("✨ NOVAS FUNCIONALIDADES v3.2:")
    print("   👥 Sistema de múltiplos administradores")
    print("   📊 Painel de resumo visual da equipe")
    print("   🎨 Cards coloridos com horários dos funcionários")
    print("   🔧 Gerenciamento completo de admins (CRUD)")
    print("   ✅ Correção total das abas do painel admin")
    print("=" * 65)
    print("📝 4 opções de registro:")
    print("   ✅ ENTRADA  |  🍽️ SAÍDA ALMOÇO  |  ↩️ RETORNO ALMOÇO  |  🚪 SAÍDA")
    print("=" * 65)
    print(f"🌐 Acesso na rede WI-FI: http://SEU_IP:{PORTA}")
    print("   (descubra seu IP com o comando: ipconfig / ifconfig)")
    print("=" * 65)
    print("\nServidor rodando... Aperte Ctrl+C para parar.\n")
    
    try:
        servidor = HTTPServer(("0.0.0.0", PORTA), ServidorPonto)
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor parado.")
