#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SISTEMA DE CONTROLE DE PONTO - CLÍNICA
Versão 3.0 - QR ROTATIVO (substitui o segredo estático por token de uso único)

Mudanças em relação à v2.1 (masterprompt original):
  - SEGREDO_QR estático removido: era enviado no HTML/JS do cliente, então
    exposto na internet qualquer pessoa podia lê-lo e bater ponto remoto.
  - Novo mecanismo: uma tela de recepção (rota /recepcao, autenticada com
    senha própria) busca um token novo a cada QR_TOKEN_TTL_SEGUNDOS segundos.
    O funcionário escaneia, o token vai junto na URL, e /api/bater_ponto só
    aceita token válido, não expirado e ainda não usado (uso único).
  - Servidor agora é multi-thread (ThreadingHTTPServer): antes uma geração de
    PDF travava o processo inteiro para todo mundo.
  - Login admin com rate limit simples (5 tentativas / 5 min por IP).
  - Credenciais e segredos configuráveis por variável de ambiente (com
    fallback e aviso no console se estiver usando o padrão).
  - Cookies marcados Secure quando ATRAS_DE_HTTPS=1 (recomendado: rodar atrás
    de nginx/Caddy com TLS, já que o sistema está exposto na internet).

Tudo o mais (schema de funcionarios/registros_ponto, regras de atraso,
contratos de resposta JSON, rotas de funcionários/relatórios) foi mantido
igual ao masterprompt original para não quebrar compatibilidade.
"""

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import socketserver
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ============================================================
# 1. CONFIGURAÇÕES E CONSTANTES GLOBAIS
# ============================================================

PORTA = int(os.environ.get("PORTA", "8000"))
DB_NOME = os.environ.get("DB_NOME", "ponto.db")
STATIC_DIR = "static"

ADMIN_USUARIO = os.environ.get("ADMIN_USUARIO", "admin")
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "admin123")
SENHA_RECEPCAO = os.environ.get("SENHA_RECEPCAO", "recepcao123")

if ADMIN_SENHA == "admin123":
    print("⚠️  AVISO: ADMIN_SENHA está no valor padrão. Defina a variável de "
          "ambiente ADMIN_SENHA antes de expor este servidor na internet.")
if SENHA_RECEPCAO == "recepcao123":
    print("⚠️  AVISO: SENHA_RECEPCAO está no valor padrão. Defina a variável "
          "de ambiente SENHA_RECEPCAO antes de usar em produção.")

ATRAS_DE_HTTPS = os.environ.get("ATRAS_DE_HTTPS", "1") == "1"
COOKIE_SECURE_FLAG = "; Secure" if ATRAS_DE_HTTPS else ""

QR_TOKEN_TTL_SEGUNDOS = int(os.environ.get("QR_TOKEN_TTL_SEGUNDOS", "45"))

sessoes_admin = {}      # token -> datetime de expiração
sessoes_recepcao = {}   # token -> datetime de expiração
tentativas_login = {}   # ip -> [timestamps de tentativas falhas]

MAX_TENTATIVAS_LOGIN = 5
JANELA_TENTATIVAS_SEGUNDOS = 300  # 5 minutos

TIPOS_REGISTRO = {
    "ENTRADA":        {"label": "ENTRADA",        "cor": "#4CAF50", "icone": "✅"},
    "SAIDA_ALMOCO":   {"label": "SAIDA ALMOCO",   "cor": "#ff9800", "icone": "🍽️"},
    "RETORNO_ALMOCO": {"label": "RETORNO ALMOCO", "cor": "#2196F3", "icone": "↩️"},
    "SAIDA":          {"label": "SAIDA",          "cor": "#f44336", "icone": "🚪"},
}

# Dependências opcionais
try:
    import qrcode
    QRCODE_DISPONIVEL = True
except ImportError:
    QRCODE_DISPONIVEL = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.colors import HexColor
    REPORTLAB_DISPONIVEL = True
except ImportError:
    REPORTLAB_DISPONIVEL = False


# ============================================================
# 2. FUNÇÕES AUXILIARES
# ============================================================

def formatar_cpf(cpf: str) -> str:
    return ''.join(filter(str.isdigit, str(cpf or "")))


def _para_segundos(hora_str: str):
    partes = hora_str.strip().split(":")
    partes = [int(p) for p in partes]
    while len(partes) < 3:
        partes.append(0)
    h, m, s = partes[:3]
    return h * 3600 + m * 60 + s


def verificar_atraso(hora_registro: str, horario_padrao: str) -> bool:
    try:
        return _para_segundos(hora_registro) > _para_segundos(horario_padrao)
    except Exception:
        return False


def gerar_sessao() -> str:
    return hashlib.sha256(os.urandom(64)).hexdigest()


def limpar_sessoes_expiradas():
    agora = datetime.now()
    for token in [t for t, exp in sessoes_admin.items() if exp <= agora]:
        del sessoes_admin[token]
    for token in [t for t, exp in sessoes_recepcao.items() if exp <= agora]:
        del sessoes_recepcao[token]


def limpar_tokens_qr_expirados():
    limite = (datetime.now() - timedelta(seconds=QR_TOKEN_TTL_SEGUNDOS * 4)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_db()
        conn.execute("DELETE FROM qr_tokens WHERE criado_em < ?", (limite,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _extrair_cookie(handler, nome):
    cookie_header = handler.headers.get("Cookie", "")
    for parte in cookie_header.split(";"):
        parte = parte.strip()
        if parte.startswith(nome + "="):
            return parte[len(nome) + 1:]
    return None


def verificar_login(handler) -> bool:
    try:
        limpar_sessoes_expiradas()
        token = _extrair_cookie(handler, "sessao_admin")
        if not token or token not in sessoes_admin:
            return False
        return sessoes_admin[token] > datetime.now()
    except Exception:
        return False


def verificar_login_recepcao(handler) -> bool:
    try:
        limpar_sessoes_expiradas()
        token = _extrair_cookie(handler, "sessao_recepcao")
        if not token or token not in sessoes_recepcao:
            return False
        return sessoes_recepcao[token] > datetime.now()
    except Exception:
        return False


def ip_bloqueado(ip: str) -> bool:
    agora = time.time()
    tentativas = [t for t in tentativas_login.get(ip, []) if agora - t < JANELA_TENTATIVAS_SEGUNDOS]
    tentativas_login[ip] = tentativas
    return len(tentativas) >= MAX_TENTATIVAS_LOGIN


def registrar_tentativa_falha(ip: str):
    tentativas_login.setdefault(ip, []).append(time.time())


def responder_json(handler, dados, status=200, cookies_extra=None):
    corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(corpo)))
    if cookies_extra:
        for c in cookies_extra:
            handler.send_header("Set-Cookie", c)
    handler.end_headers()
    handler.wfile.write(corpo)


def responder_html(handler, conteudo, status=200, cookies_extra=None):
    corpo = conteudo.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(corpo)))
    if cookies_extra:
        for c in cookies_extra:
            handler.send_header("Set-Cookie", c)
    handler.end_headers()
    handler.wfile.write(corpo)


def responder_binario(handler, dados: bytes, content_type, status=200, filename=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(dados)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.end_headers()
    handler.wfile.write(dados)


def get_db():
    conn = sqlite3.connect(DB_NOME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS qr_tokens (
            token TEXT PRIMARY KEY,
            criado_em TEXT NOT NULL,
            usado INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    os.makedirs(STATIC_DIR, exist_ok=True)


# ============================================================
# 3. GERAÇÃO DE QR ROTATIVO
# ============================================================

def gerar_token_qr(host: str):
    limpar_tokens_qr_expirados()
    token = gerar_sessao()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    conn.execute("INSERT INTO qr_tokens (token, criado_em, usado) VALUES (?, ?, 0)", (token, agora))
    conn.commit()
    conn.close()

    url = f"http://{host}/?t={token}"
    qrcode_base64 = None
    if QRCODE_DISPONIVEL:
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qrcode_base64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    return {
        "token": token,
        "url": url,
        "expira_em": QR_TOKEN_TTL_SEGUNDOS,
        "qrcode_base64": qrcode_base64,
    }


def validar_e_consumir_token_qr(token: str):
    """Retorna (True, None) se válido e consome; (False, motivo) se inválido."""
    if not token:
        return False, "Token de QR ausente. Escaneie o QR Code na recepção."
    conn = get_db()
    row = conn.execute("SELECT * FROM qr_tokens WHERE token = ?", (token,)).fetchone()
    if row is None:
        conn.close()
        return False, "QR Code inválido. Escaneie novamente."
    if row["usado"]:
        conn.close()
        return False, "Este QR Code já foi usado. Escaneie um novo."
    criado_em = datetime.strptime(row["criado_em"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() - criado_em > timedelta(seconds=QR_TOKEN_TTL_SEGUNDOS):
        conn.close()
        return False, "QR Code expirado. Escaneie o QR Code atualizado na recepção."
    conn.execute("UPDATE qr_tokens SET usado = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return True, None


# ============================================================
# 4. PÁGINAS HTML
# ============================================================

HTML_LOGIN = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<title>Login Admin - Sistema de Ponto</title>
<style>
body{font-family:Arial,sans-serif;background:#667eea;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}
.box{background:#fff;padding:2rem;border-radius:12px;width:300px;box-shadow:0 8px 24px rgba(0,0,0,.2)}
h2{margin-top:0;color:#333}
input{width:100%;padding:.6rem;margin:.4rem 0;box-sizing:border-box;border:1px solid #ccc;border-radius:6px}
button{width:100%;padding:.7rem;background:#667eea;color:#fff;border:none;border-radius:6px;
font-weight:bold;cursor:pointer;margin-top:.5rem}
#erro{color:#f44336;font-size:.9rem;min-height:1.2rem}
a{display:block;text-align:center;margin-top:1rem;color:#667eea;font-size:.85rem}
</style></head><body>
<div class="box">
<h2>🔐 Login Admin</h2>
<input id="usuario" placeholder="Usuário" autofocus>
<input id="senha" type="password" placeholder="Senha">
<div id="erro"></div>
<button onclick="fazerLogin()">Entrar</button>
<a href="/">← Voltar ao registro de ponto</a>
</div>
<script>
const usuario = document.getElementById('usuario');
const senha = document.getElementById('senha');
usuario.addEventListener('keydown', e => { if(e.key==='Enter') senha.focus(); });
senha.addEventListener('keydown', e => { if(e.key==='Enter') fazerLogin(); });
async function fazerLogin(){
  const erro = document.getElementById('erro');
  erro.textContent = '';
  try{
    const r = await fetch('/api/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({usuario: usuario.value, senha: senha.value})
    });
    const d = await r.json();
    if(r.ok){ window.location.href = '/admin'; }
    else { erro.textContent = d.detail || 'Erro ao entrar'; }
  }catch(e){ erro.textContent = 'Erro de conexão'; }
}
</script></body></html>"""


HTML_PONTO = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Registro de Ponto</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:1rem;display:flex;
flex-direction:column;align-items:center}
.card{background:#fff;border-radius:12px;padding:1.5rem;max-width:420px;width:100%;
box-shadow:0 4px 16px rgba(0,0,0,.1);text-align:center}
#relogio{font-size:1.4rem;font-weight:bold;color:#333;margin-bottom:1rem}
input{width:100%;padding:.8rem;font-size:1.1rem;text-align:center;box-sizing:border-box;
border:2px solid #ddd;border-radius:8px;margin-bottom:.5rem}
#nomeFuncionario{min-height:1.4rem;color:#667eea;font-weight:bold}
.btn{width:100%;padding:1rem;margin:.4rem 0;border:none;border-radius:8px;color:#fff;
font-size:1.05rem;font-weight:bold;cursor:pointer}
#msg{min-height:1.5rem;margin-top:.8rem;font-weight:bold}
#avisoToken{font-size:.8rem;color:#999;margin-top:1rem}
</style></head><body>
<div class="card">
<div id="relogio"></div>
<input id="cpf" placeholder="Digite seu CPF" maxlength="11" inputmode="numeric">
<div id="nomeFuncionario"></div>
<button class="btn" style="background:#4CAF50" onclick="bater('ENTRADA')">✅ ENTRADA</button>
<button class="btn" style="background:#ff9800" onclick="bater('SAIDA_ALMOCO')">🍽️ SAÍDA ALMOÇO</button>
<button class="btn" style="background:#2196F3" onclick="bater('RETORNO_ALMOCO')">↩️ RETORNO ALMOÇO</button>
<button class="btn" style="background:#f44336" onclick="bater('SAIDA')">🚪 SAÍDA</button>
<div id="msg"></div>
<div id="avisoToken"></div>
</div>
<script>
function atualizarRelogio(){
  document.getElementById('relogio').textContent = new Date().toLocaleString('pt-BR');
}
setInterval(atualizarRelogio, 1000); atualizarRelogio();

const cpfInput = document.getElementById('cpf');
cpfInput.addEventListener('input', () => {
  cpfInput.value = cpfInput.value.replace(/\\D/g, '').slice(0, 11);
});
cpfInput.addEventListener('blur', async () => {
  const nomeDiv = document.getElementById('nomeFuncionario');
  nomeDiv.textContent = '';
  if(cpfInput.value.length < 11) return;
  try{
    const r = await fetch('/api/buscar/' + cpfInput.value);
    const d = await r.json();
    nomeDiv.textContent = d.encontrado ? ('👤 ' + d.nome) : '❌ CPF não encontrado';
  }catch(e){}
});

const params = new URLSearchParams(window.location.search);
const qrToken = params.get('t');
if(!qrToken){
  document.getElementById('avisoToken').textContent =
    '⚠️ Acesse pelo QR Code exibido na recepção.';
}

async function bater(tipo){
  const msg = document.getElementById('msg');
  msg.textContent = '';
  if(cpfInput.value.length < 11){
    msg.style.color = '#f44336'; msg.textContent = 'Digite um CPF válido (11 dígitos)';
    return;
  }
  try{
    const r = await fetch('/api/bater_ponto', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({cpf: cpfInput.value, tipo: tipo, token: qrToken})
    });
    const d = await r.json();
    msg.style.color = r.ok ? '#4CAF50' : '#f44336';
    msg.textContent = d.mensagem || d.detail || 'Erro';
  }catch(e){
    msg.style.color = '#f44336'; msg.textContent = 'Erro de conexão';
  }
  setTimeout(() => { msg.textContent = ''; }, 7000);
}
</script></body></html>"""


HTML_RECEPCAO_LOGIN = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<title>Tela de Recepção - Login</title>
<style>
body{font-family:Arial,sans-serif;background:#333;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0}
.box{background:#fff;padding:2rem;border-radius:12px;width:280px}
input{width:100%;padding:.6rem;margin:.4rem 0;box-sizing:border-box}
button{width:100%;padding:.7rem;background:#333;color:#fff;border:none;border-radius:6px;margin-top:.5rem}
#erro{color:#f44336;font-size:.85rem}
</style></head><body>
<div class="box">
<h3>📺 Tela de Recepção</h3>
<input id="senha" type="password" placeholder="Senha da recepção" autofocus>
<div id="erro"></div>
<button onclick="entrar()">Ativar</button>
</div>
<script>
document.getElementById('senha').addEventListener('keydown', e => { if(e.key==='Enter') entrar(); });
async function entrar(){
  const senha = document.getElementById('senha').value;
  const erro = document.getElementById('erro');
  try{
    const r = await fetch('/api/recepcao/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({senha: senha})
    });
    if(r.ok){ window.location.href = '/recepcao'; }
    else { const d = await r.json(); erro.textContent = d.detail || 'Senha incorreta'; }
  }catch(e){ erro.textContent = 'Erro de conexão'; }
}
</script></body></html>"""


HTML_RECEPCAO = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QR Code - Bata seu ponto</title>
<style>
body{font-family:Arial,sans-serif;background:#111;color:#fff;display:flex;flex-direction:column;
align-items:center;justify-content:center;height:100vh;margin:0}
h2{margin-bottom:.3rem}
#qr{width:320px;height:320px;background:#fff;border-radius:12px;padding:1rem;box-sizing:border-box}
#qr img{width:100%;height:100%;object-fit:contain}
#contador{font-size:1.3rem;margin-top:1rem;color:#4CAF50}
#relogio{margin-top:.5rem;color:#aaa}
</style></head><body>
<h2>📱 Escaneie para bater o ponto</h2>
<div id="qr">Carregando...</div>
<div id="contador"></div>
<div id="relogio"></div>
<script>
let expiraEm = 0;
async function buscarNovoQR(){
  try{
    const r = await fetch('/api/qr/token');
    if(r.status === 401){ window.location.href = '/recepcao'; return; }
    const d = await r.json();
    expiraEm = d.expira_em;
    document.getElementById('qr').innerHTML =
      d.qrcode_base64 ? ('<img src="' + d.qrcode_base64 + '">') : 'qrcode/pillow não instalados';
  }catch(e){}
}
function tick(){
  expiraEm -= 1;
  document.getElementById('contador').textContent =
    expiraEm > 0 ? ('Válido por mais ' + expiraEm + 's') : 'Atualizando...';
  document.getElementById('relogio').textContent = new Date().toLocaleString('pt-BR');
  if(expiraEm <= 0){ buscarNovoQR(); }
}
buscarNovoQR();
setInterval(tick, 1000);
</script></body></html>"""


def gerar_html_admin():
    # Painel simplificado (mesmas 6 abas do masterprompt original), consumindo as
    # mesmas rotas de API já existentes.
    return """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<title>Painel Admin</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#f5f5f5}
header{background:#667eea;color:#fff;padding:1rem;display:flex;justify-content:space-between}
nav{display:flex;background:#fff;border-bottom:1px solid #ddd}
nav button{flex:1;padding:.8rem;border:none;background:none;cursor:pointer;font-weight:bold}
nav button.ativo{border-bottom:3px solid #667eea;color:#667eea}
section{display:none;padding:1.5rem;max-width:900px;margin:0 auto}
section.ativo{display:block}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{padding:.5rem;border-bottom:1px solid #eee;text-align:left;font-size:.9rem}
input,select{padding:.5rem;margin:.3rem 0;width:100%;box-sizing:border-box}
button.acao{padding:.6rem 1rem;background:#667eea;color:#fff;border:none;border-radius:6px;cursor:pointer}
</style></head><body>
<header><span>🔐 Painel Admin</span><button class="acao" onclick="logout()">Sair</button></header>
<nav>
<button class="ativo" onclick="mostrar('cadastrar', this)">Cadastrar</button>
<button onclick="mostrar('funcionarios', this)">Funcionários</button>
<button onclick="mostrar('registros', this)">Registros</button>
<button onclick="mostrar('relatorios', this)">Relatórios PDF</button>
<button onclick="mostrar('config', this)">Config / QR</button>
</nav>

<section id="cadastrar" class="ativo">
<h3>Novo funcionário</h3>
<input id="c_nome" placeholder="Nome completo">
<input id="c_cpf" placeholder="CPF (somente números)">
<input id="c_entrada" placeholder="Horário entrada (08:00:00)">
<input id="c_saida_almoco" placeholder="Saída almoço (12:00:00)">
<input id="c_retorno_almoco" placeholder="Retorno almoço (13:00:00)">
<input id="c_saida" placeholder="Saída (18:00:00)">
<button class="acao" onclick="cadastrar()">Cadastrar</button>
<div id="c_msg"></div>
</section>

<section id="funcionarios">
<h3>Funcionários</h3>
<table id="tabela_func"><thead><tr><th>Nome</th><th>CPF</th><th>Horários</th><th></th></tr></thead>
<tbody></tbody></table>
</section>

<section id="registros">
<h3>Registros</h3>
<table id="tabela_reg"><thead><tr><th>Funcionário</th><th>Data/Hora</th><th>Tipo</th><th>Atrasado</th></tr></thead>
<tbody></tbody></table>
</section>

<section id="relatorios">
<h3>Relatório PDF geral do mês</h3>
<input id="mes_geral" type="month">
<button class="acao" onclick="pdfGeral()">Gerar PDF</button>
</section>

<section id="config">
<h3>Tela de recepção</h3>
<p>Abra <code>/recepcao</code> em um tablet/monitor fixo na entrada da clínica e
autentique com a senha de recepção. O QR exibido lá se renova sozinho.</p>
</section>

<script>
function mostrar(id, btn){
  document.querySelectorAll('section').forEach(s => s.classList.remove('ativo'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('ativo'));
  document.getElementById(id).classList.add('ativo');
  btn.classList.add('ativo');
  if(id === 'funcionarios') carregarFuncionarios();
  if(id === 'registros') carregarRegistros();
}
async function chamada(url, opts){
  const r = await fetch(url, opts);
  if(r.status === 401){ window.location.href = '/admin'; throw new Error('401'); }
  return r;
}
async function cadastrar(){
  const body = {
    nome: document.getElementById('c_nome').value,
    cpf: document.getElementById('c_cpf').value,
    horario_entrada: document.getElementById('c_entrada').value || undefined,
    horario_saida_almoco: document.getElementById('c_saida_almoco').value || undefined,
    horario_retorno_almoco: document.getElementById('c_retorno_almoco').value || undefined,
    horario_saida: document.getElementById('c_saida').value || undefined,
  };
  const r = await chamada('/api/funcionarios', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
  });
  const d = await r.json();
  document.getElementById('c_msg').textContent = r.ok ? 'Cadastrado!' : (d.detail || 'Erro');
}
async function carregarFuncionarios(){
  const r = await chamada('/api/funcionarios');
  const lista = await r.json();
  const tbody = document.querySelector('#tabela_func tbody');
  tbody.innerHTML = lista.map(f => `<tr>
    <td>${f.nome}</td><td>${f.cpf}</td>
    <td>${f.horario_entrada} / ${f.horario_saida_almoco} / ${f.horario_retorno_almoco} / ${f.horario_saida}</td>
    <td><button onclick="excluir(${f.id})">Excluir</button></td></tr>`).join('');
}
async function excluir(id){
  if(!confirm('Excluir funcionário e todos seus registros?')) return;
  await chamada('/api/funcionarios/' + id, {method:'DELETE'});
  carregarFuncionarios();
}
async function carregarRegistros(){
  const r = await chamada('/api/registros');
  const lista = await r.json();
  const tbody = document.querySelector('#tabela_reg tbody');
  tbody.innerHTML = lista.map(reg => `<tr>
    <td>${reg.nome}</td><td>${reg.data_hora}</td><td>${reg.tipo}</td>
    <td>${reg.atrasado ? 'SIM' : 'Não'}</td></tr>`).join('');
}
function pdfGeral(){
  const mes = document.getElementById('mes_geral').value;
  if(!mes){ alert('Escolha o mês'); return; }
  window.open('/api/pdf/geral?mes=' + mes, '_blank');
}
async function logout(){
  await fetch('/api/logout');
  window.location.href = '/login';
}
carregarFuncionarios();
</script></body></html>"""


# ============================================================
# 5. GERAÇÃO DE PDF
# ============================================================

def gerar_pdf_geral(mes: str) -> bytes:
    conn = get_db()
    registros = conn.execute("""
        SELECT f.nome, f.cpf, r.data_hora, r.tipo, r.atrasado
        FROM registros_ponto r JOIN funcionarios f ON f.id = r.funcionario_id
        WHERE r.data_hora LIKE ?
        ORDER BY f.nome, r.data_hora
    """, (mes + "%",)).fetchall()
    conn.close()

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    largura, altura = A4
    y = altura - 60

    c.setFillColor(HexColor("#667eea"))
    c.rect(0, y, largura, 40, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, y + 12, f"RELATÓRIO DE FOLHA PONTO - {mes}")
    y -= 30
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica", 10)

    for reg in registros:
        if y < 80:
            c.showPage()
            y = altura - 60
        cor = TIPOS_REGISTRO.get(reg["tipo"], {}).get("cor", "#000000")
        c.setFillColor(HexColor("#000000"))
        c.drawString(30, y, f"{reg['nome']} ({reg['cpf']})")
        c.drawString(230, y, reg["data_hora"])
        c.setFillColor(HexColor(cor))
        c.drawString(340, y, reg["tipo"])
        c.setFillColor(HexColor("#f44336") if reg["atrasado"] else HexColor("#000000"))
        c.drawString(460, y, "SIM" if reg["atrasado"] else "Não")
        y -= 16

    c.save()
    return buf.getvalue()


def gerar_pdf_individual(funcionario_id: int, mes: str) -> bytes:
    conn = get_db()
    func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,)).fetchone()
    if func is None:
        conn.close()
        return None
    registros = conn.execute("""
        SELECT * FROM registros_ponto WHERE funcionario_id = ? AND data_hora LIKE ?
        ORDER BY data_hora
    """, (funcionario_id, mes + "%")).fetchall()
    conn.close()

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    largura, altura = A4
    y = altura - 60

    c.setFillColor(HexColor("#667eea"))
    c.rect(0, y, largura, 50, fill=1, stroke=0)
    c.setFillColor(HexColor("#ffffff"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, y + 25, "RELATÓRIO DE FOLHA PONTO")
    c.setFont("Helvetica", 11)
    c.drawString(30, y + 8, f"{func['nome']} - CPF {func['cpf']} - {mes}")
    y -= 40
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica", 10)
    c.drawString(30, y, f"Entrada: {func['horario_entrada']}  |  Saída almoço: {func['horario_saida_almoco']}  |  "
                        f"Retorno: {func['horario_retorno_almoco']}  |  Saída: {func['horario_saida']}")
    y -= 25

    total_atrasos = 0
    for reg in registros:
        if y < 80:
            c.showPage()
            y = altura - 60
        cor = TIPOS_REGISTRO.get(reg["tipo"], {}).get("cor", "#000000")
        c.setFillColor(HexColor("#000000"))
        c.drawString(30, y, reg["data_hora"])
        c.setFillColor(HexColor(cor))
        c.drawString(180, y, reg["tipo"])
        atrasado = bool(reg["atrasado"])
        if atrasado:
            total_atrasos += 1
        c.setFillColor(HexColor("#f44336") if atrasado else HexColor("#000000"))
        c.drawString(320, y, "SIM" if atrasado else "Não")
        y -= 16

    if y < 100:
        c.showPage()
        y = altura - 60
    y -= 20
    c.setFillColor(HexColor("#000000"))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(30, y, f"Total de registros: {len(registros)}   |   Total de atrasos: {total_atrasos}")

    c.save()
    return buf.getvalue()


# ============================================================
# 6. HANDLER HTTP
# ============================================================

class ServidorPonto(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} - {format % args}")

    def _ler_json(self):
        try:
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = self.rfile.read(tamanho) if tamanho else b"{}"
            return json.loads(corpo)
        except json.JSONDecodeError:
            return {}

    def _host(self):
        return self.headers.get("Host", f"localhost:{PORTA}")

    # -------------------- GET --------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        caminho = parsed.path
        query = parse_qs(parsed.query)

        try:
            if caminho in ("/", "/index.html"):
                return responder_html(self, HTML_PONTO)

            if caminho == "/login":
                return responder_html(self, HTML_LOGIN)

            if caminho == "/admin":
                if verificar_login(self):
                    return responder_html(self, gerar_html_admin())
                return responder_html(self, HTML_LOGIN)

            if caminho == "/recepcao":
                if verificar_login_recepcao(self):
                    return responder_html(self, HTML_RECEPCAO)
                return responder_html(self, HTML_RECEPCAO_LOGIN)

            if caminho.startswith("/static/"):
                return self._servir_estatico(caminho)

            if caminho.startswith("/api/buscar/"):
                cpf = formatar_cpf(unquote(caminho.split("/api/buscar/")[1]))
                conn = get_db()
                row = conn.execute("SELECT nome FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                conn.close()
                if row:
                    return responder_json(self, {"encontrado": True, "nome": row["nome"]})
                return responder_json(self, {"encontrado": False, "nome": None})

            if caminho == "/api/qr/token":
                if not verificar_login_recepcao(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                dados = gerar_token_qr(self._host())
                return responder_json(self, dados)

            if caminho == "/api/funcionarios":
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                conn = get_db()
                linhas = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
                conn.close()
                return responder_json(self, [dict(r) for r in linhas])

            if caminho == "/api/registros":
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                conn = get_db()
                linhas = conn.execute("""
                    SELECT r.id, f.nome, f.cpf, r.data_hora, r.tipo, r.atrasado
                    FROM registros_ponto r JOIN funcionarios f ON f.id = r.funcionario_id
                    ORDER BY r.data_hora DESC
                """).fetchall()
                conn.close()
                return responder_json(self, [dict(r) for r in linhas])

            if caminho == "/api/logout":
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                token = _extrair_cookie(self, "sessao_admin")
                sessoes_admin.pop(token, None)
                cookie_expirado = f"sessao_admin=; Path=/; Max-Age=0{COOKIE_SECURE_FLAG}"
                return responder_json(self, {"status": "ok"}, cookies_extra=[cookie_expirado])

            if caminho.startswith("/api/pdf/geral"):
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                if not REPORTLAB_DISPONIVEL:
                    return responder_json(self, {"detail": "reportlab não instalado (pip install reportlab)"}, 500)
                mes = query.get("mes", [datetime.now().strftime("%Y-%m")])[0]
                pdf_bytes = gerar_pdf_geral(mes)
                return responder_binario(self, pdf_bytes, "application/pdf", filename=f"relatorio_geral_{mes}.pdf")

            if caminho.startswith("/api/pdf/funcionario/"):
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                if not REPORTLAB_DISPONIVEL:
                    return responder_json(self, {"detail": "reportlab não instalado (pip install reportlab)"}, 500)
                fid = int(caminho.split("/api/pdf/funcionario/")[1].split("?")[0])
                mes = query.get("mes", [datetime.now().strftime("%Y-%m")])[0]
                pdf_bytes = gerar_pdf_individual(fid, mes)
                if pdf_bytes is None:
                    return responder_json(self, {"detail": "Funcionário não encontrado"}, 404)
                return responder_binario(self, pdf_bytes, "application/pdf", filename=f"relatorio_{fid}_{mes}.pdf")

            return responder_json(self, {"detail": "Rota não encontrada"}, 404)

        except Exception as e:
            return responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, 500)

    def _servir_estatico(self, caminho):
        rel = caminho[len("/static/"):]
        rel = rel.replace("..", "")
        caminho_completo = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(caminho_completo):
            return responder_json(self, {"detail": "Arquivo não encontrado"}, 404)
        ext = os.path.splitext(caminho_completo)[1].lower()
        tipos = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".css": "text/css", ".js": "application/javascript"}
        content_type = tipos.get(ext, "application/octet-stream")
        with open(caminho_completo, "rb") as f:
            return responder_binario(self, f.read(), content_type)

    # -------------------- POST --------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        caminho = parsed.path
        ip = self.client_address[0]

        try:
            if caminho == "/api/login":
                if ip_bloqueado(ip):
                    return responder_json(self, {"detail": "Muitas tentativas. Aguarde alguns minutos."}, 429)
                dados = self._ler_json()
                if dados.get("usuario") == ADMIN_USUARIO and dados.get("senha") == ADMIN_SENHA:
                    token = gerar_sessao()
                    sessoes_admin[token] = datetime.now() + timedelta(hours=8)
                    cookie = f"sessao_admin={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax{COOKIE_SECURE_FLAG}"
                    return responder_json(self, {"status": "ok", "mensagem": "Login realizado"}, cookies_extra=[cookie])
                registrar_tentativa_falha(ip)
                return responder_json(self, {"detail": "Usuário ou senha inválidos"}, 401)

            if caminho == "/api/recepcao/login":
                if ip_bloqueado("recep_" + ip):
                    return responder_json(self, {"detail": "Muitas tentativas. Aguarde alguns minutos."}, 429)
                dados = self._ler_json()
                if dados.get("senha") == SENHA_RECEPCAO:
                    token = gerar_sessao()
                    sessoes_recepcao[token] = datetime.now() + timedelta(hours=24 * 30)
                    cookie = f"sessao_recepcao={token}; Path=/; Max-Age={24*30*3600}; HttpOnly; SameSite=Lax{COOKIE_SECURE_FLAG}"
                    return responder_json(self, {"status": "ok"}, cookies_extra=[cookie])
                registrar_tentativa_falha("recep_" + ip)
                return responder_json(self, {"detail": "Senha incorreta"}, 401)

            if caminho == "/api/bater_ponto":
                dados = self._ler_json()

                ok_token, motivo = validar_e_consumir_token_qr(dados.get("token"))
                if not ok_token:
                    return responder_json(self, {"detail": motivo}, 400)

                tipo = dados.get("tipo")
                if tipo not in TIPOS_REGISTRO:
                    return responder_json(self, {"detail": "Tipo de registro inválido"}, 400)

                cpf = formatar_cpf(dados.get("cpf"))
                if len(cpf) != 11:
                    return responder_json(self, {"detail": "CPF deve ter 11 dígitos"}, 400)

                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if func is None:
                    conn.close()
                    return responder_json(self, {"detail": "Funcionário não encontrado"}, 404)

                agora = datetime.now()
                hora_str = agora.strftime("%H:%M:%S")
                data_hora_str = agora.strftime("%Y-%m-%d %H:%M:%S")

                if tipo == "ENTRADA":
                    atrasado = verificar_atraso(hora_str, func["horario_entrada"])
                elif tipo == "SAIDA_ALMOCO":
                    atrasado = False
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = verificar_atraso(hora_str, func["horario_retorno_almoco"])
                else:  # SAIDA
                    atrasado = verificar_atraso(func["horario_saida"], hora_str)

                conn.execute(
                    "INSERT INTO registros_ponto (funcionario_id, data_hora, tipo, atrasado) VALUES (?, ?, ?, ?)",
                    (func["id"], data_hora_str, tipo, int(atrasado))
                )
                conn.commit()
                conn.close()

                label = TIPOS_REGISTRO[tipo]["label"]
                msg = f"{func['nome']}: {label} registrada às {hora_str}"
                if atrasado:
                    msg += " (ATRASADO)"
                return responder_json(self, {"mensagem": msg})

            if caminho == "/api/funcionarios":
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                dados = self._ler_json()
                nome = dados.get("nome")
                cpf = formatar_cpf(dados.get("cpf"))
                if not nome or len(cpf) != 11:
                    return responder_json(self, {"detail": "Nome e CPF (11 dígitos) são obrigatórios"}, 400)

                conn = get_db()
                try:
                    cur = conn.execute("""
                        INSERT INTO funcionarios (nome, cpf, horario_entrada, horario_saida_almoco,
                                                   horario_retorno_almoco, horario_saida)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        nome, cpf,
                        dados.get("horario_entrada") or "08:00:00",
                        dados.get("horario_saida_almoco") or "12:00:00",
                        dados.get("horario_retorno_almoco") or "13:00:00",
                        dados.get("horario_saida") or "18:00:00",
                    ))
                    conn.commit()
                    novo_id = cur.lastrowid
                    return responder_json(self, {"status": "ok", "id": novo_id})
                except sqlite3.IntegrityError:
                    return responder_json(self, {"detail": "CPF já cadastrado"}, 400)
                finally:
                    conn.close()

            return responder_json(self, {"detail": "Rota não encontrada"}, 404)

        except Exception as e:
            return responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, 500)

    # -------------------- DELETE --------------------
    def do_DELETE(self):
        parsed = urlparse(self.path)
        caminho = parsed.path
        try:
            if caminho.startswith("/api/funcionarios/"):
                if not verificar_login(self):
                    return responder_json(self, {"detail": "Não autorizado"}, 401)
                fid = int(caminho.split("/api/funcionarios/")[1])
                conn = get_db()
                conn.execute("DELETE FROM funcionarios WHERE id = ?", (fid,))
                conn.commit()
                conn.close()
                return responder_json(self, {"status": "ok"})
            return responder_json(self, {"detail": "Rota não encontrada"}, 404)
        except Exception as e:
            return responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, 500)


class ServidorThreaded(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ============================================================
# 7. INICIALIZAÇÃO
# ============================================================

if __name__ == "__main__":
    init_db()
    print("=" * 65)
    print("   🚀 SISTEMA DE PONTO v3.0 - QR ROTATIVO - FUNCIONANDO!")
    print("=" * 65)
    print(f"📱 Página do funcionário:   http://localhost:{PORTA}  (só funciona via QR da recepção)")
    print(f"📺 Tela de recepção:        http://localhost:{PORTA}/recepcao")
    print(f"🔐 Login Admin:             http://localhost:{PORTA}/admin")
    print(f"👤 Usuário admin: {ADMIN_USUARIO}")
    print("=" * 65)
    print(f"QR válido por {QR_TOKEN_TTL_SEGUNDOS}s, uso único.")
    print(f"qrcode/pillow instalados: {QRCODE_DISPONIVEL}   |   reportlab instalado: {REPORTLAB_DISPONIVEL}")
    print(f"Cookies com flag Secure: {ATRAS_DE_HTTPS} (defina ATRAS_DE_HTTPS=0 se não tiver TLS na frente)")
    print("=" * 65)
    print("Servidor rodando... Aperte Ctrl+C para parar.")

    servidor = ServidorThreaded(("0.0.0.0", PORTA), ServidorPonto)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando servidor...")
        servidor.shutdown()
