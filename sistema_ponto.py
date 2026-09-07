import sqlite3
import json
import os
import io
import hashlib
import re
import base64
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ===================== HORARIO DE BRASILIA (UTC-3) =====================
FUSO_BRASILIA = timezone(timedelta(hours=-3))
def agora_brasilia():
    return datetime.now(timezone.utc).astimezone(FUSO_BRASILIA).replace(tzinfo=None)

# ===================== CONFIGURACOES =====================
SEGREDO_QR = "CLINICA_PONTO_2024"
PORTA = 8000
ADMIN_USUARIO = "admin"
ADMIN_SENHA = "3223ronte"
MAX_TENTATIVAS_FACIAIS = 5
LIMIAR_CONFIANCA_FACIAL = 70.0

sessoes_admin = {}
tentativas_login = {}
acessos_funcionarios = {}
autorizacoes_pendentes = {}
TIMEOUT_AUTORIZACAO_SEGUNDOS = 300
TOLERANCIA_ATRASO_MINUTOS = 5  # Tolerância de 5 minutos para atrasos

os.makedirs("static", exist_ok=True)
os.makedirs("static/fotos", exist_ok=True)
os.makedirs("modelos_faciais", exist_ok=True)

CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=self, microphone=(), geolocation=()"
}

# ===================== RECONHECIMENTO FACIAL =====================
try:
    import cv2
    import numpy as np
    FACE_REC_DISPONIVEL = True
    
    try:
        CAMINHO_HAAR = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        detector_faces = cv2.CascadeClassifier(CAMINHO_HAAR)
        if detector_faces.empty():
            raise Exception("Haar Cascade vazio")
    except:
        detector_faces = None
    
    def detectar_face_opencv(img_bytes, tamanho_alvo=(200, 200)):
        try:
            nparr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return None, False, "Imagem inválida"
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            face_detectada = False
            face = None
            
            if detector_faces is not None:
                # Tenta detecção normal primeiro
                faces = detector_faces.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
                if len(faces) > 0:
                    # Pega a maior face detectada
                    maior_face = max(faces, key=lambda f: f[2] * f[3])
                    x, y, w, h = maior_face
                    # Adiciona margem ao redor da face
                    margem = int(0.1 * w)
                    x1 = max(0, x - margem)
                    y1 = max(0, y - margem)
                    x2 = min(gray.shape[1], x + w + margem)
                    y2 = min(gray.shape[0], y + h + margem)
                    face = gray[y1:y2, x1:x2]
                    face_detectada = True
            
            # Se não detectou face, usa a imagem toda (fallback)
            if not face_detectada:
                face = gray
            
            # Aplica equalização de histograma para melhorar contraste
            face = cv2.equalizeHist(face)
            
            face_redimensionada = cv2.resize(face, tamanho_alvo)
            return face_redimensionada, True, "OK"
        except Exception as e:
            return None, False, f"Erro: {str(e)}"
    
    def treinar_modelo_face(funcionario_id, img_bytes):
        # Primeiro: SEMPRE salva a foto de perfil, independente da detecção
        try:
            caminho_foto = os.path.join("static/fotos", f"func_{funcionario_id}.jpg")
            nparr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                cv2.imwrite(caminho_foto, img)
                foto_perfil_caminho = f"/static/fotos/func_{funcionario_id}.jpg"
            else:
                foto_perfil_caminho = ""
        except:
            foto_perfil_caminho = ""
        
        # Depois: tenta detectar face e extrair características
        face, sucesso, msg = detectar_face_opencv(img_bytes)
        if not sucesso:
            # Atualiza banco com a foto, mas marca como não treinada
            conn = get_db()
            conn.execute("UPDATE funcionarios SET foto_perfil = ?, face_treinada = 0 WHERE id = ?",
                        (foto_perfil_caminho, funcionario_id))
            conn.commit()
            conn.close()
            return False, f"Foto salva, mas {msg}"
        
        try:
            caminho_modelo = os.path.join("modelos_faciais", f"func_{funcionario_id}.yml")
            caminho_hist = os.path.join("modelos_faciais", f"func_{funcionario_id}_hist.npy")
            
            usou_lbph = False
            try:
                if hasattr(cv2, 'face'):
                    recognizer = cv2.face.LBPHFaceRecognizer_create(radius=1, neighbors=8, grid_x=8, grid_y=8)
                    recognizer.train([face], np.array([funcionario_id]))
                    recognizer.save(caminho_modelo)
                    usou_lbph = True
            except:
                usou_lbph = False
            
            # SEMPRE salva também o histograma como fallback
            hist = cv2.calcHist([face], [0], None, [256], [0, 256])
            cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
            np.save(caminho_hist, hist)
            
            conn = get_db()
            conn.execute("UPDATE funcionarios SET foto_perfil = ?, face_treinada = 1 WHERE id = ?",
                        (foto_perfil_caminho, funcionario_id))
            conn.commit()
            conn.close()
            
            metodo = "LBPH" if usou_lbph else "Histograma"
            return True, f"Modelo facial treinado com sucesso! ({metodo})"
        except Exception as e:
            conn = get_db()
            conn.execute("UPDATE funcionarios SET foto_perfil = ?, face_treinada = 0 WHERE id = ?",
                        (foto_perfil_caminho, funcionario_id))
            conn.commit()
            conn.close()
            return False, f"Foto salva, mas erro no treinamento: {str(e)}"
    
    def verificar_face(funcionario_id, img_bytes):
        caminho_modelo = os.path.join("modelos_faciais", f"func_{funcionario_id}.yml")
        caminho_hist = os.path.join("modelos_faciais", f"func_{funcionario_id}_hist.npy")
        caminho_foto = os.path.join("static/fotos", f"func_{funcionario_id}.jpg")
        
        tem_modelo = os.path.exists(caminho_modelo)
        tem_hist = os.path.exists(caminho_hist)
        tem_foto = os.path.exists(caminho_foto)
        
        if not tem_foto and not tem_modelo and not tem_hist:
            return False, 0, "Funcionário não tem foto cadastrada. Contate o admin."
        
        face, sucesso, msg = detectar_face_opencv(img_bytes)
        if not sucesso:
            return False, 0, msg
        
        try:
            # Tenta 1: LBPH se disponível
            if tem_modelo and hasattr(cv2, 'face'):
                try:
                    recognizer = cv2.face.LBPHFaceRecognizer_create()
                    recognizer.read(caminho_modelo)
                    label, confianca = recognizer.predict(face)
                    if label == funcionario_id and confianca < LIMIAR_CONFIANCA_FACIAL:
                        return True, confianca, f"Reconhecido! Confiança: {confianca:.1f}"
                except:
                    pass
            
            # Tenta 2: Comparação de histograma (fallback)
            if tem_hist:
                hist_armazenado = np.load(caminho_hist)
                hist_novo = cv2.calcHist([face], [0], None, [256], [0, 256])
                cv2.normalize(hist_novo, hist_novo, 0, 1, cv2.NORM_MINMAX)
                
                # Compara usando correlação (quanto mais próximo de 1, melhor)
                correlacao = cv2.compareHist(hist_armazenado, hist_novo, cv2.HISTCMP_CORREL)
                
                # Também compara com ORB para mais robustez
                similaridade_orb = 0.5
                try:
                    img_original = cv2.imread(caminho_foto, cv2.IMREAD_GRAYSCALE)
                    if img_original is not None:
                        orb = cv2.ORB_create(nfeatures=100)
                        kp1, des1 = orb.detectAndCompute(img_original, None)
                        kp2, des2 = orb.detectAndCompute(face, None)
                        if des1 is not None and des2 is not None and len(des1) > 0 and len(des2) > 0:
                            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
                            matches = bf.match(des1, des2)
                            if len(kp1) > 0:
                                similaridade_orb = min(1.0, len(matches) / max(len(kp1), len(kp2), 1))
                except:
                    pass
                
                # Score combinado
                score = (correlacao * 0.7) + (similaridade_orb * 0.3)
                confianca_calculada = (1.0 - score) * 100
                
                limiar_score = 0.55
                
                if score >= limiar_score:
                    return True, confianca_calculada, f"Reconhecido! Similaridade: {score*100:.1f}%"
                else:
                    return False, confianca_calculada, f"Rosto não reconhecido. Similaridade: {score*100:.1f}%"
            
            if tem_foto:
                return False, 0, "Foto cadastrada mas modelo não treinado. Contate o admin."
            
            return False, 0, "Modelo facial não disponível."
            
        except Exception as e:
            return False, 0, f"Erro verificação: {str(e)}"
    
    def modelo_face_existe(funcionario_id):
        return (os.path.exists(os.path.join("modelos_faciais", f"func_{funcionario_id}.yml")) or
                os.path.exists(os.path.join("modelos_faciais", f"func_{funcionario_id}_hist.npy")))
    
    print("[FACE REC] OpenCV disponível - Reconhecimento facial ATIVO")

except ImportError:
    FACE_REC_DISPONIVEL = False
    print("[FACE REC] OpenCV NÃO disponível")
    
    def detectar_face_opencv(img_bytes, tamanho_alvo=(200, 200)):
        return None, False, "OpenCV não instalado"
    def treinar_modelo_face(funcionario_id, img_bytes):
        return False, "OpenCV não instalado no servidor"
    def verificar_face(funcionario_id, img_bytes):
        return False, 0, "OpenCV não instalado no servidor"
    def modelo_face_existe(funcionario_id):
        return False

# ===================== FUNCOES AUXILIARES =====================
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
HISTORICO_JSON = "historico_ponto.json"
BACKUP_DIR = "backups"
BACKUP_INTERVAL_HORAS = 24

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
        horario_saida TEXT DEFAULT '18:00:00',
        foto_perfil TEXT DEFAULT '',
        face_treinada INTEGER DEFAULT 0,
        bloqueado INTEGER DEFAULT 0,
        tentativas_reconhecimento INTEGER DEFAULT 0,
        motivo_bloqueio TEXT DEFAULT ''
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
        autorizado_admin INTEGER DEFAULT 0,
        admin_resposta TEXT DEFAULT '',
        foto_verificacao TEXT DEFAULT '',
        confianca_facial REAL DEFAULT 0
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
    
    for coluna, tipo in [
        ("foto_perfil","TEXT DEFAULT ''"),("face_treinada","INTEGER DEFAULT 0"),
        ("bloqueado","INTEGER DEFAULT 0"),("tentativas_reconhecimento","INTEGER DEFAULT 0"),
        ("motivo_bloqueio","TEXT DEFAULT ''")
    ]:
        try:
            conn.execute(f"ALTER TABLE funcionarios ADD COLUMN {coluna} {tipo}")
            print(f"[MIG FUNC] {coluna} adicionada")
        except: pass
    
    for coluna, tipo in [
        ("minutos_atraso","INTEGER DEFAULT 0"),("minutos_banco_horas","INTEGER DEFAULT 0"),
        ("justificativa","TEXT DEFAULT ''"),("ip_dispositivo","TEXT DEFAULT ''"),
        ("user_agent","TEXT DEFAULT ''"),("horario_acesso","TEXT DEFAULT ''"),
        ("autorizado_admin","INTEGER DEFAULT 0"),("admin_resposta","TEXT DEFAULT ''"),
        ("foto_verificacao","TEXT DEFAULT ''"),("confianca_facial","REAL DEFAULT 0")
    ]:
        try:
            conn.execute(f"ALTER TABLE registros_ponto ADD COLUMN {coluna} {tipo}")
            print(f"[MIG REG] {coluna} adicionada")
        except: pass
    
    conn.commit()
    conn.close()

init_db()
criar_logo_padrao()

def formatar_cpf(cpf):
    return ''.join(filter(str.isdigit, str(cpf)))

def verificar_atraso(hora_registro, horario_padrao, tolerancia_minutos=0):
    """Verifica se há atraso. Se tolerância > 0, só considera atrasado se ultrapassar a tolerância."""
    try:
        h_r = hora_registro.split(":"); h_p = horario_padrao.split(":")
        t_r = int(h_r[0])*3600 + int(h_r[1])*60 + (int(h_r[2]) if len(h_r)>2 else 0)
        t_p = int(h_p[0])*3600 + int(h_p[1])*60 + (int(h_p[2]) if len(h_p)>2 else 0)
        tolerancia_seg = tolerancia_minutos * 60
        return t_r > (t_p + tolerancia_seg)
    except: return False

def calcular_diferenca_segundos(hora1, hora2):
    """Calcula diferença em segundos entre dois horários HH:MM:SS"""
    try:
        h1 = hora1.split(":"); h2 = hora2.split(":")
        t1 = int(h1[0])*3600 + int(h1[1])*60 + (int(h1[2]) if len(h1)>2 else 0)
        t2 = int(h2[0])*3600 + int(h2[1])*60 + (int(h2[2]) if len(h2)>2 else 0)
        return t1 - t2
    except: return 0

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

# ===== BLOQUEIO/DESBLOQUEIO =====
def incrementar_tentativas_face(funcionario_id):
    conn = get_db()
    func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (funcionario_id,)).fetchone()
    if not func:
        conn.close()
        return False, "Funcionário não encontrado"
    
    novas_tentativas = (func["tentativas_reconhecimento"] or 0) + 1
    bloqueado = 0; motivo = ""
    
    if novas_tentativas >= MAX_TENTATIVAS_FACIAIS:
        bloqueado = 1
        motivo = f"Bloqueado por {MAX_TENTATIVAS_FACIAIS} tentativas de reconhecimento facial falhas"
        print(f"[BLOQUEIO] {func['nome']} (ID:{funcionario_id})")
    
    conn.execute("UPDATE funcionarios SET tentativas_reconhecimento = ?, bloqueado = ?, motivo_bloqueio = ? WHERE id = ?",
                 (novas_tentativas, bloqueado, motivo, funcionario_id))
    conn.commit(); conn.close()
    
    restantes = MAX_TENTATIVAS_FACIAIS - novas_tentativas
    if bloqueado:
        return True, f"🚫 BLOQUEADO! {motivo}. Apenas o administrador pode desbloquear."
    return False, f"Rosto não reconhecido. Tentativas restantes: {restantes}"

def resetar_tentativas_face(funcionario_id):
    conn = get_db()
    conn.execute("UPDATE funcionarios SET tentativas_reconhecimento = 0 WHERE id = ?", (funcionario_id,))
    conn.commit(); conn.close()

def desbloquear_funcionario(funcionario_id):
    conn = get_db()
    conn.execute("UPDATE funcionarios SET bloqueado = 0, tentativas_reconhecimento = 0, motivo_bloqueio = '' WHERE id = ?", (funcionario_id,))
    conn.commit()
    func = conn.execute("SELECT nome FROM funcionarios WHERE id = ?", (funcionario_id,)).fetchone()
    conn.close()
    if func:
        print(f"[DESBLOQUEIO] {func['nome']} (ID:{funcionario_id})")
        return True
    return False

def funcionario_bloqueado(funcionario_id):
    conn = get_db()
    func = conn.execute("SELECT bloqueado, motivo_bloqueio FROM funcionarios WHERE id = ?", (funcionario_id,)).fetchone()
    conn.close()
    if func and func["bloqueado"]:
        return True, func["motivo_bloqueio"] or "Funcionário bloqueado"
    return False, ""

# ===== AUTORIZACAO =====
def gerar_id_solicitacao():
    return hashlib.sha256(os.urandom(32)).hexdigest()[:16]

def limpar_autorizacoes_expiradas():
    agora = agora_brasilia()
    expiradas = [sid for sid, s in autorizacoes_pendentes.items() if s["expira"] <= agora]
    for sid in expiradas: del autorizacoes_pendentes[sid]
    return len(expiradas)

def criar_solicitacao_autorizacao(func, tipo, hora_registro, horario_padrao, minutos_diferenca, tipo_diferenca, ip_cliente, user_agent):
    limpar_autorizacoes_expiradas()
    sid = gerar_id_solicitacao()
    agora = agora_brasilia()
    for s in autorizacoes_pendentes.values():
        if s["funcionario_id"] == func["id"] and s["tipo"] == tipo and s["status"] == "pendente":
            return s["id"], False
    autorizacoes_pendentes[sid] = {
        "id": sid, "funcionario_id": func["id"], "nome": func["nome"], "cpf": func["cpf"],
        "tipo": tipo, "hora_registro": hora_registro, "horario_padrao": horario_padrao,
        "minutos_diferenca": minutos_diferenca, "tipo_diferenca": tipo_diferenca,
        "status": "pendente", "resposta_admin": "", "ip_cliente": ip_cliente, "user_agent": user_agent,
        "criado_em": agora.strftime("%Y-%m-%d %H:%M:%S"),
        "expira": agora + timedelta(seconds=TIMEOUT_AUTORIZACAO_SEGUNDOS), "respondido_em": None
    }
    print(f"[AUTORIZACAO] {sid[:8]}... {func['nome']} | {tipo} | {tipo_diferenca} {minutos_diferenca}min")
    return sid, True

def listar_autorizacoes_pendentes():
    limpar_autorizacoes_expiradas()
    return [{
        "id": s["id"], "nome": s["nome"], "cpf": s["cpf"], "tipo": s["tipo"],
        "tipo_formatado": s["tipo"].replace("_", " "), "hora_registro": s["hora_registro"],
        "horario_padrao": s["horario_padrao"], "minutos_diferenca": s["minutos_diferenca"],
        "tipo_diferenca": s["tipo_diferenca"], "criado_em": s["criado_em"],
        "segundos_restantes": max(0, int((s["expira"] - agora_brasilia()).total_seconds()))
    } for sid, s in autorizacoes_pendentes.items() if s["status"] == "pendente"]

def responder_autorizacao(sid, aprovar, resposta_admin=""):
    if sid not in autorizacoes_pendentes: return None, "Não encontrada"
    s = autorizacoes_pendentes[sid]
    if s["status"] != "pendente": return None, "Já respondida"
    s["status"] = "aprovado" if aprovar else "rejeitado"
    s["resposta_admin"] = resposta_admin
    s["respondido_em"] = agora_brasilia().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[AUTORIZACAO] {'APROVADA' if aprovar else 'REJEITADA'} | {s['nome']} | {s['tipo']}")
    return s, None

def verificar_status_autorizacao(sid):
    limpar_autorizacoes_expiradas()
    if sid not in autorizacoes_pendentes:
        return {"status": "expirado", "mensagem": "Solicitação expirada."}
    s = autorizacoes_pendentes[sid]
    if s["status"] == "pendente":
        return {"status": "pendente", "mensagem": "Aguardando autorização...",
                "segundos_restantes": max(0, int((s["expira"] - agora_brasilia()).total_seconds()))}
    return {"status": s["status"], "resposta_admin": s["resposta_admin"],
            "respondido_em": s["respondido_em"],
            "dados": {"funcionario_id": s["funcionario_id"], "cpf": s["cpf"], "tipo": s["tipo"],
                      "hora_registro": s["hora_registro"], "horario_padrao": s["horario_padrao"],
                      "minutos_diferenca": s["minutos_diferenca"], "tipo_diferenca": s["tipo_diferenca"]}}

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
    msgs = {"ENTRADA":"Ja registrou ENTRADA. Proximo: SAIDA ALMOCO ou SAIDA.",
            "SAIDA_ALMOCO":"Ja registrou SAIDA ALMOCO. Proximo: RETORNO ALMOCO.",
            "RETORNO_ALMOCO":"Ja registrou RETORNO. Proximo: SAIDA.",
            "SAIDA":"Ja registrou SAIDA hoje. Nova ENTRADA so amanha."}
    return False, msgs.get(ultimo_tipo, "Registro nao permitido.")

def gerar_sessao():
    return hashlib.sha256(os.urandom(64)).hexdigest()

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
    <span class="rodape-versao">v5.0 FACE ID</span>
  </div>
</div>
"""
ESTILOS_5D = """
.btn-3d { position: relative; border: none; border-radius: 14px; color: white; font-weight: bold; cursor: pointer; overflow: hidden; transform-style: preserve-3d; transition: all 0.3s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow: 0 6px 0 rgba(0,0,0,0.18), 0 10px 25px rgba(0,0,0,0.22), inset 0 2px 0 rgba(255,255,255,0.4), inset 0 -2px 0 rgba(0,0,0,0.08); }
.btn-3d::before { content:''; position:absolute; top:0; left:-100%; width:100%; height:100%; background:linear-gradient(90deg,transparent,rgba(255,255,255,0.35),transparent); transition:left 0.6s ease; }
.btn-3d:hover::before { left:100%; }
.btn-3d:hover { transform: translateY(-4px); box-shadow: 0 10px 0 rgba(0,0,0,0.18), 0 18px 35px rgba(0,0,0,0.28), inset 0 2px 0 rgba(255,255,255,0.4); }
.btn-3d:active { transform: translateY(2px); box-shadow: 0 2px 0 rgba(0,0,0,0.18), 0 4px 10px rgba(0,0,0,0.18), inset 0 2px 0 rgba(255,255,255,0.4); }
.btn-3d:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }
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
@keyframes pulsar-alerta { 0%,100%{box-shadow:0 0 0 0 rgba(255,87,34,0.7)} 50%{box-shadow:0 0 0 15px rgba(255,87,34,0)} }
.alerta-pulsante { animation: pulsar-alerta 1.5s infinite; }
@keyframes girar { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.spinner { display:inline-block; width:20px; height:20px; border:3px solid rgba(255,255,255,0.3); border-top-color:white; border-radius:50%; animation:girar 0.8s linear infinite; vertical-align:middle; margin-right:8px; }
.spinner-escuro { border-color:rgba(102,126,234,0.2); border-top-color:#667eea; }
.bloqueado { background:linear-gradient(135deg,#ffebee,#ffcdd2) !important; color:#b71c1c; }
.foto-perfil { width:100%; height:100%; object-fit:cover; border-radius:50%; }
.foto-perfil-container { width:72px; height:72px; border-radius:50%; overflow:hidden; border:3px solid white; box-shadow:0 10px 25px rgba(102,126,234,0.4); margin:0 auto 12px; background:linear-gradient(135deg,#667eea,#f093fb); display:flex; align-items:center; justify-content:center; color:white; font-size:30px; font-weight:bold; }
.foto-perfil-container img { width:100%; height:100%; object-fit:cover; }
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
<title>🔐 Login Admin</title>
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

# ===================== HTML - PAGINA PRINCIPAL =====================
def gerar_html_ponto():
    ts = str(int(agora_brasilia().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📱 Sistema de Ponto</title>
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
.data-hora { background:linear-gradient(135deg,#e8f0fe,#f3e8ff); color:#667eea; padding:15px; border-radius:14px; font-weight:bold; font-size:14px; margin-bottom:25px; text-align:center; border:2px solid rgba(102,126,234,0.2); }
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
<p class="subtitulo">👤 Reconhecimento Facial Obrigatório</p>
<div class="data-hora" id="dataHora">Carregando...</div>
<input type="text" id="cpf" class="input-moderno" placeholder="Digite seu CPF" maxlength="11" inputmode="numeric">
<div class="info-func" id="infoFunc"></div>
<button class="btn-3d btn-acessar" onclick="acessar()">🔓 ACESSAR</button>
<div class="mensagem" id="mensagem"></div>
<div class="dica">📸 Você precisará usar a câmera para reconhecimento facial</div>
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
      else{inf.textContent='⚠️ CPF NÃO cadastrado!';inf.className='info-func info-err';}
    }catch(e){inf.style.display='none';}
  }else inf.style.display='none';
});
async function acessar(){
  const c=cpfInp.value.replace(/\\D/g,''); const m=document.getElementById('mensagem');
  if(!c||c.length!==11){m.textContent='Digite um CPF válido!';m.className='mensagem erro';return;}
  try{
    const r=await fetch('/api/funcionario/acessar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cpf:c})});
    if(r.ok)window.location.href='/funcionario?cpf='+c;
    else{const e=await r.json();m.textContent=e.detail||'Erro';m.className='mensagem erro';}
  }catch(e){m.textContent='Erro de conexão!';m.className='mensagem erro';}
}
</script>
</body>
</html>"""

# ===================== HTML - PAINEL DO FUNCIONARIO (COM CAMERA E FACE ID) =====================
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
.status-wrapper { text-align:center; margin-bottom:15px; }
.status-acesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#2e7d32; padding:10px 16px; border-radius:25px; font-size:12px; font-weight:bold; display:inline-block; }
.status-bloqueado { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; padding:10px 16px; border-radius:25px; font-size:12px; font-weight:bold; display:inline-block; }
.horarios-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:18px; }
.horario-item { background:linear-gradient(135deg,#f5f7fa,#e4e8ec); padding:10px; border-radius:10px; text-align:center; }
.horario-label { font-size:10px; color:#888; text-transform:uppercase; font-weight:bold; }
.horario-valor { font-size:14px; color:#333; font-weight:bold; margin-top:3px; }
.data-hora { background:linear-gradient(135deg,#e8f0fe,#f3e8ff); color:#667eea; padding:14px; border-radius:14px; font-weight:bold; font-size:13px; margin-bottom:20px; text-align:center; border:2px solid rgba(102,126,234,0.2); }
.card-botoes { padding:25px; margin-bottom:20px; display:none; }
.card-botoes.visivel { display:block; animation:entrar-cima 0.5s ease; }
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
.atencao { background:linear-gradient(135deg,#fff8e1,#ffecb3); color:#e65100; display:block; border:1px solid #ffcc80; }
.disp-info { margin-top:15px; padding:12px; background:linear-gradient(135deg,#f3e5f5,#e1bee7); border-radius:12px; font-size:11px; color:#6a1b9a; text-align:center; border:1px solid #ce93d8; }

/* ===== TELA DE RECONHECIMENTO FACIAL (LOGIN) ===== */
.tela-face-login { display:block; }
.tela-face-login.oculta { display:none; }
.card-face-login { padding:30px 25px; margin-bottom:20px; text-align:center; }
.card-face-login.animar-entrar { animation-delay:0.1s; }
.card-face-login h2 { color:#333; font-size:20px; margin-bottom:8px; }
.card-face-login .sub { color:#666; font-size:13px; margin-bottom:20px; line-height:1.5; }
.camera-login-container { position:relative; width:100%; aspect-ratio:1; background:#000; border-radius:20px; overflow:hidden; margin:0 auto 18px; max-width:360px; box-shadow:0 15px 40px rgba(102,126,234,0.3); }
.camera-login-container video, .camera-login-container canvas { width:100%; height:100%; object-fit:cover; }
.camera-login-container .frame-overlay { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:75%; height:75%; border:4px dashed rgba(102,126,234,0.7); border-radius:50%; pointer-events:none; animation:pulsar-alerta 2s infinite; }
.camera-login-status { padding:14px; border-radius:12px; margin-bottom:15px; font-size:14px; font-weight:bold; }
.status-aguardando { background:linear-gradient(135deg,#e3f2fd,#bbdefb); color:#1565c0; }
.status-sucesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#2e7d32; }
.status-erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; }
.tentativas-info { text-align:center; font-size:13px; color:#888; margin-top:10px; }
.tentativas-info .restantes { color:#f44336; font-weight:bold; font-size:16px; }
.auto-capture-info { font-size:11px; color:#999; margin-top:12px; font-style:italic; }

/* ===== MODAL CAMERA PARA REGISTRO ===== */
.modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); backdrop-filter:blur(8px); z-index:1000; align-items:center; justify-content:center; padding:15px; }
.modal-overlay.ativo { display:flex; }
.modal-camera { background:white; border-radius:22px; padding:25px; width:100%; max-width:420px; box-shadow:0 30px 70px rgba(0,0,0,0.5); animation:entrar-cima 0.4s ease; }
.modal-camera h2 { color:#333; font-size:20px; margin-bottom:8px; text-align:center; }
.modal-camera .sub { color:#666; font-size:13px; margin-bottom:18px; text-align:center; }
.camera-container { position:relative; width:100%; aspect-ratio:1; background:#000; border-radius:16px; overflow:hidden; margin-bottom:15px; }
.camera-container video, .camera-container canvas { width:100%; height:100%; object-fit:cover; }
.camera-container .frame-overlay { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:70%; height:70%; border:3px dashed rgba(102,126,234,0.6); border-radius:50%; pointer-events:none; animation:pulsar-alerta 2s infinite; }
.modal-botoes { display:flex; gap:10px; }
.modal-botoes button { flex:1; padding:14px; border:none; border-radius:12px; font-weight:bold; cursor:pointer; font-size:14px; transition:all 0.3s; }
.btn-capturar { background:linear-gradient(135deg,#667eea,#764ba2); color:white; }
.btn-fechar { background:linear-gradient(135deg,#e0e0e0,#bdbdbd); color:#333; }

/* ===== TELA DE AUTORIZACAO ===== */
.tela-autorizacao { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:linear-gradient(135deg,rgba(102,126,234,0.97),rgba(118,75,162,0.97)); z-index:9999; align-items:center; justify-content:center; padding:20px; backdrop-filter:blur(10px); }
.tela-autorizacao.ativa { display:flex; }
.autorizacao-box { background:white; border-radius:28px; padding:35px 30px; width:100%; max-width:420px; text-align:center; box-shadow:0 30px 80px rgba(0,0,0,0.4); animation:entrar-cima 0.5s ease; }
.autorizacao-icone { width:100px; height:100px; border-radius:50%; background:linear-gradient(135deg,#ff9800,#ffc107); display:flex; align-items:center; justify-content:center; font-size:50px; margin:0 auto 20px; box-shadow:0 10px 30px rgba(255,152,0,0.4); animation:pulsar-alerta 2s infinite; }
.autorizacao-box h2 { color:#333; font-size:22px; margin-bottom:10px; }
.autorizacao-box .sub { color:#666; font-size:14px; margin-bottom:20px; line-height:1.5; }
.info-destaque { background:linear-gradient(135deg,#fff3e0,#ffe0b2); padding:18px; border-radius:16px; margin-bottom:20px; border:2px solid #ffcc80; }
.info-destaque .tipo-reg { font-size:18px; font-weight:bold; color:#e65100; margin-bottom:8px; }
.info-destaque .detalhes { font-size:13px; color:#bf360c; line-height:1.6; }
.info-destaque .minutos { font-size:28px; font-weight:bold; color:#f44336; margin:8px 0; }
.status-aguardando { background:linear-gradient(135deg,#e3f2fd,#bbdefb); padding:15px; border-radius:14px; margin-bottom:15px; border:2px solid #90caf9; }
.status-aguardando .texto { color:#1565c0; font-weight:bold; font-size:14px; }
.tempo-restante { font-size:12px; color:#0d47a1; margin-top:5px; }
.barra-progresso { width:100%; height:8px; background:#e3f2fd; border-radius:4px; margin-top:10px; overflow:hidden; }
.barra-progresso .preenchimento { height:100%; background:linear-gradient(90deg,#2196F3,#667eea); border-radius:4px; transition:width 1s linear; }
.resposta-admin { padding:15px; border-radius:12px; margin-top:15px; font-size:13px; font-style:italic; color:#555; background:#f5f5f5; border-left:4px solid #667eea; display:none; }
.resposta-admin.visivel { display:block; }
</style>
</head>
<body class="fundo-animado">
<div class="wrapper">

<!-- ===== TELA 1: RECONHECIMENTO FACIAL LOGIN ===== -->
<div class="tela-face-login" id="telaFaceLogin">
<div class="card-face-login card-3d animar-entrar">
<a href="/" class="voltar">← Trocar usuário</a>
<div class="foto-perfil-container" id="fotoContainerLogin">👤</div>
<h2 class="nome-func" id="nomeFuncLogin" style="font-size:20px;color:#333;margin-bottom:5px;">Carregando...</h2>
<p class="cpf-func" id="cpfFuncLogin" style="color:#888;font-size:13px;margin-bottom:20px;">CPF: ---</p>
<h2 style="color:#667eea;font-size:18px;">🔐 Verificação Facial</h2>
<p class="sub">Posicione seu rosto dentro do círculo<br>para confirmar sua identidade</p>
<div class="camera-login-container">
<video id="videoLogin" autoplay playsinline muted></video>
<canvas id="canvasLogin" style="display:none;"></canvas>
<div class="frame-overlay"></div>
</div>
<div class="camera-login-status status-aguardando" id="cameraLoginStatus">
<span class="spinner spinner-escuro"></span>Inicializando câmera...
</div>
<div class="tentativas-info" id="tentativasLoginInfo"></div>
<p class="auto-capture-info">📸 A captura será feita automaticamente em instantes</p>
</div>
</div>

<!-- ===== TELA 2: DADOS E BOTOES DE REGISTRO ===== -->
<div id="conteudoPrincipal" style="display:none;">
<div class="card-topo card-3d animar-entrar">
<div class="foto-perfil-container" id="fotoContainer">👤</div>
<h2 class="nome-func" id="nomeFunc" style="text-align:center;font-size:20px;color:#333;margin-bottom:5px;"></h2>
<p class="cpf-func" id="cpfFunc" style="text-align:center;color:#888;font-size:13px;margin-bottom:16px;"></p>
<div class="status-wrapper" id="statusWrapper"><span class="status-acesso">✅ Identidade confirmada</span></div>
<div class="horarios-grid" id="horariosInfo"></div>
</div>

<div class="card-botoes visivel">
<div class="data-hora" id="dataHora">Carregando...</div>
<h2>🎯 Selecione o Registro</h2>
<div class="botoes" id="botoesRegistro">
<button class="btn-3d btn-entrada" onclick="iniciarRegistro('ENTRADA')"><span class="icone-btn">✅</span>ENTRADA</button>
<button class="btn-3d btn-almoco" onclick="iniciarRegistro('SAIDA_ALMOCO')"><span class="icone-btn">🍽️</span>SAÍDA ALMOÇO</button>
<button class="btn-3d btn-retorno" onclick="iniciarRegistro('RETORNO_ALMOCO')"><span class="icone-btn">↩️</span>RETORNO ALMOÇO</button>
<button class="btn-3d btn-saida" onclick="iniciarRegistro('SAIDA')"><span class="icone-btn">🚪</span>SAÍDA</button>
</div>
<div class="mensagem" id="mensagem"></div>
<div class="disp-info">📸 Reconhecimento facial obrigatório em cada registro</div>
</div>
""" + RODAPE_WELL + """
</div>
</div>

<!-- ===== MODAL CAMERA PARA REGISTROS ===== -->
<div class="modal-overlay" id="modalCamera">
<div class="modal-camera">
<h2>📸 Verificação Facial</h2>
<p class="sub">Confirme sua identidade para registrar</p>
<div class="camera-container">
<video id="videoCamera" autoplay playsinline muted></video>
<canvas id="canvasCamera" style="display:none;"></canvas>
<div class="frame-overlay"></div>
</div>
<div class="camera-login-status status-aguardando" id="cameraStatus"><span class="spinner spinner-escuro"></span>Inicializando...</div>
<div class="tentativas-info" id="tentativasInfo"></div>
<div class="modal-botoes">
<button class="btn-fechar" onclick="fecharCamera()">❌ Cancelar</button>
<button class="btn-capturar" id="btnCapturar" onclick="capturarEVerificar()" disabled>📸 CAPTURAR</button>
</div>
</div>
</div>

<!-- ===== TELA DE AUTORIZACAO ===== -->
<div class="tela-autorizacao" id="telaAutorizacao">
<div class="autorizacao-box">
<div class="autorizacao-icone" id="autIcone">⏰</div>
<h2 id="autTitulo">Atenção! Horário Diferenciado</h2>
<p class="sub" id="autSubtitulo">Sua solicitação foi enviada para o administrador</p>
<div class="info-destaque">
<div class="tipo-reg" id="autTipoReg">ENTRADA</div>
<div class="detalhes" id="autDetalhes">Horário padrão: 08:00:00<br>Horário atual: 07:55:00</div>
<div class="minutos" id="autMinutos">5 min ANTES</div>
</div>
<div class="status-aguardando">
<div class="texto"><span class="spinner spinner-escuro"></span>Aguardando autorização do administrador...</div>
<div class="tempo-restante" id="autTempo">Tempo restante: 05:00</div>
<div class="barra-progresso"><div class="preenchimento" id="autBarra" style="width:100%"></div></div>
</div>
<div class="resposta-admin" id="autResposta"></div>
</div>
</div>

""" + SCRIPT_PARTICULAS + """
<script>
const QR="CLINICA_PONTO_2024";
const prm=new URLSearchParams(window.location.search);
const CPF=prm.get('cpf')||'';
let FUNC_ID=null;
let FUNC_NOME=null;
let TEM_FOTO=false;
let FACE_TREINADA=false;
let TIPO_ATUAL=null;
let streamCamera=null;
let streamLogin=null;
let idSolicitacaoAtual=null;
let pollingAutorizacao=null;
let tempoInicioAutorizacao=null;
let autoCapturaTimer=null;
const TEMPO_MAX_AUTORIZACAO=300;

function atualizarDH(){
  const el=document.getElementById('dataHora');
  if(!el)return;
  const o={weekday:'long',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'};
  el.textContent='🕐 '+new Date().toLocaleDateString('pt-BR',o);
}
setInterval(atualizarDH,1000);

async function carregar(){
  if(!CPF){window.location.href='/';return;}
  try{
    const r=await fetch('/api/buscar/'+CPF);const d=await r.json();
    if(!d.encontrado){window.location.href='/';return;}
    FUNC_ID=d.id;FUNC_NOME=d.nome;TEM_FOTO=!!d.foto_perfil;FACE_TREINADA=d.face_treinada;
    
    document.getElementById('nomeFuncLogin').textContent=d.nome;
    document.getElementById('cpfFuncLogin').textContent='CPF: '+CPF.replace(/(\\d{3})(\\d{3})(\\d{3})(\\d{2})/,'$1.$2.$3-$4');
    
    const fotoContainerLogin=document.getElementById('fotoContainerLogin');
    if(d.foto_perfil){
      fotoContainerLogin.innerHTML='<img src="'+d.foto_perfil+'?t='+Date.now()+'" class="foto-perfil" onerror="this.parentElement.innerHTML=\\''+d.nome.charAt(0).toUpperCase()+'\\'">';
    }else{
      fotoContainerLogin.textContent=d.nome.charAt(0).toUpperCase();
    }
    
    if(d.bloqueado){
      document.getElementById('cameraLoginStatus').className='camera-login-status status-erro';
      document.getElementById('cameraLoginStatus').innerHTML='🚫 VOCÊ ESTÁ BLOQUEADO!<br><small>Contate o administrador para desbloquear.</small>';
      return;
    }
    
    if(!FACE_TREINADA){
      document.getElementById('cameraLoginStatus').className='camera-login-status status-erro';
      document.getElementById('cameraLoginStatus').innerHTML='📸 Sem reconhecimento facial cadastrado.<br><small>Contate o administrador.</small>';
      return;
    }
    
    // Preenche também os dados da tela principal
    document.getElementById('nomeFunc').textContent=d.nome;
    document.getElementById('cpfFunc').textContent='CPF: '+CPF.replace(/(\\d{3})(\\d{3})(\\d{3})(\\d{2})/,'$1.$2.$3-$4');
    const fotoContainer=document.getElementById('fotoContainer');
    if(d.foto_perfil){
      fotoContainer.innerHTML='<img src="'+d.foto_perfil+'?t='+Date.now()+'" class="foto-perfil" onerror="this.parentElement.innerHTML=\\''+d.nome.charAt(0).toUpperCase()+'\\'">';
    }else{
      fotoContainer.textContent=d.nome.charAt(0).toUpperCase();
    }
    document.getElementById('horariosInfo').innerHTML=
      '<div class="horario-item"><div class="horario-label">Entrada</div><div class="horario-valor">🕐 '+d.horario_entrada+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída Almoço</div><div class="horario-valor">🍽️ '+d.horario_saida_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Retorno</div><div class="horario-valor">↩️ '+d.horario_retorno_almoco+'</div></div>'+
      '<div class="horario-item"><div class="horario-label">Saída</div><div class="horario-valor">🚪 '+d.horario_saida+'</div></div>';
    
    // Inicia a câmera de login automaticamente
    setTimeout(iniciarCameraLogin, 500);
    
  }catch(e){window.location.href='/';}
}
carregar();

async function iniciarCameraLogin(){
  const video=document.getElementById('videoLogin');
  const status=document.getElementById('cameraLoginStatus');

  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    status.className='camera-login-status status-erro';
    if(location.protocol!=='https:' && location.hostname!=='localhost' && location.hostname!=='127.0.0.1'){
      status.innerHTML='❌ ACESSO BLOQUEADO PELO NAVEGADOR!<br><small style="font-size:11px;font-weight:normal;"><strong>Motivo:</strong> Acesso via HTTP não seguro.<br><br>✅ SOLUÇÕES:<br>💻 PC: Acesse <strong>http://localhost:8000</strong><br>📱 Ou use <strong>HTTPS</strong> com certificado SSL<br>🔧 Chrome: chrome://flags/#unsafely-treat-insecure-origin-as-secure</small>';
    }else{
      status.innerHTML='❌ Navegador não suporta acesso à câmera!';
    }
    return;
  }

  const ehSeguro=location.protocol==='https:' || location.hostname==='localhost' || location.hostname==='127.0.0.1';
  if(!ehSeguro){
    status.className='camera-login-status status-erro';
    status.innerHTML='❌ ACESSO VIA HTTP NÃO SEGURO!<br><small style="font-size:11px;font-weight:normal;"><strong>Navegadores BLOQUEIAM câmera em HTTP.</strong><br><br>✅ Acesse: <strong>http://localhost:8000</strong> (no próprio PC)<br>✅ Ou configure <strong>HTTPS</strong> na rede<br>🔧 Chrome: ative flag de origem insegura</small>';
    return;
  }

  status.className='camera-login-status status-aguardando';
  status.innerHTML='<span class="spinner spinner-escuro"></span>📷 Pedindo permissão da câmera...<br><small style="font-size:11px;font-weight:normal;">👆 Procure o pop-up no topo → CLIQUE EM PERMITIR</small>';

  try{
    const opcoes=[
      {video:{facingMode:'user',width:{ideal:640},height:{ideal:640}},audio:false},
      {video:{facingMode:'user'},audio:false},
      {video:true,audio:false}
    ];
    let erroUltimo=null;
    for(let opcao of opcoes){
      try{streamLogin=await navigator.mediaDevices.getUserMedia(opcao);break;}
      catch(e){erroUltimo=e;streamLogin=null;}
    }
    if(!streamLogin) throw erroUltimo || new Error('Sem acesso');

    video.srcObject=streamLogin;
    await video.play();

    status.className='camera-login-status status-sucesso';
    status.innerHTML='✅ Câmera ativada! Capturando em 2s...<br><small style="font-size:11px;font-weight:normal;">Centralize seu rosto</small>';

    autoCapturaTimer=setTimeout(capturarLogin, 2000);

  }catch(err){
    status.className='camera-login-status status-erro';
    let dica='';
    if(err.name==='NotAllowedError')dica='<br>👉 Você NEGOU a permissão. Clique no 🔒 ao lado da URL → Permitir câmera';
    else if(err.name==='NotFoundError')dica='<br>👉 Nenhuma câmera encontrada';
    else if(err.name==='NotReadableError')dica='<br>👉 Câmera em uso por outro app';
    status.innerHTML='❌ '+err.message+dica;
  }
}

async function capturarLogin(){
  const video=document.getElementById('videoLogin');
  const canvas=document.getElementById('canvasLogin');
  const status=document.getElementById('cameraLoginStatus');
  const tentInfo=document.getElementById('tentativasLoginInfo');
  
  if(!streamLogin)return;
  
  canvas.width=video.videoWidth;
  canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);
  
  const imagemBase64=canvas.toDataURL('image/jpeg',0.8).split(',')[1];
  
  status.className='camera-login-status status-aguardando';
  status.innerHTML='<span class="spinner spinner-escuro"></span>Verificando identidade...';
  
  try{
    const r=await fetch('/api/verificar_face_login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cpf:CPF,funcionario_id:FUNC_ID,imagem:imagemBase64})
    });
    const d=await r.json();
    
    if(r.ok && d.reconhecido){
      status.className='camera-login-status status-sucesso';
      status.innerHTML='✅ Identidade confirmada! Confiança: '+(d.confianca||0).toFixed(1);
      
      // Para a câmera e mostra a tela principal
      setTimeout(()=>{
        if(streamLogin){streamLogin.getTracks().forEach(t=>t.stop());streamLogin=null;}
        document.getElementById('telaFaceLogin').classList.add('oculta');
        document.getElementById('conteudoPrincipal').style.display='block';
        atualizarDH();
      },1000);
      
    }else{
      status.className='camera-login-status status-erro';
      status.innerHTML='❌ '+(d.detail||'Rosto não reconhecido');
      
      if(d.tentativas_restantes!==undefined){
        tentInfo.innerHTML='Tentativas restantes: <span class="restantes">'+d.tentativas_restantes+'</span> de """ + str(MAX_TENTATIVAS_FACIAIS) + """';
      }
      
      if(d.bloqueado){
        setTimeout(()=>{
          if(streamLogin){streamLogin.getTracks().forEach(t=>t.stop());streamLogin=null;}
          status.innerHTML='🚫 BLOQUEADO!<br><small>Após """ + str(MAX_TENTATIVAS_FACIAIS) + """ tentativas falhas. Contate o administrador.</small>';
        },1500);
      }else{
        // Tenta novamente automaticamente após 2 segundos
        autoCapturaTimer=setTimeout(capturarLogin, 2500);
      }
    }
  }catch(e){
    status.className='camera-login-status status-erro';
    status.innerHTML='❌ Erro de conexão';
    autoCapturaTimer=setTimeout(capturarLogin, 3000);
  }
}

function travarBotoes(travar){
  document.querySelectorAll('#botoesRegistro button').forEach(b=>b.disabled=travar);
}

async function iniciarRegistro(tipo){
  if(!FUNC_ID)return;
  TIPO_ATUAL=tipo;
  await abrirCamera();
}

async function abrirCamera(){
  const modal=document.getElementById('modalCamera');
  const video=document.getElementById('videoCamera');
  const status=document.getElementById('cameraStatus');
  const btnCap=document.getElementById('btnCapturar');
  const tentInfo=document.getElementById('tentativasInfo');

  status.className='camera-login-status status-aguardando';
  btnCap.disabled=true;
  tentInfo.innerHTML='';

  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    status.className='camera-login-status status-erro';
    status.innerHTML='❌ Navegador não suporta câmera! Use localhost ou HTTPS.';
    modal.classList.add('ativo');
    setTimeout(fecharCamera,4000);
    return;
  }

  const ehSeguro=location.protocol==='https:' || location.hostname==='localhost' || location.hostname==='127.0.0.1';
  if(!ehSeguro){
    status.className='camera-login-status status-erro';
    status.innerHTML='❌ HTTP bloqueia câmera! Use http://localhost:8000 ou HTTPS';
    modal.classList.add('ativo');
    return;
  }

  modal.classList.add('ativo');
  status.innerHTML='<span class="spinner spinner-escuro"></span>📷 Pedindo permissão...';

  try{
    const opcoes=[
      {video:{facingMode:'user',width:{ideal:640},height:{ideal:640}},audio:false},
      {video:{facingMode:'user'},audio:false},
      {video:true,audio:false}
    ];
    let erroUltimo=null;
    for(let opcao of opcoes){
      try{streamCamera=await navigator.mediaDevices.getUserMedia(opcao);break;}
      catch(e){erroUltimo=e;streamCamera=null;}
    }
    if(!streamCamera) throw erroUltimo || new Error('Sem acesso');

    video.srcObject=streamCamera;
    await video.play();

    status.className='camera-login-status status-sucesso';
    status.innerHTML='✅ Câmera pronta! Posicione e capture';
    btnCap.disabled=false;

  }catch(err){
    status.className='camera-login-status status-erro';
    let dica='';
    if(err.name==='NotAllowedError')dica=' - Permissão negada';
    status.innerHTML='❌ '+err.message+dica;
    setTimeout(fecharCamera,4000);
  }
}

function fecharCamera(){
  const modal=document.getElementById('modalCamera');
  if(streamCamera){streamCamera.getTracks().forEach(t=>t.stop());streamCamera=null;}
  modal.classList.remove('ativo');
  TIPO_ATUAL=null;
}

async function capturarEVerificar(){
  const video=document.getElementById('videoCamera');
  const canvas=document.getElementById('canvasCamera');
  const status=document.getElementById('cameraStatus');
  const btnCap=document.getElementById('btnCapturar');
  const tentInfo=document.getElementById('tentativasInfo');
  
  btnCap.disabled=true;
  status.className='camera-login-status status-aguardando';
  status.innerHTML='<span class="spinner spinner-escuro"></span>Processando e verificando...';
  
  canvas.width=video.videoWidth;
  canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);
  
  const imagemBase64=canvas.toDataURL('image/jpeg',0.8).split(',')[1];
  
  try{
    const r=await fetch('/api/verificar_face',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cpf:CPF,funcionario_id:FUNC_ID,tipo:TIPO_ATUAL,imagem:imagemBase64})
    });
    const d=await r.json();
    
    if(r.ok && d.reconhecido){
      status.className='camera-login-status status-sucesso';
      status.innerHTML='✅ Rosto reconhecido! Confiança: '+(d.confianca||0).toFixed(1);
      setTimeout(()=>{
        fecharCamera();
        continuarRegistroAposFace(d.foto_salva||'',d.confianca||0);
      },800);
    }else{
      status.className='camera-login-status status-erro';
      status.innerHTML='❌ '+(d.detail||'Rosto não reconhecido');
      if(d.tentativas_restantes!==undefined){
        tentInfo.innerHTML='Tentativas restantes: <span class="restantes">'+d.tentativas_restantes+'</span> de """ + str(MAX_TENTATIVAS_FACIAIS) + """';
      }
      if(d.bloqueado){
        setTimeout(()=>{
          fecharCamera();
          mostrar('🚫 VOCÊ FOI BLOQUEADO!\\nApós """ + str(MAX_TENTATIVAS_FACIAIS) + """ tentativas falhas de reconhecimento facial.\\n\\nContate o administrador para desbloquear.','erro');
          setTimeout(()=>location.reload(),4000);
        },1500);
      }else{
        btnCap.disabled=false;
      }
    }
  }catch(e){
    status.className='camera-login-status status-erro';
    status.innerHTML='❌ Erro de conexão';
    btnCap.disabled=false;
  }
}

async function continuarRegistroAposFace(fotoSalva,confianca){
  travarBotoes(true);
  try{
    const r=await fetch('/api/solicitar_ponto',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cpf:CPF,tipo:TIPO_ATUAL,qr_code:QR,foto_verificacao:fotoSalva,confianca_facial:confianca})
    });
    const d=await r.json();
    
    if(!r.ok){mostrar(d.detail||'Erro','erro');travarBotoes(false);return;}
    
    if(d.requer_autorizacao){
      abrirTelaAutorizacao(d);
    }else{
      await finalizarRegistro(d.registro_id);
    }
  }catch(e){mostrar('Erro de conexão!','erro');travarBotoes(false);}
}

function abrirTelaAutorizacao(dados){
  idSolicitacaoAtual=dados.solicitacao_id;
  tempoInicioAutorizacao=Date.now();
  
  const tipoLabel=dados.tipo.replace('_',' ');
  document.getElementById('autTipoReg').textContent=tipoLabel;
  
  let textoDiferenca='';
  if(dados.tipo_diferenca==='antecipado'){
    document.getElementById('autTitulo').textContent='⏰ Entrando Antes do Horário';
    document.getElementById('autSubtitulo').textContent='Sua solicitação foi enviada para o administrador';
    textoDiferenca=dados.minutos_diferenca+' min ANTES do horário';
  }else{
    document.getElementById('autTitulo').textContent='⚠️ Atraso Detectado';
    document.getElementById('autSubtitulo').textContent='Sua solicitação foi enviada para o administrador';
    textoDiferenca=dados.minutos_diferenca+' min ATRASADO';
  }
  
  document.getElementById('autDetalhes').innerHTML=
    'Horário padrão: <strong>'+dados.horario_padrao+'</strong><br>'+
    'Horário atual: <strong>'+dados.hora_registro+'</strong>';
  document.getElementById('autMinutos').textContent=textoDiferenca;
  document.getElementById('autResposta').className='resposta-admin';
  document.getElementById('telaAutorizacao').classList.add('ativa');
  
  pollingAutorizacao=setInterval(verificarStatusAutorizacao,2000);
  atualizarTempoRestante();
}

function atualizarTempoRestante(){
  if(!tempoInicioAutorizacao)return;
  const decorrido=(Date.now()-tempoInicioAutorizacao)/1000;
  const restante=Math.max(0,TEMPO_MAX_AUTORIZACAO-decorrido);
  const min=Math.floor(restante/60);
  const seg=Math.floor(restante%60);
  document.getElementById('autTempo').textContent='Tempo restante: '+String(min).padStart(2,'0')+':'+String(seg).padStart(2,'0');
  document.getElementById('autBarra').style.width=(restante/TEMPO_MAX_AUTORIZACAO*100)+'%';
  
  if(restante<=0 && pollingAutorizacao){
    clearInterval(pollingAutorizacao);
    pollingAutorizacao=null;
    fecharTelaAutorizacao();
    mostrar('⏱️ Tempo esgotado! Solicitação expirada.','erro');
    travarBotoes(false);
    return;
  }
  if(pollingAutorizacao)setTimeout(atualizarTempoRestante,1000);
}

async function verificarStatusAutorizacao(){
  if(!idSolicitacaoAtual)return;
  try{
    const r=await fetch('/api/status_autorizacao/'+idSolicitacaoAtual);
    const d=await r.json();
    
    if(d.status==='aprovado'){
      clearInterval(pollingAutorizacao);pollingAutorizacao=null;
      if(d.resposta_admin){
        const respEl=document.getElementById('autResposta');
        respEl.textContent='📝 Admin: '+d.resposta_admin;
        respEl.className='resposta-admin visivel';
      }
      document.querySelector('.status-aguardando .texto').innerHTML='✅ <strong style="color:#2e7d32;">AUTORIZADO!</strong> Registrando...';
      document.getElementById('autIcone').style.background='linear-gradient(135deg,#4CAF50,#81c784)';
      document.getElementById('autIcone').textContent='✅';
      setTimeout(async ()=>{await finalizarRegistroComAutorizacao(idSolicitacaoAtual,d.resposta_admin||'');},1000);
    }else if(d.status==='rejeitado'){
      clearInterval(pollingAutorizacao);pollingAutorizacao=null;
      document.querySelector('.status-aguardando .texto').innerHTML='❌ <strong style="color:#c62828;">NEGADO!</strong>';
      document.getElementById('autIcone').style.background='linear-gradient(135deg,#f44336,#e57373)';
      document.getElementById('autIcone').textContent='❌';
      if(d.resposta_admin){
        const respEl=document.getElementById('autResposta');
        respEl.textContent='📝 Motivo: '+d.resposta_admin;
        respEl.className='resposta-admin visivel';
      }
      setTimeout(()=>{
        fecharTelaAutorizacao();
        mostrar('❌ Solicitação negada pelo administrador.'+(d.resposta_admin?'\\nMotivo: '+d.resposta_admin:''),'erro');
        travarBotoes(false);
      },2500);
    }else if(d.status==='expirado'){
      clearInterval(pollingAutorizacao);pollingAutorizacao=null;
      fecharTelaAutorizacao();
      mostrar('⏱️ Solicitação expirada!','erro');
      travarBotoes(false);
    }
  }catch(e){}
}

function fecharTelaAutorizacao(){
  document.getElementById('telaAutorizacao').classList.remove('ativa');
  document.getElementById('autIcone').style.background='';
  document.getElementById('autIcone').textContent='⏰';
}

async function finalizarRegistro(registroId){
  try{
    const r=await fetch('/api/obter_registro/'+registroId);
    const d=await r.json();
    if(r.ok && d.mensagem){
      let t='sucesso';
      if(d.mensagem.includes('Banco'))t='banco-horas';
      mostrar(d.mensagem,t);
    }
  }catch(e){}
  travarBotoes(false);
}

async function finalizarRegistroComAutorizacao(solicitacaoId,respostaAdmin){
  try{
    const r=await fetch('/api/executar_autorizado',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({solicitacao_id:solicitacaoId,resposta_admin:respostaAdmin})
    });
    const d=await r.json();
    if(r.ok){
      let t='sucesso';
      if(d.mensagem.includes('Banco'))t='banco-horas';
      mostrar(d.mensagem+'\\n\\n✅ COM AUTORIZAÇÃO DO ADMINISTRADOR',t);
    }else{
      mostrar(d.detail||'Erro','erro');
    }
  }catch(e){mostrar('Erro de conexão!','erro');}
  setTimeout(()=>{fecharTelaAutorizacao();travarBotoes(false);},1500);
}

function mostrar(texto,tipo){
  const m=document.getElementById('mensagem');
  m.textContent=texto;
  m.className='mensagem '+tipo;
  setTimeout(()=>m.className='mensagem',15000);
}
</script>
</body>
</html>"""

def gerar_html_admin():
    ts = str(int(agora_brasilia().timestamp()))
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚙️ Painel Administrativo</title>
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#667eea">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Ponto">
<link rel="icon" type="image/png" href="/static/logo.png">
<link rel="apple-touch-icon" href="/static/logo.png">
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

.alerta-autorizacoes { display:none; background:linear-gradient(135deg,#ff5722,#ff9800); color:white; padding:15px 25px; text-align:center; cursor:pointer; position:relative; overflow:hidden; box-shadow:0 4px 20px rgba(255,87,34,0.4); }
.alerta-autorizacoes.visivel { display:block; animation:entrar-cima 0.4s ease; }
.alerta-autorizacoes .conteudo { display:flex; align-items:center; justify-content:center; gap:12px; font-weight:bold; font-size:15px; }
.alerta-autorizacoes .icone { font-size:24px; }
.alerta-autorizacoes .badge { background:white; color:#ff5722; padding:3px 12px; border-radius:20px; font-weight:bold; font-size:13px; }

.container { max-width:1250px; margin:25px auto; padding:0 20px; }
.tabs { display:flex; gap:6px; margin-bottom:20px; flex-wrap:wrap; }
.tab { padding:12px 20px; background:#dde2e8; border:none; border-radius:12px 12px 0 0; cursor:pointer; font-weight:bold; font-size:13px; color:#555; transition:all 0.3s; position:relative; }
.tab:hover { background:#cfd6de; transform:translateY(-2px); }
.tab.ativo { background:white; color:#667eea; box-shadow:0 -4px 15px rgba(0,0,0,0.08); }
.tab .badge-aut { position:absolute; top:-5px; right:-5px; background:#ff5722; color:white; width:20px; height:20px; border-radius:50%; font-size:11px; display:flex; align-items:center; justify-content:center; display:none; }
.tab .badge-aut.visivel { display:flex; }

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
.linha-bloqueada { background-color:#ffebee !important; }
.linha-bloqueada td { color:#b71c1c; }
.badge-status { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:bold; }
.badge-ativo { background:#e8f5e9; color:#2e7d32; }
.badge-bloqueado { background:#ffebee; color:#b71c1c; }
.badge-sem-foto { background:#fff3e0; color:#e65100; }
.foto-miniatura { width:36px; height:36px; border-radius:50%; object-fit:cover; border:2px solid #ddd; }
.sem-foto { width:36px; height:36px; border-radius:50%; background:#eee; display:flex; align-items:center; justify-content:center; font-size:14px; color:#999; font-weight:bold; }

.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:22px; }
@media(max-width:700px){.grid-2{grid-template-columns:1fr}}
.mensagem { padding:14px; border-radius:12px; margin:12px 0; display:none; font-weight:bold; font-size:14px; }
.sucesso { background:linear-gradient(135deg,#e8f5e9,#c8e6c9); color:#1b5e20; display:block; border:1px solid #a5d6a7; }
.erro { background:linear-gradient(135deg,#ffebee,#ffcdd2); color:#b71c1c; display:block; border:1px solid #ef9a9a; }
.qr-info { background:linear-gradient(135deg,#e3f2fd,#bbdefb); padding:16px; border-radius:12px; margin:15px 0; word-break:break-all; border:1px solid #90caf9; }
.card { background:linear-gradient(135deg,#fafafa,#f5f7fa); padding:18px; border-radius:14px; margin:12px 0; border-left:4px solid #667eea; box-shadow:0 3px 10px rgba(0,0,0,0.05); }
.horarios-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.info-box { background:linear-gradient(135deg,#fff8e1,#ffecb3); border-left:4px solid #ffc107; padding:14px; border-radius:8px; margin:15px 0; font-size:13px; color:#856404; }
.filtros { display:flex; gap:10px; margin-bottom:15px; flex-wrap:wrap; align-items:center; }
.filtros input, .filtros select { margin:0; width:auto; min-width:150px; }
label { font-size:13px; color:#555; font-weight:bold; display:block; margin-top:8px; }

/* Upload de foto */
.upload-foto { border:3px dashed #ccc; border-radius:14px; padding:20px; text-align:center; cursor:pointer; transition:all 0.3s; margin:10px 0; }
.upload-foto:hover { border-color:#667eea; background:#f5f7fa; }
.upload-foto.drag { border-color:#667eea; background:#e8f0fe; }
.upload-foto .icone { font-size:40px; color:#999; margin-bottom:8px; }
.upload-foto .texto { color:#666; font-size:13px; }
.preview-foto { max-width:150px; max-height:150px; border-radius:12px; margin:10px auto; display:none; border:3px solid #667eea; }
.preview-foto.visivel { display:block; }

.lista-autorizacoes { display:grid; gap:15px; margin-top:15px; }
.card-autorizacao { background:linear-gradient(135deg,#fff8e1,#ffe0b2); border-left:5px solid #ff9800; border-radius:14px; padding:20px; box-shadow:0 4px 15px rgba(255,152,0,0.15); position:relative; }
.card-autorizacao.urgente { animation:pulsar-alerta 1.5s infinite; }
.card-autorizacao .cabecalho { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:12px; }
.card-autorizacao .nome { font-size:18px; font-weight:bold; color:#bf360c; }
.card-autorizacao .tipo { display:inline-block; padding:4px 12px; border-radius:20px; font-size:12px; font-weight:bold; color:white; }
.card-autorizacao .detalhes { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:15px; font-size:13px; }
.card-autorizacao .det-item { background:rgba(255,255,255,0.6); padding:8px 12px; border-radius:8px; }
.card-autorizacao .det-label { font-size:10px; color:#888; text-transform:uppercase; font-weight:bold; }
.card-autorizacao .det-valor { font-weight:bold; color:#333; margin-top:2px; }
.card-autorizacao .diferenca { text-align:center; padding:12px; background:linear-gradient(135deg,#ff5722,#f44336); color:white; border-radius:10px; font-weight:bold; margin-bottom:15px; }
.card-autorizacao .acoes { display:flex; gap:10px; }
.card-autorizacao .acoes button { flex:1; padding:12px; font-size:13px; }
.card-autorizacao textarea { width:100%; margin-bottom:10px; min-height:60px; resize:vertical; }
.tempo-urgencia { position:absolute; top:15px; right:15px; font-size:11px; color:#e65100; font-weight:bold; background:rgba(255,255,255,0.7); padding:3px 8px; border-radius:10px; }
.vazio-aut { text-align:center; padding:40px; color:#999; font-size:14px; }
.vazio-aut .icone { font-size:48px; margin-bottom:10px; opacity:0.5; }
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

<div class="alerta-autorizacoes" id="alertaAut" onclick="abrir('autorizacoes',this)">
<div class="conteudo">
<span class="icone">⏰</span>
<span><span id="qtdAut">0</span> SOLICITAÇÃO(ÕES) PENDENTE(S)!</span>
<span class="badge" id="badgeAut">0</span>
<span style="margin-left:10px;font-size:12px;opacity:0.9;">→ Clique para ver</span>
</div>
</div>

<div class="container">
<div class="tabs">
<button class="tab ativo" onclick="abrir('cadastro',this)">👤 Cadastrar</button>
<button class="tab" onclick="abrir('funcionarios',this)">📋 Funcionários</button>
<button class="tab" onclick="abrir('registros',this)">📊 Registros</button>
<button class="tab" onclick="abrir('autorizacoes',this)">⏰ Autorizações<span class="badge-aut" id="tabBadgeAut">0</span></button>
<button class="tab" onclick="abrir('relatorios',this)">📄 Relatórios</button>
<button class="tab" onclick="abrir('qrcode',this)">📱 QR Code</button>
<button class="tab" onclick="abrir('acessos',this)">📡 Acessos</button>
<button class="tab" onclick="abrir('config',this)">🔧 Configurações</button>
</div>

<div id="cadastro" class="painel ativo">
<h2>👤 Cadastrar Novo Funcionário</h2>
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

<label style="margin-top:18px;">📸 Foto de Rosto (OBRIGATÓRIA para reconhecimento facial):</label>
<div class="upload-foto" id="uploadFoto" onclick="document.getElementById('fotoInput').click()">
<div class="icone">📷</div>
<div class="texto">Clique para enviar uma foto de rosto<br><small style="color:#999;">Formatos: JPG, PNG | Use foto bem iluminada, só o rosto</small></div>
</div>
<input type="file" id="fotoInput" accept="image/*" style="display:none;">
<img id="previewFoto" class="preview-foto" alt="Preview">

<button class="btn-success" onclick="cadastrar()">💾 Salvar Cadastro</button>
</div>

<div id="funcionarios" class="painel">
<h2>📋 Funcionários Cadastrados</h2>
<button onclick="carregarFuncs()">🔄 Atualizar Lista</button>
<table><thead><tr><th>Foto</th><th>ID</th><th>Nome</th><th>CPF</th><th>Entrada</th><th>Status</th><th>Tentativas</th><th>Ações</th></tr></thead><tbody id="tbodyFunc"></tbody></table>
</div>

<div id="registros" class="painel">
<h2>📊 Todos os Registros de Ponto</h2>
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
<table><thead><tr><th>Funcionário</th><th>CPF</th><th>Data</th><th>Hora</th><th>Tipo</th><th>Atrasado</th><th>Min.</th><th>Banco</th><th>Face</th><th>Aut.</th><th>IP</th></tr></thead><tbody id="tbodyReg"></tbody></table>
</div>

<div id="autorizacoes" class="painel">
<h2>⏰ Solicitações de Autorização Pendentes</h2>
<div class="info-box">
<strong>ℹ️ Como funciona:</strong> Quando um funcionário tenta registrar ponto fora do horário padrão, sua tela fica bloqueada até que você aprove ou rejeite.
</div>
<button onclick="carregarAutorizacoes()">🔄 Atualizar</button>
<div class="lista-autorizacoes" id="listaAut">
<div class="vazio-aut"><div class="icone">✅</div>Nenhuma solicitação pendente.</div>
</div>
</div>

<div id="relatorios" class="painel">
<h2>📄 Gerar Relatórios em PDF</h2>
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
<h2>📡 Registros de Acesso</h2>
<button onclick="carregarAcessos()">🔄 Atualizar</button>
<table><thead><tr><th>Data/Hora</th><th>CPF</th><th>Funcionário</th><th>IP</th><th>Dispositivo</th><th>Tipo</th></tr></thead><tbody id="tbodyAcessos"></tbody></table>
</div>

<div id="config" class="painel">
<h2>🔧 Configurações</h2>
<div class="card">
<h3 style="margin-bottom:10px;">👤 Reconhecimento Facial</h3>
<p style="font-size:14px;line-height:1.6;">
• <strong>Obrigatório</strong> para todos os registros de ponto<br>
• Algoritmo: LBPH (OpenCV)<br>
• Limite de confiança: <strong>""" + str(LIMIAR_CONFIANCA_FACIAL) + """</strong><br>
• Tentativas antes do bloqueio: <strong>""" + str(MAX_TENTATIVAS_FACIAIS) + """</strong><br>
• Funcionários bloqueados só podem ser desbloqueados pelo admin
</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">🔐 Credenciais</h3>
<p style="font-size:14px;"><strong>Usuário:</strong> admin<br><strong>Senha:</strong> 3223ronte</p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">📱 Instalar como Aplicativo</h3>
<p style="font-size:14px;margin-bottom:10px;">Adicione este sistema como um app nativo na tela inicial do seu celular.</p>
<button class="btn-warning" id="btnInstalarPWA" style="display:none;margin-bottom:10px;">📲 Instalar App</button>
<p style="font-size:12px;color:#666;"><a href="/static/instalar-app.html" target="_blank" style="color:#667eea;">📖 Ver instruções passo a passo</a></p>
</div>
<div class="card">
<h3 style="margin-bottom:10px;">⏰ Horário do Servidor</h3>
<p style="font-size:14px;"><strong>Horário de Brasília:</strong> """ + agora_brasilia().strftime("%d/%m/%Y %H:%M:%S") + """</p>
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

let fotoBase64=null;
const fotoInput=document.getElementById('fotoInput');
const uploadFoto=document.getElementById('uploadFoto');
const previewFoto=document.getElementById('previewFoto');

fotoInput.addEventListener('change',function(e){
  const f=e.target.files[0];
  if(!f)return;
  if(f.size>5*1024*1024){alert('Arquivo muito grande! Máximo 5MB');return;}
  const r=new FileReader();
  r.onload=function(ev){
    fotoBase64=ev.target.result.split(',')[1];
    previewFoto.src=ev.target.result;
    previewFoto.classList.add('visivel');
  };
  r.readAsDataURL(f);
});

['dragenter','dragover'].forEach(ev=>uploadFoto.addEventListener(ev,e=>{e.preventDefault();uploadFoto.classList.add('drag');}));
['dragleave','drop'].forEach(ev=>uploadFoto.addEventListener(ev,e=>{e.preventDefault();uploadFoto.classList.remove('drag');}));
uploadFoto.addEventListener('drop',e=>{
  const f=e.dataTransfer.files[0];
  if(f){fotoInput.files=e.dataTransfer.files;fotoInput.dispatchEvent(new Event('change'));}
});

let audioAlerta=null;
// Som via Web Audio API (tocarSomNotificacao)

function abrir(n,btn){
  document.querySelectorAll('.painel').forEach(p=>p.classList.remove('ativo'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('ativo'));
  document.getElementById(n).classList.add('ativo');
  if(btn)btn.classList.add('ativo');
  if(n==='funcionarios')carregarFuncs();
  if(n==='registros')carregarRegs();
  if(n==='relatorios')carregarSel();
  if(n==='acessos')carregarAcessos();
  if(n==='autorizacoes')carregarAutorizacoes();
}

function sair(){document.cookie='sessao_admin=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';window.location.href='/admin';}
function msg(id,texto,tipo){const e=document.getElementById(id);e.textContent=texto;e.className='mensagem '+tipo;setTimeout(()=>e.className='mensagem',4000);}

async function cadastrar(){
  const d={
    nome:document.getElementById('nome').value.trim(),
    cpf:document.getElementById('cpfCad').value.replace(/\\D/g,''),
    horario_entrada:document.getElementById('hEntrada').value.trim()||'08:00:00',
    horario_saida_almoco:document.getElementById('hSaidaAlmoco').value.trim()||'12:00:00',
    horario_retorno_almoco:document.getElementById('hRetornoAlmoco').value.trim()||'13:00:00',
    horario_saida:document.getElementById('hSaida').value.trim()||'18:00:00',
    foto_base64:fotoBase64
  };
  if(!d.nome||!d.cpf){msg('msgCad','Preencha nome e CPF!','erro');return;}
  if(d.cpf.length!==11){msg('msgCad','CPF deve ter 11 dígitos!','erro');return;}
  if(!d.foto_base64){msg('msgCad','📸 A foto de rosto é OBRIGATÓRIA!','erro');return;}
  
  const r=await fetch('/api/funcionarios',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  if(r.ok){
    msg('msgCad','✅ Funcionário cadastrado com foto!','sucesso');
    document.getElementById('nome').value='';document.getElementById('cpfCad').value='';
    document.getElementById('fotoInput').value='';previewFoto.classList.remove('visivel');fotoBase64=null;
  }else{const e=await r.json();msg('msgCad','❌ '+(e.detail||'Erro'),'erro');}
}

async function carregarFuncs(){
  const r=await fetch('/api/funcionarios');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const tb=document.getElementById('tbodyFunc');
  if(d.length===0){tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:#999;padding:20px;">Nenhum cadastrado.</td></tr>';return;}
  tb.innerHTML=d.map(f=>{
    const fotoHtml=f.foto_perfil
      ?'<img src="'+f.foto_perfil+'?t='+Date.now()+'" class="foto-miniatura">'
      :'<div class="sem-foto">📷</div>';
    let statusBadge='';
    if(f.bloqueado)statusBadge='<span class="badge-status badge-bloqueado">🚫 BLOQUEADO</span>';
    else if(!f.face_treinada && !f.foto_perfil)statusBadge='<span class="badge-status badge-sem-foto">📷 Sem foto</span>';
    else if(!f.face_treinada && f.foto_perfil)statusBadge='<span class="badge-status badge-sem-foto" style="background:#e3f2fd;color:#1565c0;">📷 Foto cadastrada</span>';
    else statusBadge='<span class="badge-status badge-ativo">✅ Ativo</span>';
    
    const linhaClasse=f.bloqueado?'linha-bloqueada':'';
    const tentativas=f.tentativas_reconhecimento||0;
    const tentHtml=tentativas>0?'<strong style="color:#f44336;">'+tentativas+'/""" + str(MAX_TENTATIVAS_FACIAIS) + """</strong>':'0/""" + str(MAX_TENTATIVAS_FACIAIS) + """';
    
    const acoes=f.bloqueado
      ?'<button class="btn-success btn-small" onclick="desbloquear('+f.id+')">🔓 Desbloquear</button>'
      :'<button class="btn-danger btn-small" onclick="excluir('+f.id+')">Excluir</button>';
    
    return '<tr class="'+linhaClasse+'"><td>'+fotoHtml+'</td><td>'+f.id+'</td><td><strong>'+f.nome+'</strong></td><td>'+f.cpf+'</td><td>'+f.horario_entrada+'</td><td>'+statusBadge+'</td><td>'+tentHtml+'</td><td>'+acoes+'</td></tr>';
  }).join('');
}

async function excluir(id){if(confirm('Tem CERTEZA? Todos os registros serão APAGADOS!')){const r=await fetch('/api/funcionarios/'+id,{method:'DELETE'});if(r.status===401){window.location.href='/admin';return;}carregarFuncs();}}

async function desbloquear(id){
  if(!confirm('Deseja realmente DESBLOQUEAR este funcionário?\\nAs tentativas de reconhecimento serão resetadas.'))return;
  const r=await fetch('/api/funcionarios/desbloquear/'+id,{method:'POST'});
  if(r.status===401){window.location.href='/admin';return;}
  if(r.ok)carregarFuncs();
  else{const e=await r.json();alert('Erro: '+(e.detail||''));}
}

async function carregarRegs(){
  const r=await fetch('/api/registros');if(r.status===401){window.location.href='/admin';return;}
  let d=await r.json();
  const fn=document.getElementById('filtroNome').value.toLowerCase();
  const ft=document.getElementById('filtroTipo').value;
  if(fn)d=d.filter(x=>x.nome.toLowerCase().includes(fn));
  if(ft)d=d.filter(x=>x.tipo===ft);
  const tb=document.getElementById('tbodyReg');
  if(d.length===0){tb.innerHTML='<tr><td colspan="11" style="text-align:center;color:#999;padding:20px;">Nenhum registro.</td></tr>';return;}
  tb.innerHTML=d.map(function(r){
    const ct='tipo-'+r.tipo.toLowerCase().replace(/_/g,'-');
    const cf=r.dia_semana>=5?' fim-semana':'';
    const mh=r.minutos_atraso>0?'<span style="color:#f44336;font-weight:bold;">'+r.minutos_atraso+'</span>':'-';
    const bh=r.minutos_banco_horas>0?'<span style="color:#0c5460;font-weight:bold;">+'+r.minutos_banco_horas+'</span>':'-';
    const face=r.confianca_facial>0?'<span style="color:#4CAF50;font-weight:bold;">'+r.confianca_facial.toFixed(0)+'</span>':'-';
    const aut=r.autorizado_admin?'<span style="color:#4CAF50;font-weight:bold;">✅</span>':'-';
    const ip=r.ip_dispositivo||'<span style="color:#ccc;">-</span>';
    return '<tr class="'+cf+'"><td><strong>'+r.nome+'</strong></td><td>'+r.cpf+'</td><td>'+r.data+'</td><td>'+r.hora+'</td><td class="'+ct+'">'+r.tipo_formatado+'</td><td>'+(r.atrasado?'⚠️':'✅')+'</td><td>'+mh+'</td><td>'+bh+'</td><td>'+face+'</td><td>'+aut+'</td><td style="font-size:11px;color:#888;">'+ip+'</td></tr>';
  }).join('');
}

async function carregarSel(){
  const r=await fetch('/api/funcionarios');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const s=document.getElementById('selFunc');
  if(d.length===0){s.innerHTML='<option value="">Cadastre funcionários primeiro</option>';return;}
  s.innerHTML=d.map(f=>'<option value="'+f.id+'">'+f.nome+' ('+f.cpf+')</option>').join('');
}

function gerarGeral(){const m=document.getElementById('mesAno').value;if(!m){alert('Selecione o mês!');return;}window.open('/api/pdf/geral?mes='+m,'_blank');}
function gerarInd(){const i=document.getElementById('selFunc').value;const m=document.getElementById('mesAnoFunc').value;if(!i||!m){alert('Preencha!');return;}window.open('/api/pdf/funcionario/'+i+'?mes='+m,'_blank');}

async function gerarQR(){
  const r=await fetch('/api/gerar_qrcode');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();
  if(d.detail)document.getElementById('qrImg').innerHTML='<p style="color:#f44336;">❌ '+d.detail+'</p>';
  else document.getElementById('qrImg').innerHTML='<img src="'+d.caminho+'?t='+Date.now()+'" style="max-width:250px;border:3px solid #ddd;border-radius:14px;">';
}

async function carregarAcessos(){
  const r=await fetch('/api/acessos');if(r.status===401){window.location.href='/admin';return;}
  const d=await r.json();const tb=document.getElementById('tbodyAcessos');
  if(d.length===0){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:#999;padding:20px;">Nenhum acesso.</td></tr>';return;}
  tb.innerHTML=d.map(a=>'<tr><td>'+a.data_hora+'</td><td>'+a.cpf+'</td><td>'+(a.nome||'-')+'</td><td style="font-size:11px;">'+a.ip+'</td><td style="font-size:10px;color:#888;max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="'+(a.user_agent||'').replace(/"/g,'&quot;')+'">'+(a.user_agent||'-')+'</td><td>'+a.tipo_acesso+'</td></tr>').join('');
}

let qtdAnterior=0;

// Preservar conteúdo dos textareas durante re-renderização
function preservarTextareas(){
  const dados={};
  document.querySelectorAll('.obs-autorizacao').forEach(t=>{if(t&&t.id)dados[t.id]=t.value;});
  return dados;
}
function restaurarTextareas(dados){
  Object.keys(dados).forEach(id=>{const t=document.getElementById(id);if(t)t.value=dados[id];});
}

// Som sofisticado via Web Audio API
let audioCtxPWA=null;
function tocarSomNotificacao(){
  try{
    if(!audioCtxPWA)audioCtxPWA=new (window.AudioContext||window.webkitAudioContext)();
    const ctx=audioCtxPWA;const now=ctx.currentTime;
    const notas=[523.25,659.25,783.99];
    notas.forEach((freq,i)=>{
      const osc=ctx.createOscillator();const gain=ctx.createGain();
      osc.type='sine';osc.frequency.value=freq;
      const inicio=now+i*0.15;
      gain.gain.setValueAtTime(0,inicio);
      gain.gain.linearRampToValueAtTime(0.12,inicio+0.05);
      gain.gain.exponentialRampToValueAtTime(0.001,inicio+1.2);
      osc.connect(gain);gain.connect(ctx.destination);
      osc.start(inicio);osc.stop(inicio+1.3);
    });
    const osc2=ctx.createOscillator();const gain2=ctx.createGain();
    osc2.type='triangle';osc2.frequency.value=1046.50;
    const inicio2=now+0.08;
    gain2.gain.setValueAtTime(0,inicio2);
    gain2.gain.linearRampToValueAtTime(0.04,inicio2+0.1);
    gain2.gain.exponentialRampToValueAtTime(0.001,inicio2+0.8);
    osc2.connect(gain2);gain2.connect(ctx.destination);
    osc2.start(inicio2);osc2.stop(inicio2+0.9);
  }catch(e){}
}

async function verificarAutorizacoesPendentes(){
  try{
    const r=await fetch('/api/autorizacoes_pendentes');
    if(r.status===401){window.location.href='/admin';return;}
    const d=await r.json();
    const qtd=d.length||0;
    document.getElementById('qtdAut').textContent=qtd;
    document.getElementById('badgeAut').textContent=qtd;
    document.getElementById('tabBadgeAut').textContent=qtd;
    const alerta=document.getElementById('alertaAut');
    const tabBadge=document.getElementById('tabBadgeAut');
    if(qtd>0){
      alerta.classList.add('visivel');tabBadge.classList.add('visivel');
      if(qtd>qtdAnterior && audioAlerta){try{audioAlerta.play().catch(()=>{});}catch(e){}}
    }else{alerta.classList.remove('visivel');tabBadge.classList.remove('visivel');}
    qtdAnterior=qtd;
    if(document.getElementById('autorizacoes').classList.contains('ativo')){
      const _textareasSalvos=preservarTextareas();
      renderizarAutorizacoes(d);
      restaurarTextareas(_textareasSalvos);
    }
  }catch(e){}
}

function carregarAutorizacoes(){verificarAutorizacoesPendentes();}

function renderizarAutorizacoes(lista){
  const el=document.getElementById('listaAut');
  if(!lista||lista.length===0){
    el.innerHTML='<div class="vazio-aut"><div class="icone">✅</div>Nenhuma solicitação pendente.</div>';
    return;
  }
  const coresTipo={'ENTRADA':'linear-gradient(135deg,#4CAF50,#66bb6a)','SAIDA_ALMOCO':'linear-gradient(135deg,#ff9800,#ffb74d)','RETORNO_ALMOCO':'linear-gradient(135deg,#2196F3,#64b5f6)','SAIDA':'linear-gradient(135deg,#f44336,#ef5350)'};
  el.innerHTML=lista.map(s=>{
    const urgente=s.segundos_restantes<60;
    const textoDif=s.tipo_diferenca==='antecipado'
      ? '⏰ '+s.minutos_diferenca+' MIN ANTES DO HORÁRIO'
      : '⚠️ ATRASO DE '+s.minutos_diferenca+' MIN';
    return '<div class="card-autorizacao'+(urgente?' urgente':'')+'" id="card-'+s.id+'">'+
      '<div class="tempo-urgencia">⏱️ '+Math.floor(s.segundos_restantes/60)+':'+String(s.segundos_restantes%60).padStart(2,'0')+'</div>'+
      '<div class="cabecalho"><div class="nome">👤 '+s.nome+'</div><span class="tipo" style="background:'+(coresTipo[s.tipo]||'#666')+'">'+s.tipo_formatado+'</span></div>'+
      '<div class="detalhes">'+
        '<div class="det-item"><div class="det-label">CPF</div><div class="det-valor">'+s.cpf+'</div></div>'+
        '<div class="det-item"><div class="det-label">Horário Padrão</div><div class="det-valor">🕐 '+s.horario_padrao+'</div></div>'+
        '<div class="det-item"><div class="det-label">Horário Atual</div><div class="det-valor">⏰ '+s.hora_registro+'</div></div>'+
        '<div class="det-item"><div class="det-label">Solicitado</div><div class="det-valor">'+s.criado_em+'</div></div>'+
      '</div>'+
      '<div class="diferenca">'+textoDif+'</div>'+
      '<textarea id="resp-'+s.id+'" class="obs-autorizacao" placeholder="📝 Escreva aqui a observação..." style="width:100%;margin-bottom:10px;min-height:60px;resize:vertical;padding:10px;border:2px solid #ddd;border-radius:10px;font-family:inherit;font-size:13px;"></textarea>'+
      '<div class="acoes">'+
        '<button class="btn-danger" onclick="responderAut(\\''+s.id+'\\',false)">❌ NEGAR</button>'+
        '<button class="btn-success" onclick="responderAut(\\''+s.id+'\\',true)">✅ APROVAR</button>'+
      '</div></div>';
  }).join('');
}

async function responderAut(sid,aprovar){
  const resp=document.getElementById('resp-'+sid);
  const observacao=resp?resp.value.trim():'';
  if(!aprovar && !observacao){if(!confirm('Negar sem observação?'))return;}
  try{
    const r=await fetch('/api/responder_autorizacao',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({solicitacao_id:sid,aprovar:aprovar,resposta:observacao})
    });
    if(r.status===401){window.location.href='/admin';return;}
    if(r.ok){
      const card=document.getElementById('card-'+sid);
      if(card){card.style.opacity='0.5';card.style.transform='scale(0.98)';
        setTimeout(()=>{carregarAutorizacoes();verificarAutorizacoesPendentes();},500);}
    }else{const d=await r.json();alert('Erro: '+(d.detail||''));}
  }catch(e){alert('Erro de conexão!');}
}

// Registrar Service Worker para PWA
if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/static/sw.js').catch(e=>{});});}
// Prompt de instalação PWA
let deferredPrompt=null;window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredPrompt=e;const b=document.getElementById('btnInstalarPWA');if(b){b.style.display='inline-block';b.onclick=async()=>{if(deferredPrompt){deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;}};}});

setInterval(verificarAutorizacoesPendentes,3000);
verificarAutorizacoesPendentes();
</script>
</body>
</html>"""

# ===================== BACKUP E HISTORICO =====================
os.makedirs(BACKUP_DIR, exist_ok=True)

def salvar_historico_json():
    try:
        conn = get_db()
        funcionarios = conn.execute("SELECT * FROM funcionarios ORDER BY id").fetchall()
        registros = conn.execute("SELECT * FROM registros_ponto ORDER BY data_hora").fetchall()
        acessos = conn.execute("SELECT * FROM acessos_dispositivos ORDER BY data_hora_acesso").fetchall()
        conn.close()
        dados = {"meta":{"versao_sistema":"5.0 FACE ID","ultima_atualizacao":agora_brasilia().strftime("%Y-%m-%d %H:%M:%S"),"total_funcionarios":len(funcionarios),"total_registros_ponto":len(registros)},"funcionarios":[dict(f) for f in funcionarios],"registros_ponto":[dict(r) for r in registros],"acessos_dispositivos":[dict(a) for a in acessos]}
        caminho_temp = HISTORICO_JSON + ".tmp"
        with open(caminho_temp, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2, default=str)
        os.replace(caminho_temp, HISTORICO_JSON)
        return True
    except Exception as e:
        print(f"[ERRO HISTORICO] {e}")
        return False

def fazer_backup_db():
    try:
        timestamp = agora_brasilia().strftime("%Y%m%d_%H%M%S")
        caminho_backup = os.path.join(BACKUP_DIR, f"ponto_backup_{timestamp}.db")
        conn = get_db()
        conn.execute("VACUUM INTO ?", (caminho_backup,))
        conn.close()
        print(f"[BACKUP] {caminho_backup}")
        return caminho_backup
    except Exception as e:
        print(f"[ERRO BACKUP] {e}")
        return None

def verificar_backup_periodico():
    try:
        arquivo_controle = os.path.join(BACKUP_DIR, ".ultimo_backup")
        agora = agora_brasilia()
        if os.path.exists(arquivo_controle):
            with open(arquivo_controle, "r") as f:
                ultima_data = datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
            if (agora - ultima_data).total_seconds() < BACKUP_INTERVAL_HORAS * 3600:
                return False
        caminho = fazer_backup_db()
        if caminho:
            with open(arquivo_controle, "w") as f:
                f.write(agora.strftime("%Y-%m-%d %H:%M:%S"))
            return True
        return False
    except: return False

salvar_historico_json()
verificar_backup_periodico()

# ===================== SERVIDOR HTTP =====================
class ServidorPonto(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        print(f"[{agora_brasilia().strftime('%H:%M:%S')}] {args[0]}")
    
    def do_GET(self):
        url = urlparse(self.path)
        caminho = url.path
        params = parse_qs(url.query)
        
        if caminho == "/" or caminho == "/index.html":
            responder_html(self, gerar_html_ponto()); return
        
        if caminho == "/funcionario":
            responder_html(self, gerar_html_funcionario()); return
        
        if caminho == "/admin":
            if verificar_login(self): responder_html(self, gerar_html_admin())
            else: responder_html(self, gerar_html_login())
            return
        
        if caminho == "/login":
            responder_html(self, gerar_html_login()); return
        
        if caminho.startswith("/static/"):
            nome_arquivo = caminho.replace("/static/", "").split("?")[0]
            caminho_completo = os.path.join("static", nome_arquivo)
            if os.path.exists(caminho_completo):
                self.send_response(200)
                if nome_arquivo.endswith(".png"): self.send_header("Content-Type", "image/png")
                elif nome_arquivo.endswith(".jpg") or nome_arquivo.endswith(".jpeg"): self.send_header("Content-Type", "image/jpeg")
                else: self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "no-cache")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
                with open(caminho_completo, "rb") as f: self.wfile.write(f.read())
            else:
                self.send_response(404)
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers()
            return
        
        if caminho.startswith("/api/buscar/"):
            cpf = formatar_cpf(caminho.replace("/api/buscar/", ""))
            if len(cpf) != 11:
                responder_json(self, {"encontrado": False}); return
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                conn.close()
                if func:
                    responder_json(self, {
                        "encontrado": True, "id": func["id"], "nome": func["nome"],
                        "horario_entrada": func["horario_entrada"],
                        "horario_saida_almoco": func["horario_saida_almoco"],
                        "horario_retorno_almoco": func["horario_retorno_almoco"],
                        "horario_saida": func["horario_saida"],
                        "foto_perfil": func["foto_perfil"] or "",
                        "face_treinada": bool(func["face_treinada"] or 0),
                        "bloqueado": bool(func["bloqueado"] or 0)
                    })
                else:
                    responder_json(self, {"encontrado": False})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/status_autorizacao/"):
            sid = caminho.replace("/api/status_autorizacao/", "")
            responder_json(self, verificar_status_autorizacao(sid))
            return
        
        rotas_admin = ["/api/funcionarios", "/api/registros", "/api/gerar_qrcode", "/api/pdf/geral", "/api/logout", "/api/acessos", "/api/autorizacoes_pendentes"]
        precisa_login = (caminho in rotas_admin or caminho.startswith("/api/funcionarios/") or caminho.startswith("/api/pdf/funcionario/") or caminho.startswith("/api/obter_registro/"))
        
        if precisa_login and not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado."}, status=401); return
        
        if caminho == "/api/autorizacoes_pendentes":
            responder_json(self, listar_autorizacoes_pendentes()); return
        
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
                    "horario_saida": f["horario_saida"],
                    "foto_perfil": f["foto_perfil"] or "",
                    "face_treinada": bool(f["face_treinada"] or 0),
                    "bloqueado": bool(f["bloqueado"] or 0),
                    "tentativas_reconhecimento": f["tentativas_reconhecimento"] or 0,
                    "motivo_bloqueio": f["motivo_bloqueio"] or ""
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
                    ORDER BY r.data_hora DESC LIMIT 500
                """).fetchall()
                conn.close()
                resultado = []
                for r in regs:
                    dh = datetime.strptime(r["data_hora"], "%Y-%m-%d %H:%M:%S")
                    resultado.append({
                        "nome": r["nome"], "cpf": r["cpf"],
                        "data": dh.strftime("%d/%m/%Y"), "hora": dh.strftime("%H:%M:%S"),
                        "tipo": r["tipo"], "tipo_formatado": r["tipo"].replace("_", " "),
                        "atrasado": bool(r["atrasado"]),
                        "minutos_atraso": r["minutos_atraso"] or 0,
                        "minutos_banco_horas": r["minutos_banco_horas"] or 0,
                        "autorizado_admin": bool(r["autorizado_admin"] or 0),
                        "confianca_facial": r["confianca_facial"] or 0,
                        "ip_dispositivo": r["ip_dispositivo"] or "",
                        "dia_semana": dh.weekday()
                    })
                responder_json(self, resultado)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/obter_registro/"):
            try:
                reg_id = int(caminho.replace("/api/obter_registro/", ""))
                conn = get_db()
                r = conn.execute("SELECT r.*, f.nome FROM registros_ponto r JOIN funcionarios f ON r.funcionario_id=f.id WHERE r.id=?", (reg_id,)).fetchone()
                conn.close()
                if not r:
                    responder_json(self, {"detail": "Não encontrado"}, status=404); return
                tipo_info = TIPOS_REGISTRO[r["tipo"]]
                dh = datetime.strptime(r["data_hora"], "%Y-%m-%d %H:%M:%S")
                msg = f"{tipo_info['icone']} {tipo_info['label']} registrada!\n👤 {r['nome']}\n📅 {dh.strftime('%d/%m/%Y')}\n⏰ {dh.strftime('%H:%M:%S')}"
                if r["atrasado"]: msg += f"\n⚠️ Atraso: {r['minutos_atraso'] or 0} min"
                if (r["minutos_banco_horas"] or 0) > 0: msg += f"\n⏱️ Banco: +{r['minutos_banco_horas']} min"
                responder_json(self, {"mensagem": msg})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        if caminho == "/api/acessos":
            try:
                conn = get_db()
                acs = conn.execute("""
                    SELECT a.*, f.nome FROM acessos_dispositivos a 
                    LEFT JOIN funcionarios f ON a.funcionario_id=f.id 
                    ORDER BY a.data_hora_acesso DESC LIMIT 200
                """).fetchall()
                conn.close()
                resultado = []
                for a in acs:
                    dh = datetime.strptime(a["data_hora_acesso"], "%Y-%m-%d %H:%M:%S")
                    resultado.append({"data_hora":dh.strftime("%d/%m/%Y %H:%M:%S"),"cpf":a["cpf"],"nome":a["nome"],"ip":a["ip_dispositivo"] or "","user_agent":a["user_agent"] or "","tipo_acesso":a["tipo_acesso"] or ""})
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
                qr.add_data(url); qr.make(fit=True)
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
            if not mes: responder_json(self, {"detail": "Informe o mês"}, status=400); return
            try:
                pdf_bytes = gerar_pdf_geral(mes)
                self.send_response(200); self.send_header("Content-Type","application/pdf")
                self.send_header("Content-Disposition",f"attachment; filename=Relatorio_Geral_{mes}.pdf")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(pdf_bytes.getvalue())
            except ImportError: responder_html(self, "<h1>Erro</h1><p>Instale: pip install reportlab</p>")
            except Exception as e: responder_json(self, {"detail": f"Erro PDF: {str(e)}"}, status=500)
            return
        
        if caminho.startswith("/api/pdf/funcionario/"):
            func_id = int(caminho.replace("/api/pdf/funcionario/", ""))
            mes = params.get("mes", [""])[0]
            if not mes: responder_json(self, {"detail": "Informe o mês"}, status=400); return
            try:
                pdf_bytes = gerar_pdf_individual(func_id, mes)
                self.send_response(200); self.send_header("Content-Type","application/pdf")
                self.send_header("Content-Disposition",f"attachment; filename=Relatorio_Func_{func_id}_{mes}.pdf")
                for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
                self.end_headers(); self.wfile.write(pdf_bytes.getvalue())
            except ImportError: responder_html(self, "<h1>Erro</h1><p>Instale: pip install reportlab</p>")
            except Exception as e: responder_json(self, {"detail": f"Erro PDF: {str(e)}"}, status=500)
            return
        
        self.send_response(404)
        for k,v in CABECALHOS_SEGURANCA.items(): self.send_header(k,v)
        self.end_headers()
    
    def do_POST(self):
        url = urlparse(self.path)
        caminho = url.path
        
        tamanho = int(self.headers.get("Content-Length", 0))
        if tamanho > 10000000:
            responder_json(self, {"detail": "Requisição muito grande"}, status=413); return
        
        corpo = self.rfile.read(tamanho).decode("utf-8")
        try: dados = json.loads(corpo) if corpo else {}
        except: dados = {}
        
        ip_cliente = obter_ip_cliente(self)
        user_agent = sanitizar_texto(self.headers.get("User-Agent", ""), 500)
        
        if caminho == "/api/login":
            if not verificar_rate_limit(ip_cliente):
                responder_json(self, {"detail": "Muitas tentativas."}, status=429); return
            usuario = sanitizar_texto(dados.get("usuario", ""), 50)
            senha = sanitizar_texto(dados.get("senha", ""), 100)
            if usuario == ADMIN_USUARIO and senha == ADMIN_SENHA:
                token = gerar_sessao()
                sessoes_admin[token] = agora_brasilia() + timedelta(hours=8)
                cookie = f"sessao_admin={token}; Path=/; Max-Age=28800; HttpOnly; SameSite=Lax"
                tentativas_login.pop(ip_cliente, None)
                responder_json(self, {"status": "ok"}, cookies_extra=[cookie])
            else:
                responder_json(self, {"detail": "Usuário ou senha incorretos!"}, status=401)
            return
        
        if caminho == "/api/funcionario/acessar":
            cpf = formatar_cpf(dados.get("cpf", ""))
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido!"}, status=400); return
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if not func:
                    conn.close(); responder_json(self, {"detail": "CPF não cadastrado!"}, status=404); return
                if func["bloqueado"]:
                    conn.close(); responder_json(self, {"detail": "🚫 Funcionário BLOQUEADO. Contate o administrador."}, status=403); return
                func_id = func["id"]; conn.close()
                registrar_acesso_dispositivo(cpf, func_id, ip_cliente, user_agent, "login_funcionario")
                acessos_funcionarios[cpf] = {"funcionario_id": func_id, "ip": ip_cliente, "expira": agora_brasilia() + timedelta(hours=2)}
                print(f"[ACESSO FUNC] {func['nome']} | IP: {ip_cliente}")
                responder_json(self, {"status": "ok", "nome": func["nome"]})
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        # ===== NOVA ROTA: VERIFICACAO FACIAL =====
        if caminho == "/api/verificar_face":
            cpf = formatar_cpf(dados.get("cpf", ""))
            funcionario_id = dados.get("funcionario_id")
            imagem_b64 = dados.get("imagem", "")
            
            if len(cpf) != 11 or not funcionario_id:
                responder_json(self, {"detail": "Dados inválidos"}, status=400); return
            if not imagem_b64:
                responder_json(self, {"detail": "Nenhuma imagem capturada"}, status=400); return
            
            try:
                # Verifica se está bloqueado
                bloqueado, motivo = funcionario_bloqueado(funcionario_id)
                if bloqueado:
                    responder_json(self, {"detail": f"🚫 BLOQUEADO! {motivo}", "bloqueado": True}, status=403); return
                
                img_bytes = base64.b64decode(imagem_b64)
                reconhecido, confianca, msg = verificar_face(funcionario_id, img_bytes)
                
                if reconhecido:
                    resetar_tentativas_face(funcionario_id)
                    # Salva a foto de verificação
                    caminho_foto_ver = os.path.join("static/fotos", f"verificacao_{funcionario_id}_{agora_brasilia().strftime('%Y%m%d_%H%M%S')}.jpg")
                    try:
                        with open(caminho_foto_ver, "wb") as f: f.write(img_bytes)
                        foto_salva = f"/static/fotos/{os.path.basename(caminho_foto_ver)}"
                    except:
                        foto_salva = ""
                    responder_json(self, {"reconhecido": True, "confianca": confianca, "foto_salva": foto_salva})
                else:
                    bloqueado_agora, msg_bloqueio = incrementar_tentativas_face(funcionario_id)
                    conn = get_db()
                    func = conn.execute("SELECT tentativas_reconhecimento FROM funcionarios WHERE id=?", (funcionario_id,)).fetchone()
                    conn.close()
                    tentativas_feitas = func["tentativas_reconhecimento"] or 0
                    tentativas_restantes = MAX_TENTATIVAS_FACIAIS - tentativas_feitas
                    responder_json(self, {
                        "reconhecido": False, "detail": msg,
                        "tentativas_restantes": tentativas_restantes,
                        "bloqueado": bloqueado_agora
                    }, status=400)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        # ===== NOVA ROTA: VERIFICACAO FACIAL NO LOGIN =====
        if caminho == "/api/verificar_face_login":
            cpf = formatar_cpf(dados.get("cpf", ""))
            funcionario_id = dados.get("funcionario_id")
            imagem_b64 = dados.get("imagem", "")
            
            if len(cpf) != 11 or not funcionario_id:
                responder_json(self, {"detail": "Dados inválidos"}, status=400); return
            if not imagem_b64:
                responder_json(self, {"detail": "Nenhuma imagem capturada"}, status=400); return
            
            try:
                bloqueado, motivo = funcionario_bloqueado(funcionario_id)
                if bloqueado:
                    responder_json(self, {"detail": f"🚫 BLOQUEADO! {motivo}", "bloqueado": True}, status=403); return
                
                img_bytes = base64.b64decode(imagem_b64)
                reconhecido, confianca, msg = verificar_face(funcionario_id, img_bytes)
                
                if reconhecido:
                    resetar_tentativas_face(funcionario_id)
                    registrar_acesso_dispositivo(cpf, funcionario_id, ip_cliente, user_agent, "reconhecimento_face_login")
                    print(f"[FACE LOGIN OK] ID:{funcionario_id} Confiança:{confianca:.1f}")
                    responder_json(self, {"reconhecido": True, "confianca": confianca})
                else:
                    bloqueado_agora, msg_bloqueio = incrementar_tentativas_face(funcionario_id)
                    conn = get_db()
                    func = conn.execute("SELECT tentativas_reconhecimento FROM funcionarios WHERE id=?", (funcionario_id,)).fetchone()
                    conn.close()
                    tentativas_feitas = func["tentativas_reconhecimento"] or 0
                    tentativas_restantes = MAX_TENTATIVAS_FACIAIS - tentativas_feitas
                    responder_json(self, {
                        "reconhecido": False, "detail": msg,
                        "tentativas_restantes": tentativas_restantes,
                        "bloqueado": bloqueado_agora
                    }, status=400)
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return

        # ===== NOVA ROTA: SOLICITAR PONTO =====
        if caminho == "/api/solicitar_ponto":
            cpf = formatar_cpf(dados.get("cpf", ""))
            tipo = dados.get("tipo", "ENTRADA")
            qr_code = dados.get("qr_code", "")
            foto_verificacao = dados.get("foto_verificacao", "")
            confianca_facial = float(dados.get("confianca_facial", 0) or 0)
            
            if qr_code != SEGREDO_QR:
                responder_json(self, {"detail": "QR inválido!"}, status=400); return
            if tipo not in TIPOS_REGISTRO:
                responder_json(self, {"detail": "Tipo inválido!"}, status=400); return
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF inválido!"}, status=400); return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if not func:
                    conn.close(); responder_json(self, {"detail": "CPF não cadastrado!"}, status=404); return
                
                bloqueado, motivo = funcionario_bloqueado(func["id"])
                if bloqueado:
                    conn.close(); responder_json(self, {"detail": f"🚫 BLOQUEADO! {motivo}"}, status=403); return
                
                agora = agora_brasilia()
                data_str = agora.strftime("%Y-%m-%d")
                hora_str = agora.strftime("%H:%M:%S")
                
                ultimo = obter_ultimo_registro(func["id"], data_str)
                ultimo_tipo = ultimo["tipo"] if ultimo else None
                
                valido, msg_erro = verificar_sequencia_valida(ultimo_tipo, tipo)
                if not valido:
                    conn.close(); responder_json(self, {"detail": "⛔ " + msg_erro}, status=400); return
                
                if verificar_registro_duplicado(func["id"], data_str, tipo):
                    conn.close(); responder_json(self, {"detail": f"⛔ Já registrado hoje!"}, status=400); return
                
                # Verifica se precisa de autorizacao
                requer_autorizacao = False
                tipo_diferenca = ""
                minutos_diferenca = 0
                horario_padrao = ""
                
                if tipo == "ENTRADA":
                    horario_padrao = func["horario_entrada"]
                    dif_seg = calcular_diferenca_segundos(hora_str, horario_padrao)
                    if dif_seg > 0:
                        # Atrasado - verificar tolerância
                        if dif_seg > (TOLERANCIA_ATRASO_MINUTOS * 60):
                            minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                            requer_autorizacao = True; tipo_diferenca = "atrasado"
                    elif dif_seg < 0:
                        # Antecipado
                        minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                        if minutos_diferenca > 0: requer_autorizacao = True; tipo_diferenca = "antecipado"
                elif tipo == "SAIDA_ALMOCO":
                    horario_padrao = func["horario_saida_almoco"]
                    dif_seg = calcular_diferenca_segundos(hora_str, horario_padrao)
                    if dif_seg < 0:
                        # Saindo para almoço ANTES do horário - verificar tolerância
                        if abs(dif_seg) > (TOLERANCIA_ATRASO_MINUTOS * 60):
                            minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                            requer_autorizacao = True; tipo_diferenca = "antecipado"
                    elif dif_seg > 0:
                        # Saindo DEPOIS = trabalhando mais, não pede autorização
                        pass
                elif tipo == "RETORNO_ALMOCO":
                    horario_padrao = func["horario_retorno_almoco"]
                    dif_seg = calcular_diferenca_segundos(hora_str, horario_padrao)
                    if dif_seg > 0:
                        if dif_seg > (TOLERANCIA_ATRASO_MINUTOS * 60):
                            minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                            requer_autorizacao = True; tipo_diferenca = "atrasado"
                    elif dif_seg < 0:
                        minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                        if minutos_diferenca > 0: requer_autorizacao = True; tipo_diferenca = "antecipado"
                elif tipo == "SAIDA":
                    horario_padrao = func["horario_saida"]
                    dif_seg = calcular_diferenca_segundos(hora_str, horario_padrao)
                    if dif_seg < 0:
                        # Saindo ANTES do horário - verificar tolerância
                        if abs(dif_seg) > (TOLERANCIA_ATRASO_MINUTOS * 60):
                            minutos_diferenca = calcular_minutos(hora_str, horario_padrao)
                            requer_autorizacao = True; tipo_diferenca = "antecipado"
                    elif dif_seg > 0:
                        # Saindo DEPOIS = banco de horas, não pede autorização
                        pass
                
                conn.close()
                
                if requer_autorizacao and minutos_diferenca > 0:
                    sid, criada = criar_solicitacao_autorizacao(
                        func, tipo, hora_str, horario_padrao,
                        minutos_diferenca, tipo_diferenca,
                        ip_cliente, user_agent
                    )
                    # Armazena dados da verificacao facial na solicitacao
                    if sid in autorizacoes_pendentes:
                        autorizacoes_pendentes[sid]["foto_verificacao"] = foto_verificacao
                        autorizacoes_pendentes[sid]["confianca_facial"] = confianca_facial
                    
                    responder_json(self, {
                        "requer_autorizacao": True, "solicitacao_id": sid,
                        "tipo": tipo, "tipo_diferenca": tipo_diferenca,
                        "minutos_diferenca": minutos_diferenca,
                        "hora_registro": hora_str, "horario_padrao": horario_padrao,
                        "nome": func["nome"]
                    })
                else:
                    registro_id = self._executar_registro(func, tipo, agora, hora_str, ip_cliente, user_agent, "", 0, "", foto_verificacao, confianca_facial)
                    responder_json(self, {"requer_autorizacao": False, "registro_id": registro_id})
                    
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        # ===== NOVA ROTA: EXECUTAR DEPOIS DE AUTORIZACAO =====
        if caminho == "/api/executar_autorizado":
            solicitacao_id = dados.get("solicitacao_id", "")
            resposta_admin = sanitizar_texto(dados.get("resposta_admin", ""), 500)
            
            if not solicitacao_id or solicitacao_id not in autorizacoes_pendentes:
                responder_json(self, {"detail": "Solicitação inválida ou expirada"}, status=400); return
            
            s = autorizacoes_pendentes[solicitacao_id]
            if s["status"] != "aprovado":
                responder_json(self, {"detail": "Solicitação não foi aprovada"}, status=400); return
            
            try:
                conn = get_db()
                func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (s["funcionario_id"],)).fetchone()
                conn.close()
                
                if not func:
                    responder_json(self, {"detail": "Funcionário não encontrado"}, status=404); return
                
                agora = agora_brasilia()
                hora_str = agora.strftime("%H:%M:%S")
                foto_ver = s.get("foto_verificacao", "")
                confianca = s.get("confianca_facial", 0)
                
                registro_id = self._executar_registro(
                    func, s["tipo"], agora, hora_str,
                    s["ip_cliente"], s["user_agent"],
                    resposta_admin, 1, resposta_admin, foto_ver, confianca
                )
                
                if solicitacao_id in autorizacoes_pendentes:
                    del autorizacoes_pendentes[solicitacao_id]
                
                conn = get_db()
                r = conn.execute("SELECT * FROM registros_ponto WHERE id = ?", (registro_id,)).fetchone()
                conn.close()
                
                tipo_info = TIPOS_REGISTRO[s["tipo"]]
                msg = f"{tipo_info['icone']} {tipo_info['label']} registrada!\n👤 {func['nome']}\n📅 {agora.strftime('%d/%m/%Y')}\n⏰ {hora_str}"
                if r["atrasado"]: msg += f"\n⚠️ Atraso: {r['minutos_atraso'] or 0} min"
                if (r["minutos_banco_horas"] or 0) > 0: msg += f"\n⏱️ Banco: +{r['minutos_banco_horas']} min"
                
                print(f"[PONTO AUTORIZADO] {func['nome']} | {s['tipo']}")
                responder_json(self, {"mensagem": msg, "registro_id": registro_id})
                
            except Exception as e:
                responder_json(self, {"detail": f"Erro: {str(e)}"}, status=500)
            return
        
        # ===== ROTA ADMIN: RESPONDER AUTORIZACAO =====
        if caminho == "/api/responder_autorizacao":
            if not verificar_login(self):
                responder_json(self, {"detail": "Não autorizado"}, status=401); return
            
            solicitacao_id = dados.get("solicitacao_id", "")
            aprovar = bool(dados.get("aprovar", False))
            resposta = sanitizar_texto(dados.get("resposta", ""), 500)
            
            resultado, erro = responder_autorizacao(solicitacao_id, aprovar, resposta)
            if erro: responder_json(self, {"detail": erro}, status=400)
            else: responder_json(self, {"status": "ok", "acao": "aprovado" if aprovar else "rejeitado"})
            return
        
        # Rotas que precisam de login
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401); return
        
        # ===== CADASTRAR FUNCIONARIO COM FOTO =====
        if caminho == "/api/funcionarios":
            nome = sanitizar_texto(dados.get("nome", ""), 150)
            cpf = formatar_cpf(dados.get("cpf", ""))
            foto_base64 = dados.get("foto_base64", "")
            
            if not nome or not cpf:
                responder_json(self, {"detail": "Preencha nome e CPF!"}, status=400); return
            if len(cpf) != 11:
                responder_json(self, {"detail": "CPF deve ter 11 dígitos!"}, status=400); return
            
            h_entrada = sanitizar_texto(dados.get("horario_entrada", "08:00:00"), 20) or "08:00:00"
            h_saida_almoco = sanitizar_texto(dados.get("horario_saida_almoco", "12:00:00"), 20) or "12:00:00"
            h_retorno_almoco = sanitizar_texto(dados.get("horario_retorno_almoco", "13:00:00"), 20) or "13:00:00"
            h_saida = sanitizar_texto(dados.get("horario_saida", "18:00:00"), 20) or "18:00:00"
            
            try:
                conn = get_db()
                existe = conn.execute("SELECT id FROM funcionarios WHERE cpf = ?", (cpf,)).fetchone()
                if existe:
                    conn.close(); responder_json(self, {"detail": "CPF já cadastrado!"}, status=400); return
                
                conn.execute("""INSERT INTO funcionarios 
                    (nome, cpf, horario_entrada, horario_saida_almoco, horario_retorno_almoco, horario_saida)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (nome, cpf, h_entrada, h_saida_almoco, h_retorno_almoco, h_saida))
                conn.commit()
                
                novo_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
                conn.close()
                
                # Treina reconhecimento facial se houver foto
                face_treinada = False
                if foto_base64:
                    try:
                        img_bytes = base64.b64decode(foto_base64)
                        sucesso, msg_face = treinar_modelo_face(novo_id, img_bytes)
                        if sucesso:
                            face_treinada = True
                            print(f"[FACE ID] Treinado: {nome} (ID:{novo_id})")
                        else:
                            print(f"[FACE ID] Falha: {nome} - {msg_face}")
                    except Exception as e:
                        print(f"[FACE ID] Erro treinamento: {e}")
                
                print(f"[CADASTRO] {nome} | CPF: {cpf} | Face: {'OK' if face_treinada else 'FALHOU'}")
                salvar_historico_json()
                responder_json(self, {"status": "ok", "id": novo_id, "face_treinada": face_treinada})
            except Exception as e:
                responder_json(self, {"detail": f"Erro BD: {str(e)}"}, status=500)
            return
        
        # ===== DESBLOQUEAR FUNCIONARIO =====
        if caminho.startswith("/api/funcionarios/desbloquear/"):
            func_id = int(caminho.replace("/api/funcionarios/desbloquear/", ""))
            if desbloquear_funcionario(func_id):
                salvar_historico_json()
                responder_json(self, {"status": "ok"})
            else:
                responder_json(self, {"detail": "Erro ao desbloquear"}, status=500)
            return
        
        responder_json(self, {"detail": "Rota não encontrada"}, status=404)
    
    def _executar_registro(self, func, tipo, agora, hora_str, ip_cliente, user_agent, justificativa, autorizado_admin, admin_resposta, foto_verificacao="", confianca_facial=0):
        data_hora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
        horario_acesso = agora.strftime("%Y-%m-%d %H:%M:%S")
        
        atrasado = 0; minutos_atraso = 0
        
        if tipo == "ENTRADA":
            atrasado = 1 if verificar_atraso(hora_str, func["horario_entrada"], TOLERANCIA_ATRASO_MINUTOS) else 0
            if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_entrada"])
        elif tipo == "RETORNO_ALMOCO":
            atrasado = 1 if verificar_atraso(hora_str, func["horario_retorno_almoco"], TOLERANCIA_ATRASO_MINUTOS) else 0
            if atrasado: minutos_atraso = calcular_minutos(hora_str, func["horario_retorno_almoco"])
        elif tipo == "SAIDA":
            atrasado = 1 if verificar_atraso(func["horario_saida"], hora_str, TOLERANCIA_ATRASO_MINUTOS) else 0
            if atrasado: minutos_atraso = calcular_minutos(func["horario_saida"], hora_str)
        
        minutos_banco = calcular_banco_horas(tipo, hora_str, func)
        
        conn = get_db()
        conn.execute("""INSERT INTO registros_ponto 
            (funcionario_id, data_hora, tipo, atrasado, minutos_atraso, minutos_banco_horas, justificativa, 
             ip_dispositivo, user_agent, horario_acesso, autorizado_admin, admin_resposta, foto_verificacao, confianca_facial)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (func["id"], data_hora_str, tipo, atrasado, minutos_atraso, minutos_banco, justificativa,
             ip_cliente, user_agent, horario_acesso, autorizado_admin, admin_resposta, foto_verificacao, confianca_facial))
        
        novo_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        conn.commit(); conn.close()
        
        salvar_historico_json()
        verificar_backup_periodico()
        return novo_id
    
    def do_DELETE(self):
        if not verificar_login(self):
            responder_json(self, {"detail": "Não autorizado"}, status=401); return
        
        url = urlparse(self.path)
        caminho = url.path
        
        if caminho.startswith("/api/funcionarios/"):
            try:
                func_id = int(caminho.replace("/api/funcionarios/", ""))
                conn = get_db()
                conn.execute("DELETE FROM registros_ponto WHERE funcionario_id = ?", (func_id,))
                conn.execute("DELETE FROM funcionarios WHERE id = ?", (func_id,))
                conn.commit(); conn.close()
                
                # Remove arquivos de foto e modelo
                for f in [f"static/fotos/func_{func_id}.jpg", f"modelos_faciais/func_{func_id}.yml"]:
                    try:
                        if os.path.exists(f): os.remove(f)
                    except: pass
                
                print(f"[EXCLUSAO] Funcionário ID: {func_id}")
                salvar_historico_json()
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
    c.drawString(95, y - 9, "HORA")
    c.drawString(145, y - 9, "TIPO")
    c.drawString(215, y - 9, "ATR")
    c.drawString(255, y - 9, "MIN")
    c.drawString(295, y - 9, "BANCO")
    c.drawString(345, y - 9, "FACE")
    c.drawString(385, y - 9, "DIA")
    c.drawString(420, y - 9, "JUSTIFICATIVA")
    y -= 32
    
    c.setFont("Helvetica", 7)
    contagem = {"ENTRADA": 0, "SAIDA_ALMOCO": 0, "RETORNO_ALMOCO": 0, "SAIDA": 0}
    total_atrasos = 0; total_min_atraso = 0; total_banco_horas = 0
    
    for reg in registros:
        if y < 100:
            c.showPage(); y = altura - 50; c.setFont("Helvetica", 7)
        
        dh = datetime.strptime(reg["data_hora"], "%Y-%m-%d %H:%M:%S")
        data = dh.strftime("%d/%m/%Y"); hora = dh.strftime("%H:%M:%S")
        dia_semana = dias_semana[dh.weekday()]; tipo_fmt = reg["tipo"].replace("_", " ")
        
        if dh.weekday() >= 5:
            c.setFillColor(colors.HexColor("#fff3cd"))
            c.rect(40, y - 2, largura - 80, 12, fill=True, stroke=False)
            c.setFillColor(colors.black)
        
        c.drawString(45, y, data); c.drawString(95, y, hora)
        
        if reg["tipo"] == "ENTRADA": c.setFillColor(colors.HexColor("#4CAF50"))
        elif reg["tipo"] == "SAIDA_ALMOCO": c.setFillColor(colors.HexColor("#ff9800"))
        elif reg["tipo"] == "RETORNO_ALMOCO": c.setFillColor(colors.HexColor("#2196F3"))
        else: c.setFillColor(colors.HexColor("#f44336"))
        
        contagem[reg["tipo"]] += 1
        c.drawString(145, y, tipo_fmt); c.setFillColor(colors.black)
        
        if reg["atrasado"]:
            c.setFillColor(colors.HexColor("#f44336")); c.drawString(215, y, "SIM")
            c.setFillColor(colors.black); total_atrasos += 1
        else: c.drawString(215, y, "Nao")
        
        min_atraso = reg["minutos_atraso"] or 0
        if min_atraso > 0:
            c.setFillColor(colors.HexColor("#f44336")); c.drawString(255, y, str(min_atraso) + "m")
            c.setFillColor(colors.black); total_min_atraso += min_atraso
        else: c.drawString(255, y, "-")
        
        min_banco = reg["minutos_banco_horas"] or 0
        if min_banco > 0:
            c.setFillColor(colors.HexColor("#0c5460")); c.drawString(295, y, "+" + str(min_banco) + "m")
            c.setFillColor(colors.black); total_banco_horas += min_banco
        else: c.drawString(295, y, "-")
        
        conf = reg["confianca_facial"] or 0
        if conf > 0:
            c.setFillColor(colors.HexColor("#4CAF50")); c.drawString(345, y, f"{conf:.0f}")
            c.setFillColor(colors.black)
        else: c.drawString(345, y, "-")
        
        c.drawString(385, y, dia_semana[:3])
        
        justificativa = reg["justificativa"] or reg["admin_resposta"] or ""
        if justificativa:
            c.setFillColor(colors.HexColor("#666666"))
            if len(justificativa) > 30: justificativa = justificativa[:27] + "..."
            c.drawString(420, y, justificativa)
            c.setFillColor(colors.black)
        
        y -= 13
    
    if y < 200: c.showPage(); y = altura - 50
    
    y -= 10
    c.setFillColor(colors.HexColor("#f5f5f5"))
    c.rect(40, y - 100, largura - 80, 110, fill=True, stroke=False)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y - 15, "RESUMO DO MES:")
    c.setFont("Helvetica", 9)
    c.drawString(50, y - 35, "Total: " + str(len(registros)) + " registros")
    c.drawString(170, y - 35, "Entradas: " + str(contagem["ENTRADA"]))
    c.drawString(290, y - 35, "Saidas: " + str(contagem["SAIDA"]))
    c.drawString(50, y - 50, "Retornos: " + str(contagem["RETORNO_ALMOCO"]))
    c.drawString(170, y - 50, "Atrasos: " + str(total_atrasos))
    c.drawString(290, y - 50, "Min. atraso: " + str(total_min_atraso))
    
    c.setFillColor(colors.HexColor("#0c5460"))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(50, y - 65, "Banco horas: +" + str(total_banco_horas) + " min")
    c.setFillColor(colors.black)
    
    y -= 120
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "_______________________________________________________")
    y -= 15
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Assinatura Funcionario: ___________________________")
    y -= 30
    c.drawString(300, y, "Assinatura Responsavel: ___________________________")
    
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
        registros = conn.execute("""SELECT * FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m', data_hora) = ? ORDER BY data_hora""", (func["id"], mes)).fetchall()
        desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors)
    
    conn.close(); c.save(); buffer.seek(0)
    return buffer

def gerar_pdf_individual(func_id, mes):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    
    conn = get_db()
    func = conn.execute("SELECT * FROM funcionarios WHERE id = ?", (func_id,)).fetchone()
    
    if not func:
        conn.close(); buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        c.drawString(100, 400, "Funcionario nao encontrado")
        c.save(); buffer.seek(0); return buffer
    
    registros = conn.execute("""SELECT * FROM registros_ponto WHERE funcionario_id = ? AND strftime('%Y-%m', data_hora) = ? ORDER BY data_hora""", (func_id, mes)).fetchall()
    
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    largura, altura = A4
    dias_semana = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]
    
    desenhar_pagina_funcionario(c, func, registros, mes, dias_semana, altura, largura, colors)
    
    conn.close(); c.save(); buffer.seek(0)
    return buffer

# ===================== INICIAR SERVIDOR =====================
if __name__ == "__main__":
    print("=" * 65)
    print("   🚀 SISTEMA DE PONTO v5.0 FACE ID - FUNCIONANDO!")
    print("=" * 65)
    print(f"📱 Pagina inicial:        http://localhost:{PORTA}")
    print(f"👤 Painel Funcionario:    http://localhost:{PORTA}/funcionario")
    print(f"🔐 Login Admin:           http://localhost:{PORTA}/admin")
    print(f"👤 Usuário: {ADMIN_USUARIO}   |   Senha: {ADMIN_SENHA}")
    print("=" * 65)
    print("⭐ NOVAS FUNCIONALIDADES v5.0:")
    print("   📸 Reconhecimento facial OBRIGATÓRIO")
    print("   👤 Foto de perfil no cadastro")
    print("   🚫 Bloqueio após " + str(MAX_TENTATIVAS_FACIAIS) + " tentativas faciais falhas")
    print("   🔓 Desbloqueio somente pelo administrador")
    print("   ⏰ Autorização de horário (SEM botão cancelar)")
    print("   🕐 Horário fixo de Brasília (UTC-3)")
    print("=" * 65)
    if FACE_REC_DISPONIVEL:
        print("✅ Reconhecimento facial: ATIVO (OpenCV)")
    else:
        print("⚠️  Reconhecimento facial: DESATIVADO (instale opencv-python)")
    print("=" * 65)
    print(f"🌐 WI-FI: http://SEU_IP:{PORTA}")
    print("=" * 65)
    print("\nServidor rodando... Ctrl+C para parar.\n")
    
    try:
        servidor = HTTPServer(("0.0.0.0", PORTA), ServidorPonto)
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor parado.")
        servidor.server_close()
