import os
import logging
import telebot
import requests
import time
import urllib.parse # <--- ADICIONE ESTE NOVO IMPORT
import threading # <--- ADICIONE ESTE NOVO IMPORT PARA O ROBÔ DE VENCIMENTO
from telebot import types
import json
import uuid
from fastapi import BackgroundTasks # <--- IMPORTANTE
from sqlalchemy import text  # Importante para o SQL
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
from sqlalchemy import func

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, Pedido, SystemConfig, RemarketingCampaign, BotAdmin, engine

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
# 🛠️ AUTO-REPARO E INÍCIO DOS JOBS
# =========================================================
@app.on_event("startup")
def on_startup():
    # 1. Cria tabelas e corrige colunas
    init_db()
    try:
        with engine.connect() as conn:
            logger.info("🔧 Verificando integridade do banco de dados...")
            comandos = [
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS data_envio TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS campaign_id VARCHAR;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS config TEXT;"
            ]
            for cmd in comandos:
                try: conn.execute(text(cmd))
                except Exception as e: logger.warning(f"Aviso SQL: {e}")
            
            conn.commit()
            logger.info("✅ BANCO DE DADOS PRONTO!")
            
        # --- 2. INICIA O CEIFADOR (VERIFICADOR DE VENCIMENTOS) ---
        # Roda em segundo plano (daemon) para não travar o site
        thread = threading.Thread(target=loop_verificar_vencimentos)
        thread.daemon = True
        thread.start()
        logger.info("💀 O Ceifador (Auto-Kick) foi iniciado!")
            
    except Exception as e:
        logger.error(f"❌ Erro na inicialização: {e}")

def get_db():
    """Gera conexão com o banco de dados"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================================================
# 💀 O CEIFADOR: VERIFICA VENCIMENTOS E REMOVE (KICK SUAVE)
# =========================================================
def loop_verificar_vencimentos():
    """Roda a cada 60 minutos para remover usuários vencidos"""
    while True:
        try:
            logger.info("⏳ Verificando assinaturas vencidas...")
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
                    # (Como não salvamos dias no pedido antes, usamos o nome como referência)
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
                            # Isso permite que ele compre de novo e entre sem erro de "User was kicked"
                            tb.unban_chat_member(canal_id, int(user.telegram_id))
                            
                            # 4. Atualiza DB para 'expired' (Para o Porteiro barrar depois)
                            user.status = 'expired'
                            db.commit()
                            
                            # 5. Avisa o usuário
                            try:
                                tb.send_message(user.telegram_id, "Seu plano VIP expirou! 😢\nPara voltar ao canal, renove sua assinatura digitando /start")
                            except: pass
                            
                        except Exception as e_kick:
                            # Se der erro (ex: user já saiu), marca como expirado mesmo assim
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

# --- ROTAS DE INTEGRAÇÃO (SALVAR TOKEN) ---
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
    
    config.value = data.token
    config.updated_at = datetime.utcnow()
    db.commit()
    
    if len(data.token) > 10:
        return {"status": "conectado", "msg": "Token salvo com sucesso!"}
    else:
        return {"status": "erro", "msg": "Token parece inválido."}

# --- MODELOS ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str

# Novo modelo para Atualização
class BotUpdate(BaseModel):
    nome: Optional[str] = None
    token: Optional[str] = None
    id_canal_vip: Optional[str] = None

# Modelo para Criar Admin
class BotAdminCreate(BaseModel):
    telegram_id: str
    nome: Optional[str] = "Admin"

# Modelo de Resposta com Estatísticas
class BotResponse(BotCreate):
    id: int
    status: str
    # Novos campos de métricas
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

# ✅ MODELO COMPLETO PARA O WIZARD DE REMARKETING
class RemarketingRequest(BaseModel):
    bot_id: int
    tipo_envio: str = "massivo"
    target: str = "todos" # 'todos', 'pendentes', 'pagantes', 'expirados'
    mensagem: str
    media_url: Optional[str] = None
    incluir_oferta: bool = False
    plano_oferta_id: Optional[str] = None
    is_test: bool = False
    
    # Campos Extras do Wizard
    plano_oferta_id: Optional[str] = None
    valor_oferta: Optional[float] = 0.0
    expire_timestamp: Optional[int] = 0
    is_periodic: bool = False
    
    # Oferta
    incluir_oferta: bool = False
    plano_oferta_id: Optional[str] = None
    
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
        status=status
    )
    db.add(novo_bot)
    db.commit()
    db.refresh(novo_bot)
    return novo_bot

@app.put("/api/admin/bots/{bot_id}")
def atualizar_bot(bot_id: int, dados: BotUpdate, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # Se houver troca de token, precisamos atualizar o Webhook
    if dados.token and dados.token != bot_db.token:
        try:
            # 1. Tenta remover webhook do token antigo (opcional, mas bom)
            try:
                old_tb = telebot.TeleBot(bot_db.token)
                old_tb.delete_webhook()
            except: pass

            # 2. Configura o novo webhook
            tb = telebot.TeleBot(dados.token)
            public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "zenyx-gbs-production.up.railway.app")
            # Garante que não tem https:// duplicado
            if public_url.startswith("https://"): public_url = public_url.replace("https://", "")
            
            webhook_url = f"https://{public_url}/webhook/{dados.token}"
            tb.set_webhook(url=webhook_url)
            
            logger.info(f"♻️ Webhook atualizado para o novo token do bot {bot_db.nome}")
            bot_db.status = "conectado"
        except Exception as e:
            logger.error(f"Erro ao atualizar webhook: {e}")
            bot_db.status = "erro_token"
    
    # Atualiza os campos no banco
    if dados.nome: bot_db.nome = dados.nome
    if dados.token: bot_db.token = dados.token
    if dados.id_canal_vip: bot_db.id_canal_vip = dados.id_canal_vip
    
    db.commit()
    db.refresh(bot_db)
    return {"status": "updated", "msg": "Bot atualizado com sucesso!"}

# --- NOVA ROTA: LIGAR/DESLIGAR BOT (TOGGLE) ---
@app.post("/api/admin/bots/{bot_id}/toggle")
def toggle_bot(bot_id: int, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # Inverte o status atual
    if bot_db.status == "pausado":
        bot_db.status = "conectado" # Liga
    else:
        bot_db.status = "pausado" # Desliga
        
    db.commit()
    return {"id": bot_db.id, "status": bot_db.status}

# --- NOVA ROTA: EXCLUIR BOT ---
@app.delete("/api/admin/bots/{bot_id}")
def deletar_bot(bot_id: int, db: Session = Depends(get_db)):
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # 1. Tenta remover o Webhook do Telegram para limpar
    try:
        tb = telebot.TeleBot(bot_db.token)
        tb.delete_webhook()
    except:
        pass # Se der erro (ex: token inválido), continua e apaga do banco
    
    # 2. Apaga do Banco de Dados
    db.delete(bot_db)
    db.commit()
    
    return {"status": "deleted", "msg": "Bot removido com sucesso"}

# =========================================================
# 🛡️ GESTÃO DE ADMINISTRADORES (FASE 1)
# =========================================================

@app.get("/api/admin/bots/{bot_id}/admins")
def listar_admins(bot_id: int, db: Session = Depends(get_db)):
    """Lista todos os admins de um bot específico"""
    admins = db.query(BotAdmin).filter(BotAdmin.bot_id == bot_id).all()
    return admins

@app.post("/api/admin/bots/{bot_id}/admins")
def adicionar_admin(bot_id: int, dados: BotAdminCreate, db: Session = Depends(get_db)):
    """Adiciona um novo admin ao bot"""
    # Verifica se o bot existe
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")
    
    # Verifica se já é admin
    existente = db.query(BotAdmin).filter(
        BotAdmin.bot_id == bot_id, 
        BotAdmin.telegram_id == dados.telegram_id
    ).first()
    
    if existente:
        raise HTTPException(status_code=400, detail="Este ID já é administrador deste bot.")
    
    novo_admin = BotAdmin(
        bot_id=bot_id,
        telegram_id=dados.telegram_id,
        nome=dados.nome
    )
    db.add(novo_admin)
    db.commit()
    db.refresh(novo_admin)
    return novo_admin

@app.delete("/api/admin/bots/{bot_id}/admins/{telegram_id}")
def remover_admin(bot_id: int, telegram_id: str, db: Session = Depends(get_db)):
    """Remove um admin pelo Telegram ID"""
    admin_db = db.query(BotAdmin).filter(
        BotAdmin.bot_id == bot_id,
        BotAdmin.telegram_id == telegram_id
    ).first()
    
    if not admin_db:
        raise HTTPException(status_code=404, detail="Administrador não encontrado")
    
    db.delete(admin_db)
    db.commit()
    return {"status": "deleted", "msg": "Administrador removido com sucesso"}

# --- NOVA ROTA: LISTAR BOTS ---

@app.get("/api/admin/bots", response_model=List[BotResponse])
def listar_bots(db: Session = Depends(get_db)):
    bots = db.query(Bot).all()
    resultado = []
    
    for bot in bots:
        # 1. Calcula Leads (Total de Telegram IDs únicos que interagiram com este bot)
        leads = db.query(func.count(func.distinct(Pedido.telegram_id)))\
                  .filter(Pedido.bot_id == bot.id).scalar() or 0
        
        # 2. Calcula Receita Total (Soma dos pedidos 'paid')
        revenue = db.query(func.sum(Pedido.valor))\
                    .filter(Pedido.bot_id == bot.id, Pedido.status == 'paid').scalar() or 0.0
        
        # Monta o objeto de resposta manual para incluir os campos extras
        bot_dict = {
            "id": bot.id,
            "nome": bot.nome,
            "token": bot.token,
            "id_canal_vip": bot.id_canal_vip,
            "status": bot.status,
            "leads": leads,
            "revenue": revenue
        }
        resultado.append(bot_dict)
        
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
# 💰 ROTA WEBHOOK PIX (HÍBRIDA + CORREÇÃO DE ID + HTML)
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    print("🔔 WEBHOOK PIX CHEGOU!") 
    try:
        # 1. PEGA O CORPO BRUTO
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        if not body_str:
            return {"status": "ignored", "reason": "empty_body"}

        # 2. TENTA DECIFRAR (JSON OU FORM DATA) - Lógica "Poliglota"
        data = {}
        try:
            data = json.loads(body_str) # Tenta JSON
        except:
            try:
                parsed = urllib.parse.parse_qs(body_str) # Tenta Form Data
                data = {k: v[0] for k, v in parsed.items()}
            except:
                return {"status": "error", "reason": "invalid_format"}

        # 3. EXTRAÇÃO INTELIGENTE DO ID (CORREÇÃO DE OURO)
        raw_tx_id = data.get("id") or data.get("external_reference") or data.get("uuid")
        tx_id = str(raw_tx_id).lower() if raw_tx_id else None
        status_pix = str(data.get("status", "")).lower()
        
        print(f"🔎 Processando: ID={tx_id} | Status={status_pix}")

        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            return {"status": "ignored"}

        # 4. BUSCA O PEDIDO
        pedido = db.query(Pedido).filter(Pedido.transaction_id == tx_id).first()

        if not pedido:
            print(f"❌ Pedido {tx_id} não encontrado no banco.")
            return {"status": "ok", "msg": "Order not found"}

        if pedido.status == "paid":
            return {"status": "ok", "msg": "Already paid"}

        # 5. ATUALIZA BANCO
        pedido.status = "paid"
        pedido.mensagem_enviada = True
        db.commit()
        print(f"✅ Pedido {tx_id} APROVADO!")
        
        # 6. ENTREGA O ACESSO (USANDO HTML PARA NÃO QUEBRAR O LINK)
        try:
            bot_data = db.query(Bot).filter(Bot.id == pedido.bot_id).first()
            if bot_data:
                tb = telebot.TeleBot(bot_data.token)
                
                # Tratamento do ID do Canal
                try: canal_id = int(str(bot_data.id_canal_vip).strip())
                except: canal_id = bot_data.id_canal_vip

                # Tenta desbanir antes (Kick Suave)
                try: tb.unban_chat_member(canal_id, int(pedido.telegram_id))
                except: pass

                # Gera Link Único
                convite = tb.create_chat_invite_link(
                    chat_id=canal_id, 
                    member_limit=1, 
                    name=f"Venda {pedido.first_name}"
                )
                
                # MENSAGEM EM HTML (CRÍTICO: Evita erro de parse do Telegram)
                msg = f"✅ <b>Pagamento Confirmado!</b>\n\nSeu acesso exclusivo:\n👉 {convite.invite_link}"
                tb.send_message(int(pedido.telegram_id), msg, parse_mode="HTML")
                print("🏆 LINK ENVIADO!")
            else:
                print("❌ Bot não encontrado.")

        except Exception as e_tg:
            print(f"❌ Erro Telegram: {e_tg}")
            try: tb.send_message(int(pedido.telegram_id), "✅ Pagamento recebido! Link sendo gerado.")
            except: pass

        return {"status": "received"}

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

# =========================================================
# 🚀 WEBHOOK GERAL DO BOT (COM PORTEIRO + PAUSA)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    
    # Proteção contra loop do pix
    if bot_token == "pix": return {"status": "ignored_loop"}
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    # --- 🛑 NOVA VERIFICAÇÃO: BOT PAUSADO? ---
    # Se você clicou no botão de desligar no painel, ele para aqui.
    if bot_db.status == "pausado":
        return {"status": "paused_by_admin"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- 🚪 O PORTEIRO (VERIFICA ENTRADA NO GRUPO) ---
        # Se alguém tentar entrar no grupo pelo link...
        if update.message and update.message.new_chat_members:
            chat_id_atual = str(update.message.chat.id)
            # Normaliza o ID do canal do banco
            canal_vip_db = str(bot_db.id_canal_vip).strip()
            
            # Verifica se o evento aconteceu no Canal VIP protegido
            if chat_id_atual == canal_vip_db:
                for member in update.message.new_chat_members:
                    if member.is_bot: continue # Ignora bots
                    
                    user_id = str(member.id)
                    logger.info(f"👤 Verificando entrada de {user_id} no canal {canal_vip_db}")
                    
                    # 1. Busca se tem pedido PAGO e VÁLIDO no banco
                    pedido = db.query(Pedido).filter(
                        Pedido.bot_id == bot_db.id,
                        Pedido.telegram_id == user_id
                    ).order_by(text("created_at DESC")).first() # Pega o último
                    
                    acesso_autorizado = False
                    
                    if pedido and pedido.status == 'paid':
                        # Verifica data de validade (Dupla checagem)
                        dias = 30
                        nome = (pedido.plano_nome or "").lower()
                        
                        if "vital" in nome or "mega" in nome: 
                            acesso_autorizado = True
                        else:
                            if "diario" in nome or "24" in nome: dias = 1
                            elif "trimestral" in nome: dias = 90
                            elif "semanal" in nome: dias = 7
                            
                            validade = pedido.created_at + timedelta(days=dias)
                            # Se a data atual for MENOR que a validade, deixa entrar
                            if datetime.utcnow() < validade:
                                acesso_autorizado = True
                    
                    # 2. Se não tiver autorizado, CHUTA!
                    if not acesso_autorizado:
                        logger.warning(f"🚫 Intruso detectado! Removendo {user_id}...")
                        try:
                            # Ban + Unban (Kick Suave para não poluir a blacklist)
                            bot_temp.ban_chat_member(chat_id_atual, int(user_id))
                            bot_temp.unban_chat_member(chat_id_atual, int(user_id))
                            
                            # Avisa no privado
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
            
            # Textos e Botão
            texto = fluxo.msg_boas_vindas if fluxo else f"Olá! Eu sou o {bot_db.nome}."
            btn_txt = fluxo.btn_text_1 if (fluxo and fluxo.btn_text_1) else "🔓 DESBLOQUEAR ACESSO"
            
            # Cria botão para o próximo passo
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text=btn_txt, callback_data="passo_2"))

            # Envia Mídia ou Texto
            media = fluxo.media_url if (fluxo and fluxo.media_url) else None
            if media:
                try:
                    if media.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_temp.send_video(chat_id, media, caption=texto, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media, caption=texto, reply_markup=markup)
                except Exception as e:
                    logger.error(f"Erro mídia 1: {e}")
                    # Fallback: envia texto se a mídia falhar
                    bot_temp.send_message(chat_id, texto, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto, reply_markup=markup)

        # --- 2. CLIQUE NO BOTÃO (OFERTA) ---
        elif update.callback_query and update.callback_query.data == "passo_2":
            chat_id = update.callback_query.message.chat.id
            msg_id = update.callback_query.message.message_id
            fluxo = bot_db.fluxo
            
            # A) Autodestruição (Se configurado)
            if fluxo and fluxo.autodestruir_1:
                try:
                    bot_temp.delete_message(chat_id, msg_id)
                except Exception as e:
                    logger.warning(f"Falha ao deletar msg: {e}")

            # B) Prepara Segunda Mensagem
            texto_2 = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Escolha seu plano:"
            media_2 = fluxo.msg_2_media if (fluxo and fluxo.msg_2_media) else None
            
            # C) Cria Botões dos Planos
            markup = types.InlineKeyboardMarkup()
            if fluxo and fluxo.mostrar_planos_2:
                for p in bot_db.planos:
                    # Cria botão com preço e callback de checkout
                    markup.add(types.InlineKeyboardButton(text=f"{p.nome_exibicao} - R$ {p.preco_atual:.2f}", callback_data=f"checkout_{p.id}"))
            
            # D) Envia Mensagem 2 (Mídia ou Texto)
            if media_2:
                try:
                    if media_2.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_temp.send_video(chat_id, media_2, caption=texto_2, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media_2, caption=texto_2, reply_markup=markup)
                except:
                    bot_temp.send_message(chat_id, texto_2, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto_2, reply_markup=markup)
            
            # Para o "reloginho" do botão
            bot_temp.answer_callback_query(update.callback_query.id)

        # ==================================================================
        # 🕒 AQUI ENTRA A NOVIDADE: VERIFICAÇÃO DE OFERTA COM EXPIRAÇÃO
        # ==================================================================
        elif update.callback_query and update.callback_query.data.startswith("promo_"):
            chat_id = update.callback_query.message.chat.id
            # Pega o ID da campanha que vem no botão (ex: promo_123e4567...)
            campanha_uuid = update.callback_query.data.split("_")[1]
            
            # 1. Busca a Campanha no Banco para ver as regras
            campanha = db.query(RemarketingCampaign).filter(RemarketingCampaign.campaign_id == campanha_uuid).first()
            
            if not campanha or not campanha.plano_id:
                bot_temp.answer_callback_query(update.callback_query.id, "Oferta não encontrada.")
                return {"status": "error"}

            # 2. VERIFICA SE A OFERTA EXPIROU (O GRANDE TRUQUE)
            # Se tiver data de expiração E a data de agora for maior que a data limite...
            if campanha.expiration_at and datetime.utcnow() > campanha.expiration_at:
                
                # Manda a mensagem de escassez
                msg_esgotado = "🚫 **OFERTA ENCERRADA!**\n\nInfelizmente as vagas promocionais esgotaram ou o tempo da oferta acabou.\n\nFique atento às próximas oportunidades!"
                bot_temp.send_message(chat_id, msg_esgotado, parse_mode="Markdown")
                
                bot_temp.answer_callback_query(update.callback_query.id, "Oferta expirada!")
                return {"status": "expired"}

            # 3. SE ESTIVER VÁLIDA: GERA O PIX COM O PREÇO PROMOCIONAL
            # Pega o plano original para saber o nome
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == campanha.plano_id).first()
            
            # Usa o preço da promoção (se existir) ou o preço atual do plano
            preco_final = campanha.promo_price if campanha.promo_price else plano.preco_atual
            
            msg_aguarde = bot_temp.send_message(chat_id, f"⏳ Gerando oferta exclusiva de R$ {preco_final:.2f}...")
            
            # Gera PIX
            temp_uuid = str(uuid.uuid4())
            pix_data = gerar_pix_pushinpay(preco_final, temp_uuid)
            
            if pix_data:
                qr_code_text = pix_data.get("qr_code_text") or pix_data.get("qr_code")
                provider_id = pix_data.get("id") or temp_uuid
                final_tx_id = str(provider_id).lower()

                # Cria o pedido como "Pendente"
                novo_pedido = Pedido(
                    bot_id=bot_db.id,
                    transaction_id=final_tx_id,
                    telegram_id=str(chat_id),
                    first_name=update.callback_query.from_user.first_name,
                    username=update.callback_query.from_user.username,
                    plano_nome=f"{plano.nome_exibicao} (OFERTA)", # Marca no nome que foi oferta
                    valor=preco_final,
                    status="pending",
                    qr_code=qr_code_text
                )
                db.add(novo_pedido)
                db.commit()

                try: bot_temp.delete_message(chat_id, msg_aguarde.message_id)
                except: pass

                # Manda o PIX Bonitinho
                legenda_pix = f"""🎉 **OFERTA ATIVADA COM SUCESSO!**
🎁 Plano: {plano.nome_exibicao}
💸 **Valor Promocional: R$ {preco_final:.2f}**

Copie o código abaixo para garantir sua vaga:

```
{qr_code_text}
```

👆 Toque no código para copiar.
⏳ Pague agora antes que expire!"""

                bot_temp.send_message(chat_id, legenda_pix, parse_mode="Markdown")
            else:
                bot_temp.send_message(chat_id, "❌ Erro ao gerar oferta.")

            bot_temp.answer_callback_query(update.callback_query.id)
            return {"status": "processed"}

        # ==================================================================
        # 🛒 CHECKOUT PADRÃO (SEM VALIDADE / PREÇO CHEIO)
        # ==================================================================
        elif update.callback_query and update.callback_query.data.startswith("checkout_"):
            chat_id = update.callback_query.message.chat.id
            plano_id = update.callback_query.data.split("_")[1]
            
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
            if not plano:
                bot_temp.send_message(chat_id, "Plano não encontrado.")
                return {"status": "error"}

            msg_aguarde = bot_temp.send_message(chat_id, "⏳ Gerando seu PIX, aguarde...")
            
            temp_uuid = str(uuid.uuid4())
            pix_data = gerar_pix_pushinpay(plano.preco_atual, temp_uuid)
            
            if pix_data:
                qr_code_text = pix_data.get("qr_code_text") or pix_data.get("qr_code")
                provider_id = pix_data.get("id") or temp_uuid
                final_tx_id = str(provider_id).lower()

                novo_pedido = Pedido(
                    bot_id=bot_db.id,
                    transaction_id=final_tx_id, 
                    telegram_id=str(chat_id),
                    first_name=update.callback_query.from_user.first_name,
                    username=update.callback_query.from_user.username,
                    plano_nome=plano.nome_exibicao,
                    valor=plano.preco_atual,
                    status="pending",
                    qr_code=qr_code_text
                )
                db.add(novo_pedido)
                db.commit()

                try: bot_temp.delete_message(chat_id, msg_aguarde.message_id)
                except: pass

                legenda_pix = f"""🌟 Seu pagamento foi gerado com sucesso:
🎁 Plano: {plano.nome_exibicao}
💰 Valor: R$ {plano.preco_atual:.2f}
🔐 Pague via Pix Copia e Cola:

```
{qr_code_text}
```

👆 Toque na chave PIX acima para copiá-la
‼️ Após o pagamento, o acesso será liberado automaticamente!"""

                bot_temp.send_message(chat_id, legenda_pix, parse_mode="Markdown")
            else:
                bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX. Tente novamente ou contate o suporte.")

            bot_temp.answer_callback_query(update.callback_query.id)

        return {"status": "processed"}
        
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return {"status": "error"}

# =========================================================
# 👥 ROTAS DE CRM (BASE DE CONTATOS)
# =========================================================
@app.get("/api/admin/contacts")
def listar_contatos(status: str = "todos", db: Session = Depends(get_db)):
    """
    Lista usuários com base nos pedidos gerados.
    Filtros: 'todos', 'pagantes' (paid), 'pendentes' (pending)
    """
    query = db.query(Pedido)
    
    if status == "pagantes":
        query = query.filter(Pedido.status == "paid")
    elif status == "pendentes":
        query = query.filter(Pedido.status == "pending")
    
    # Ordena pelos mais recentes
    contatos = query.order_by(Pedido.created_at.desc()).all()
    return contatos

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
# 📢 LÓGICA DE REMARKETING (OFERTA + VALIDADE)
# =========================================================
CAMPAIGN_STATUS = {"running": False, "sent": 0, "total": 0, "blocked": 0}

def processar_envio_remarketing(bot_id: int, payload: RemarketingRequest, db: Session):
    global CAMPAIGN_STATUS
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
        # Busca plano
        plano_db = db.query(PlanoConfig).filter(
            (PlanoConfig.key_id == payload.plano_oferta_id) | 
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
                if payload.expiration_mode == "minutes":
                    data_expiracao = agora + timedelta(minutes=payload.expiration_value)
                elif payload.expiration_mode == "hours":
                    data_expiracao = agora + timedelta(hours=payload.expiration_value)
                elif payload.expiration_mode == "days":
                    data_expiracao = agora + timedelta(days=payload.expiration_value)

    # --- 2. SALVAR A CAMPANHA NO BANCO (ANTES DE ENVIAR) ---
    # Precisamos salvar antes para que o ID da campanha exista quando o usuário clicar
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
    if not payload.is_test:
        db.add(nova_campanha)
        db.commit()

    # --- 3. DEFINIR PÚBLICO ALVO ---
    bot_sender = telebot.TeleBot(bot_db.token)
    usuarios_para_envio = []

    if payload.is_test and payload.specific_user_id:
        class MockUser:
            def __init__(self, tid): self.telegram_id = tid
        usuarios_para_envio = [MockUser(payload.specific_user_id)]
    else:
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
        # O callback 'promo_' leva o ID da campanha. O Webhook vai checar se expirou.
        btn_text = f"🔥 {plano_db.nome_exibicao} - R$ {preco_final:.2f}"
        
        # Se for teste, usamos checkout direto pois não salvamos campanha no banco
        if payload.is_test:
             markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"checkout_{plano_db.id}"))
        else:
             # Callback aponta para a campanha para validar data
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
        
        time.sleep(0.05) 

    CAMPAIGN_STATUS["running"] = False
    
    # Atualiza status final no banco
    if not payload.is_test:
        nova_campanha.status = "concluido"
        nova_campanha.total_leads = len(usuarios_para_envio)
        nova_campanha.sent_success = sent_count
        nova_campanha.blocked_count = blocked_count
        db.commit()
    
    # --- D. SALVAR HISTÓRICO (Apenas se não for teste) ---
    if not payload.is_test:
        try:
            config_summary = json.dumps({
                "msg": payload.mensagem, 
                "offer": payload.incluir_oferta,
                "media": payload.media_url,
                "target": payload.target
            })
            
            db.add(RemarketingCampaign(
                bot_id=bot_id, 
                campaign_id=str(uuid.uuid4()), 
                config=config_summary, 
                target=payload.target,
                type="massivo",
                status="concluido", 
                total_leads=len(usuarios_para_envio), 
                sent_success=CAMPAIGN_STATUS["sent"], 
                blocked_count=CAMPAIGN_STATUS["blocked"]
            ))
            db.commit()
        except Exception as e:
            logger.error(f"Erro ao salvar historico: {e}")

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
# 📊 ROTA DE DASHBOARD (KPIs REAIS)
# =========================================================
@app.get("/api/admin/dashboard/stats")
def dashboard_stats(bot_id: Optional[int] = None, db: Session = Depends(get_db)): # <--- Adicione bot_id
    """Calcula métricas. Se bot_id for passado, filtra por ele."""
    
    # Base das queries
    q_revenue = db.query(func.sum(Pedido.valor)).filter(Pedido.status == "paid")
    q_users = db.query(Pedido.telegram_id).filter(Pedido.status == "paid")
    
    today = datetime.utcnow().date()
    start_of_day = datetime.combine(today, datetime.min.time())
    q_sales_today = db.query(func.sum(Pedido.valor)).filter(
        Pedido.status == "paid", 
        Pedido.created_at >= start_of_day
    )

    # APLICA FILTRO SE TIVER BOT_ID
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
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    try:
        # 1. Lê e Loga TUDO que chegou (Pra gente ver no Railway)
        raw_body = await request.body()
        data = await request.json()
        logger.info(f"💰 JSON RECEBIDO PUSHINPAY: {data}")

        # 2. Flexibilidade nos Campos
        # PushinPay pode mandar 'id', 'external_reference', 'uuid' dependendo da versão
        tx_id = data.get("external_reference") or data.get("id") or data.get("uuid")
        status_pix = str(data.get("status", "")).lower()

        # 3. Validação Flexível de Status
        status_validos = ["paid", "approved", "completed", "succeeded"]
        
        if not tx_id:
            logger.warning("⚠️ Webhook ignorado: Sem ID de transação.")
            return {"status": "ignored", "reason": "no_tx_id"}
            
        if status_pix not in status_validos:
            logger.warning(f"⚠️ Webhook ignorado: Status '{status_pix}' não é de pagamento aprovado.")
            return {"status": "ignored", "reason": f"status_{status_pix}"}

        # 4. Busca Pedido + Bot
        pedido = db.query(Pedido).join(Bot).filter(Pedido.transaction_id == tx_id).first()

        if not pedido:
            logger.error(f"❌ Pedido não encontrado no banco para TX: {tx_id}")
            # Retorna 200 para o PushinPay parar de mandar, mas loga erro
            return {"status": "ok", "msg": "Order not found internally"}

        if pedido.status == "paid":
            logger.info(f"ℹ️ Pedido {tx_id} já estava pago.")
            return {"status": "ok", "msg": "Already paid"}

        # 5. Atualiza Banco
        pedido.status = "paid"
        pedido.mensagem_enviada = True
        db.commit()
        logger.info(f"✅ Pedido {tx_id} atualizado para PAID no banco.")
        
        # 6. Liberação no Telegram
        bot_data = pedido.bot 
        try:
            tb = telebot.TeleBot(bot_data.token)
            chat_id_user = int(pedido.telegram_id)
            
            # Tenta converter ID do canal (remove espaços)
            try:
                canal_vip_id = int(str(bot_data.id_canal_vip).strip())
            except:
                canal_vip_id = bot_data.id_canal_vip # Tenta como string se for @canal

            logger.info(f"🚀 Tentando gerar link no canal {canal_vip_id} para user {chat_id_user}...")

            # GERA O LINK ÚNICO
            convite = tb.create_chat_invite_link(
                chat_id=canal_vip_id, 
                member_limit=1, 
                name=f"Venda {pedido.first_name}"
            )
            link_acesso = convite.invite_link

            msg_sucesso = f"""
✅ **Pagamento Confirmado!**

Seu acesso ao **{bot_data.nome}** foi liberado.
Toque no link abaixo para entrar no Canal VIP:

👉 {link_acesso}

⚠️ *Este link é único e válido apenas para você.*
"""
            tb.send_message(chat_id_user, msg_sucesso, parse_mode="Markdown")
            logger.info(f"🏆 SUCESSO! Link enviado para {pedido.first_name}")

        except Exception as e_telegram:
            logger.error(f"❌ ERRO TELEGRAM: {e_telegram}")
            # DICA: Se der erro aqui, verifique se o bot é ADMIN no canal
            tb.send_message(chat_id_user, "✅ Pagamento recebido! \n\n⚠️ Houve um erro ao gerar seu link automático. Um administrador entrará em contato em breve.")

        return {"status": "received"}

    except Exception as e:
        logger.error(f"❌ ERRO CRÍTICO NO WEBHOOK: {e}")
        return {"status": "error"}

@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online - Banco Atualizado"}
