import sqlite3
import json
import os
import io
import hashlib
import base64
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ===================== CONFIGURACOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = 8000
ADMIN_USUARIO = "admin"
ADMIN_SENHA = "admin123"
sessoes_admin = {}
os.makedirs("static", exist_ok=True)

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
            FOREIGN KEY (funcionario_id) REFERENCES funcionarios(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===================== FUNCOES AUXILIARES =====================
def formatar_cpf(cpf):
    return ''.join(filter(str.isdigit, str(cpf)))

def verificar_atraso(hora_registro, horario_padrao):
    """Verifica se a hora do registro é MAIOR que o horário padrão (atrasado)."""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0]) * 3600 + int(h_r[1]) * 60 + (int(h_r[2]) if len(h_r) > 2 else 0)
        t_p = int(h_p[0]) * 3600 + int(h_p[1]) * 60 + (int(h_p[2]) if len(h_p) > 2 else 0)
        return t_r > t_p
    except:
        return False

def gerar_sessao():
    """Gera um token de sessão seguro e compatível com cookies."""
    return hashlib.sha256(os.urandom(64)).hexdigest()

def limpar_sessoes_expiradas():
    """Remove sessões que já expiraram."""
    agora = datetime.now()
    expiradas = [token for token, expira in sessoes_admin.items() if expira <= agora]
    for token in expiradas:
        del sessoes_admin[token]

def verificar_login(handler):
    """Verifica se o admin está logado pelo cookie."""
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
    """Responde com JSON e opcionalmente define cookies."""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    if cookies_extra:
        for cookie in cookies_extra:
            handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(json.dumps(dados, ensure_ascii=False).encode("utf-8"))

def responder_html(handler, conteudo, status=200, cookies_extra=None):
    """Responde com HTML e opcionalmente define cookies."""
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
    "SAIDA_ALMOCO": {"label": "SAIDA ALMOCO", "cor": "#ff9800", "icone": "🍽️"},
    "RETORNO_ALMOCO": {"label": "RETORNO ALMOCO", "cor": "#2196F3", "icone": "↩️"},
    "SAIDA": {"label": "SAIDA", "cor": "#f44336", "icone": "🚪"}
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
.login-box { background: white; padding: 40px; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); width: 100%; max-width: 380px; }
.login-box h1 { color: #333; margin-bottom: 10px; font-size: 24px; text-align: center; }
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
<h1>🔐 Login Admin</h1>
<p class="sub">Acesso ao painel administrativo</p>
<input type="text" id="usuario" placeholder="Usuário" autocomplete="username">
<input type="password" id="senha" placeholder="Senha" autocomplete="current-password">
<button onclick="logar()">ENTRAR</button>
<div class="mensagem" id="mensagem"></div>
<a href="/" class="voltar">← Voltar para o registro de ponto</a>
</div>
<script>
document.getElementById('senha').addEventListener('keypress', function(e) {
    if(e.key === 'Enter') logar();
});
document.getElementById('usuario').addEventListener('keypress', function(e) {
    if(e.key === 'Enter') document.getElementById('senha').focus();
});
document.getElementById('usuario').focus();

async function logar() {
    const usuario = document.getElementById('usuario').value.trim();
    const senha = document.getElementById('senha').value;
    const msg = document.getElementById('mensagem');
    
    if(!usuario || !senha) {
        msg.textContent = 'Preencha usuário e senha!';
        msg.className = 'mensagem erro';
        return;
    }
    
    try {
        const res = await fetch('/api/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({usuario: usuario, senha: senha})
        });
        
        if(res.ok) {
            window.location.href = '/admin';
        } else {
            const err = await res.json();
            msg.textContent = err.detail || 'Erro ao fazer login';
            msg.className = 'mensagem erro';
        }
    } catch(e) {
        msg.textContent = 'Erro de conexão com o servidor!';
        msg.className = 'mensagem erro';
    }
}
</script>
</body>
</html>"""

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
.logo { max-width: 120px; margin-bottom: 15px; }
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
.admin-link { margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }
.admin-link a { color: #888; font-size: 12px; text-decoration: none; }
.admin-link a:hover { color: #667eea; text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
<img src="/static/logo.png" alt="Logo" class="logo" onerror="this.style.display='none'">
<h1>Registro de Ponto</h1>
<p class="subtitulo">Clínica - Controle de Funcionários</p>
<div class="data-hora" id="dataHora">Carregando...</div>
<input type="text" id="cpf" placeholder="Digite seu CPF (apenas números)" maxlength="11" inputmode="numeric">
<div class="info-func" id="infoFunc"></div>
<div class="botoes">
<button class="btn-entrada" onclick="registrar('ENTRADA')">
<span class="icone-btn">✅</span>ENTRADA
</button>
<button class="btn-almoco" onclick="registrar('SAIDA_ALMOCO')">
<span class="icone-btn">🍽️</span>SAÍDA ALMOÇO
</button>
<button class="btn-retorno" onclick="registrar('RETORNO_ALMOCO')">
<span class="icone-btn">↩️</span>RETORNO ALMOÇO
</button>
<button class="btn-saida" onclick="registrar('SAIDA')">
<span class="icone-btn">🚪</span>SAÍDA
</button>
</div>
<div class="mensagem" id="mensagem"></div>
<div class="admin-link"><a href="/admin">🔐 Acesso Administrador</a></div>
</div>
<script>
const QR_SEGREDO = "CLINICA_PONTO_2024";
function atualizarDataHora() {
    const agora = new Date();
    const opcoes = { weekday: 'long', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' };
    document.getElementById('dataHora').textContent = agora.toLocaleDateString('pt-BR', opcoes);
}
setInterval(atualizarDataHora, 1000);
atualizarDataHora();

document.getElementById('cpf').addEventListener('input', function() {
    this.value = this.value.replace(/\\D/g, '');
});

document.getElementById('cpf').addEventListener('blur', async function() {
    const cpf = this.value.replace(/\\D/g, '');
    const info = document.getElementById('infoFunc');
    if (cpf.length === 11) {
        try {
            const res = await fetch('/api/buscar/' + cpf);
            const dados = await res.json();
            if (dados.encontrado) {
                info.textContent = '👤 ' + dados.nome;
                info.style.background = '#e8f5e9';
                info.style.color = '#2e7d32';
                info.style.display = 'block';
            } else {
                info.textContent = '⚠️ CPF NÃO cadastrado! Procure o administrador.';
                info.style.background = '#fff3cd';
                info.style.color = '#856404';
                info.style.display = 'block';
            }
        } catch(e) { info.style.display = 'none'; }
    } else {
        info.style.display = 'none';
    }
});

async function registrar(tipo) {
    const cpf = document.getElementById('cpf').value.replace(/\\D/g, '');
    if (!cpf || cpf.length !== 11) {
        mostrarMensagem('Digite um CPF válido com 11 números!', 'erro');
        return;
    }
    try {
        const res = await fetch('/api/bater_ponto', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({cpf: cpf, tipo: tipo, qr_code: QR_SEGREDO})
        });
        const dados = await res.json();
        if (res.ok) {
            mostrarMensagem(dados.mensagem, 'sucesso');
            document.getElementById('cpf').value = '';
            document.getElementById('infoFunc').style.display = 'none';
        } else {
            mostrarMensagem(dados.detail || 'Erro ao registrar', 'erro');
        }
    } catch(e) {
        mostrarMensagem('Erro de conexão com o servidor!\\nVerifique se está na mesma rede Wi-Fi.', 'erro');
    }
}

function mostrarMensagem(texto, tipo) {
    const msg = document.getElementById('mensagem');
    msg.textContent = texto;
    msg.className = 'mensagem ' + tipo;
    setTimeout(function() { msg.className = 'mensagem'; }, 7000);
}
</script>
</body>
</html>"""

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
.header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 18px; text-align: center; position: relative; }
.header h1 { font-size: 22px; }
.logout { position: absolute; right: 20px; top: 50%; transform: translateY(-50%); background: rgba(255,255,255,0.2); padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; border: 1px solid rgba(255,255,255,0.3); }
.logout:hover { background: rgba(255,255,255,0.3); }
.container { max-width: 1000px; margin: 20px auto; padding: 0 20px; }
.tabs { display: flex; gap: 5px; margin-bottom: 20px; flex-wrap: wrap; }
.tab { padding: 12px 18px; background: #ddd; border: none; border-radius: 8px 8px 0 0; cursor: pointer; font-weight: bold; font-size: 13px; }
.tab.ativo { background: white; color: #667eea; }
.painel { background: white; border-radius: 0 12px 12px 12px; padding: 25px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); display: none; }
.painel.ativo { display: block; }
h2 { color: #333; margin-bottom: 20px; font-size: 20px; }
input, select { width: 100%; padding: 12px; margin: 8px 0; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 14px; }
button { padding: 12px 24px; background: #667eea; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 14px; margin: 5px 5px 5px 0; }
button:hover { opacity: 0.9; }
.btn-success { background: #4CAF50; }
.btn-danger { background: #f44336; }
.btn-warning { background: #ff9800; }
table { width: 100%; border-collapse: collapse; margin-top: 15px; display: block; overflow-x: auto; }
th, td { padding: 10px; text-align: left; border-bottom: 1px solid #eee; font-size: 13px; white-space: nowrap; }
th { background: #f8f9fa; font-weight: bold; color: #555; }
.atrasado { color: #f44336; font-weight: bold; }
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
</style>
</head>
<body>
<div class="header">
<h1>⚙️ Painel Administrativo</h1>
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
<input type="text" id="nome" placeholder="Nome Completo">
<input type="text" id="cpfCad" placeholder="CPF (apenas números)" maxlength="11" inputmode="numeric">
<div class="horarios-grid">
<input type="text" id="hEntrada" placeholder="Horário Entrada (HH:MM:SS)" value="08:00:00">
<input type="text" id="hSaidaAlmoco" placeholder="Saída Almoço" value="12:00:00">
<input type="text" id="hRetornoAlmoco" placeholder="Retorno Almoço" value="13:00:00">
<input type="text" id="hSaida" placeholder="Horário Saída" value="18:00:00">
</div>
<button class="btn-success" onclick="cadastrar()">💾 Salvar Cadastro</button>
</div>

<div id="funcionarios" class="painel">
<h2>Funcionários Cadastrados</h2>
<button onclick="carregarFuncionarios()">🔄 Atualizar Lista</button>
<table><thead><tr><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Almoço</th><th>Retorno</th><th>Saída</th><th>Ação</th></tr></thead><tbody id="tbodyFunc"></tbody></table>
</div>

<div id="registros" class="painel">
<h2>Todos os Registros de Ponto</h2>
<button onclick="carregarRegistros()">🔄 Atualizar</button>
<table><thead><tr><th>Funcionário</th><th>CPF</th><th>Data</th><th>Hora</th><th>Tipo</th><th>Atrasado</th></tr></thead><tbody id="tbodyReg"></tbody></table>
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
<p><strong>URL Local (esta máquina):</strong></p>
<p id="urlLocal" style="font-weight:bold; color:#1565c0;"></p>
<p style="margin-top:10px; font-size:13px; color:#555;">Escaneie com a câmera do celular para abrir o registro de ponto.</p>
</div>
<div class="info-box">
<strong>📌 Dica:</strong> Para acessar de fora da clínica, use um serviço como o <strong>ngrok</strong> ou hospede em um servidor. Veja a aba "Configurações".
</div>
<button onclick="gerarQR()">🔄 Gerar/Atualizar QR Code</button>
<div id="qrImagem" style="margin-top:20px;"></div>
<p style="margin-top:10px; font-size:12px; color:#666;">Arquivo salvo em: <strong>static/qrcode_empresa.png</strong></p>
</div>

<div id="config" class="painel">
<h2>🔧 Configurações e Acesso Externo</h2>
<div class="card">
<h3 style="margin-bottom:10px;">📡 Acesso na mesma rede Wi-Fi</h3>
<p style="font-size:14px; line-height:1.6;">
1. No seu computador, abra o CMD e digite: <strong>ipconfig</strong><br>
2. Procure por "Endereço IPv4" (ex: 192.168.0.10)<br>
3. Os celulares devem acessar: <strong>http://SEU_IP:8000</strong><br>
4. Gere um novo QR code clicando no botão da aba "QR Code".
</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🌐 Acesso externo 24h com Ngrok (gratuito)</h3>
<p style="font-size:14px; line-height:1.6;">
1. Baixe o ngrok em: <strong>ngrok.com</strong><br>
2. Com o sistema rodando, abra outro CMD e digite:<br>
&nbsp;&nbsp;&nbsp;<code style="background:#eee; padding:3px 8px; border-radius:4px;">ngrok http 8000</code><br>
3. Ele dará uma URL como: <strong>https://abc123.ngrok.io</strong><br>
4. Use essa URL para gerar o QR code e acessar de QUALQUER lugar!
</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">⚡ Iniciar automaticamente</h3>
<p style="font-size:14px; line-height:1.6;">
Use o arquivo <strong>ligar.vbs</strong> para iniciar com um clique.<br>
Para iniciar com o Windows: crie um atalho do .vbs na pasta de inicialização.<br>
(Win+R → digite <strong>shell:startup</strong> → cole o atalho lá)
</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🔐 Login de Admin atual</h3>
<p style="font-size:14px;">
<strong>Usuário:</strong> admin<br>
<strong>Senha:</strong> admin123<br>
<small style="color:#666;">(Altere no início do arquivo sistema_ponto.py)</small>
</p>
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

document.getElementById('cpfCad').addEventListener('input', function() {
    this.value = this.value.replace(/\\D/g, '');
});

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
        horario_entrada: document.getElementById('hEntrada').value,
        horario_saida_almoco: document.getElementById('hSaidaAlmoco').value,
        horario_retorno_almoco: document.getElementById('hRetornoAlmoco').value,
        horario_saida: document.getElementById('hSaida').value
    };
    if(!dados.nome || !dados.cpf) { mostrarMsg('msgCadastro','Preencha nome e CPF!','erro'); return; }
    if(dados.cpf.length !== 11) { mostrarMsg('msgCadastro','CPF deve ter 11 dígitos!','erro'); return; }
    
    const res = await fetch('/api/funcionarios', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(dados)
    });
    if(res.ok) {
        mostrarMsg('msgCadastro','✅ Funcionário cadastrado!','sucesso');
        document.getElementById('nome').value='';
        document.getElementById('cpfCad').value='';
    } else {
        const err = await res.json();
        mostrarMsg('msgCadastro','❌ ' + (err.detail || 'Erro'),'erro');
    }
}

async function carregarFuncionarios() {
    const res = await fetch('/api/funcionarios');
    if(res.status === 401) { window.location.href = '/admin'; return; }
    const dados = await res.json();
    const tbody = document.getElementById('tbodyFunc');
    if(dados.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#999; padding:20px;">Nenhum funcionário cadastrado.</td></tr>';
        return;
    }
    tbody.innerHTML = dados.map(function(f) {
        return '<tr><td>'+f.id+'</td><td>'+f.nome+'</td><td>'+f.cpf+'</td><td>'+f.horario_entrada+'</td><td>'+f.horario_saida_almoco+'</td><td>'+f.horario_retorno_almoco+'</td><td>'+f.horario_saida+'</td><td><button class="btn-danger" onclick="excluir('+f.id+')">Excluir</button></td></tr>';
    }).join('');
}

async function excluir(id) {
    if(confirm('Tem CERTEZA? Todos os registros deste funcionário serão APAGADOS!')) {
        const res = await fetch('/api/funcionarios/' + id, {method:'DELETE'});
        if(res.status === 401) { window.location.href = '/admin'; return; }
        carregarFuncionarios();
    }
}

async function carregarRegistros() {
    const res = await fetch('/api/registros');
    if(res.status === 401) { window.location.href = '/admin'; return; }
    const dados = await res.json();
    const tbody = document.getElementById('tbodyReg');
    if(dados.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:#999; padding:20px;">Nenhum registro ainda.</td></tr>';
        return;
    }
    tbody.innerHTML = dados.map(function(r) {
        const classeTipo = 'tipo-' + r.tipo.toLowerCase().replace(/_/g, '-');
        return '<tr><td>'+r.nome+'</td><td>'+r.cpf+'</td><td>'+r.data+'</td><td>'+r.hora+'</td><td class="'+classeTipo+'">'+r.tipo_formatado+'</td><td class="'+(r.atrasado?'atrasado':'')+'">'+(r.atrasado?'⚠️ SIM':'✅ NÃO')+'</td></tr>';
    }).join('');
}

async function carregarSelect() {
    const res = await fetch('/api/funcionarios');
    if(res.status === 401) { window.location.href = '/admin'; return; }
    const dados = await res.json();
    const sel = document.getElementById('selectFunc');
    if(dados.length === 0) { sel.innerHTML = '<option value="">Cadastre funcionários primeiro</option>'; return; }
    sel.innerHTML = dados.map(function(f) { return '<option value="'+f.id+'">'+f.nome+' ('+f.cpf+')</option>'; }).join('');
}

function gerarGeral() {
    const mes = document.getElementById('mesAno').value;
    if(!mes) { alert('Selecione o mês/ano!'); return; }
    window.open('/api/pdf/geral?mes=' + mes, '_blank');
}

function gerarIndividual() {
    const id = document.getElementById('selectFunc').value;
    const mes = document.getElementById('mesAnoFunc').value;
    if(!id || !mes) { alert('Preencha todos os campos!'); return; }
    window.open('/api/pdf/funcionario/' + id + '?mes=' + mes, '_blank');
}

async function gerarQR() {
    const res = await fetch('/api/gerar_qrcode');
    if(res.status === 401) { window.location.href = '/admin'; return; }
    const dados = await res.json();
    if(dados.detail) {
        document.getElementById('qrImagem').innerHTML = '<p style="color:#f44336;">❌ ' + dados.detail + '</p>';
    } else {
        document.getElementById('qrImagem').innerHTML = '<img src="'+dados.caminho+'?t='+Date.now()+'" style="max-width:250px; border:2px solid #ddd; border-radius:8px;">';
    }
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
        
        # Página principal - funcionário bate ponto
        if caminho == "/" or caminho == "/index.html":
            responder_html(self, HTML_PONTO)
            return
        
        # Painel admin - verifica login
        if caminho == "/admin":
            if verificar_login(self):
                responder_html(self, HTML_ADMIN)
            else:
                responder_html(self, HTML_LOGIN)
            return
        
        # Página de login
        if caminho == "/login":
            responder_html(self, HTML_LOGIN)
            return
        
        # Arquivos static (logo, qrcode)
        if caminho.startswith("/static/"):
            nome_arquivo = caminho.replace("/static/", "")
            caminho_completo = os.path.join("static", nome_arquivo)
            if os.path.exists(caminho_completo):
                self.send_response(200)
                if nome_arquivo.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                elif nome_arquivo.endswith(".jpg") or nome_arquivo.endswith(".jpeg"):
                    self.send_header("Content-Type", "image/jpeg")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                with open(caminho_completo, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
            return
        
        # API - Buscar funcionário por CPF
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
                    responder_json(self, {"encontrado": True, "nome": func["nome"]})
                else:
                    responder_json(self, {"encontrado": False})
            except Exception as e:
                responder_json(self, {"detail": "Erro no banco de dados"}, status=500)
            return
        
        # Rotas que precisam de login admin
        rotas_admin = [
            "/api/funcionarios", "/api/registros", "/api/gerar_qrcode",
            "/api/pdf/geral", "/api/logout"
        ]
        
        precisa_login = (
            caminho in rotas_admin or
            caminho.startswith("/api/funcionarios/") or
            caminho.startswith("/api/pdf/funcionario/")
        )
        
        if precisa_login and not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado. Faça login."}, status=401)
            return
        
        # API - Listar funcionários
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
                responder_json(self, {"detail": "Erro no banco de dados"}, status=500)
            return
        
        # API - Listar registros
        if caminho == "/api/registros":
            try:
                conn = get_db()
                regs = conn.execute("""
                    SELECT r.*, f.nome, f.cpf 
                    FROM registros_ponto r 
                    JOIN funcionarios f ON r.funcionario_id = f.id 
                    ORDER BY r.data_hora DESC
                """).fetchall()
                conn.close()
                resultado = []
                for r in regs:
                    dh = datetime.strptime(r["data_hora"], "%Y-%m-%d %H:%M:%S")
                    tipo_fmt = r["tipo"].replace("_", " ")
                    resultado.append({
                        "nome": r["nome"], "cpf": r["cpf"],
                        "data": dh.strftime("%d/%m/%Y"),
                        "hora": dh.strftime("%H:%M:%S"),
                        "tipo": r["tipo"], "tipo_formatado": tipo_fmt,
                        "atrasado": bool(r["atrasado"])
                    })
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": "Erro no banco de dados"}, status=500)
            return
        
        # API - Logout
        if caminho == "/api/logout":
            responder_json(self, {"status": "ok"}, cookies_extra=['sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;'])
            return
        
        # API - Gerar QR Code
        if caminho == "/api/gerar_qrcode":
            try:
                import qrcode
                # Detecta a URL correta baseada no Host do cabeçalho
                host = self.headers.get("Host", f"localhost:{PORTA}")
                url = f"http://{host}/"
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                caminho_qr = "static/qrcode_empresa.png"
                img.save(caminho_qr)
                responder_json(self, {"status": "ok", "caminho": "/static/qrcode_empresa.png", "url": url})
            except ImportError:
                responder_json(self, {"detail": "Biblioteca qrcode não instalada. Execute: pip install qrcode pillow"}, status=500)
            except Exception as e:
                responder_json(self, {"detail": f"Erro ao gerar QR Code: {str(e)}"}, status=500)
            return
        
        # API - PDF Geral
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
                self.end_headers()
                self.wfile.write(pdf_bytes.getvalue())
            except ImportError:
                responder_html(self, "<h1>Erro</h1><p>Biblioteca reportlab não instalada.<br>Execute: pip install reportlab</p>")
            except Exception as e:
                responder_json(self, {"detail": f"Erro ao gerar PDF: {str(e)}"}, status=500)
            return
        
        # API - PDF Individual
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
                self.send_header("Content-Disposition", f"attachment; filename=Relatorio_Funcionario_{func_id}_{mes}.pdf")
                self.end_headers()
                self.wfile.write(pdf_bytes.getvalue())
            except ImportError:
                responder_html(self, "<h1>Erro</h1><p>Biblioteca reportlab não instalada.<br>Execute: pip install reportlab</p>")
            except Exception as e:
                responder_json(self, {"detail": f"Erro ao gerar PDF: {str(e)}"}, status=500)
            return
        
        # 404
        self.send_response(404)
        self.end_headers()
    
    def do_POST(self):
        url = urlparse(self.path)
        caminho = url.path
        
        tamanho = int(self.headers.get("Content-Length", 0))
        corpo = self.rfile.read(tamanho).decode("utf-8")
        
        try:
            dados = json.loads(corpo) if corpo else {}
        except:
            dados = {}
        
        # API - Login admin
        if caminho == "/api/login":
            usuario = dados.get("usuario", "")
            senha = dados.get("senha", "")
            
            if usuario == ADMIN_USUARIO and senha == ADMIN_SENHA:
                token = gerar_sessao()
                expira = datetime.now() + timedelta(hours=8)
                sessoes_admin[token] = expira
                cookie = f"sessao_admin={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
                responder_json(self, {"status": "ok", "mensagem": "Login realizado"}, cookies_extra=[cookie])
            else:
                responder_json(self, {"detail": "Usuário ou senha incorretos!"}, status=401)
            return
        
        # API - Bater ponto (NÃO precisa de login)
        if caminho == "/api/bater_ponto":
            cpf = formatar_cpf(dados.get("cpf", ""))
            tipo = dados.get("tipo", "ENTRADA")
            qr_code = dados.get("qr_code", "")
            
            if qr_code != SEGREDO_QR:
                responder_json(self, {"detail": "QR Code inválido!"}, status=400)
                return
            
            if tipo not in TIPOS_REGISTRO:
                responder_json(self, {"detail": "Tipo de registro inválido!"}, status=400)
                return
            
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido! Deve ter 11 dígitos."}, status=400)
                return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                
                if not func:
                    conn.close()
                    responder_json(self, {"detail": "CPF não cadastrado! Procure o administrador."}, status=404)
                    return
                
                agora = datetime.now()
                data_hora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
                hora_str = agora.strftime("%H:%M:%S")
                
                # Verifica atraso baseado no tipo de registro
                atrasado = 0
                if tipo == "ENTRADA":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"]) else 0
                elif tipo == "SAIDA_ALMOCO":
                    # Para saída almoço: NÃO é atrasado se sair no horário ou depois
                    # Só é "problema" se sair ANTES, mas não contamos como atraso
                    atrasado = 0
                elif tipo == "RETORNO_ALMOCO":
                    atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"]) else 0
                elif tipo == "SAIDA":
                    # Para saída: só é atrasado se sair ANTES do horário (sair mais cedo)
                    # Usamos a lógica inversa: verifica se horário padrão > hora registro
                    atrasado = 1 if verificar_atraso(func["horario_saida"], hora_str) else 0
                
                conn.execute("""
                    INSERT INTO registros_ponto (funcionario_id, data_hora, tipo, atrasado)
                    VALUES (?, ?, ?, ?)
                """, (func["id"], data_hora_str, tipo, atrasado))
                conn.commit()
                conn.close()
                
                tipo_info = TIPOS_REGISTRO[tipo]
                msg = f"{tipo_info['icone']} {tipo_info['label']} registrada!\n"
                msg += f"👤 Funcionário: {func['nome']}\n"
                msg += f"📅 Data: {agora.strftime('%d/%m/%Y')}\n"
                msg += f"⏰ Hora: {hora_str}"
                if atrasado:
                    msg += "\n⚠️ ATENÇÃO: Atraso detectado!"
                
                responder_json(self, {"mensagem": msg})
            except Exception as e:
                responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, status=500)
            return
        
        # Daqui para baixo, precisa de login
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401)
            return
        
        # API - Criar funcionário
        if caminho == "/api/funcionarios":
            nome = dados.get("nome", "").strip()
            cpf = formatar_cpf(dados.get("cpf", ""))
            
            if not nome or not cpf:
                responder_json(self, {"detail": "Preencha nome e CPF!"}, status=400)
                return
            
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF deve ter 11 dígitos!"}, status=400)
                return
            
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
                """, (
                    nome, cpf,
                    dados.get("horario_entrada", "08:00:00"),
                    dados.get("horario_saida_almoco", "12:00:00"),
                    dados.get("horario_retorno_almoco", "13:00:00"),
                    dados.get("horario_saida", "18:00:00")
                ))
                conn.commit()
                novo_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                conn.close()
                
                responder_json(self, {"status": "ok", "id": novo_id})
            except Exception as e:
                responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, status=500)
            return
        
        responder_json(self, {"detail": "Rota não encontrada"}, status=404)
    
    def do_DELETE(self):
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
                responder_json(self, {"status": "ok"})
            except Exception as e:
                responder_json(self, {"detail": f"Erro no banco de dados: {str(e)}"}, status=500)
            return
        
        responder_json(self, {"detail": "Rota não encontrada"}, status=404)

# ===================== GERAÇÃO DE PDF =====================
def gerar_pdf_geral(mes):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    
    ano, mes_num = map(int, mes.split("-"))
    conn = get_db()
    funcionarios = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    largura, altura = A4
    dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    
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
        c.drawString(100, 400, "Funcionário não encontrado")
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
    dias_semana = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    
    desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors)
    
    conn.close()
    c.save()
    buffer.seek(0)
    return buffer

def desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors):
    # Cabeçalho
    c.setFillColor(colors.HexColor("#667eea"))
    c.rect(0, altura - 80, largura, 80, fill=True, stroke=False)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, altura - 50, "RELATÓRIO DE FOLHA PONTO")
    c.setFont("Helvetica", 10)
    c.drawString(40, altura - 68, "Mês/Ano: " + mes)
    
    # Dados funcionário
    y = altura - 110
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(40, y, "Funcionário: " + func["nome"])
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "CPF: " + func["cpf"])
    c.drawString(200, y, "Entrada: " + func["horario_entrada"])
    c.drawString(320, y, "Saída Almoço: " + func["horario_saida_almoco"])
    y -= 14
    c.drawString(200, y, "Retorno: " + func["horario_retorno_almoco"])
    c.drawString(320, y, "Saída: " + func["horario_saida"])
    y -= 25
    
    # Cabeçalho tabela
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(colors.HexColor("#f0f0f0"))
    c.rect(40, y - 14, largura - 80, 18, fill=True, stroke=False)
    c.setFillColor(colors.black)
    c.drawString(50, y - 9, "DATA")
    c.drawString(130, y - 9, "HORA EXATA")
    c.drawString(220, y - 9, "TIPO")
    c.drawString(350, y - 9, "ATRASO")
    c.drawString(440, y - 9, "DIA SEMANA")
    y -= 32
    
    c.setFont("Helvetica", 9)
    contagem = {"ENTRADA": 0, "SAIDA_ALMOCO": 0, "RETORNO_ALMOCO": 0, "SAIDA": 0}
    total_atrasos = 0
    
    for reg in registros:
        if y < 80:
            c.showPage()
            y = altura - 50
            c.setFont("Helvetica", 9)
        
        dh = datetime.strptime(reg["data_hora"], "%Y-%m-%d %H:%M:%S")
        data = dh.strftime("%d/%m/%Y")
        hora = dh.strftime("%H:%M:%S")
        dia_semana = dias_semana[dh.weekday()]
        tipo_fmt = reg["tipo"].replace("_", " ")
        
        c.drawString(50, y, data)
        c.drawString(130, y, hora)
        
        if reg["tipo"] == "ENTRADA":
            c.setFillColor(colors.HexColor("#4CAF50"))
        elif reg["tipo"] == "SAIDA_ALMOCO":
            c.setFillColor(colors.HexColor("#ff9800"))
        elif reg["tipo"] == "RETORNO_ALMOCO":
            c.setFillColor(colors.HexColor("#2196F3"))
        else:
            c.setFillColor(colors.HexColor("#f44336"))
        
        contagem[reg["tipo"]] += 1
        c.drawString(220, y, tipo_fmt)
        c.setFillColor(colors.black)
        
        if reg["atrasado"]:
            c.setFillColor(colors.HexColor("#f44336"))
            c.drawString(350, y, "SIM")
            c.setFillColor(colors.black)
            total_atrasos += 1
        else:
            c.drawString(350, y, "Não")
        
        c.drawString(440, y, dia_semana)
        y -= 16
    
    # Resumo
    if y < 120:
        c.showPage()
        y = altura - 50
    
    y -= 10
    c.setFillColor(colors.HexColor("#f5f5f5"))
    c.rect(40, y - 55, largura - 80, 65, fill=True, stroke=False)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y - 15, "📊 RESUMO DO MÊS:")
    c.setFont("Helvetica", 9)
    c.drawString(50, y - 35, "Total: " + str(len(registros)) + " registros")
    c.drawString(160, y - 35, "✅ Entradas: " + str(contagem["ENTRADA"]))
    c.drawString(270, y - 35, "🍽️ Saída Almoço: " + str(contagem["SAIDA_ALMOCO"]))
    c.drawString(50, y - 50, "↩️ Retorno: " + str(contagem["RETORNO_ALMOCO"]))
    c.drawString(160, y - 50, "🚪 Saídas: " + str(contagem["SAIDA"]))
    c.drawString(270, y - 50, "⚠️ Atrasos: " + str(total_atrasos))
    
    c.showPage()

# ===================== INICIAR SERVIDOR =====================
if __name__ == "__main__":
    print("=" * 65)
    print("   🚀 SISTEMA DE PONTO v2.1 - FUNCIONANDO!")
    print("=" * 65)
    print(f"📱 Página do funcionário:  http://localhost:{PORTA}")
    print(f"🔐 Login Admin:            http://localhost:{PORTA}/admin")
    print(f"👤 Usuário: {ADMIN_USUARIO}   |   Senha: {ADMIN_SENHA}")
    print("=" * 65)
    print("📝 4 opções de registro:")
    print("   ✅ ENTRADA  |  🍽️ SAÍDA ALMOÇO  |  ↩️ RETORNO ALMOÇO  |  🚪 SAÍDA")
    print("=" * 65)
    print("🌐 Acesso na rede WI-FI: http://SEU_IP:8000")
    print("   (descubra seu IP com o comando: ipconfig)")
    print("=" * 65)
    print("\nServidor rodando... Aperte Ctrl+C para parar.\n")
    
    try:
        servidor = HTTPServer(("0.0.0.0", PORTA), ServidorPonto)
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor parado.")
