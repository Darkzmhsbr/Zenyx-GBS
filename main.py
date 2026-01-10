import os
import logging
import telebot
import requests
import time
import urllib.parse
import threading
from telebot import types
import json
import uuid

# --- IMPORTS ---
from sqlalchemy import func, desc, text
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta

# Importa banco de dados e script de update
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, BotFlowStep, Pedido, SystemConfig, RemarketingCampaign, BotAdmin, engine
import update_db # Importa para rodar o reparo na inicialização

# Configuração de Log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zenyx Gbot SaaS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# 1. FUNÇÃO DE CONEXÃO COM BANCO (DEVE VIR PRIMEIRO)
# =========================================================
def get_db():
    """Gera conexão com o banco de dados"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================================================
# 2. AUTO-REPARO DO BANCO DE DADOS (REPARO TOTAL)
# =========================================================
@app.on_event("startup")
def on_startup():
    # 1. Cria tabelas que não existem
    init_db()
    
    # 2. FORÇA A CRIAÇÃO DE TODAS AS COLUNAS QUE PODEM FALTAR
    try:
        with engine.connect() as conn:
            logger.info("🔧 [STARTUP] Forçando verificação COMPLETA do banco...")
            
            comandos = [
                # --- TABELA PEDIDOS (Onde está dando erro agora) ---
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_nome VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS txid VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS qr_code TEXT;",
                # Adicionamos transaction_id para evitar erro de mapeamento antigo
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS transaction_id VARCHAR;", 
                
                # Colunas de data e acesso (Causa do erro atual)
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_aprovacao TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_expiracao TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS link_acesso VARCHAR;",
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mensagem_enviada BOOLEAN DEFAULT FALSE;",

                # --- OUTRAS TABELAS (Para garantir que nada mais quebre) ---
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;",
                
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS target VARCHAR DEFAULT 'todos';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'massivo';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS promo_price FLOAT;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS expiration_at TIMESTAMP WITHOUT TIME ZONE;",
                
                # --- TABELA NOVA (V2) ---
                """
                CREATE TABLE IF NOT EXISTS bot_flow_steps (
                    id SERIAL PRIMARY KEY,
                    bot_id INTEGER REFERENCES bots(id),
                    step_order INTEGER DEFAULT 1,
                    msg_texto TEXT,
                    msg_media VARCHAR,
                    btn_texto VARCHAR DEFAULT 'Próximo ▶️',
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
                );
                """
            ]
            
            for cmd in comandos:
                try:
                    conn.execute(text(cmd))
                    conn.commit()
                except Exception as e_sql:
                    logger.warning(f"SQL Warning: {e_sql}")
            
            logger.info("✅ [STARTUP] Banco de dados TOTALMENTE verificado!")
            
    except Exception as e:
        logger.error(f"❌ Falha no reparo do banco: {e}")

    # 3. Inicia o Ceifador
    thread = threading.Thread(target=loop_verificar_vencimentos)
    thread.daemon = True
    thread.start()
    logger.info("💀 O Ceifador (Auto-Kick) foi iniciado!")

# =========================================================
# 💀 O CEIFADOR: VERIFICA VENCIMENTOS E REMOVE (KICK SUAVE)
# =========================================================
def loop_verificar_vencimentos():
    """Roda a cada 60 minutos para remover usuários vencidos"""
    while True:
        try:
            # logger.info("⏳ Verificando assinaturas vencidas...")
            verificar_expiracao_massa()
        except Exception as e:
            logger.error(f"Erro no loop de vencimento: {e}")
        
        time.sleep(3600) # Espera 1 hora (3600 segundos)

def verificar_expiracao_massa():
    db = SessionLocal()
    try:
        # Busca todos os bots para processar cada um
        bots = db.query(Bot).all()
        
        for bot_data in bots:
            if not bot_data.token or not bot_data.id_canal_vip:
                continue
                
            try:
                tb = telebot.TeleBot(bot_data.token)
                
                # Busca pedidos PAGOS deste bot
                usuarios_ativos = db.query(Pedido).filter(
                    Pedido.bot_id == bot_data.id,
                    Pedido.status == 'paid'
                ).all()
                
                for user in usuarios_ativos:
                    # Determina a duração baseada no nome do plano
                    dias_duracao = 30 # Padrão Mensal
                    nome_plano = (user.plano_nome or "").lower()
                    
                    if "vital" in nome_plano or "mega" in nome_plano:
                        continue # Nunca vence
                    
                    if "24" in nome_plano or "diario" in nome_plano or "1 dia" in nome_plano:
                        dias_duracao = 1
                    elif "trimestral" in nome_plano:
                        dias_duracao = 90
                    elif "semanal" in nome_plano:
                        dias_duracao = 7
                    
                    # Calcula data de vencimento
                    data_vencimento = user.created_at + timedelta(days=dias_duracao)
                    agora = datetime.utcnow()
                    
                    if agora > data_vencimento:
                        logger.info(f"🚫 Assinatura vencida: {user.telegram_id} (Bot: {bot_data.nome})")
                        
                        # --- A LÓGICA DO KICK SUAVE (REMOVE DA BLACKLIST) ---
                        try:
                            # 1. Identifica o Canal
                            try: canal_id = int(str(bot_data.id_canal_vip).strip())
                            except: canal_id = bot_data.id_canal_vip

                            # 2. Banir (Remove do canal)
                            tb.ban_chat_member(canal_id, int(user.telegram_id))
                            
                            # 3. Desbanir Imediatamente (Limpa a Blacklist)
                            tb.unban_chat_member(canal_id, int(user.telegram_id))
                            
                            # 4. Atualiza DB para 'expired'
                            user.status = 'expired'
                            db.commit()
                            
                            # 5. Avisa o usuário
                            try:
                                tb.send_message(user.telegram_id, "Seu plano VIP expirou! 😢\nPara voltar ao canal, renove sua assinatura digitando /start")
                            except: pass
                            
                        except Exception as e_kick:
                            logger.error(f"Erro ao remover membro {user.telegram_id}: {e_kick}")
                            user.status = 'expired'
                            db.commit()
                            
            except Exception as e_bot:
                logger.error(f"Erro ao processar bot {bot_data.nome}: {e_bot}")
                
    finally:
        db.close()

# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (DINÂMICA)
# =========================================================
def get_pushin_token():
    """Busca o token no banco, se não achar, tenta variável de ambiente"""
    db = SessionLocal()
    try:
        # Tenta pegar do banco de dados (Painel de Integrações)
        config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
        if config and config.value:
            return config.value
        # Se não tiver no banco, pega do Railway Variables
        return os.getenv("PUSHIN_PAY_TOKEN")
    finally:
        db.close()

# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (CORRIGIDA)
# =========================================================
def gerar_pix_pushinpay(valor_float: float, transaction_id: str):
    token = get_pushin_token()
    
    if not token:
        logger.error("❌ Token Pushin Pay não configurado!")
        return None
    
    url = "https://api.pushinpay.com.br/api/pix/cashIn"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # URL DO RAILWAY FIXA (Garante que o Webhook chegue)
    seus_dominio = "zenyx-gbs-production.up.railway.app" 
    
    payload = {
        "value": int(valor_float * 100), 
        "webhook_url": f"https://{seus_dominio}/webhook/pix",
        "external_reference": transaction_id
    }

    try:
        logger.info(f"📤 Gerando PIX. Webhook definido para: https://{seus_dominio}/webhook/pix")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if response.status_code in [200, 201]:
            return response.json()
        else:
            logger.error(f"Erro PushinPay: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exceção PushinPay: {e}")
        return None

# --- HELPER: Notificar Admin Principal ---
def notificar_admin_principal(bot_db: Bot, mensagem: str):
    if not bot_db.admin_principal_id:
        return
    try:
        sender = telebot.TeleBot(bot_db.token)
        sender.send_message(bot_db.admin_principal_id, mensagem, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Falha ao notificar admin principal {bot_db.admin_principal_id}: {e}")

# --- FUNÇÃO AUXILIAR: ENVIAR OFERTA FINAL ---
def enviar_oferta_final(tb, chat_id, flow, bot_id, db):
    """Envia a mensagem final de oferta com os planos."""
    markup = types.InlineKeyboardMarkup()
    
    # Busca Planos
    planos = db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
    if flow and flow.mostrar_planos_2:
        for plano in planos:
            markup.add(types.InlineKeyboardButton(
                text=f"💎 {plano.nome_exibicao} - R$ {plano.preco_atual:.2f}", 
                callback_data=f"checkout_{plano.id}"
            ))
    
    # Envia Mensagem (Texto ou Mídia)
    msg_texto = flow.msg_2_texto if (flow and flow.msg_2_texto) else "Escolha seu plano abaixo:"
    msg_media = flow.msg_2_media if (flow and flow.msg_2_media) else None
    
    if msg_media:
        try:
            if msg_media.lower().endswith(('.mp4', '.mov', '.avi')):
                tb.send_video(chat_id, msg_media, caption=msg_texto, reply_markup=markup)
            else:
                tb.send_photo(chat_id, msg_media, caption=msg_texto, reply_markup=markup)
        except:
            tb.send_message(chat_id, msg_texto, reply_markup=markup)
    else:
        tb.send_message(chat_id, msg_texto, reply_markup=markup)

# --- ROTAS DE INTEGRAÇÃO ---
class IntegrationUpdate(BaseModel):
    token: str

@app.get("/api/admin/integrations/pushinpay")
def get_pushin_status(db: Session = Depends(get_db)):
    config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
    token = config.value if config else os.getenv("PUSHIN_PAY_TOKEN")
    if not token:
        return {"status": "desconectado", "token_mask": ""}
    mask = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "****"
    return {"status": "conectado", "token_mask": mask}

@app.post("/api/admin/integrations/pushinpay")
def save_pushin_token(data: IntegrationUpdate, db: Session = Depends(get_db)):
    config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
    if not config:
        config = SystemConfig(key="pushin_pay_token")
        db.add(config)
    
    config.value = data.token.strip()
    config.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "conectado", "msg": "Integração salva com sucesso!"}

# --- MODELOS ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str
    admin_principal_id: Optional[str] = None

class BotUpdate(BaseModel):
    nome: Optional[str] = None
    token: Optional[str] = None
    id_canal_vip: Optional[str] = None
    admin_principal_id: Optional[str] = None

class BotAdminCreate(BaseModel):
    telegram_id: str
    nome: Optional[str] = "Admin"

class BotResponse(BotCreate):
    id: int
    status: str
    leads: int = 0
    revenue: float = 0.0
    class Config:
        from_attributes = True

class PlanoCreate(BaseModel):
    bot_id: int
    nome_exibicao: str
    preco: float
    dias_duracao: int

class FlowUpdate(BaseModel):
    msg_boas_vindas: str
    media_url: Optional[str] = None
    btn_text_1: str
    autodestruir_1: bool
    msg_2_texto: Optional[str] = None
    msg_2_media: Optional[str] = None
    mostrar_planos_2: bool

# [V2] Modelo Novo
class FlowStepCreate(BaseModel):
    msg_texto: str
    msg_media: Optional[str] = None
    btn_texto: str = "Próximo ▶️"
    step_order: int

class StepReorder(BaseModel):
    step_id: int
    new_order: int

# [V2] Modelo Novo
class RemarketingRequest(BaseModel):
    bot_id: int
    tipo_envio: str = "massivo"
    target: str = "todos" # 'todos', 'pendentes', 'pagantes', 'expirados'
    mensagem: str
    media_url: Optional[str] = None
    
    # Campos Extras do Wizard
    plano_oferta_id: Optional[str] = None
    valor_oferta: Optional[float] = 0.0
    expire_timestamp: Optional[int] = 0
    is_periodic: bool = False
    
    # Oferta
    incluir_oferta: bool = False
    
    # Preço
    price_mode: str = "original" # original, custom
    custom_price: Optional[float] = 0.0

    # Validade (Expiração)
    expiration_mode: str = "none" # none, minutes, hours, days
    expiration_value: Optional[int] = 0

    # Controle de Teste
    is_test: bool = False
    specific_user_id: Optional[str] = None # Telegram ID para teste

# ===========================
# ⚙️ GESTÃO DE BOTS
# ===========================

@app.post("/api/admin/bots", response_model=BotResponse)
def criar_bot(bot_data: BotCreate, db: Session = Depends(get_db)):
    if db.query(Bot).filter(Bot.token == bot_data.token).first():
        raise HTTPException(status_code=400, detail="Token já cadastrado.")

    try:
        tb = telebot.TeleBot(bot_data.token)
        public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if public_url:
            webhook_url = f"https://{public_url}/webhook/{bot_data.token}"
            tb.set_webhook(url=webhook_url)
        status = "conectado"
    except Exception as e:
        logger.error(f"Erro: {e}")
        raise HTTPException(status_code=400, detail="Token inválido.")

    novo_bot = Bot(
        nome=bot_data.nome,
        token=bot_data.token,
        id_canal_vip=bot_data.id_canal_vip,
        status=status,
        admin_principal_id=bot_data.admin_principal_id # Salva já na criação
    )
    db.add(novo_bot)
    db.commit()
    db.refresh(novo_bot)
    return novo_bot

@app.put("/api/admin/bots/{bot_id}")
def update_bot(bot_id: int, dados: BotCreate, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: raise HTTPException(404, "Bot não encontrado")
    
    old_token = bot_db.token

    if dados.nome: bot_db.nome = dados.nome
    if dados.token: bot_db.token = dados.token
    if dados.id_canal_vip: bot_db.id_canal_vip = dados.id_canal_vip
    if dados.admin_principal_id is not None: bot_db.admin_principal_id = dados.admin_principal_id
    
    if dados.token and dados.token != old_token:
        try:
            try:
                old_tb = telebot.TeleBot(old_token)
                old_tb.delete_webhook()
            except: pass

            tb = telebot.TeleBot(dados.token)
            public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-production.up.railway.app")
            if public_url.startswith("https://"): public_url = public_url.replace("https://", "")
            
            webhook_url = f"https://{public_url}/webhook/{dados.token}"
            tb.set_webhook(url=webhook_url)
            
            logger.info(f"♻️ Webhook atualizado para o bot {bot_db.nome}")
            bot_db.status = "ativo" 
        except Exception as e:
            logger.error(f"Erro ao atualizar webhook: {e}")
            bot_db.status = "erro_token"
    
    db.commit()
    db.refresh(bot_db)
    return {"status": "ok", "msg": "Bot atualizado com sucesso"}

@app.post("/api/admin/bots/{bot_id}/toggle")
def toggle_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot: raise HTTPException(404, "Bot não encontrado")
    
    novo_status = "ativo" if bot.status != "ativo" else "pausado"
    bot.status = novo_status
    db.commit()
    
    try:
        emoji = "🟢" if novo_status == "ativo" else "🔴"
        msg = f"{emoji} *STATUS DO BOT ALTERADO*\n\nO bot *{bot.nome}* agora está: *{novo_status.upper()}*"
        notificar_admin_principal(bot, msg)
    except Exception as e:
        logger.error(f"Erro ao notificar admin sobre toggle: {e}")
    
    return {"status": novo_status}

@app.delete("/api/admin/bots/{bot_id}")
def deletar_bot(bot_id: int, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    try:
        tb = telebot.TeleBot(bot_db.token)
        tb.delete_webhook()
    except: pass
    
    db.delete(bot_db)
    db.commit()
    return {"status": "deleted", "msg": "Bot removido com sucesso"}

# =========================================================
# 🛡️ GESTÃO DE ADMINISTRADORES
# =========================================================

@app.get("/api/admin/bots/{bot_id}/admins")
def listar_admins(bot_id: int, db: Session = Depends(get_db)):
    admins = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id).all()
    return admins

@app.post("/api/admin/bots/{bot_id}/admins")
def adicionar_admin(bot_id: int, dados: BotAdminCreate, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot: raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    existente = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id, BotAdmin.telegram_id == dados.telegram_id).first()
    if existente: raise HTTPException(status_code=400, detail="Este ID já é administrador.")
    
    novo_admin = BotAdmin(bot_id=bot_id, telegram_id=dados.telegram_id, nome=dados.nome)
    db.add(novo_admin)
    db.commit()
    db.refresh(novo_admin)
    return novo_admin

@app.delete("/api/admin/bots/{bot_id}/admins/{telegram_id}")
def remover_admin(bot_id: int, telegram_id: str, db: Session = Depends(get_db)):
    admin_db = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id, BotAdmin.telegram_id == telegram_id).first()
    if not admin_db: raise HTTPException(status_code=404, detail="Administrador não encontrado")
    
    db.delete(admin_db)
    db.commit()
    return {"status": "deleted", "msg": "Administrador removido com sucesso"}

# =========================================================
# 🤖 LISTAR BOTS
# =========================================================
@app.get("/api/admin/bots")
def list_bots(db: Session = Depends(get_db)):
    bots = db.query(Bot).all()
    resultado = []
    
    for bot in bots:
        u_name = bot.username or "..."
        if u_name != "...": u_name = f"@{u_name.lstrip('@')}"

        leads = db.query(func.count(Pedido.telegram_id.distinct())).filter(Pedido.bot_id == bot.id).scalar() or 0
        receita = db.query(func.sum(Pedido.valor)).filter(
            Pedido.bot_id == bot.id, 
            Pedido.status.in_(['paid', 'approved', 'completed', 'succeeded', 'active', 'expired'])
        ).scalar() or 0.0

        resultado.append({
            "id": bot.id,
            "nome": bot.nome,
            "token": bot.token,
            "username": u_name,
            "status": bot.status,
            "admin_principal_id": bot.admin_principal_id,
            "id_canal_vip": bot.id_canal_vip,
            "leads": leads,
            "revenue": receita
        })
    return resultado

# ===========================
# 💎 PLANOS & FLUXO
# ===========================

@app.post("/api/admin/plans")
def criar_plano(plano: PlanoCreate, db: Session = Depends(get_db)):
    novo_plano = PlanoConfig(
        bot_id=plano.bot_id,
        key_id=f"plan_{plano.bot_id}_{plano.dias_duracao}d",
        nome_exibicao=plano.nome_exibicao,
        descricao=f"Acesso de {plano.dias_duracao} dias",
        preco_cheio=plano.preco * 2,
        preco_atual=plano.preco,
        dias_duracao=plano.dias_duracao
    )
    db.add(novo_plano)
    db.commit()
    return {"status": "ok"}

@app.get("/api/admin/plans/{bot_id}")
def listar_planos(bot_id: int, db: Session = Depends(get_db)):
    return db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()

@app.delete("/api/admin/plans/{plan_id}")
def deletar_plano(plan_id: int, db: Session = Depends(get_db)):
    plano = db.query(PlanoConfig).filter(PlanoConfig.id == plan_id).first()
    if plano:
        db.delete(plano)
        db.commit()
    return {"status": "deleted"}

@app.get("/api/admin/bots/{bot_id}/flow")
def obter_fluxo(bot_id: int, db: Session = Depends(get_db)):
    fluxo = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not fluxo:
        return {
            "msg_boas_vindas": "Olá! Seja bem-vindo(a).",
            "media_url": "",
            "btn_text_1": "🔓 DESBLOQUEAR ACESSO",
            "autodestruir_1": False,
            "msg_2_texto": "Escolha seu plano abaixo:",
            "msg_2_media": "",
            "mostrar_planos_2": True
        }
    return fluxo

@app.post("/api/admin/bots/{bot_id}/flow")
def salvar_fluxo(bot_id: int, flow: FlowUpdate, db: Session = Depends(get_db)):
    fluxo_db = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not fluxo_db:
        fluxo_db = BotFlow(bot_id=bot_id)
        db.add(fluxo_db)
    
    fluxo_db.msg_boas_vindas = flow.msg_boas_vindas
    fluxo_db.media_url = flow.media_url
    fluxo_db.btn_text_1 = flow.btn_text_1
    fluxo_db.autodestruir_1 = flow.autodestruir_1
    fluxo_db.msg_2_texto = flow.msg_2_texto
    fluxo_db.msg_2_media = flow.msg_2_media
    fluxo_db.mostrar_planos_2 = flow.mostrar_planos_2
    
    db.commit()
    return {"status": "saved"}

# =========================================================
# 🧩 ROTAS DE PASSOS DINÂMICOS (FLOW V2)
# =========================================================

@app.get("/api/admin/bots/{bot_id}/flow/steps")
def listar_passos_flow(bot_id: int, db: Session = Depends(get_db)):
    steps = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_id).order_by(BotFlowStep.step_order).all()
    return steps

@app.post("/api/admin/bots/{bot_id}/flow/steps")
def adicionar_passo_flow(bot_id: int, payload: FlowStepCreate, db: Session = Depends(get_db)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot: raise HTTPException(404, "Bot não encontrado")

    novo_passo = BotFlowStep(
        bot_id=bot_id,
        step_order=payload.step_order,
        msg_texto=payload.msg_texto,
        msg_media=payload.msg_media,
        btn_texto=payload.btn_texto
    )
    db.add(novo_passo)
    db.commit()
    return {"status": "success", "msg": "Passo adicionado com sucesso!"}

@app.delete("/api/admin/bots/{bot_id}/flow/steps/{step_id}")
def remover_passo_flow(bot_id: int, step_id: int, db: Session = Depends(get_db)):
    passo = db.query(BotFlowStep).filter(BotFlowStep.id == step_id, BotFlowStep.bot_id == bot_id).first()
    if not passo: raise HTTPException(404, "Passo não encontrado")
    db.delete(passo)
    db.commit()
    return {"status": "deleted"}

# =========================================================
# 💰 ROTA WEBHOOK PIX (HÍBRIDA + CORREÇÃO DE ID)
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    # print("🔔 WEBHOOK PIX CHEGOU!") 
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        if not body_str: return {"status": "ignored", "reason": "empty_body"}

        data = {}
        try: data = json.loads(body_str)
        except:
            try: 
                parsed = urllib.parse.parse_qs(body_str)
                data = {k: v[0] for k, v in parsed.items()}
            except: return {"status": "error", "reason": "invalid_format"}

        # 3. EXTRAÇÃO INTELIGENTE DO ID (CORREÇÃO DE OURO)
        raw_tx_id = data.get("id") or data.get("external_reference") or data.get("uuid")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        status_pix = str(data.get("status", "")).lower()
        
        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            return {"status": "ignored"}

        # 4. BUSCA O PEDIDO (USANDO txid CORRETO)
        pedido = db.query(Pedido).filter(Pedido.txid == tx_id).first()

        if not pedido:
            # print(f"❌ Pedido {tx_id} não encontrado no banco.")
            return {"status": "ok", "msg": "Order not found"}

        if pedido.status == "paid":
            return {"status": "ok", "msg": "Already paid"}

        # 5. ATUALIZA BANCO
        pedido.status = "paid"
        pedido.mensagem_enviada = True
        db.commit()
        # print(f"✅ Pedido {tx_id} APROVADO!")
        
        # 6. ENTREGA O ACESSO
        try:
            bot_data = db.query(Bot).filter(Bot.id == pedido.bot_id).first()
            if bot_data:
                tb = telebot.TeleBot(bot_data.token)
                try: canal_id = int(str(bot_data.id_canal_vip).strip())
                except: canal_id = bot_data.id_canal_vip

                try: tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                except: pass

                convite = tb.create_chat_invite_link(chat_id=canal_id, member_limit=1, name=f"Venda {pedido.first_name}")
                
                msg = f"✅ <b>Pagamento Confirmado!</b>\n\nSeu acesso exclusivo:\n👉 {convite.invite_link}"
                tb.send_message(int(pedido.telegram_id), msg, parse_mode="HTML")
                # print("🏆 LINK ENVIADO!")
            else:
                pass
                # print("❌ Bot não encontrado.")

        except Exception as e_tg:
            logger.error(f"❌ Erro Telegram: {e_tg}")
            try: tb.send_message(int(pedido.telegram_id), "✅ Pagamento recebido! Link sendo gerado.")
            except: pass

        return {"status": "received"}

    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

# =========================================================
# 🚀 WEBHOOK GERAL DO BOT (COM PORTEIRO + PAUSA)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    if bot_token == "pix": return {"status": "ignored_loop"}
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    if bot_db.status == "pausado":
        return {"status": "paused_by_admin"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- 🚪 O PORTEIRO (VERIFICA ENTRADA NO GRUPO) ---
        if update.message and update.message.new_chat_members:
            chat_id_atual = str(update.message.chat.id)
            canal_vip_db = str(bot_db.id_canal_vip).strip()
            
            if chat_id_atual == canal_vip_db:
                for member in update.message.new_chat_members:
                    if member.is_bot: continue
                    user_id = str(member.id)
                    
                    # 1. Busca se tem pedido PAGO e VÁLIDO no banco
                    pedido = db.query(Pedido).filter(
                        Pedido.bot_id == bot_db.id,
                        Pedido.telegram_id == user_id
                    ).order_by(text("created_at DESC")).first()
                    
                    acesso_autorizado = False
                    if pedido and pedido.status == 'paid':
                        dias = 30
                        nome = (pedido.plano_nome or "").lower()
                        
                        if "vital" in nome or "mega" in nome: acesso_autorizado = True
                        else:
                            if "diario" in nome or "24" in nome: dias = 1
                            elif "trimestral" in nome: dias = 90
                            elif "semanal" in nome: dias = 7
                            
                            validade = pedido.created_at + timedelta(days=dias)
                            if datetime.utcnow() < validade: acesso_autorizado = True
                    
                    if not acesso_autorizado:
                        try:
                            bot_temp.ban_chat_member(chat_id_atual, int(user_id))
                            bot_temp.unban_chat_member(chat_id_atual, int(user_id))
                            try:
                                bot_temp.send_message(int(user_id), "🚫 **Acesso Negado**\n\nSua assinatura venceu ou não foi encontrada. Faça um novo pagamento para entrar.")
                            except: pass
                        except Exception as e_kick:
                            logger.error(f"Erro ao kickar intruso: {e_kick}")
            
            return {"status": "member_checked"}
        
        # --- 1. COMANDO /START (Início do Funil) ---
        if update.message and update.message.text == "/start":
            chat_id = update.message.chat.id
            fluxo = bot_db.fluxo
            
            texto = fluxo.msg_boas_vindas if fluxo else f"Olá! Eu sou o {bot_db.nome}."
            btn_txt = fluxo.btn_text_1 if (fluxo and fluxo.btn_text_1) else "🔓 DESBLOQUEAR ACESSO"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text=btn_txt, callback_data="passo_2"))

            media = fluxo.media_url if (fluxo and fluxo.media_url) else None
            if media:
                try:
                    if media.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_temp.send_video(chat_id, media, caption=texto, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media, caption=texto, reply_markup=markup)
                except:
                    bot_temp.send_message(chat_id, texto, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto, reply_markup=markup)

        elif update.callback_query:
            call = update.callback_query
            data = call.data
            cid = call.message.chat.id

            # --- 2. PASSO 2 (LÓGICA V2: VERIFICA PASSOS INTERMEDIÁRIOS) ---
            if data == "passo_2":
                if bot_db.fluxo and bot_db.fluxo.autodestruir_1:
                    try: bot_temp.delete_message(cid, call.message.message_id)
                    except: pass

                # Verifica se tem passos dinâmicos no banco
                primeiro_passo = db.query(BotFlowStep).filter(
                    BotFlowStep.bot_id == bot_db.id, 
                    BotFlowStep.step_order == 1
                ).first()

                if primeiro_passo:
                    markup_step = types.InlineKeyboardMarkup()
                    segundo_passo = db.query(BotFlowStep).filter(
                        BotFlowStep.bot_id == bot_db.id, 
                        BotFlowStep.step_order == 2
                    ).first()
                    next_callback = "next_step_2" if segundo_passo else "go_checkout"
                    markup_step.add(types.InlineKeyboardButton(text=primeiro_passo.btn_texto, callback_data=next_callback))

                    if primeiro_passo.msg_media:
                        try:
                            if primeiro_passo.msg_media.lower().endswith(('.mp4', '.mov')):
                                bot_temp.send_video(cid, primeiro_passo.msg_media, caption=primeiro_passo.msg_texto, reply_markup=markup_step)
                            else:
                                bot_temp.send_photo(cid, primeiro_passo.msg_media, caption=primeiro_passo.msg_texto, reply_markup=markup_step)
                        except:
                            bot_temp.send_message(cid, primeiro_passo.msg_texto, reply_markup=markup_step)
                    else:
                        bot_temp.send_message(cid, primeiro_passo.msg_texto, reply_markup=markup_step)
                else:
                    # Se não tem passos extras, vai pro final (V1)
                    enviar_oferta_final(bot_temp, cid, bot_db.fluxo, bot_db.id, db)
            
                bot_temp.answer_callback_query(call.id)

            # --- 3. NAVEGAÇÃO DINÂMICA (V2) ---
            elif data.startswith("next_step_"):
                try: step_order = int(data.split("_")[2])
                except: step_order = 1
                
                passo_atual = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id, BotFlowStep.step_order == step_order).first()

                if passo_atual:
                    markup_step = types.InlineKeyboardMarkup()
                    proximo_passo = db.query(BotFlowStep).filter(BotFlowStep.bot_id == bot_db.id, BotFlowStep.step_order == step_order + 1).first()
                    next_callback = f"next_step_{step_order + 1}" if proximo_passo else "go_checkout"
                    markup_step.add(types.InlineKeyboardButton(text=passo_atual.btn_texto, callback_data=next_callback))

                    if passo_atual.msg_media:
                        try:
                            if passo_atual.msg_media.lower().endswith(('.mp4', '.mov')):
                                bot_temp.send_video(cid, passo_atual.msg_media, caption=passo_atual.msg_texto, reply_markup=markup_step)
                            else:
                                bot_temp.send_photo(cid, passo_atual.msg_media, caption=passo_atual.msg_texto, reply_markup=markup_step)
                        except:
                            bot_temp.send_message(cid, passo_atual.msg_texto, reply_markup=markup_step)
                    else:
                        bot_temp.send_message(cid, passo_atual.msg_texto, reply_markup=markup_step)
                else:
                    enviar_oferta_final(bot_temp, cid, bot_db.fluxo, bot_db.id, db)
                
                bot_temp.answer_callback_query(call.id)

            # --- 4. FINAL DO FLUXO (GO CHECKOUT) ---
            elif data == "go_checkout":
                enviar_oferta_final(bot_temp, cid, bot_db.fluxo, bot_db.id, db)
                bot_temp.answer_callback_query(call.id)

            # --- 5. CHECKOUT (GERAR PIX) ---
            elif data.startswith("checkout_") or data.startswith("promo_"):
                plano_id = None
                preco_final = 0.0
                nome_plano_str = ""
                
                if "checkout_" in data:
                    plano_id = data.split("_")[1]
                    plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
                    if plano:
                        preco_final = plano.preco_atual
                        nome_plano_str = plano.nome_exibicao
                
                elif "promo_" in data:
                    campanha_uuid = data.split("_")[1]
                    campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.campaign_id == campanha_uuid).first()
                    if campanha and campanha.plano_id:
                        plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
                        if plano:
                            preco_final = campanha.promo_price if campanha.promo_price else plano.preco_atual
                            nome_plano_str = f"{plano.nome_exibicao} (OFERTA)"
                            plano_id = campanha.plano_id

                if preco_final > 0:
                    msg_aguarde = bot_temp.send_message(cid, "⏳ Gerando seu PIX, aguarde...")
                    temp_uuid = str(uuid.uuid4())
                    pix_data = gerar_pix_pushinpay(preco_final, temp_uuid)
                    
                    if pix_data:
                        qr_code_text = pix_data.get("qr_code_text") or pix_data.get("qr_code")
                        # [CORREÇÃO FINAL: USA txid AO INVÉS DE transaction_id]
                        provider_id = pix_data.get("id") or temp_uuid
                        final_tx_id = str(provider_id).lower()

                        novo_pedido = Pedido(
                            bot_id=bot_db.id,
                            txid=final_tx_id, # <--- AQUI ESTAVA O PROBLEMA DO WEBHOOK (CORRIGIDO)
                            telegram_id=str(cid),
                            first_name=call.from_user.first_name,
                            username=call.from_user.username,
                            plano_nome=nome_plano_str,
                            plano_id=plano_id,
                            valor=preco_final,
                            status="pending",
                            qr_code=qr_code_text
                        )
                        db.add(novo_pedido)
                        db.commit()

                        try: bot_temp.delete_message(cid, msg_aguarde.message_id)
                        except: pass

                        legenda_pix = f"""🌟 Seu pagamento foi gerado com sucesso:
🎁 Plano: {nome_plano_str}
💰 Valor: R$ {preco_final:.2f}
🔐 Pague via Pix Copia e Cola:

```
{qr_code_text}
```

👆 Toque na chave PIX acima para copiá-la
‼️ Após o pagamento, o acesso será liberado automaticamente!"""
                        bot_temp.send_message(cid, legenda_pix, parse_mode="Markdown")
                    else:
                        bot_temp.send_message(cid, "❌ Erro ao gerar PIX. Tente novamente.")
                
                bot_temp.answer_callback_query(call.id)

        return {"status": "processed"}
        
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return {"status": "error"}

# =========================================================
# 👥 ROTAS DE CRM (BASE DE CONTATOS CORRIGIDA)
# =========================================================
@app.get("/api/admin/contacts")
def listar_contatos(bot_id: Optional[int] = None, status: str = "todos", db: Session = Depends(get_db)):
    query = db.query(Pedido)
    if bot_id: query = query.filter(Pedido.bot_id == bot_id)
    if status == "pagantes": query = query.filter(Pedido.status.in_(['paid', 'active', 'approved']))
    elif status == "pendentes": query = query.filter(Pedido.status == "pending")
    elif status == "expirados": query = query.filter(Pedido.status == "expired")
    return query.order_by(desc(Pedido.created_at)).all()

# --- NOVA ROTA: DISPARO INDIVIDUAL (VIA HISTÓRICO) ---
class IndividualRemarketingRequest(BaseModel):
    bot_id: int
    user_telegram_id: str
    campaign_history_id: int # ID do histórico para copiar a msg

@app.post("/api/admin/remarketing/send-individual")
def enviar_remarketing_individual(payload: IndividualRemarketingRequest, db: Session = Depends(get_db)):
    # 1. Busca os dados da campanha antiga
    campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.id == payload.campaign_history_id).first()
    if not campanha:
        raise HTTPException(404, "Campanha original não encontrada")
    
    # 2. Decodifica a configuração
    try:
        config = json.loads(campanha.config) if isinstance(campanha.config, str) else campanha.config
        # Se config for string dentro de um json (caso antigo), tenta parsear de novo
        if isinstance(config, str): config = json.loads(config)
    except:
        config = {}

    # 3. Reconstrói o Payload
    msg = config.get("msg", "")
    media = config.get("media", "")
    
    # [CORREÇÃO CRÍTICA] Não buscamos mais 'offer' do config JSON, pois ele pode não ter sido salvo lá.
    # A verificação será feita direto pelo ID do plano na tabela.

    # 4. Prepara envio
    bot_db = db.query(Bot).filter(Bot.id == payload.bot_id).first()
    if not bot_db: raise HTTPException(404, "Bot não encontrado")
    
    sender = telebot.TeleBot(bot_db.token)
    
    # 5. Monta Botão (CORRIGIDO: Se tiver plano_id no banco, TEM oferta)
    markup = None
    if campanha.plano_id:
        # Recupera plano
        plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
        if plano:
            markup = types.InlineKeyboardMarkup()
            # Usa o preço promocional salvo na campanha ou o atual
            preco = campanha.promo_price or plano.preco_atual
            btn_text = f"🔥 {plano.nome_exibicao} - R$ {preco:.2f}"
            
            # OBS: Usamos um checkout direto aqui para garantir que funcione, 
            # já que links de promoções antigas poderiam estar expirados.
            # Se quiser forçar a mesma campanha, use f"promo_{campanha.campaign_id}"
            # Mas checkout direto é mais seguro para disparo individual manual.
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"checkout_{plano.id}"))

    # 6. Envia
    try:
        if media:
            try:
                # Tenta enviar como vídeo ou foto
                if media.lower().endswith(('.mp4', '.mov', '.avi')):
                    sender.send_video(payload.user_telegram_id, media, caption=msg, reply_markup=markup)
                else:
                    sender.send_photo(payload.user_telegram_id, media, caption=msg, reply_markup=markup)
            except Exception as e_media:
                # Se falhar a mídia (link quebrado), envia só texto com o botão
                logger.warning(f"Falha ao enviar mídia: {e_media}. Tentando texto.")
                sender.send_message(payload.user_telegram_id, msg, reply_markup=markup)
        else:
            sender.send_message(payload.user_telegram_id, msg, reply_markup=markup)
            
        return {"status": "sent", "msg": "Mensagem enviada com sucesso!"}
    except Exception as e:
        logger.error(f"Erro envio individual: {e}")
        # Retorna erro 500 para o frontend saber
        raise HTTPException(status_code=500, detail=f"Falha ao enviar: {str(e)}")

# =========================================================
# 📢 ROTAS DE REMARKETING (DISPARADOR AVANÇADO)
# =========================================================

# Variável Global para monitorar o envio em tempo real
CAMPAIGN_STATUS = {
    "running": False,
    "sent": 0,
    "total": 0,
    "blocked": 0
}

# =========================================================
# 📢 LÓGICA DE REMARKETING (OFERTA + VALIDADE + TESTE)
# =========================================================
# Variável Global para monitorar o envio em tempo real
CAMPAIGN_STATUS = {
    "running": False,
    "sent": 0,
    "total": 0,
    "blocked": 0
}

def processar_envio_remarketing(bot_id: int, payload: RemarketingRequest, db: Session):
    global CAMPAIGN_STATUS
    # Reinicia status
    CAMPAIGN_STATUS = {"running": True, "sent": 0, "total": 0, "blocked": 0}
    
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: 
        CAMPAIGN_STATUS["running"] = False
        return

    # --- 1. CONFIGURAÇÃO DA OFERTA (PREÇO E DATA) ---
    uuid_campanha = str(uuid.uuid4())
    data_expiracao = None
    preco_final = 0.0
    plano_db = None

    if payload.incluir_oferta and payload.plano_oferta_id:
        # Busca plano (aceita ID numérico ou string chave)
        plano_db = db.query(PlanoConfig).filter(
            (PlanoConfig.key_id == str(payload.plano_oferta_id)) | 
            (PlanoConfig.id == int(payload.plano_oferta_id) if str(payload.plano_oferta_id).isdigit() else False)
        ).first()

        if plano_db:
            # Define Preço
            if payload.price_mode == "custom" and payload.custom_price > 0:
                preco_final = payload.custom_price
            else:
                preco_final = plano_db.preco_atual
            
            # Define Expiração
            if payload.expiration_mode != "none" and payload.expiration_value > 0:
                agora = datetime.utcnow()
                val = payload.expiration_value
                if payload.expiration_mode == "minutes":
                    data_expiracao = agora + timedelta(minutes=val)
                elif payload.expiration_mode == "hours":
                    data_expiracao = agora + timedelta(hours=val)
                elif payload.expiration_mode == "days":
                    data_expiracao = agora + timedelta(days=val)

    # --- 2. SALVAR A CAMPANHA NO BANCO (ANTES DE ENVIAR) ---
    # Precisamos salvar antes para que o ID da campanha exista quando o usuário clicar
    # Se for teste, NÃO salvamos no banco para não sujar o histórico, a menos que queira log
    nova_campanha = None
    if not payload.is_test:
        nova_campanha = RemarketingCampaign(
            bot_id=bot_id,
            campaign_id=uuid_campanha,
            type="massivo",
            target=payload.target,
            config=json.dumps({"msg": payload.mensagem, "media": payload.media_url}),
            status="enviando",
            
            # Dados da Oferta
            plano_id=plano_db.id if plano_db else None,
            promo_price=preco_final if plano_db else None,
            expiration_at=data_expiracao
        )
        db.add(nova_campanha)
        db.commit()

    # --- 3. DEFINIR PÚBLICO ALVO ---
    bot_sender = telebot.TeleBot(bot_db.token)
    usuarios_para_envio = []

    if payload.is_test:
        # Se for teste, manda só para o ID específico (Admin)
        if payload.specific_user_id:
            class MockUser:
                def __init__(self, tid): self.telegram_id = tid
            usuarios_para_envio = [MockUser(payload.specific_user_id)]
    else:
        # Lógica normal de busca no banco
        query = db.query(Pedido).filter(Pedido.bot_id == bot_id)
        if payload.target == "pendentes": query = query.filter(Pedido.status == "pending")
        elif payload.target == "pagantes": query = query.filter(Pedido.status == "paid")
        elif payload.target == "expirados": query = query.filter(Pedido.status == "expired")
        usuarios_para_envio = query.distinct(Pedido.telegram_id).all()

    CAMPAIGN_STATUS["total"] = len(usuarios_para_envio)

    # --- 4. PREPARAR BOTÃO (CALLBACK ESPECIAL) ---
    markup = None
    if plano_db:
        markup = types.InlineKeyboardMarkup()
        btn_text = f"🔥 {plano_db.nome_exibicao} - R$ {preco_final:.2f}"
        
        # Se for teste, usamos um callback fictício ou direto para checkout padrão (pois não tem campanha salva)
        if payload.is_test:
             # No teste, forçamos um checkout direto só pra ver o botão, mas sem validação de tempo do banco
             markup.add(types.InlineKeyboardButton(f"[TESTE] {btn_text}", callback_data=f"checkout_{plano_db.id}"))
        else:
             # O callback 'promo_' leva o ID da campanha. O Webhook vai checar se expirou.
             markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"promo_{uuid_campanha}"))

    # --- 5. DISPARO ---
    sent_count = 0
    blocked_count = 0

    for u in usuarios_para_envio:
        try:
            midia_ok = False
            if payload.media_url and len(payload.media_url) > 5:
                try:
                    if payload.media_url.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_sender.send_video(u.telegram_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                    else:
                        bot_sender.send_photo(u.telegram_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                    midia_ok = True
                except: pass
            
            if not midia_ok:
                bot_sender.send_message(u.telegram_id, payload.mensagem, reply_markup=markup)
            
            sent_count += 1
        except Exception as e:
            if "blocked" in str(e).lower() or "kicked" in str(e).lower(): blocked_count += 1
        
        time.sleep(0.05) # Evita flood

    CAMPAIGN_STATUS["running"] = False
    
    # Atualiza status final no banco (Se não for teste)
    if not payload.is_test and nova_campanha:
        nova_campanha.status = "concluido"
        nova_campanha.total_leads = len(usuarios_para_envio)
        nova_campanha.sent_success = sent_count
        nova_campanha.blocked_count = blocked_count
        db.commit()

# --- ROTAS DA API ---

@app.post("/api/admin/remarketing/send")
def enviar_remarketing(payload: RemarketingRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # Lógica para Teste: Se for teste e não tiver ID, pega o último do banco
    if payload.is_test and not payload.specific_user_id:
        ultimo = db.query(Pedido).filter(Pedido.bot_id == payload.bot_id).order_by(Pedido.id.desc()).first()
        if ultimo:
            payload.specific_user_id = ultimo.telegram_id
        else:
            # Tenta pegar um admin se não tiver clientes
            admin = db.query(BotAdmin).filter(BotAdmin.bot_id == payload.bot_id).first()
            if admin: payload.specific_user_id = admin.telegram_id
            else: raise HTTPException(400, "Nenhum usuário encontrado para teste. Interaja com o bot primeiro (/start).")

    background_tasks.add_task(processar_envio_remarketing, payload.bot_id, payload, db)
    return {"status": "enviando", "msg": "Campanha iniciada!"}

@app.get("/api/admin/remarketing/status")
def status_remarketing():
    return CAMPAIGN_STATUS

@app.get("/api/admin/remarketing/history/{bot_id}")
def historico_remarketing(bot_id: int, db: Session = Depends(get_db)):
    history = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id).order_by(RemarketingCampaign.data_envio.desc()).all()
    return [{
        "id": h.id, 
        "data": h.data_envio.strftime("%d/%m/%Y %H:%M"), 
        "total": h.total_leads, 
        "sent": h.sent_success, 
        "blocked": h.blocked_count, 
        "config": {"content_data": h.config}
    } for h in history]

# =========================================================
# 📊 ROTA DE DASHBOARD (KPIs REAIS E CUMULATIVOS)
# =========================================================
@app.get("/api/admin/dashboard/stats")
def dashboard_stats(bot_id: Optional[int] = None, db: Session = Depends(get_db)): 
    """Calcula métricas. Se bot_id for passado, filtra por ele."""
    
    # [CORREÇÃO FINANCEIRA] - Faturamento Total
    # Soma vendas ativas E expiradas. O dinheiro entrou, conta como receita.
    q_revenue = db.query(func.sum(Pedido.valor)).filter(
        Pedido.status.in_(['paid', 'active', 'approved', 'expired', 'completed', 'succeeded'])
    )
    
    # Usuários Ativos (Aqui SIM ignoramos os expirados, pois queremos saber quem está no canal agora)
    q_users = db.query(Pedido.telegram_id).filter(
        Pedido.status.in_(['paid', 'active', 'approved', 'completed', 'succeeded'])
    )
    
    # Vendas Hoje (Considera qualquer venda feita hoje, mesmo que tenha sido teste curto e expirou)
    today = datetime.utcnow().date()
    start_of_day = datetime.combine(today, datetime.min.time())
    q_sales_today = db.query(func.sum(Pedido.valor)).filter(
        Pedido.status.in_(['paid', 'active', 'approved', 'expired', 'completed', 'succeeded']),
        Pedido.created_at >= start_of_day
    )

    # APLICA FILTRO DE BOT (SE SELECIONADO)
    if bot_id:
        q_revenue = q_revenue.filter(Pedido.bot_id == bot_id)
        q_users = q_users.filter(Pedido.bot_id == bot_id)
        q_sales_today = q_sales_today.filter(Pedido.bot_id == bot_id)

    total_revenue = q_revenue.scalar() or 0.0
    active_users = q_users.distinct().count()
    sales_today = q_sales_today.scalar() or 0.0

    return {
        "total_revenue": total_revenue,
        "active_users": active_users,
        "sales_today": sales_today
    }
# =========================================================
# 💸 WEBHOOK DE PAGAMENTO (BLINDADO E TAGARELA)
# =========================================================
@app.post("/api/webhook")
async def webhook(req: Request, bg_tasks: BackgroundTasks):
    try:
        raw = await req.body()
        try: 
            payload = json.loads(raw)
        except: 
            # Fallback para formato x-www-form-urlencoded
            payload = {k: v[0] for k,v in parse_qs(raw.decode()).items()}
        
        # Log para debug (opcional, pode remover em produção)
        # logger.info(f"Webhook recebido: {payload}")

        # Se for pagamento APROVADO (Vários status possíveis de gateways)
        if str(payload.get('status')).upper() in ['PAID', 'APPROVED', 'COMPLETED', 'SUCCEEDED']:
            db = SessionLocal()
            tx = str(payload.get('id')).lower() # ID da transação
            
            # Busca o pedido pelo ID da transação
            p = db.query(Pedido).filter(Pedido.transaction_id == tx).first()
            
            # Se achou o pedido e ele ainda não estava pago
            if p and p.status != 'paid':
                p.status = 'paid'
                db.commit() # Salva o status pago
                
                # --- 🔔 NOTIFICAÇÃO AO ADMIN (NOVO) ---
                try:
                    bot_db = db.query(Bot).filter(Bot.id == p.bot_id).first()
                    
                    # Verifica se o bot tem um Admin configurado para receber o aviso
                    if bot_db and bot_db.admin_principal_id:
                        msg_venda = (
                            f"💰 *VENDA APROVADA!*\n\n"
                            f"👤 Cliente: {p.first_name}\n"
                            f"💎 Plano: {p.plano_nome}\n"
                            f"💵 Valor: R$ {p.valor:.2f}\n"
                            f"📅 Data: {datetime.now().strftime('%d/%m %H:%M')}"
                        )
                        # Chama a função auxiliar de notificação
                        notificar_admin_principal(bot_db, msg_venda) 
                except Exception as e_notify:
                    logger.error(f"Erro ao notificar admin: {e_notify}")
                # --------------------------------------

                # --- ENVIO DO LINK DE ACESSO AO CLIENTE ---
                if not p.mensagem_enviada:
                    try:
                        bot_data = db.query(Bot).filter(Bot.id == p.bot_id).first()
                        tb = telebot.TeleBot(bot_data.token)
                        
                        # Tenta converter o ID do canal VIP com segurança
                        try: canal_vip_id = int(str(bot_data.id_canal_vip).strip())
                        except: canal_vip_id = bot_data.id_canal_vip

                        # Tenta desbanir o usuário antes (garantia caso ele tenha sido expulso antes)
                        try: tb.unban_chat_member(canal_vip_id, int(p.telegram_id))
                        except: pass

                        # Gera Link Único (Válido para 1 pessoa)
                        convite = tb.create_chat_invite_link(
                            chat_id=canal_vip_id, 
                            member_limit=1, 
                            name=f"Venda {p.first_name}"
                        )
                        link_acesso = convite.invite_link

                        msg_sucesso = f"""
✅ <b>Pagamento Confirmado!</b>

Seu acesso ao <b>{bot_data.nome}</b> foi liberado.
Toque no link abaixo para entrar no Canal VIP:

👉 {link_acesso}

⚠️ <i>Este link é único e válido apenas para você.</i>
"""
                        # Envia a mensagem com o link para o usuário
                        tb.send_message(int(p.telegram_id), msg_sucesso, parse_mode="HTML")
                        
                        # Marca que a mensagem foi enviada para não enviar duplicado
                        p.mensagem_enviada = True
                        db.commit()
                        logger.info(f"🏆 Link enviado para {p.first_name}")

                    except Exception as e_telegram:
                        logger.error(f"❌ ERRO TELEGRAM: {e_telegram}")
                        # Fallback: Avisa o cliente que deu erro no envio do link, mas confirma o pagamento
                        try:
                            tb.send_message(int(p.telegram_id), "✅ Pagamento recebido! \n\n⚠️ Houve um erro ao gerar seu link automático. Um administrador entrará em contato em breve.")
                        except: pass

            db.close()
        
        # Retorna 200 OK para o Gateway de Pagamento parar de mandar o Webhook
        return {"status": "received"}

    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        # Mesmo com erro, retornamos 200 ou estrutura json para não travar o gateway (opcional, depende da estratégia)
        return {"status": "error"}

@app.get("/")
def home():
    return {"status": "Zenyx V2.0 Online (Fixed)"}
