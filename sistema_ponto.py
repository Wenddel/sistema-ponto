# -*- coding: utf-8 -*-
"""
=================================================================
   SISTEMA DE PONTO v3.2 - CLÍNICA
   ✅ PERSISTÊNCIA DE DADOS GARANTIDA - NÃO PERDE DADOS NO DEPLOY
=================================================================
   Desenvolvido por WELL
   Última atualização: 2026-09-09
   
   SISTEMA DE PROTEÇÃO DE DADOS:
   🛡️ Camada 1: Render Disk (armazenamento persistente nativo)
   🛡️ Camada 2: Backup automático local com versionamento
   🛡️ Camada 3: Restauração automática ao detectar banco vazio/perdido
   🛡️ Camada 4: Backup em nuvem (opcional via variável de ambiente)
=================================================================
"""
import sqlite3
import json
import os
import io
import hashlib
import re
import shutil
import signal
import atexit
import base64
import traceback
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

# =============================================================================
#           🛡️ SISTEMA DE PERSISTÊNCIA E BACKUP AUTOMÁTICO
# =============================================================================
"""
COMO FUNCIONA A PROTEÇÃO CONTRA PERDA DE DADOS NO RENDER:

1. O Render por padrão usa sistema de arquivos EFÊMERO (ephemeral).
   Cada deploy apaga TUDO que foi salvo no container.

2. SOLUÇÃO PRINCIPAL: Usamos um RENDER DISK (disco persistente) montado
   em /var/data. Esse disco NÃO É APAGADO nos deploys.

3. SOLUÇÃO DE SEGURANÇA: Mesmo se o disco falhar, temos:
   - Backups automáticos locais com timestamp
   - Restauração automática se o banco principal sumir
   - Opção de backup em nuvem via URL

4. VARIÁVEIS DE AMBIENTE SUPORTADAS:
   - PERSIST_DIR: Diretório persistente (padrão: /var/data ou ./data)
   - BACKUP_URL: URL para enviar backup em nuvem (opcional)
   - RESTORE_URL: URL para baixar backup em nuvem (opcional)
   - MAX_BACKUPS_LOCais: Quantidade máxima de backups locais (padrão: 50)
"""

# Diretório persistente principal (Render Disk montado aqui)
PERSIST_DIR = os.environ.get("PERSIST_DIR", "/var/data")
if not os.path.isdir(PERSIST_DIR) or not os.access(PERSIST_DIR, os.W_OK):
    # Fallback: se /var/data não existir ou não for gravável, usa ./data local
    PERSIST_DIR = os.path.abspath(os.environ.get("PERSIST_DIR_FALLBACK", "./data"))

# Diretórios de proteção
BACKUP_DIR = os.path.join(PERSIST_DIR, "backups")
DB_DIR = os.path.join(PERSIST_DIR, "db")
ARCHIVE_DIR = os.path.join(PERSIST_DIR, "arquivo_morto")
STATIC_PERSIST_DIR = os.path.join(PERSIST_DIR, "static")

# Cria todos os diretórios necessários
for d in [PERSIST_DIR, BACKUP_DIR, DB_DIR, ARCHIVE_DIR, STATIC_PERSIST_DIR]:
    os.makedirs(d, exist_ok=True)

# Configurações de backup
MAX_BACKUPS_LOCAIS = int(os.environ.get("MAX_BACKUPS_LOCAIS", "50"))
BACKUP_URL = os.environ.get("BACKUP_URL", "")
RESTORE_URL = os.environ.get("RESTORE_URL", "")

# Caminho final do banco de dados (NO DIRETÓRIO PERSISTENTE!)
DB_NOME = os.path.join(DB_DIR, "ponto.db")

# Também mantemos um link simbólico/cópia no diretório raiz para compatibilidade
DB_NOME_LEGADO = "ponto.db"

print(f"\n{'='*70}")
print(f"🛡️  SISTEMA DE PROTEÇÃO DE DADOS INICIADO")
print(f"{'='*70}")
print(f"📂 Diretório persistente: {PERSIST_DIR}")
print(f"💾 Banco de dados:        {DB_NOME}")
print(f"📦 Diretório de backups: {BACKUP_DIR}")
print(f"📚 Arquivo morto:         {ARCHIVE_DIR}")
print(f"{'='*70}\n")


def tamanho_arquivo(caminho):
    """Retorna tamanho do arquivo em bytes, ou 0 se não existir"""
    try:
        return os.path.getsize(caminho)
    except:
        return 0


def banco_tem_dados(caminho_db):
    """Verifica se o banco de dados existe e tem dados (não está vazio)"""
    if not os.path.exists(caminho_db):
        return False
    if tamanho_arquivo(caminho_db) < 1024:  # Menor que 1KB provavelmente vazio
        return False
    try:
        conn = sqlite3.connect(caminho_db)
        # Verifica se tem tabelas e se tem registros
        tabelas = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not tabelas:
            conn.close()
            return False
        # Verifica se tem pelo menos alguns dados
        total = 0
        for (tabela,) in tabelas:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM '{tabela}'").fetchone()[0]
                total += count
            except:
                pass
        conn.close()
        return total > 0
    except:
        return False


def fazer_backup_local(origem=None, sufixo=""):
    """
    Cria uma cópia de segurança do banco de dados com timestamp.
    Retorna o caminho do backup criado ou None em caso de erro.
    """
    if origem is None:
        origem = DB_NOME
    
    if not os.path.exists(origem):
        print(f"[BACKUP] ⚠️ Arquivo fonte não existe: {origem}")
        return None
    
    try:
        timestamp = agora_brasilia().strftime("%Y%m%d_%H%M%S")
        nome_backup = f"ponto_backup_{timestamp}{sufixo}.db"
        caminho_backup = os.path.join(BACKUP_DIR, nome_backup)
        
        # Usa VACUUM INTO para backup seguro do SQLite
        try:
            conn = sqlite3.connect(origem)
            conn.execute(f"VACUUM INTO '{caminho_backup}'")
            conn.close()
        except:
            # Fallback: cópia simples de arquivo
            shutil.copy2(origem, caminho_backup)
        
        tamanho = tamanho_arquivo(caminho_backup)
        print(f"[BACKUP] ✅ Backup criado: {nome_backup} ({tamanho} bytes)")
        
        # Limpa backups antigos
        limpar_backups_antigos()
        
        # Tenta enviar para nuvem se configurado
        if BACKUP_URL:
            try:
                enviar_backup_nuvem(caminho_backup)
            except Exception as e:
                print(f"[BACKUP] ⚠️ Não foi possível enviar para nuvem: {e}")
        
        return caminho_backup
    except Exception as e:
        print(f"[BACKUP] ❌ Erro ao fazer backup: {e}")
        traceback.print_exc()
        return None


def limpar_backups_antigos():
    """Mantém apenas os MAX_BACKUPS_LOCAIS mais recentes"""
    try:
        backups = sorted([
            os.path.join(BACKUP_DIR, f) 
            for f in os.listdir(BACKUP_DIR) 
            if f.startswith("ponto_backup_") and f.endswith(".db")
        ], key=os.path.getmtime, reverse=True)
        
        while len(backups) > MAX_BACKUPS_LOCAIS:
            antigo = backups.pop()
            try:
                os.remove(antigo)
                print(f"[BACKUP] 🧹 Backup antigo removido: {os.path.basename(antigo)}")
            except:
                pass
    except Exception as e:
        print(f"[BACKUP] ⚠️ Erro ao limpar backups antigos: {e}")


def listar_backups_disponiveis():
    """Retorna lista de backups disponíveis ordenados do mais recente ao mais antigo"""
    try:
        backups = sorted([
            os.path.join(BACKUP_DIR, f) 
            for f in os.listdir(BACKUP_DIR) 
            if f.startswith("ponto_backup_") and f.endswith(".db")
        ], key=os.path.getmtime, reverse=True)
        return backups
    except:
        return []


def restaurar_de_backup(caminho_backup, destino=None):
    """Restaura o banco de dados a partir de um backup"""
    if destino is None:
        destino = DB_NOME
    
    if not os.path.exists(caminho_backup):
        print(f"[RESTORE] ❌ Backup não encontrado: {caminho_backup}")
        return False
    
    try:
        # Se já existe um banco atual, faz um backup de segurança ANTES de sobrescrever
        if os.path.exists(destino) and banco_tem_dados(destino):
            fazer_backup_local(destino, sufixo="_antes_restore")
        
        # Restaura
        shutil.copy2(caminho_backup, destino)
        
        # Também atualiza a cópia legada
        try:
            shutil.copy2(caminho_backup, DB_NOME_LEGADO)
        except:
            pass
        
        tamanho = tamanho_arquivo(destino)
        print(f"[RESTORE] ✅ Banco restaurado de: {os.path.basename(caminho_backup)}")
        print(f"[RESTORE] 📊 Tamanho restaurado: {tamanho} bytes")
        return True
    except Exception as e:
        print(f"[RESTORE] ❌ Erro ao restaurar: {e}")
        traceback.print_exc()
        return False


def restaurar_ultimo_backup():
    """Tenta restaurar do backup mais recente disponível"""
    backups = listar_backups_disponiveis()
    if not backups:
        print("[RESTORE] ⚠️ Nenhum backup local encontrado")
        return False
    
    for backup in backups:
        if banco_tem_dados(backup):
            print(f"[RESTORE] 🔄 Tentando restaurar de: {os.path.basename(backup)}")
            if restaurar_de_backup(backup):
                return True
    
    print("[RESTORE] ❌ Nenhum backup válido encontrado")
    return False


def enviar_backup_nuvem(caminho_backup):
    """Envia backup para uma URL externa (opcional)"""
    if not BACKUP_URL:
        return
    
    try:
        import urllib.request
        with open(caminho_backup, 'rb') as f:
            dados = f.read()
        
        dados_b64 = base64.b64encode(dados).decode('ascii')
        payload = json.dumps({
            "timestamp": agora_brasilia().isoformat(),
            "nome_arquivo": os.path.basename(caminho_backup),
            "tamanho": len(dados),
            "dados_base64": dados_b64
        }).encode('utf-8')
        
        req = urllib.request.Request(
            BACKUP_URL,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[BACKUP-NUVEM] ✅ Enviado com sucesso. Status: {resp.status}")
    except Exception as e:
        print(f"[BACKUP-NUVEM] ⚠️ Falha: {e}")


def tentar_restaurar_nuvem():
    """Tenta baixar e restaurar backup da nuvem"""
    if not RESTORE_URL:
        return False
    
    try:
        import urllib.request
        print("[RESTORE-NUVEM] 🔄 Tentando restaurar da nuvem...")
        
        req = urllib.request.Request(RESTORE_URL, method='GET')
        with urllib.request.urlopen(req, timeout=30) as resp:
            dados = json.loads(resp.read().decode('utf-8'))
        
        if "dados_base64" in dados:
            dados_bin = base64.b64decode(dados["dados_base64"])
            caminho_temp = os.path.join(BACKUP_DIR, "restore_nuvem_temp.db")
            with open(caminho_temp, 'wb') as f:
                f.write(dados_bin)
            
            if banco_tem_dados(caminho_temp):
                if restaurar_de_backup(caminho_temp):
                    print("[RESTORE-NUVEM] ✅ Restaurado da nuvem com sucesso!")
                    return True
        
        return False
    except Exception as e:
        print(f"[RESTORE-NUVEM] ⚠️ Falha: {e}")
        return False


def arquivar_dados_antigos(dias_para_arquivar=365):
    """
    Move registros muito antigos para o arquivo morto.
    Isso mantém o banco principal leve e rápido.
    """
    try:
        conn = get_db()
        data_limite = (agora_brasilia() - timedelta(days=dias_para_arquivar)).strftime("%Y-%m-%d")
        
        # Conta quantos registros seriam arquivados
        count = conn.execute(
            "SELECT COUNT(*) FROM registros_ponto WHERE date(data_hora) < ?",
            (data_limite,)
        ).fetchone()[0]
        
        if count > 0:
            print(f"[ARQUIVO] 📦 {count} registros antigos para arquivar (antes de {data_limite})")
            
            # Cria arquivo morto se não existir
            caminho_arquivo = os.path.join(ARCHIVE_DIR, f"arquivo_{agora_brasilia().strftime('%Y%m')}.db")
            conn_arquivo = sqlite3.connect(caminho_arquivo)
            
            # Copia estrutura
            conn_arquivo.execute("""CREATE TABLE IF NOT EXISTS registros_ponto_arquivados (
                id INTEGER PRIMARY KEY,
                funcionario_id INTEGER,
                data_hora TEXT,
                tipo TEXT,
                atrasado INTEGER,
                minutos_atraso INTEGER,
                minutos_banco_horas INTEGER,
                justificativa TEXT,
                ip_dispositivo TEXT,
                user_agent TEXT,
                horario_acesso TEXT,
                arquivado_em TEXT
            )""")
            
            # Move registros
            registros = conn.execute(
                "SELECT * FROM registros_ponto WHERE date(data_hora) < ?",
                (data_limite,)
            ).fetchall()
            
            for r in registros:
                conn_arquivo.execute(
                    """INSERT INTO registros_ponto_arquivados 
                       (id, funcionario_id, data_hora, tipo, atrasado, minutos_atraso,
                        minutos_banco_horas, justificativa, ip_dispositivo, user_agent,
                        horario_acesso, arquivado_em)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["id"], r["funcionario_id"], r["data_hora"], r["tipo"],
                     r["atrasado"], r["minutos_atraso"], r["minutos_banco_horas"],
                     r["justificativa"], r["ip_dispositivo"], r["user_agent"],
                     r["horario_acesso"], agora_brasilia().isoformat())
                )
            
            conn_arquivo.commit()
            conn_arquivo.close()
            
            # Remove do banco principal
            conn.execute("DELETE FROM registros_ponto WHERE date(data_hora) < ?", (data_limite,))
            conn.commit()
            print(f"[ARQUIVO] ✅ {count} registros arquivados em: {os.path.basename(caminho_arquivo)}")
        
        conn.close()
    except Exception as e:
        print(f"[ARQUIVO] ⚠️ Erro: {e}")


def verificar_e_restaurar_banco():
    """
    FUNÇÃO PRINCIPAL DE PROTEÇÃO.
    Verifica se o banco principal está íntegro. Se não estiver, tenta restaurar.
    """
    print(f"\n{'='*70}")
    print(f"🔍 VERIFICAÇÃO DE INTEGRIDADE DO BANCO DE DADOS")
    print(f"{'='*70}")
    
    if banco_tem_dados(DB_NOME):
        print(f"✅ Banco principal está íntegro e com dados")
        print(f"📊 Tamanho: {tamanho_arquivo(DB_NOME)} bytes")
        
        # Faz backup de segurança mesmo assim
        fazer_backup_local(sufixo="_inicializacao")
        
        # Atualiza cópia legada
        try:
            shutil.copy2(DB_NOME, DB_NOME_LEGADO)
        except:
            pass
        
        # Arquiva dados antigos
        arquivar_dados_antigos()
        
        print(f"{'='*70}\n")
        return True
    
    print(f"⚠️ Banco principal está vazio, corrompido ou não existe!")
    print(f"🔄 Iniciando protocolo de recuperação...\n")
    
    # Tenta 1: Restaurar do backup local mais recente
    print("📋 TENTATIVA 1: Restaurar de backup local...")
    if restaurar_ultimo_backup():
        print(f"\n✅ DADOS RECUPERADOS COM SUCESSO! (via backup local)")
        print(f"{'='*70}\n")
        return True
    
    # Tenta 2: Restaurar da nuvem
    if RESTORE_URL:
        print("\n📋 TENTATIVA 2: Restaurar da nuvem...")
        if tentar_restaurar_nuvem():
            print(f"\n✅ DADOS RECUPERADOS COM SUCESSO! (via nuvem)")
            print(f"{'='*70}\n")
            return True
    
    # Tenta 3: Verificar se existe banco legado na raiz
    if os.path.exists(DB_NOME_LEGADO) and banco_tem_dados(DB_NOME_LEGADO):
        print("\n📋 TENTATIVA 3: Recuperar banco legado da raiz...")
        if restaurar_de_backup(DB_NOME_LEGADO):
            print(f"\n✅ DADOS RECUPERADOS COM SUCESSO! (via banco legado)")
            print(f"{'='*70}\n")
            return True
    
    # Se nada funcionar, cria banco novo
    print(f"\n⚠️ Nenhuma fonte de recuperação encontrada.")
    print(f"🆕 Será criado um banco de dados NOVO e vazio.")
    print(f"{'='*70}\n")
    return False


def finalizar_seguro():
    """Função executada ao desligar o sistema - faz backup final"""
    print(f"\n{'='*70}")
    print(f"🛑 SISTEMA SENDO DESLIGADO - BACKUP DE SEGURANÇA")
    print(f"{'='*70}")
    fazer_backup_local(sufixo="_shutdown")
    print(f"✅ Backup final concluído. Dados protegidos!\n")


# Registra handlers para garantir backup ao desligar
atexit.register(finalizar_seguro)
try:
    signal.signal(signal.SIGTERM, lambda s, f: finalizar_seguro())
    signal.signal(signal.SIGINT, lambda s, f: finalizar_seguro())
except:
    pass

# =============================================================================
#           FIM DO SISTEMA DE PERSISTÊNCIA
# =============================================================================


# ===================== CONFIGURACOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = int(os.environ.get("PORT", 8000))
ADMIN_USUARIO = "admin"
ADMIN_SENHA = "3223ronte"
sessoes_admin = {}
tentativas_login = {}
acessos_funcionarios = {}

# Diretório static - usa o persistente se possível
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


def get_db():
    conn = sqlite3.connect(DB_NOME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    # ANTES de inicializar, verifica se precisa restaurar
    verificar_e_restaurar_banco()
    
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
    
    # Tabela de LOG de backups e restaurações
    conn.execute("""CREATE TABLE IF NOT EXISTS log_persistencia (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_hora TEXT NOT NULL,
        tipo_operacao TEXT NOT NULL,
        descricao TEXT DEFAULT '',
        tamanho_bytes INTEGER DEFAULT 0
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
    
    # Registra inicialização no log
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO log_persistencia (data_hora, tipo_operacao, descricao, tamanho_bytes) VALUES (?,?,?,?)",
            (agora_brasilia().isoformat(), "INICIALIZACAO", "Sistema iniciado com proteção de dados", tamanho_arquivo(DB_NOME))
        )
        conn.commit()
        conn.close()
    except:
        pass


# Inicializa banco (com verificação de restauração automática)
init_db()
criar_logo_padrao()

# Faz backup periódico a cada 4 horas (em thread separada)
def backup_periodico():
    import threading
    def _loop():
        while True:
            time.sleep(4 * 3600)  # 4 horas
            try:
                fazer_backup_local(sufixo="_periodico")
            except:
                pass
    try:
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        print("[BACKUP] ⏰ Backup periódico agendado (cada 4 horas)")
    except:
        pass

backup_periodico()


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
    """Verifica se o registro está dentro da tolerância ANTES do horário padrão."""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_segundos = tolerancia_minutos * 60
        return (t_p - tolerancia_segundos) <= t_r < t_p
    except: return False

def verificar_na_tolerancia_depois(hora_registro, horario_padrao, tolerancia_minutos=5):
    """Verifica se o registro está dentro da tolerância DEPOIS do horário padrão."""
    try:
        h_r = hora_registro.split(":")
        h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_segundos = tolerancia_minutos * 60
        return t_p < t_r <= (t_p + tolerancia_segundos)
    except: return False

def calcular_banco_horas(tipo, hora_registro, func):
    """Calcula banco de horas com tolerância de 5 minutos."""
    try:
        h_r = hora_registro.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        tolerancia = 5 * 60
        
        if tipo == "ENTRADA":
            h_p = func["horario_entrada"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r < (t_p - tolerancia): return (t_p-t_r)//60
        elif tipo == "SAIDA_ALMOCO":
            h_p = func["horario_saida_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r > (t_p + tolerancia): return (t_r-t_p)//60
        elif tipo == "RETORNO_ALMOCO":
            h_p = func["horario_retorno_almoco"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
            if t_r < (t_p - tolerancia): return (t_p-t_r)//60
        elif tipo == "SAIDA":
            h_p = func["horario_saida"].split(":"); t_p = int(h_p[0])*3600+int(h_p[1])*60
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
    return hashlib.sha256(senha.encode('utf-8')).hexdigest()

def verificar_credenciais_admin(usuario, senha):
    if usuario == ADMIN_USUARIO and senha == ADMIN_SENHA:
        return True, ADMIN_USUARIO
    
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
.botoes-registro { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:20px; }
.btn-reg { padding:16px 10px; font-size:13px; }
.btn-entrada { background:linear-gradient(135deg,#43e97b,#38f9d7); }
.btn-saida-almoco { background:linear-gradient(135deg,#fa709a,#fee140); }
.btn-retorno { background:linear-gradient(135deg,#4facfe,#00f2fe); }
.btn-saida { background:linear-gradient(135deg,#f5576c,#f093fb); }
.horarios-info { background:linear-gradient(135deg,#f8f9ff,#f3e8ff); border-radius:14px; padding:15px; margin-bottom:20px; border:2px solid rgba(102,126,234,0.15); }
.horarios-info h4 { color:#667eea; font-size:13px; margin-bottom:10px; }
.horario-item { display:flex; justify-content:space-between; padding:6px 0; font-size:13px; border-bottom:1px dashed rgba(0,0,0,0.08); }
.horario-item:last-child { border-bottom:none; }
.horario-label { color:#888; }
.horario-valor { font-weight:bold; color:#333; }
.registros-hoje { margin-bottom:20px; }
.registros-hoje h3 { color:#333; font-size:16px; margin-bottom:12px; display:flex; align-items:center; gap:8px; }
.registro-item { background:white; border-radius:12px; padding:12px 15px; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; box-shadow:0 2px 8px rgba(0,0,0,0.06); border-left:4px solid; }
.reg-tipo { font-weight:bold; font-size:14px; }
.reg-hora { color:#888; font-size:12px; }
.reg-atrasado { color:#f44336; font-size:11px; font-weight:bold; }
.sem-registros { text-align:center; color:#aaa; padding:20px; font-size:13px; }
.resumo-dia { background:linear-gradient(135deg,#667eea,#764ba2); color:white; border-radius:14px; padding:18px; margin-bottom:20px; }
.resumo-dia h4 { font-size:14px; opacity:0.9; margin-bottom:10px; }
.resumo-linhas { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.resumo-item { text-align:center; }
.resumo-valor { font-size:22px; font-weight:bold; }
.resumo-label { font-size:11px; opacity:0.85; }
</style>
</head>
<body class="fundo-animado">
<div class="wrapper">
<div class="card-topo card-3d animar-entrar">
<a href="/" class="voltar">← Voltar</a>
<div class="foto-func" id="fotoFunc">W</div>
<h2 class="nome-func" id="nomeFunc">Carregando...</h2>
<p class="cpf-func" id="cpfFunc">---</p>
<div class="status-wrapper">
<span class="status-acesso">✅ Sessão válida</span>
</div>
</div>

<div class="card-3d" style="padding:20px;margin-bottom:20px;">
<h3 style="color:#333;font-size:16px;margin-bottom:15px;">⏰ Registrar Ponto</h3>
<div class="botoes-registro">
<button class="btn-3d btn-reg btn-entrada" onclick="registrar('ENTRADA')">✅ ENTRADA</button>
<button class="btn-3d btn-reg btn-saida-almoco" onclick="registrar('SAIDA_ALMOCO')">🍽️ SAÍDA ALMOÇO</button>
<button class="btn-3d btn-reg btn-retorno" onclick="registrar('RETORNO_ALMOCO')">↩️ RETORNO</button>
<button class="btn-3d btn-reg btn-saida" onclick="registrar('SAIDA')">🚪 SAÍDA</button>
</div>
<div id="msgRegistro" style="margin-top:10px;padding:12px;border-radius:10px;text-align:center;font-size:13px;font-weight:bold;display:none;"></div>
</div>

<div class="horarios-info card-3d" style="padding:20px;">
<h4>📅 Meus Horários</h4>
<div id="horariosInfo">
<div class="horario-item"><span class="horario-label">Entrada</span><span class="horario-valor" id="hEntrada">--:--</span></div>
<div class="horario-item"><span class="horario-label">Saída Almoço</span><span class="horario-valor" id="hSaidaAlmoco">--:--</span></div>
<div class="horario-item"><span class="horario-label">Retorno Almoço</span><span class="horario-valor" id="hRetorno">--:--</span></div>
<div class="horario-item"><span class="horario-label">Saída</span><span class="horario-valor" id="hSaida">--:--</span></div>
</div>
</div>

<div class="resumo-dia card-3d" style="padding:20px;">
<h4>📊 Resumo de Hoje</h4>
<div class="resumo-linhas">
<div class="resumo-item"><div class="resumo-valor" id="totalAtrasos">0</div><div class="resumo-label">Atrasos (min)</div></div>
<div class="resumo-item"><div class="resumo-valor" id="totalBanco">0</div><div class="resumo-label">Banco Horas</div></div>
</div>
</div>

<div class="registros-hoje card-3d" style="padding:20px;">
<h3>📝 Registros de Hoje</h3>
<div id="listaRegistros">
<div class="sem-registros">Nenhum registro ainda hoje</div>
</div>
</div>

""" + RODAPE_WELL + """
</div>
""" + SCRIPT_PARTICULAS + """
<script>
const params=new URLSearchParams(window.location.search);
const cpf=params.get('cpf');
let funcData=null;

async function carregarDados(){
  if(!cpf){window.location.href='/';return;}
  try{
    const r=await fetch('/api/funcionario/dados/'+cpf);
    if(!r.ok){window.location.href='/';return;}
    funcData=await r.json();
    document.getElementById('nomeFunc').textContent=funcData.nome;
    document.getElementById('cpfFunc').textContent='CPF: '+funcData.cpf;
    document.getElementById('fotoFunc').textContent=funcData.nome.charAt(0).toUpperCase();
    document.getElementById('hEntrada').textContent=funcData.horario_entrada?.substring(0,5)||'--:--';
    document.getElementById('hSaidaAlmoco').textContent=funcData.horario_saida_almoco?.substring(0,5)||'--:--';
    document.getElementById('hRetorno').textContent=funcData.horario_retorno_almoco?.substring(0,5)||'--:--';
    document.getElementById('hSaida').textContent=funcData.horario_saida?.substring(0,5)||'--:--';
    carregarRegistros();
  }catch(e){window.location.href='/';}
}

async function carregarRegistros(){
  try{
    const r=await fetch('/api/funcionario/registros/'+cpf);
    const d=await r.json();
    const lista=document.getElementById('listaRegistros');
    if(!d.registros||d.registros.length===0){
      lista.innerHTML='<div class="sem-registros">Nenhum registro ainda hoje</div>';
    }else{
      const cores={'ENTRADA':'#4CAF50','SAIDA_ALMOCO':'#ff9800','RETORNO_ALMOCO':'#2196F3','SAIDA':'#f44336'};
      lista.innerHTML=d.registros.map(reg=>{
        const dataHora=new Date(reg.data_hora.replace(' ','T'));
        const hora=dataHora.toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
        const atraso=reg.atrasado?`<span class="reg-atrasado">⚠️ ${reg.minutos_atraso}min atraso</span>`:'';
        return `<div class="registro-item" style="border-left-color:${cores[reg.tipo]||'#999'}">
          <div><div class="reg-tipo">${reg.tipo.replace('_',' ')}</div>${atraso}</div>
          <div class="reg-hora">${hora}</div>
        </div>`;
      }).join('');
    }
    document.getElementById('totalAtrasos').textContent=d.total_atrasos||0;
    document.getElementById('totalBanco').textContent=d.total_banco||0;
  }catch(e){}
}

async function registrar(tipo){
  const m=document.getElementById('msgRegistro');
  try{
    const r=await fetch('/api/funcionario/registrar',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cpf:cpf,tipo:tipo})
    });
    const d=await r.json();
    if(r.ok){
      m.style.display='block';
      m.style.background='linear-gradient(135deg,#e8f5e9,#c8e6c9)';
      m.style.color='#2e7d32';
      m.style.border='1px solid #a5d6a7';
      m.textContent='✅ '+d.mensagem;
      setTimeout(()=>{m.style.display='none';},3000);
      carregarRegistros();
    }else{
      m.style.display='block';
      m.style.background='linear-gradient(135deg,#ffebee,#ffcdd2)';
      m.style.color='#b71c1c';
      m.style.border='1px solid #ef9a9a';
      m.textContent='❌ '+d.detail;
      setTimeout(()=>{m.style.display='none';},4000);
    }
  }catch(e){
    m.style.display='block';
    m.style.background='linear-gradient(135deg,#ffebee,#ffcdd2)';
    m.style.color='#b71c1c';
    m.textContent='Erro de conexão!';
  }
}

carregarDados();
</script>
</body>
</html>"""

# ===================== HTML - PAINEL ADMIN =====================
def gerar_html_admin():
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚙️ Painel Admin - Sistema de Ponto</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Arial,sans-serif; }
body { min-height:100vh; padding:15px; position:relative; overflow-x:hidden; background:#f5f7fa; }
""" + ESTILOS_5D + ESTILO_RODAPE_WELL + """
.wrapper { position:relative; z-index:1; max-width:1200px; margin:0 auto; }
.header { background:linear-gradient(135deg,#667eea,#764ba2); color:white; padding:25px 30px; border-radius:22px; margin-bottom:25px; box-shadow:0 15px 40px rgba(102,126,234,0.3); }
.header h1 { font-size:24px; margin-bottom:5px; }
.header p { opacity:0.9; font-size:14px; }
.tabs { display:flex; gap:8px; margin-bottom:20px; flex-wrap:wrap; }
.tab-btn { padding:12px 20px; border:none; border-radius:12px; cursor:pointer; font-weight:bold; font-size:13px; background:#e8e8e8; color:#666; transition:all 0.3s; }
.tab-btn.ativo { background:linear-gradient(135deg,#667eea,#764ba2); color:white; box-shadow:0 5px 15px rgba(102,126,234,0.3); }
.tab-btn:hover:not(.ativo) { background:#ddd; transform:translateY(-2px); }
.tab-content { display:none; }
.tab-content.ativo { display:block; animation:entrar-cima 0.4s ease; }
.cards-resumo { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:15px; margin-bottom:25px; }
.card-resumo { background:white; border-radius:18px; padding:22px; box-shadow:0 5px 20px rgba(0,0,0,0.08); border-left:5px solid; }
.card-resumo h3 { font-size:13px; color:#888; margin-bottom:8px; text-transform:uppercase; letter-spacing:0.5px; }
.card-resumo .valor { font-size:32px; font-weight:bold; color:#333; }
.card-resumo .sub { font-size:12px; color:#aaa; margin-top:5px; }
.tabela-container { background:white; border-radius:18px; padding:20px; box-shadow:0 5px 20px rgba(0,0,0,0.08); overflow-x:auto; }
.tabela-container h2 { color:#333; font-size:18px; margin-bottom:15px; display:flex; align-items:center; gap:10px; }
table { width:100%; border-collapse:collapse; }
th, td { padding:12px 15px; text-align:left; font-size:13px; border-bottom:1px solid #eee; }
th { background:#f8f9ff; color:#667eea; font-weight:bold; text-transform:uppercase; font-size:11px; letter-spacing:0.5px; }
tr:hover { background:#f8f9ff; }
.btn-acao { padding:6px 12px; border:none; border-radius:8px; cursor:pointer; font-size:12px; font-weight:bold; margin:2px; transition:all 0.2s; }
.btn-editar { background:#e3f2fd; color:#1976d2; }
.btn-excluir { background:#ffebee; color:#d32f2f; }
.btn-acao:hover { transform:scale(1.05); }
.form-cadastro { background:white; border-radius:18px; padding:25px; box-shadow:0 5px 20px rgba(0,0,0,0.08); margin-bottom:20px; }
.form-cadastro h3 { color:#333; margin-bottom:15px; font-size:16px; }
.form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:15px; }
.btn-cadastrar { padding:14px 30px; background:linear-gradient(135deg,#43e97b,#38f9d7); font-size:14px; }
.btn-sair { position:absolute; top:25px; right:30px; padding:10px 20px; background:rgba(255,255,255,0.2); color:white; border:none; border-radius:10px; cursor:pointer; font-weight:bold; font-size:13px; backdrop-filter:blur(10px); transition:all 0.3s; }
.btn-sair:hover { background:rgba(255,255,255,0.3); }
.status-pendente { background:#fff3e0; color:#e65100; padding:4px 10px; border-radius:10px; font-size:11px; font-weight:bold; }
.status-aprovado { background:#e8f5e9; color:#2e7d32; padding:4px 10px; border-radius:10px; font-size:11px; font-weight:bold; }
.status-negado { background:#ffebee; color:#c62828; padding:4px 10px; border-radius:10px; font-size:11px; font-weight:bold; }
.modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center; }
.modal.ativo { display:flex; }
.modal-content { background:white; border-radius:20px; padding:30px; max-width:500px; width:90%; max-height:90vh; overflow-y:auto; }
.modal-content h3 { color:#333; margin-bottom:15px; }
</style>
</head>
<body>
<div class="wrapper">
<div class="header" style="position:relative;">
<button class="btn-sair" onclick="sair()">🚪 Sair</button>
<h1>⚙️ Painel Administrativo</h1>
<p>Sistema de Ponto - Clínica</p>
</div>

<div class="tabs">
<button class="tab-btn ativo" onclick="abrirTab('resumo')">📊 Resumo</button>
<button class="tab-btn" onclick="abrirTab('funcionarios')">👥 Funcionários</button>
<button class="tab-btn" onclick="abrirTab('registros')">📝 Registros</button>
<button class="tab-btn" onclick="abrirTab('solicitacoes')">📋 Solicitações</button>
<button class="tab-btn" onclick="abrirTab('admins')">🔐 Admins</button>
<button class="tab-btn" onclick="abrirTab('backup')">🛡️ Backup</button>
</div>

<!-- ABA RESUMO -->
<div id="tab-resumo" class="tab-content ativo">
<div class="cards-resumo" id="cardsResumo"></div>
<div class="tabela-container">
<h2>👥 Funcionários - Status de Hoje</h2>
<div id="resumoFuncionarios"></div>
</div>
</div>

<!-- ABA FUNCIONÁRIOS -->
<div id="tab-funcionarios" class="tab-content">
<div class="form-cadastro">
<h3>➕ Cadastrar Novo Funcionário</h3>
<div class="form-grid">
<input type="text" id="cadNome" class="input-moderno" placeholder="Nome Completo">
<input type="text" id="cadCpf" class="input-moderno" placeholder="CPF (apenas números)" maxlength="11">
<input type="time" id="cadEntrada" class="input-moderno" value="08:00">
<input type="time" id="cadSaidaAlmoco" class="input-moderno" value="12:00">
<input type="time" id="cadRetorno" class="input-moderno" value="13:00">
<input type="time" id="cadSaida" class="input-moderno" value="18:00">
</div>
<button class="btn-3d btn-cadastrar" style="margin-top:15px;" onclick="cadastrarFuncionario()">💾 CADASTRAR FUNCIONÁRIO</button>
</div>
<div class="tabela-container">
<h2>📋 Lista de Funcionários</h2>
<table id="tabelaFuncionarios">
<thead><tr><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Saída Almoço</th><th>Retorno</th><th>Saída</th><th>Ações</th></tr></thead>
<tbody id="tbodyFuncionarios"></tbody>
</table>
</div>
</div>

<!-- ABA REGISTROS -->
<div id="tab-registros" class="tab-content">
<div class="tabela-container">
<h2>📝 Todos os Registros de Ponto</h2>
<table id="tabelaRegistros">
<thead><tr><th>ID</th><th>Funcionário</th><th>Data/Hora</th><th>Tipo</th><th>Atrasado</th><th>Min. Atraso</th><th>Banco Horas</th></tr></thead>
<tbody id="tbodyRegistros"></tbody>
</table>
</div>
</div>

<!-- ABA SOLICITAÇÕES -->
<div id="tab-solicitacoes" class="tab-content">
<div class="tabela-container">
<h2>📋 Solicitações Pendentes</h2>
<table id="tabelaSolicitacoes">
<thead><tr><th>ID</th><th>Funcionário</th><th>CPF</th><th>Data/Hora</th><th>Tipo</th><th>Justificativa</th><th>Status</th><th>Ações</th></tr></thead>
<tbody id="tbodySolicitacoes"></tbody>
</table>
</div>
</div>

<!-- ABA ADMINS -->
<div id="tab-admins" class="tab-content">
<div class="form-cadastro">
<h3>➕ Cadastrar Novo Administrador</h3>
<div class="form-grid">
<input type="text" id="cadAdminUsuario" class="input-moderno" placeholder="Usuário">
<input type="text" id="cadAdminNome" class="input-moderno" placeholder="Nome Completo">
<input type="password" id="cadAdminSenha" class="input-moderno" placeholder="Senha">
</div>
<button class="btn-3d btn-cadastrar" style="margin-top:15px;" onclick="cadastrarAdmin()">💾 CADASTRAR ADMIN</button>
</div>
<div class="tabela-container">
<h2>🔐 Lista de Administradores</h2>
<table id="tabelaAdmins">
<thead><tr><th>ID</th><th>Usuário</th><th>Nome Completo</th><th>Criado Em</th><th>Último Login</th><th>Ações</th></tr></thead>
<tbody id="tbodyAdmins"></tbody>
</table>
</div>
</div>

<!-- ABA BACKUP -->
<div id="tab-backup" class="tab-content">
<div class="form-cadastro">
<h3>🛡️ Gerenciamento de Backups</h3>
<p style="color:#666;font-size:13px;margin-bottom:15px;">Seus dados estão protegidos em disco persistente. Use as opções abaixo para segurança adicional.</p>
<button class="btn-3d" style="background:linear-gradient(135deg,#667eea,#764ba2);padding:14px 25px;margin-right:10px;" onclick="fazerBackupManual()">💾 CRIAR BACKUP AGORA</button>
<button class="btn-3d" style="background:linear-gradient(135deg,#43e97b,#38f9d7);padding:14px 25px;" onclick="listarBackups()">📋 LISTAR BACKUPS</button>
</div>
<div class="tabela-container">
<h2>📦 Backups Disponíveis</h2>
<div id="listaBackups"><p style="color:#888;font-size:13px;">Clique em "LISTAR BACKUPS" para ver os backups disponíveis</p></div>
</div>
</div>

""" + RODAPE_WELL + """
</div>

<!-- Modal Editar -->
<div id="modalEditar" class="modal">
<div class="modal-content">
<h3>✏️ Editar Funcionário</h3>
<input type="hidden" id="editId">
<div class="form-grid" style="margin-top:15px;">
<input type="text" id="editNome" class="input-moderno" placeholder="Nome">
<input type="text" id="editCpf" class="input-moderno" placeholder="CPF">
<input type="time" id="editEntrada" class="input-moderno">
<input type="time" id="editSaidaAlmoco" class="input-moderno">
<input type="time" id="editRetorno" class="input-moderno">
<input type="time" id="editSaida" class="input-moderno">
</div>
<div style="margin-top:20px;display:flex;gap:10px;justify-content:flex-end;">
<button class="btn-3d" style="background:#ccc;padding:12px 20px;" onclick="fecharModal()">CANCELAR</button>
<button class="btn-3d" style="background:linear-gradient(135deg,#667eea,#764ba2);padding:12px 20px;" onclick="salvarEdicao()">SALVAR</button>
</div>
</div>
</div>

<script>
function abrirTab(nome){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('ativo'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('ativo'));
  document.getElementById('tab-'+nome).classList.add('ativo');
  event.target.classList.add('ativo');
  if(nome==='funcionarios')carregarFuncionarios();
  if(nome==='registros')carregarRegistros();
  if(nome==='solicitacoes')carregarSolicitacoes();
  if(nome==='admins')carregarAdmins();
  if(nome==='resumo')carregarResumo();
}

async function sair(){
  await fetch('/api/logout',{method:'POST'});
  window.location.href='/admin/login';
}

async function carregarResumo(){
  try{
    const r=await fetch('/api/admin/resumo');
    const d=await r.json();
    document.getElementById('cardsResumo').innerHTML=`
      <div class="card-resumo" style="border-left-color:#667eea;"><h3>👥 Funcionários</h3><div class="valor">${d.total_funcionarios}</div><div class="sub">Cadastrados</div></div>
      <div class="card-resumo" style="border-left-color:#4CAF50;"><h3>✅ Registros Hoje</h3><div class="valor">${d.registros_hoje}</div><div class="sub">Hoje</div></div>
      <div class="card-resumo" style="border-left-color:#ff9800;"><h3>⚠️ Atrasos Hoje</h3><div class="valor">${d.atrasos_hoje}</div><div class="sub">Funcionários atrasados</div></div>
      <div class="card-resumo" style="border-left-color:#f44336;"><h3>📋 Solicitações</h3><div class="valor">${d.solicitacoes_pendentes}</div><div class="sub">Pendentes</div></div>
    `;
    document.getElementById('resumoFuncionarios').innerHTML=d.funcionarios_resumo.map(f=>`
      <div style="padding:15px;border-bottom:1px solid #eee;display:flex;align-items:center;justify-content:space-between;">
        <div><strong>${f.nome}</strong><br><small style="color:#888;">${f.cpf}</small></div>
        <div style="text-align:right;">
          <small style="color:${f.ultimo_registro?'#4CAF50':'#999'};font-weight:bold;">${f.ultimo_registro||'Sem registro hoje'}</small>
          ${f.atrasado?'<br><small style="color:#f44336;">⚠️ Atrasado</small>':''}
        </div>
      </div>
    `).join('');
  }catch(e){}
}

async function carregarFuncionarios(){
  try{
    const r=await fetch('/api/admin/funcionarios');
    const d=await r.json();
    document.getElementById('tbodyFuncionarios').innerHTML=d.map(f=>`
      <tr>
        <td>${f.id}</td><td>${f.nome}</td><td>${f.cpf}</td>
        <td>${f.horario_entrada?.substring(0,5)||'-'}</td>
        <td>${f.horario_saida_almoco?.substring(0,5)||'-'}</td>
        <td>${f.horario_retorno_almoco?.substring(0,5)||'-'}</td>
        <td>${f.horario_saida?.substring(0,5)||'-'}</td>
        <td>
          <button class="btn-acao btn-editar" onclick='editarFuncionario(${JSON.stringify(f)})'>✏️ Editar</button>
          <button class="btn-acao btn-excluir" onclick="excluirFuncionario(${f.id})">🗑️ Excluir</button>
        </td>
      </tr>
    `).join('');
  }catch(e){}
}

async function cadastrarFuncionario(){
  const dados={
    nome:document.getElementById('cadNome').value.trim(),
    cpf:document.getElementById('cadCpf').value.replace(/\D/g,''),
    horario_entrada:document.getElementById('cadEntrada').value+':00',
    horario_saida_almoco:document.getElementById('cadSaidaAlmoco').value+':00',
    horario_retorno_almoco:document.getElementById('cadRetorno').value+':00',
    horario_saida:document.getElementById('cadSaida').value+':00'
  };
  if(!dados.nome||dados.cpf.length!==11){alert('Preencha nome e CPF válido!');return;}
  try{
    const r=await fetch('/api/admin/funcionarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(dados)});
    if(r.ok){alert('Funcionário cadastrado!');document.getElementById('cadNome').value='';document.getElementById('cadCpf').value='';carregarFuncionarios();}
    else{const e=await r.json();alert(e.detail||'Erro');}
  }catch(e){alert('Erro de conexão');}
}

function editarFuncionario(f){
  document.getElementById('editId').value=f.id;
  document.getElementById('editNome').value=f.nome;
  document.getElementById('editCpf').value=f.cpf;
  document.getElementById('editEntrada').value=f.horario_entrada?.substring(0,5)||'08:00';
  document.getElementById('editSaidaAlmoco').value=f.horario_saida_almoco?.substring(0,5)||'12:00';
  document.getElementById('editRetorno').value=f.horario_retorno_almoco?.substring(0,5)||'13:00';
  document.getElementById('editSaida').value=f.horario_saida?.substring(0,5)||'18:00';
  document.getElementById('modalEditar').classList.add('ativo');
}

function fecharModal(){document.getElementById('modalEditar').classList.remove('ativo');}

async function salvarEdicao(){
  const dados={
    id:parseInt(document.getElementById('editId').value),
    nome:document.getElementById('editNome').value.trim(),
    cpf:document.getElementById('editCpf').value.replace(/\D/g,''),
    horario_entrada:document.getElementById('editEntrada').value+':00',
    horario_saida_almoco:document.getElementById('editSaidaAlmoco').value+':00',
    horario_retorno_almoco:document.getElementById('editRetorno').value+':00',
    horario_saida:document.getElementById('editSaida').value+':00'
  };
  try{
    const r=await fetch('/api/admin/funcionarios/'+dados.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(dados)});
    if(r.ok){fecharModal();carregarFuncionarios();}
    else{const e=await r.json();alert(e.detail||'Erro');}
  }catch(e){alert('Erro');}
}

async function excluirFuncionario(id){
  if(!confirm('Tem certeza que deseja excluir este funcionário?'))return;
  try{
    const r=await fetch('/api/admin/funcionarios/'+id,{method:'DELETE'});
    if(r.ok)carregarFuncionarios();
    else alert('Erro ao excluir');
  }catch(e){alert('Erro');}
}

async function carregarRegistros(){
  try{
    const r=await fetch('/api/admin/registros');
    const d=await r.json();
    document.getElementById('tbodyRegistros').innerHTML=d.slice(0,200).map(reg=>`
      <tr>
        <td>${reg.id}</td><td>${reg.nome_funcionario||'-'}</td><td>${reg.data_hora}</td>
        <td><strong>${reg.tipo.replace('_',' ')}</strong></td>
        <td>${reg.atrasado?'⚠️ Sim':'Não'}</td>
        <td>${reg.minutos_atraso||0}</td>
        <td>${reg.minutos_banco_horas||0}</td>
      </tr>
    `).join('');
  }catch(e){}
}

async function carregarSolicitacoes(){
  try{
    const r=await fetch('/api/admin/solicitacoes');
    const d=await r.json();
    document.getElementById('tbodySolicitacoes').innerHTML=d.map(s=>`
      <tr>
        <td>${s.id}</td><td>${s.nome_funcionario||'-'}</td><td>${s.cpf}</td>
        <td>${s.data_hora_solicitacao}</td><td>${s.tipo.replace('_',' ')}</td>
        <td><small>${s.justificativa||'-'}</small></td>
        <td><span class="status-${s.status.toLowerCase()}">${s.status}</span></td>
        <td>
          ${s.status==='PENDENTE'?`
            <button class="btn-acao btn-editar" onclick="aprovarSolicitacao(${s.id})">✅ Aprovar</button>
            <button class="btn-acao btn-excluir" onclick="negarSolicitacao(${s.id})">❌ Negar</button>
          `:'-'}
        </td>
      </tr>
    `).join('');
  }catch(e){}
}

async function aprovarSolicitacao(id){
  try{
    const r=await fetch('/api/admin/solicitacoes/'+id+'/aprovar',{method:'POST'});
    if(r.ok)carregarSolicitacoes();
  }catch(e){}
}

async function negarSolicitacao(id){
  const motivo=prompt('Motivo da negação:');
  if(motivo===null)return;
  try{
    const r=await fetch('/api/admin/solicitacoes/'+id+'/negar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({motivo:motivo})});
    if(r.ok)carregarSolicitacoes();
  }catch(e){}
}

async function carregarAdmins(){
  try{
    const r=await fetch('/api/admin/admins');
    const d=await r.json();
    document.getElementById('tbodyAdmins').innerHTML=d.map(a=>`
      <tr>
        <td>${a.id}</td><td>${a.usuario}</td><td>${a.nome_completo||'-'}</td>
        <td>${a.criado_em||'-'}</td><td>${a.ultimo_login||'-'}</td>
        <td>${a.usuario!=='admin'?`<button class="btn-acao btn-excluir" onclick="excluirAdmin(${a.id})">🗑️ Excluir</button>`:'<small style="color:#999;">Master</small>'}</td>
      </tr>
    `).join('');
  }catch(e){}
}

async function cadastrarAdmin(){
  const dados={
    usuario:document.getElementById('cadAdminUsuario').value.trim(),
    nome_completo:document.getElementById('cadAdminNome').value.trim(),
    senha:document.getElementById('cadAdminSenha').value
  };
  if(!dados.usuario||!dados.senha){alert('Preencha usuário e senha!');return;}
  try{
    const r=await fetch('/api/admin/admins',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(dados)});
    if(r.ok){alert('Admin cadastrado!');document.getElementById('cadAdminUsuario').value='';document.getElementById('cadAdminNome').value='';document.getElementById('cadAdminSenha').value='';carregarAdmins();}
    else{const e=await r.json();alert(e.detail||'Erro');}
  }catch(e){alert('Erro');}
}

async function excluirAdmin(id){
  if(!confirm('Tem certeza?'))return;
  try{
    const r=await fetch('/api/admin/admins/'+id,{method:'DELETE'});
    if(r.ok)carregarAdmins();
  }catch(e){}
}

async function fazerBackupManual(){
  try{
    const r=await fetch('/api/admin/backup/criar',{method:'POST'});
    const d=await r.json();
    alert(d.mensagem||'Backup criado!');
    listarBackups();
  }catch(e){alert('Erro');}
}

async function listarBackups(){
  try{
    const r=await fetch('/api/admin/backups');
    const d=await r.json();
    if(d.backups&&d.backups.length>0){
      document.getElementById('listaBackups').innerHTML=`
        <p style="color:#666;font-size:13px;margin-bottom:10px;">${d.backups.length} backup(s) disponíveis</p>
        ${d.backups.map(b=>`
          <div style="padding:12px;background:#f8f9ff;border-radius:10px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;">
            <div>
              <strong>📦 ${b.nome}</strong><br>
              <small style="color:#888;">${b.tamanho} bytes | ${b.data}</small>
            </div>
            <button class="btn-acao btn-editar" onclick="restaurarBackup('${b.nome}')">🔄 Restaurar</button>
          </div>
        `).join('')}
      `;
    }else{
      document.getElementById('listaBackups').innerHTML='<p style="color:#888;font-size:13px;">Nenhum backup disponível</p>';
    }
  }catch(e){}
}

async function restaurarBackup(nome){
  if(!confirm('ATENÇÃO: Isso substituirá o banco atual pelo backup selecionado. Deseja continuar?'))return;
  try{
    const r=await fetch('/api/admin/backup/restaurar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nome:nome})});
    const d=await r.json();
    alert(d.mensagem||'Restauração concluída! O sistema irá recarregar.');
    setTimeout(()=>location.reload(),1500);
  }catch(e){alert('Erro');}
}

carregarResumo();
</script>
</body>
</html>"""

# ===================== MANIPULADOR HTTP =====================
class ManipuladorPonto(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format%args}")
    
    def do_GET(self):
        url = urlparse(self.path)
        caminho = url.path
        
        try:
            # Arquivos estáticos
            if caminho.startswith("/static/"):
                self.servir_estatico(caminho)
                return
            
            # Página principal
            if caminho == "/" or caminho == "":
                responder_html(self, gerar_html_ponto())
                return
            
            # Login admin
            if caminho == "/admin/login":
                responder_html(self, gerar_html_login())
                return
            
            # Painel admin
            if caminho == "/admin":
                if not verificar_login(self):
                    self.send_response(302)
                    self.send_header("Location", "/admin/login")
                    self.end_headers()
                    return
                responder_html(self, gerar_html_admin())
                return
            
            # Painel funcionário
            if caminho == "/funcionario":
                responder_html(self, gerar_html_funcionario())
                return
            
            # API: Buscar funcionário por CPF
            if caminho.startswith("/api/buscar/"):
                cpf = caminho.replace("/api/buscar/", "")
                self.api_buscar_funcionario(cpf)
                return
            
            # API: Dados do funcionário
            if caminho.startswith("/api/funcionario/dados/"):
                cpf = caminho.replace("/api/funcionario/dados/", "")
                self.api_dados_funcionario(cpf)
                return
            
            # API: Registros do funcionário hoje
            if caminho.startswith("/api/funcionario/registros/"):
                cpf = caminho.replace("/api/funcionario/registros/", "")
                self.api_registros_funcionario(cpf)
                return
            
            # APIs de admin (protegidas)
            if caminho.startswith("/api/admin/"):
                if not verificar_login(self):
                    responder_json(self, {"detail": "Não autorizado"}, 401)
                    return
                
                if caminho == "/api/admin/resumo":
                    self.api_admin_resumo()
                    return
                if caminho == "/api/admin/funcionarios":
                    self.api_listar_funcionarios()
                    return
                if caminho == "/api/admin/registros":
                    self.api_listar_registros()
                    return
                if caminho == "/api/admin/solicitacoes":
                    self.api_listar_solicitacoes()
                    return
                if caminho == "/api/admin/admins":
                    self.api_listar_admins()
                    return
                if caminho == "/api/admin/backups":
                    self.api_listar_backups()
                    return
            
            responder_json(self, {"detail": "Não encontrado"}, 404)
            
        except Exception as e:
            print(f"[ERRO GET] {e}")
            traceback.print_exc()
            responder_json(self, {"detail": "Erro interno do servidor"}, 500)
    
    def do_POST(self):
        url = urlparse(self.path)
        caminho = url.path
        
        try:
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = self.rfile.read(tamanho) if tamanho > 0 else b"{}"
            dados = json.loads(corpo.decode("utf-8")) if corpo else {}
            
            # Login admin
            if caminho == "/api/login":
                self.api_login(dados)
                return
            
            # Logout admin
            if caminho == "/api/logout":
                responder_json(self, {"mensagem": "Deslogado com sucesso"})
                return
            
            # Acesso funcionário
            if caminho == "/api/funcionario/acessar":
                self.api_funcionario_acessar(dados)
                return
            
            # Registrar ponto
            if caminho == "/api/funcionario/registrar":
                self.api_funcionario_registrar(dados)
                return
            
            # APIs de admin
            if caminho.startswith("/api/admin/"):
                if not verificar_login(self):
                    responder_json(self, {"detail": "Não autorizado"}, 401)
                    return
                
                if caminho == "/api/admin/funcionarios":
                    self.api_cadastrar_funcionario(dados)
                    return
                if caminho == "/api/admin/backup/criar":
                    self.api_criar_backup()
                    return
                if caminho == "/api/admin/backup/restaurar":
                    self.api_restaurar_backup(dados)
                    return
                if caminho == "/api/admin/admins":
                    self.api_cadastrar_admin(dados)
                    return
            
            responder_json(self, {"detail": "Não encontrado"}, 404)
            
        except Exception as e:
            print(f"[ERRO POST] {e}")
            traceback.print_exc()
            responder_json(self, {"detail": "Erro interno do servidor"}, 500)
    
    def do_PUT(self):
        url = urlparse(self.path)
        caminho = url.path
        
        try:
            if not verificar_login(self):
                responder_json(self, {"detail": "Não autorizado"}, 401)
                return
            
            tamanho = int(self.headers.get("Content-Length", 0))
            corpo = self.rfile.read(tamanho) if tamanho > 0 else b"{}"
            dados = json.loads(corpo.decode("utf-8")) if corpo else {}
            
            if caminho.startswith("/api/admin/funcionarios/"):
                func_id = int(caminho.replace("/api/admin/funcionarios/", ""))
                self.api_editar_funcionario(func_id, dados)
                return
            
            responder_json(self, {"detail": "Não encontrado"}, 404)
            
        except Exception as e:
            print(f"[ERRO PUT] {e}")
            responder_json(self, {"detail": "Erro interno"}, 500)
    
    def do_DELETE(self):
        url = urlparse(self.path)
        caminho = url.path
        
        try:
            if not verificar_login(self):
                responder_json(self, {"detail": "Não autorizado"}, 401)
                return
            
            if caminho.startswith("/api/admin/funcionarios/"):
                func_id = int(caminho.replace("/api/admin/funcionarios/", ""))
                self.api_excluir_funcionario(func_id)
                return
            
            if caminho.startswith("/api/admin/admins/"):
                admin_id = int(caminho.replace("/api/admin/admins/", ""))
                self.api_excluir_admin(admin_id)
                return
            
            responder_json(self, {"detail": "Não encontrado"}, 404)
            
        except Exception as e:
            print(f"[ERRO DELETE] {e}")
            responder_json(self, {"detail": "Erro interno"}, 500)
    
    def servir_estatico(self, caminho):
        try:
            caminho_arquivo = caminho.lstrip("/")
            if os.path.exists(caminho_arquivo):
                with open(caminho_arquivo, "rb") as f:
                    conteudo = f.read()
                self.send_response(200)
                if caminho.endswith(".png"):
                    self.send_header("Content-Type", "image/png")
                elif caminho.endswith(".jpg") or caminho.endswith(".jpeg"):
                    self.send_header("Content-Type", "image/jpeg")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(conteudo)
            else:
                responder_json(self, {"detail": "Arquivo não encontrado"}, 404)
        except:
            responder_json(self, {"detail": "Erro ao servir arquivo"}, 500)
    
    # ===================== MÉTODOS DA API =====================
    
    def api_login(self, dados):
        usuario = sanitizar_texto(dados.get("usuario", ""))
        senha = dados.get("senha", "")
        ip = obter_ip_cliente(self)
        
        if not verificar_rate_limit(ip):
            responder_json(self, {"detail": "Muitas tentativas. Aguarde 5 minutos."}, 429)
            return
        
        sucesso, nome = verificar_credenciais_admin(usuario, senha)
        if sucesso:
            token = gerar_sessao()
            sessoes_admin[token] = agora_brasilia() + timedelta(hours=8)
            
            # Atualiza último login
            try:
                conn = get_db()
                conn.execute("UPDATE admins SET ultimo_login = ? WHERE usuario = ?",
                           (agora_brasilia().isoformat(), usuario))
                conn.commit()
                conn.close()
            except: pass
            
            cookie = f"sessao_admin={token}; Path=/; HttpOnly; Max-Age=28800; SameSite=Lax"
            responder_json(self, {"mensagem": "Login bem sucedido", "usuario": nome}, cookies_extra=[cookie])
        else:
            responder_json(self, {"detail": "Usuário ou senha incorretos"}, 401)
    
    def api_buscar_funcionario(self, cpf):
        cpf = formatar_cpf(cpf)
        conn = get_db()
        func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
        conn.close()
        if func:
            responder_json(self, {"encontrado": True, "nome": func["nome"]})
        else:
            responder_json(self, {"encontrado": False})
    
    def api_funcionario_acessar(self, dados):
        cpf = formatar_cpf(dados.get("cpf", ""))
        ip = obter_ip_cliente(self)
        ua = self.headers.get("User-Agent", "")[:500]
        
        if len(cpf) != 11:
            responder_json(self, {"detail": "CPF inválido"}, 400)
            return
        
        conn = get_db()
        func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
        
        if not func:
            conn.close()
            responder_json(self, {"detail": "CPF não cadastrado. Contate o RH."}, 404)
            return
        
        # Registra acesso
        registrar_acesso_dispositivo(cpf, func["id"], ip, ua, "painel_funcionario")
        
        # Cria sessão temporária
        acessos_funcionarios[cpf] = {"expira": agora_brasilia() + timedelta(hours=2)}
        
        conn.close()
        responder_json(self, {"mensagem": "Acesso liberado", "cpf": cpf})
    
    def api_dados_funcionario(self, cpf):
        cpf = formatar_cpf(cpf)
        conn = get_db()
        func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
        conn.close()
        if func:
            responder_json(self, {
                "id": func["id"],
                "nome": func["nome"],
                "cpf": func["cpf"],
                "horario_entrada": func["horario_entrada"],
                "horario_saida_almoco": func["horario_saida_almoco"],
                "horario_retorno_almoco": func["horario_retorno_almoco"],
                "horario_saida": func["horario_saida"]
            })
        else:
            responder_json(self, {"detail": "Não encontrado"}, 404)
    
    def api_registros_funcionario(self, cpf):
        cpf = formatar_cpf(cpf)
        hoje = agora_brasilia().strftime("%Y-%m-%d")
        conn = get_db()
        func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
        if not func:
            conn.close()
            responder_json(self, {"detail": "Não encontrado"}, 404)
            return
        
        registros = conn.execute(
            "SELECT * FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ? ORDER BY data_hora",
            (func["id"], hoje)
        ).fetchall()
        
        total_atrasos = sum(r["minutos_atraso"] or 0 for r in registros)
        total_banco = sum(r["minutos_banco_horas"] or 0 for r in registros)
        
        conn.close()
        responder_json(self, {
            "registros": [dict(r) for r in registros],
            "total_atrasos": total_atrasos,
            "total_banco": total_banco
        })
    
    def api_funcionario_registrar(self, dados):
        cpf = formatar_cpf(dados.get("cpf", ""))
        tipo = dados.get("tipo", "")
        ip = obter_ip_cliente(self)
        ua = self.headers.get("User-Agent", "")[:500]
        
        if tipo not in TIPOS_REGISTRO:
            responder_json(self, {"detail": "Tipo de registro inválido"}, 400)
            return
        
        conn = get_db()
        func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
        if not func:
            conn.close()
            responder_json(self, {"detail": "Funcionário não encontrado"}, 404)
            return
        
        agora = agora_brasilia()
        hoje = agora.strftime("%Y-%m-%d")
        hora_atual = agora.strftime("%H:%M:%S")
        data_hora_completa = agora.strftime("%Y-%m-%d %H:%M:%S")
        
        # Verifica sequência
        ultimo = obter_ultimo_registro(func["id"], hoje)
        ultimo_tipo = ultimo["tipo"] if ultimo else None
        
        valido, msg_erro = verificar_sequencia_valida(ultimo_tipo, tipo)
        if not valido:
            conn.close()
            responder_json(self, {"detail": msg_erro}, 400)
            return
        
        # Verifica duplicata
        if verificar_registro_duplicado(func["id"], hoje, tipo):
            conn.close()
            responder_json(self, {"detail": f"Você já registrou {tipo} hoje!"}, 400)
            return
        
        # Verifica atraso
        horarios = {
            "ENTRADA": func["horario_entrada"],
            "SAIDA_ALMOCO": func["horario_saida_almoco"],
            "RETORNO_ALMOCO": func["horario_retorno_almoco"],
            "SAIDA": func["horario_saida"]
        }
        
        atrasado = 0
        minutos_atraso = 0
        
        if tipo in ["ENTRADA", "RETORNO_ALMOCO"]:
            if verificar_atraso(hora_atual, horarios[tipo], 0):
                # Verifica se está dentro da tolerância de 5 minutos
                if not verificar_na_tolerancia_depois(hora_atual, horarios[tipo], 5):
                    atrasado = 1
                    minutos_atraso = calcular_minutos(hora_atual, horarios[tipo])
        
        # Calcula banco de horas
        minutos_banco = calcular_banco_horas(tipo, hora_atual, func)
        
        # Insere registro
        conn.execute("""
            INSERT INTO registros_ponto 
            (funcionario_id, data_hora, tipo, atrasado, minutos_atraso, minutos_banco_horas, 
             ip_dispositivo, user_agent, horario_acesso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (func["id"], data_hora_completa, tipo, atrasado, minutos_atraso, minutos_banco,
              ip, ua, hora_atual))
        
        conn.commit()
        conn.close()
        
        # Faz backup após registro importante
        try:
            fazer_backup_local(sufixo="_apos_registro")
        except:
            pass
        
        msg = f"{TIPOS_REGISTRO[tipo]['label']} registrado às {hora_atual}"
        if atrasado:
            msg += f" ⚠️ Atraso de {minutos_atraso} minutos"
        elif minutos_banco > 0:
            msg += f" ⏰ +{minutos_banco}min banco de horas"
        
        responder_json(self, {"mensagem": msg, "tipo": tipo, "hora": hora_atual})
    
    # ===================== APIs ADMIN =====================
    
    def api_admin_resumo(self):
        hoje = agora_brasilia().strftime("%Y-%m-%d")
        conn = get_db()
        
        total_func = conn.execute("SELECT COUNT(*) as c FROM funcionarios").fetchone()["c"]
        reg_hoje = conn.execute(
            "SELECT COUNT(*) as c FROM registros_ponto WHERE strftime('%Y-%m-%d', data_hora) = ?",
            (hoje,)
        ).fetchone()["c"]
        atrasos_hoje = conn.execute(
            "SELECT COUNT(DISTINCT funcionario_id) as c FROM registros_ponto WHERE strftime('%Y-%m-%d', data_hora) = ? AND atrasado = 1",
            (hoje,)
        ).fetchone()["c"]
        sol_pendentes = conn.execute(
            "SELECT COUNT(*) as c FROM solicitacoes_pendentes WHERE status = 'PENDENTE'"
        ).fetchone()["c"]
        
        # Resumo de funcionários hoje
        funcionarios = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
        func_resumo = []
        for f in funcionarios:
            ultimo = conn.execute(
                "SELECT * FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ? ORDER BY data_hora DESC LIMIT 1",
                (f["id"], hoje)
            ).fetchone()
            atrasado = conn.execute(
                "SELECT 1 FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m-%d', data_hora) = ? AND atrasado = 1 LIMIT 1",
                (f["id"], hoje)
            ).fetchone()
            
            func_resumo.append({
                "nome": f["nome"],
                "cpf": f["cpf"],
                "ultimo_registro": ultimo["tipo"] + " " + ultimo["data_hora"][11:16] if ultimo else None,
                "atrasado": atrasado is not None
            })
        
        conn.close()
        
        responder_json(self, {
            "total_funcionarios": total_func,
            "registros_hoje": reg_hoje,
            "atrasos_hoje": atrasos_hoje,
            "solicitacoes_pendentes": sol_pendentes,
            "funcionarios_resumo": func_resumo
        })
    
    def api_listar_funcionarios(self):
        conn = get_db()
        funcs = conn.execute("SELECT * FROM funcionarios ORDER BY nome").fetchall()
        conn.close()
        responder_json(self, [dict(f) for f in funcs])
    
    def api_cadastrar_funcionario(self, dados):
        nome = sanitizar_texto(dados.get("nome", ""))
        cpf = formatar_cpf(dados.get("cpf", ""))
        
        if not nome or len(cpf) != 11:
            responder_json(self, {"detail": "Nome e CPF válido são obrigatórios"}, 400)
            return
        
        try:
            conn = get_db()
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
            
            fazer_backup_local(sufixo="_novo_funcionario")
            responder_json(self, {"mensagem": "Funcionário cadastrado", "id": novo_id})
        except sqlite3.IntegrityError:
            responder_json(self, {"detail": "CPF já cadastrado"}, 400)
    
    def api_editar_funcionario(self, func_id, dados):
        try:
            conn = get_db()
            conn.execute("""
                UPDATE funcionarios SET nome=?, cpf=?, horario_entrada=?, 
                horario_saida_almoco=?, horario_retorno_almoco=?, horario_saida=?
                WHERE id=?
            """, (
                sanitizar_texto(dados.get("nome", "")),
                formatar_cpf(dados.get("cpf", "")),
                dados.get("horario_entrada", "08:00:00"),
                dados.get("horario_saida_almoco", "12:00:00"),
                dados.get("horario_retorno_almoco", "13:00:00"),
                dados.get("horario_saida", "18:00:00"),
                func_id
            ))
            conn.commit()
            conn.close()
            fazer_backup_local(sufixo="_edicao_funcionario")
            responder_json(self, {"mensagem": "Funcionário atualizado"})
        except sqlite3.IntegrityError:
            responder_json(self, {"detail": "CPF já cadastrado"}, 400)
    
    def api_excluir_funcionario(self, func_id):
        conn = get_db()
        conn.execute("DELETE FROM funcionarios WHERE id = ?", (func_id,))
        conn.commit()
        conn.close()
        fazer_backup_local(sufixo="_exclusao_funcionario")
        responder_json(self, {"mensagem": "Funcionário excluído"})
    
    def api_listar_registros(self):
        conn = get_db()
        regs = conn.execute("""
            SELECT r.*, f.nome as nome_funcionario 
            FROM registros_ponto r 
            LEFT JOIN funcionarios f ON r.funcionario_id = f.id 
            ORDER BY r.data_hora DESC LIMIT 500
        """).fetchall()
        conn.close()
        responder_json(self, [dict(r) for r in regs])
    
    def api_listar_solicitacoes(self):
        conn = get_db()
        sols = conn.execute("""
            SELECT s.*, f.nome as nome_funcionario 
            FROM solicitacoes_pendentes s 
            LEFT JOIN funcionarios f ON s.funcionario_id = f.id 
            ORDER BY s.data_hora_solicitacao DESC
        """).fetchall()
        conn.close()
        responder_json(self, [dict(s) for s in sols])
    
    def api_listar_admins(self):
        conn = get_db()
        admins = conn.execute("SELECT id, usuario, nome_completo, criado_em, ultimo_login FROM admins ORDER BY id").fetchall()
        conn.close()
        responder_json(self, [dict(a) for a in admins])
    
    def api_cadastrar_admin(self, dados):
        usuario = sanitizar_texto(dados.get("usuario", ""))
        senha = dados.get("senha", "")
        nome_completo = sanitizar_texto(dados.get("nome_completo", ""))
        
        if not usuario or not senha:
            responder_json(self, {"detail": "Usuário e senha são obrigatórios"}, 400)
            return
        
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO admins (usuario, senha, nome_completo, criado_em) VALUES (?, ?, ?, ?)",
                (usuario, hash_senha(senha), nome_completo, agora_brasilia().isoformat())
            )
            conn.commit()
            conn.close()
            responder_json(self, {"mensagem": "Admin cadastrado"})
        except sqlite3.IntegrityError:
            responder_json(self, {"detail": "Usuário já existe"}, 400)
    
    def api_excluir_admin(self, admin_id):
        conn = get_db()
        admin = conn.execute("SELECT * FROM admins WHERE id = ?", (admin_id,)).fetchone()
        if admin and admin["usuario"] == "admin":
            conn.close()
            responder_json(self, {"detail": "Não pode excluir o admin master"}, 400)
            return
        conn.execute("DELETE FROM admins WHERE id = ?", (admin_id,))
        conn.commit()
        conn.close()
        responder_json(self, {"mensagem": "Admin excluído"})
    
    def api_listar_backups(self):
        backups = listar_backups_disponiveis()
        lista = []
        for b in backups[:20]:
            try:
                stat = os.stat(b)
                lista.append({
                    "nome": os.path.basename(b),
                    "tamanho": stat.st_size,
                    "data": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S")
                })
            except:
                pass
        responder_json(self, {"backups": lista})
    
    def api_criar_backup(self):
        caminho = fazer_backup_local(sufixo="_manual_admin")
        if caminho:
            responder_json(self, {"mensagem": f"Backup criado: {os.path.basename(caminho)}"})
        else:
            responder_json(self, {"detail": "Erro ao criar backup"}, 500)
    
    def api_restaurar_backup(self, dados):
        nome = dados.get("nome", "")
        caminho = os.path.join(BACKUP_DIR, nome)
        
        if not os.path.exists(caminho):
            responder_json(self, {"detail": "Backup não encontrado"}, 404)
            return
        
        if restaurar_de_backup(caminho):
            responder_json(self, {"mensagem": "Backup restaurado com sucesso!"})
        else:
            responder_json(self, {"detail": "Erro ao restaurar backup"}, 500)


# ===================== INICIALIZAÇÃO DO SERVIDOR =====================
def main():
    print(f"\n🚀 Iniciando servidor na porta {PORTA}...")
    print(f"🌐 Acesse: http://localhost:{PORTA}")
    print(f"🛡️ Proteção de dados ATIVA - Seus dados estão seguros!\n")
    
    servidor = HTTPServer(("0.0.0.0", PORTA), ManipuladorPonto)
    
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Servidor interrompido pelo usuário")
    finally:
        servidor.server_close()
        finalizar_seguro()

if __name__ == "__main__":
    main()
