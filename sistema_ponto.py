import sqlite3
import json
import os
import io
import hashlib
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
# ===================== CONFIGURACAOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = 8000
ADMIN_USUARIO = "admin"
ADMIN_SENHA = "3223ronte"
sessoes_admin = {}
os.makedirs("static", exist_ok=True)
# ===================== CRIAR LOGO PADRAO =====================
def criar_logo_padrao():
    """Cria uma logo padrao automaticamente se nao existir."""
    caminho_logo = os.path.join("static", "logo.png")
    if os.path.exists(caminho_logo):
        tamanho = os.path.getsize(caminho_logo)
        print(f"[LOGO] Encontrada: {caminho_logo} ({tamanho} bytes)")
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
        print("[LOGO] Dica: Substitua static/logo.png pela logo da sua clinica!")
        return True
    except ImportError:
        print("[LOGO] Pillow nao instalado. Instale: pip install pillow")
        return False
    except Exception as e:
        print(f"[LOGO] Erro: {e}")
        return False
# ===================== BANCO DE DADOS =====================
DB_NOME = "ponto.db"
def get_db():
    conn = sqlite3.connect(DB_NOME)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS funcionarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cpf TEXT UNIQUE NOT NULL,
            horario_entrada TEXT DEFAULT '08:00:00',
            horario_saida_almoco TEXT DEFAULT '12:00:00',
            horario_retorno_almoco TEXT DEFAULT '13:00:00',
            horario_saida TEXT DEFAULT '18:00:00'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registros_ponto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            funcionario_id INTEGER NOT NULL,
            data_hora TEXT NOT NULL,
            tipo TEXT NOT NULL,
            atrasado INTEGER DEFAULT 0,
            minutos_atraso INTEGER DEFAULT 0,
            minutos_banco_horas INTEGER DEFAULT 0,
            justificativa TEXT DEFAULT '',
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
        )
    """)
    # Migracoes para bancos antigos
    migracoes = [
        ("minutos_atraso", "INTEGER DEFAULT 0"),
        ("minutos_banco_horas", "INTEGER DEFAULT 0"),
        ("justificativa", "TEXT DEFAULT ''")
    ]
    for coluna, tipo in migracoes:
        try:
            conn.execute(f"ALTER TABLE registros_ponto ADD COLUMN {coluna} {tipo}")
            print(f"[MIGRACAO] Coluna {coluna} adicionada")
        except:
            pass
    conn.commit()
    conn.close()
init_db()
criar_logo_padrao()
# ===================== FUNCOES AUXILIARES =====================
def formatar_cpf(cpf):
    return ''.join(filter(str.isdigit, str(cpf)))
def verificar_atraso(hora_registro, horario_padrao):
    """Verifica se hora_registro > horario_padrao (atrasado)."""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0]) * 3600 + int(h_r[1]) * 60 + (int(h_r[2]) if len(h_r) > 2 else 0)
        t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
        return t_r > t_p
    except:
        return False
def calcular_minutos(hora1, hora2):
    """Calcula diferenca em minutos entre duas horas (absoluta)."""
    try:
        h1 = hora1.split(":")
        h2 = hora2.split(":")
        t1 = int(h1[0]) * 3600 + int(h1[1]) * 60 + (int(h1[2]) if len(h1) > 2 else 0)
        t2 = int(h2[0]) * 3600 + int(h2[1]) * 60 + (int(h2[2]) if len(h2) > 2 else 0)
        return abs(t1 - t2) // 60
    except:
        return 0
def calcular_banco_horas(tipo, hora_registro, func):
    """Calcula minutos de banco de horas (horas extras trabalhadas).
    Retorna minutos positivos se houve banco de horas."""
    try:
        h_r = hora_registro.split(":")
        t_r = int(h_r[0]) * 3600 + int(h_r[1]) * 60 + (int(h_r[2]) if len(h_r) > 2 else 0)
        
        if tipo == "ENTRADA":
            h_p = func["horario_entrada"].split(":")
            t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
            if t_r < t_p:
                return (t_p - t_r) // 60
        
        elif tipo == "SAIDA_ALMOCO":
            h_p = func["horario_saida_almoco"].split(":")
            t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
            if t_r > t_p:
                return (t_r - t_p) // 60
        
        elif tipo == "RETORNO_ALMOCO":
            h_p = func["horario_retorno_almoco"].split(":")
            t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
            if t_r < t_p:
                return (t_p - t_r) // 60
        
        elif tipo == "SAIDA":
            h_p = func["horario_saida"].split(":")
            t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
            if t_r > t_p:
                return (t_r - t_p) // 60
        
        return 0
    except:
        return 0
def obter_ultimo_registro(funcionario_id, data_str):
    conn = get_db()
    ultimo = conn.execute("""
        SELECT * FROM registros_ponto 
        WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ?
        ORDER BY data_hora DESC LIMIT 1
    """, (funcionario_id, data_str)).fetchone()
    conn.close()
    return ultimo
def verificar_sequencia_valida(ultimo_tipo, novo_tipo):
    """Verifica ordem correta: não permite duplicar o mesmo tipo no mesmo dia."""
    sequencia = {
        None: ["ENTRADA"],
        "ENTRADA": ["SAIDA_ALMOCO", "SAIDA"],
        "SAIDA_ALMOCO": ["RETORNO_ALMOCO"],
        "RETORNO_ALMOCO": ["SAIDA"],
        "SAIDA": ["ENTRADA"]
    }
    
    proximos_permitidos = sequencia.get(ultimo_tipo, ["ENTRADA"])
    
    if novo_tipo in proximos_permitidos:
        return True, ""
    
    mensagens = {
        "ENTRADA": "Você já registrou ENTRADA hoje. Próximo: SAÍDA ALMOÇO ou SAÍDA.",
        "SAIDA_ALMOCO": "Você já registrou SAÍDA ALMOÇO. Próximo: RETORNO ALMOÇO.",
        "RETORNO_ALMOCO": "Você já registrou RETORNO ALMOÇO. Próximo: SAÍDA.",
        "SAIDA": "Você já registrou SAÍDA hoje. Nova ENTRADA só amanhã."
    }
    
    return False, mensagens.get(ultimo_tipo, "Registro não permitido agora.")
def gerar_sessao():
    return hashlib.sha256(os.urandom(64)).hexdigest()
def limpar_sessoes_expiradas():
    agora = datetime.now()
    expiradas = [token for token, expira in sessoes_admin.items() if expira <= agora]
    for token in expiradas:
        del sessoes_admin[token]
def verificar_login(handler):
    limpar_sessoes_expiradas()
    try:
        cookies = handler.headers.get("Cookie", "")
        for cookie in cookies.split(";"):
            cookie = cookie.strip()
            if cookie.startswith("sessao_admin="):
                token = cookie.replace("sessao_admin=", "").strip()
                expira = sessoes_admin.get(token)
                if expira and expira > datetime.now():
                    return True
    except:
        pass
    return False
def responder_json(handler, dados, status=200, cookies_extra=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    if cookies_extra:
        for cookie in cookies_extra:
            handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(json.dumps(dados, ensure_ascii=False).encode("utf-8"))
def responder_html(handler, conteudo, status=200, cookies_extra=None):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    if cookies_extra:
        for cookie in cookies_extra:
            handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(conteudo.encode("utf-8"))
# ===================== TIPOS DE REGISTRO =====================
TIPOS_REGISTRO = {
    "ENTRADA": {"label": "ENTRADA", "cor": "#4CAF50", "icone": "✅"},
    "SAIDA_ALMOCO": {"label": "SAÍDA ALMOÇO", "cor": "#ff9800", "icone": "🍽️"},
    "RETORNO_ALMOCO": {"label": "RETORNO ALMOÇO", "cor": "#2196F3", "icone": "↩️"},
    "SAIDA": {"label": "SAÍDA", "cor": "#f44336", "icone": "🚪"}
}
# ===================== HTML - LOGIN ADMIN =====================
HTML_LOGIN = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login Admin</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: Arial, sans-serif; }
body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
.login-box { background: white; padding: 40px; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); width: 100%; max-width: 380px; text-align: center; }
.logo-container { display: flex; justify-content: center; margin-bottom: 15px; }
.logo { max-width: 100px; max-height: 100px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
.logo-fallback { width: 100px; height: 100px; border-radius: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; color: white; font-size: 36px; font-weight: bold; }
.login-box h1 { color: #333; margin-bottom: 10px; font-size: 24px; }
.login-box p.sub { color: #888; margin-bottom: 30px; text-align: center; font-size: 14px; }
.login-box input { width: 100%; padding: 14px; margin: 8px 0; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 15px; transition: border 0.2s; }
.login-box input:focus { border-color: #667eea; outline: none; }
.login-box button { width: 100%; padding: 14px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 15px; transition: opacity 0.2s; }
.login-box button:hover { opacity: 0.9; }
.mensagem { padding: 12px; border-radius: 8px; margin-top: 15px; text-align: center; font-size: 14px; display: none; }
.erro { background: #f8d7da; color: #721c24; display: block; }
.voltar { display: block; text-align: center; margin-top: 20px; color: #667eea; text-decoration: none; font-size: 14px; }
.voltar:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="login-box">
<div class="logo-container">
<img src="/static/logo.png?t=TIMESTAMP" alt="Logo" class="logo" onerror="this.outerHTML='<div class=\'logo-fallback\'>🏥</div>'">
</div>
<h1>🔐 Login Admin</h1>
<p class="sub">Acesso ao painel administrativo</p>
<input type="text" id="usuario" placeholder="Usuário" autocomplete="username">
<input type="password" id="senha" placeholder="Senha" autocomplete="current-password">
<button onclick="logar()">ENTRAR</button>
<div class="mensagem" id="mensagem"></div>
<a href="/" class="voltar">← Voltar para o registro de ponto</a>
</div>
<script>
document.querySelectorAll('img[src*="logo.png"]').forEach(function(img) {
    img.src = img.src.replace('TIMESTAMP', Date.now());
});
document.getElementById('senha').addEventListener('keypress', function(e) { if(e.key === 'Enter') logar(); });
document.getElementById('usuario').addEventListener('keypress', function(e) { if(e.key === 'Enter') document.getElementById('senha').focus(); });
document.getElementById('usuario').focus();
async function logar() {
    const usuario = document.getElementById('usuario').value.trim();
    const senha = document.getElementById('senha').value;
    const msg = document.getElementById('mensagem');
    if(!usuario || !senha) { msg.textContent = 'Preencha usuário e senha!'; msg.className = 'mensagem erro'; return; }
    try {
        const res = await fetch('/api/login', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({usuario: usuario, senha: senha})
        });
        if(res.ok) { window.location.href = '/admin'; }
        else { const err = await res.json(); msg.textContent = err.detail || 'Erro'; msg.className = 'mensagem erro'; }
    } catch(e) { msg.textContent = 'Erro de conexão!'; msg.className = 'mensagem erro'; }
}
</script>
</body>
</html>""".replace('TIMESTAMP', str(int(datetime.now().timestamp())))
# ===================== HTML - PAGINA PRINCIPAL (PONTO) =====================
HTML_PONTO = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Registro de Ponto</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: Arial, sans-serif; }
body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 15px; }
.container { background: white; padding: 30px; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); width: 100%; max-width: 420px; text-align: center; }
.logo-container { display: flex; justify-content: center; margin-bottom: 15px; }
.logo { max-width: 140px; max-height: 140px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
.logo-fallback { width: 120px; height: 120px; border-radius: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; color: white; font-size: 48px; }
.container h1 { color: #333; font-size: 26px; margin-bottom: 5px; }
.subtitulo { color: #888; font-size: 14px; margin-bottom: 20px; }
.data-hora { background: #f0f4ff; color: #667eea; padding: 12px; border-radius: 8px; font-weight: bold; font-size: 14px; margin-bottom: 20px; }
.container input { width: 100%; padding: 14px; margin: 8px 0; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 16px; text-align: center; transition: border 0.2s; }
.container input:focus { border-color: #667eea; outline: none; }
.botoes { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 20px; }
.botoes button { padding: 16px 8px; border: none; border-radius: 10px; color: white; font-weight: bold; font-size: 13px; cursor: pointer; transition: transform 0.1s, opacity 0.2s; }
.botoes button:hover { opacity: 0.9; }
.botoes button:active { transform: scale(0.97); }
.btn-entrada { background: linear-gradient(135deg, #4CAF50, #45a049); }
.btn-almoco { background: linear-gradient(135deg, #ff9800, #f57c00); }
.btn-retorno { background: linear-gradient(135deg, #2196F3, #1976D2); }
.btn-saida { background: linear-gradient(135deg, #f44336, #d32f2f); }
.icone-btn { font-size: 18px; display: block; margin-bottom: 4px; }
.info-func { margin-top: 15px; padding: 12px; border-radius: 8px; font-size: 14px; font-weight: bold; display: none; }
.mensagem { padding: 14px; border-radius: 8px; margin-top: 18px; font-size: 14px; font-weight: bold; white-space: pre-line; line-height: 1.5; display: none; }
.sucesso { background: #d4edda; color: #155724; display: block; }
.erro { background: #f8d7da; color: #721c24; display: block; }
.banco-horas { background: #d1ecf1; color: #0c5460; display: block; }
.justificativa-aceita { background: #fff3cd; color: #856404; display: block; }
.admin-link { margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }
.admin-link a { color: #888; font-size: 12px; text-decoration: none; }
.admin-link a:hover { color: #667eea; text-decoration: underline; }
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.6); z-index: 1000; align-items: center; justify-content: center; padding: 20px; }
.modal-overlay.ativo { display: flex; }
.modal-box { background: white; border-radius: 16px; padding: 30px; width: 100%; max-width: 400px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); }
.modal-box h2 { color: #f44336; font-size: 20px; margin-bottom: 10px; }
.modal-box p { color: #555; font-size: 14px; margin-bottom: 15px; line-height: 1.5; }
.modal-box .info-atraso { background: #fff3cd; padding: 12px; border-radius: 8px; margin-bottom: 15px; font-weight: bold; color: #856404; font-size: 15px; }
.modal-box textarea { width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 14px; resize: vertical; min-height: 80px; font-family: Arial, sans-serif; }
.modal-box textarea:focus { border-color: #667eea; outline: none; }
.modal-botoes { display: flex; gap: 10px; margin-top: 15px; }
.modal-botoes button { flex: 1; padding: 12px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; font-size: 14px; }
.btn-cancelar { background: #e0e0e0; color: #333; }
.btn-confirmar { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
</style>
</head>
<body>
<div class="container">
<div class="logo-container">
<img src="/static/logo.png?t=TS_LOGO" alt="Logo Clínica" class="logo" onerror="this.outerHTML='<div class=\'logo-fallback\'>🏥</div>'">
</div>
<h1>Registro de Ponto</h1>
<p class="subtitulo">Clínica - Controle de Funcionários</p>
<div class="data-hora" id="dataHora">Carregando...</div>
<input type="text" id="cpf" placeholder="Digite seu CPF (apenas números)" maxlength="11" inputmode="numeric">
<div class="info-func" id="infoFunc"></div>
<div class="botoes">
<button class="btn-entrada" onclick="registrar('ENTRADA')"><span class="icone-btn">✅</span>ENTRADA</button>
<button class="btn-almoco" onclick="registrar('SAIDA_ALMOCO')"><span class="icone-btn">🍽️</span>SAÍDA ALMOÇO</button>
<button class="btn-retorno" onclick="registrar('RETORNO_ALMOCO')"><span class="icone-btn">↩️</span>RETORNO ALMOÇO</button>
<button class="btn-saida" onclick="registrar('SAIDA')"><span class="icone-btn">🚪</span>SAÍDA</button>
</div>
<div class="mensagem" id="mensagem"></div>
<div class="admin-link"><a href="/admin">🔐 Acesso Administrador</a></div>
</div>
<div class="modal-overlay" id="modalJustificativa">
<div class="modal-box">
<h2>⚠️ Atenção!</h2>
<p id="modalTexto"></p>
<div class="info-atraso" id="modalInfoAtraso"></div>
<p style="font-weight:bold; color:#333; margin-bottom:8px;">Informe a justificativa:</p>
<textarea id="justificativa" placeholder="Descreva o motivo..."></textarea>
<div class="modal-botoes">
<button class="btn-cancelar" onclick="fecharModal()">Cancelar</button>
<button class="btn-confirmar" onclick="confirmarComJustificativa()">Confirmar Registro</button>
</div>
</div>
</div>
<script>
const QR_SEGREDO = "CLINICA_PONTO_2024";
let registroPendente = null;
document.querySelectorAll('img[src*="logo.png"]').forEach(function(img) {
    img.src = img.src.replace('TS_LOGO', Date.now());
});
function atualizarDataHora() {
    const agora = new Date();
    const opcoes = { weekday: 'long', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' };
    document.getElementById('dataHora').textContent = agora.toLocaleDateString('pt-BR', opcoes);
}
setInterval(atualizarDataHora, 1000);
atualizarDataHora();
document.getElementById('cpf').addEventListener('input', function() { this.value = this.value.replace(/\\D/g, ''); });
document.getElementById('cpf').addEventListener('blur', async function() {
    const cpf = this.value.replace(/\\D/g, '');
    const info = document.getElementById('infoFunc');
    if (cpf.length === 11) {
        try {
            const res = await fetch('/api/buscar/' + cpf);
            const dados = await res.json();
            if (dados.encontrado) {
                info.textContent = '👤 ' + dados.nome;
                info.style.background = '#e8f5e9'; info.style.color = '#2e7d32'; info.style.display = 'block';
            } else {
                info.textContent = '⚠️ CPF NÃO cadastrado!';
                info.style.background = '#fff3cd'; info.style.color = '#856404'; info.style.display = 'block';
            }
        } catch(e) { info.style.display = 'none'; }
    } else { info.style.display = 'none'; }
});
async function registrar(tipo) {
    const cpf = document.getElementById('cpf').value.replace(/\\D/g, '');
    if (!cpf || cpf.length !== 11) { mostrarMensagem('Digite um CPF válido com 11 números!', 'erro'); return; }
    
    try {
        const res = await fetch('/api/verificar_ponto', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({cpf: cpf, tipo: tipo, qr_code: QR_SEGREDO})
        });
        const dados = await res.json();
        
        if (!res.ok) { mostrarMensagem(dados.detail || 'Erro', 'erro'); return; }
        
        if (dados.precisa_justificativa) {
            registroPendente = { cpf: cpf, tipo: tipo };
            document.getElementById('modalTexto').textContent = dados.mensagem_justificativa;
            document.getElementById('modalInfoAtraso').textContent = '⏱️ ' + dados.info_atraso;
            document.getElementById('justificativa').value = '';
            document.getElementById('modalJustificativa').classList.add('ativo');
            document.getElementById('justificativa').focus();
        } else {
            await executarRegistro(cpf, tipo, '');
        }
    } catch(e) { mostrarMensagem('Erro de conexão!', 'erro'); }
}
function fecharModal() {
    document.getElementById('modalJustificativa').classList.remove('ativo');
    registroPendente = null;
}
async function confirmarComJustificativa() {
    if (!registroPendente) return;
    const justificativa = document.getElementById('justificativa').value.trim();
    if (!justificativa) { alert('Informe a justificativa!'); document.getElementById('justificativa').focus(); return; }
    fecharModal();
    await executarRegistro(registroPendente.cpf, registroPendente.tipo, justificativa);
}
async function executarRegistro(cpf, tipo, justificativa) {
    try {
        const res = await fetch('/api/bater_ponto', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ cpf: cpf, tipo: tipo, qr_code: QR_SEGREDO, justificativa: justificativa })
        });
        const dados = await res.json();
        
        if (res.ok) {
            let tipoMsg = 'sucesso';
            if (dados.mensagem.includes('Banco')) tipoMsg = 'banco-horas';
            if (dados.mensagem.includes('Justificativa aceita')) tipoMsg = 'justificativa-aceita';
            mostrarMensagem(dados.mensagem, tipoMsg);
            document.getElementById('cpf').value = '';
            document.getElementById('infoFunc').style.display = 'none';
        } else {
            mostrarMensagem(dados.detail || 'Erro', 'erro');
        }
    } catch(e) { mostrarMensagem('Erro de conexão!', 'erro'); }
}
function mostrarMensagem(texto, tipo) {
    const msg = document.getElementById('mensagem');
    msg.textContent = texto;
    msg.className = 'mensagem ' + tipo;
    setTimeout(function() { msg.className = 'mensagem'; }, 7000);
}
document.getElementById('modalJustificativa').addEventListener('click', function(e) {
    if (e.target === this) fecharModal();
});
</script>
</body>
</html>""".replace('TS_LOGO', str(int(datetime.now().timestamp())))
# ===================== HTML - PAINEL ADMIN =====================
HTML_ADMIN = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Painel Admin</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: Arial, sans-serif; }
body { background: #f5f5f5; }
.header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 15px; text-align: center; position: relative; }
.header-content { display: flex; align-items: center; justify-content: center; gap: 15px; }
.logo-header { width: 48px; height: 48px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
.logo-header-fallback { width: 48px; height: 48px; border-radius: 10px; background: rgba(255,255,255,0.2); display: flex; align-items: center; justify-content: center; font-size: 24px; }
.header h1 { font-size: 22px; }
.logout { position: absolute; right: 20px; top: 50%; transform: translateY(-50%); background: rgba(255,255,255,0.2); padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; border: 1px solid rgba(255,255,255,0.3); }
.logout:hover { background: rgba(255,255,255,0.3); }
.container { max-width: 1200px; margin: 20px auto; padding: 0 20px; }
.tabs { display: flex; gap: 5px; margin-bottom: 20px; flex-wrap: wrap; }
.tab { padding: 12px 18px; background: #ddd; border: none; border-radius: 8px 8px 0 0; cursor: pointer; font-weight: bold; font-size: 13px; }
.tab.ativo { background: white; color: #667eea; }
.painel { background: white; border-radius: 0 12px 12px 12px; padding: 25px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); display: none; }
.painel.ativo { display: block; }
h2 { color: #333; margin-bottom: 20px; font-size: 20px; }
input, select, textarea { width: 100%; padding: 12px; margin: 8px 0; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 14px; font-family: Arial, sans-serif; }
button { padding: 12px 24px; background: #667eea; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 14px; margin: 5px 5px 5px 0; }
button:hover { opacity: 0.9; }
.btn-success { background: #4CAF50; }
.btn-danger { background: #f44336; }
.btn-warning { background: #ff9800; }
.btn-small { padding: 6px 12px; font-size: 12px; }
table { width: 100%; border-collapse: collapse; margin-top: 15px; display: block; overflow-x: auto; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; white-space: nowrap; }
th { background: #f8f9fa; font-weight: bold; color: #555; }
.atrasado { color: #f44336; font-weight: bold; }
.banco-horas { color: #0c5460; font-weight: bold; }
.tipo-entrada { color: #4CAF50; font-weight: bold; }
.tipo-saida-almoco { color: #ff9800; font-weight: bold; }
.tipo-retorno-almoco { color: #2196F3; font-weight: bold; }
.tipo-saida { color: #f44336; font-weight: bold; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 700px) { .grid-2 { grid-template-columns: 1fr; } }
.mensagem { padding: 12px; border-radius: 8px; margin: 10px 0; display: none; }
.sucesso { background: #d4edda; color: #155724; display: block; }
.erro { background: #f8d7da; color: #721c24; display: block; }
.qr-info { background: #e3f2fd; padding: 15px; border-radius: 8px; margin: 15px 0; word-break: break-all; }
.card { background: #fafafa; padding: 15px; border-radius: 8px; margin: 10px 0; border-left: 4px solid #667eea; }
.horarios-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.info-box { background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px; border-radius: 4px; margin: 15px 0; font-size: 13px; color: #856404; }
.justificativa-cell { max-width: 180px; font-size: 11px; color: #666; font-style: italic; overflow: hidden; text-overflow: ellipsis; }
.minutos-cell { color: #f44336; font-weight: bold; }
.banco-cell { color: #0c5460; font-weight: bold; }
.fim-semana { background-color: #fffde7 !important; }
.fim-semana td { color: #e65100; }
.filtros { display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap; align-items: center; }
.filtros input, .filtros select { margin: 0; width: auto; min-width: 150px; }
label { font-size: 13px; color: #555; font-weight: bold; display: block; margin-top: 8px; }
</style>
</head>
<body>
<div class="header">
<div class="header-content">
<img src="/static/logo.png?t=TS_ADMIN" alt="Logo" class="logo-header" onerror="this.outerHTML='<div class=\'logo-header-fallback\'>🏥</div>'">
<h1>⚙️ Painel Administrativo</h1>
</div>
<div class="logout" onclick="sair()">🚪 Sair</div>
</div>
<div class="container">
<div class="tabs">
<button class="tab ativo" onclick="abrirAba('cadastro', this)">👤 Cadastrar</button>
<button class="tab" onclick="abrirAba('funcionarios', this)">📋 Funcionários</button>
<button class="tab" onclick="abrirAba('registros', this)">📊 Registros</button>
<button class="tab" onclick="abrirAba('relatorios', this)">📄 Relatórios PDF</button>
<button class="tab" onclick="abrirAba('qrcode', this)">📱 QR Code</button>
<button class="tab" onclick="abrirAba('config', this)">🔧 Configurações</button>
</div>
<div id="cadastro" class="painel ativo">
<h2>Cadastrar Novo Funcionário</h2>
<div class="mensagem" id="msgCadastro"></div>
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
<button onclick="carregarFuncionarios()">🔄 Atualizar Lista</button>
<table><thead><tr><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Saída Almoço</th><th>Retorno</th><th>Saída</th><th>Ação</th></tr></thead><tbody id="tbodyFunc"></tbody></table>
</div>
<div id="registros" class="painel">
<h2>Todos os Registros de Ponto</h2>
<div class="filtros">
<input type="text" id="filtroNome" placeholder="Filtrar por nome..." oninput="carregarRegistros()">
<select id="filtroTipo" onchange="carregarRegistros()">
<option value="">Todos os tipos</option>
<option value="ENTRADA">ENTRADA</option>
<option value="SAIDA_ALMOCO">SAÍDA ALMOÇO</option>
<option value="RETORNO_ALMOCO">RETORNO ALMOÇO</option>
<option value="SAIDA">SAÍDA</option>
</select>
<button onclick="carregarRegistros()">🔄 Atualizar</button>
</div>
<table><thead><tr><th>Funcionário</th><th>CPF</th><th>Data</th><th>Hora</th><th>Tipo</th><th>Atrasado</th><th>Min. Atraso</th><th>Banco Horas</th><th>Justificativa</th></tr></thead><tbody id="tbodyReg"></tbody></table>
</div>
<div id="relatorios" class="painel">
<h2>Gerar Relatórios em PDF</h2>
<div class="grid-2">
<div class="card">
<h3 style="margin:10px 0; color:#555;">📄 Relatório Geral do Mês</h3>
<label>Mês/Ano:</label>
<input type="month" id="mesAno">
<button class="btn-success" onclick="gerarGeral()">⬇️ Baixar PDF Geral</button>
</div>
<div class="card">
<h3 style="margin:10px 0; color:#555;">👤 Relatório por Funcionário</h3>
<select id="selectFunc"></select>
<label>Mês/Ano:</label>
<input type="month" id="mesAnoFunc">
<button class="btn-warning" onclick="gerarIndividual()">⬇️ Baixar PDF</button>
</div>
</div>
</div>
<div id="qrcode" class="painel">
<h2>📱 QR Code do Sistema</h2>
<div class="qr-info">
<p><strong>URL Local:</strong></p>
<p id="urlLocal" style="font-weight:bold; color:#1565c0;"></p>
</div>
<button onclick="gerarQR()">🔄 Gerar/Atualizar QR Code</button>
<div id="qrImagem" style="margin-top:20px;"></div>
</div>
<div id="config" class="painel">
<h2>🔧 Configurações</h2>
<div class="card">
<h3 style="margin-bottom:10px;">🖼️ Logo da Clínica</h3>
<p style="font-size:14px; line-height:1.6;">
Coloque sua logo em <strong>static/logo.png</strong> (formato PNG).<br>
Se a logo não aparecer, pressione <strong>Ctrl+F5</strong> para atualizar o cache do navegador.
</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🔐 Credenciais de Acesso</h3>
<p style="font-size:14px;"><strong>Usuário:</strong> admin<br><strong>Senha:</strong> 3223ronte</p>
</div>
</div>
</div>
</div>
<script>
const hoje = new Date();
const mesAno = hoje.toISOString().slice(0,7);
document.getElementById('mesAno').value = mesAno;
document.getElementById('mesAnoFunc').value = mesAno;
document.getElementById('urlLocal').textContent = window.location.origin + '/';
document.querySelectorAll('img[src*="logo.png"]').forEach(function(img) {
    img.src = img.src.replace('TS_ADMIN', Date.now());
});
document.getElementById('cpfCad').addEventListener('input', function() { this.value = this.value.replace(/\\D/g, ''); });
function abrirAba(nome, btn) {
    document.querySelectorAll('.painel').forEach(p => p.classList.remove('ativo'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('ativo'));
    document.getElementById(nome).classList.add('ativo');
    btn.classList.add('ativo');
    if(nome === 'funcionarios') carregarFuncionarios();
    if(nome === 'registros') carregarRegistros();
    if(nome === 'relatorios') carregarSelect();
}
function sair() {
    document.cookie = 'sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
    window.location.href = '/admin';
}
function mostrarMsg(id, texto, tipo) {
    const el = document.getElementById(id);
    el.textContent = texto;
    el.className = 'mensagem ' + tipo;
    setTimeout(function() { el.className = 'mensagem'; }, 4000);
}
async function cadastrar() {
    const dados = {
        nome: document.getElementById('nome').value.trim(),
        cpf: document.getElementById('cpfCad').value.replace(/\\D/g,''),
        horario_entrada: document.getElementById('hEntrada').value.trim() || '08:00:00',
        horario_saida_almoco: document.getElementById('hSaidaAlmoco').value.trim() || '12:00:00',
        horario_retorno_almoco: document.getElementById('hRetornoAlmoco').value.trim() ||
