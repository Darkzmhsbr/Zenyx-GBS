import os
import logging
import telebot
import requests
import uuid
import json  # <--- AGORA O JSON ESTÁ AQUI
import time
import urllib.parse # <--- IMPORTANTE PARA LER O PUSHINPAY
from fastapi import BackgroundTasks, FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from typing import List, Optional
from datetime import datetime, timedelta

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, Pedido, SystemConfig, RemarketingCampaign, engine

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
# 🛠️ AUTO-REPARO DO BANCO DE DADOS
# =========================================================
@app.on_event("startup")
def on_startup():
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
                except: pass
            conn.commit()
            logger.info("✅ BANCO DE DADOS PRONTO!")
    except Exception as e:
        logger.error(f"❌ Erro banco: {e}")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# =========================================================
# 🔌 INTEGRAÇÃO PUSHIN PAY (FIXA E SEGURA)
# =========================================================
def get_pushin_token():
    db = SessionLocal()
    try:
        config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
        if config and config.value: return config.value
        return os.getenv("PUSHIN_PAY_TOKEN")
    finally: db.close()

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
    
    # URL FIXA DO RAILWAY (PELO SEU LOG ANTERIOR)
    seus_dominio = "zenyx-gbs-production.up.railway.app" 
    
    payload = {
        "value": int(valor_float * 100), 
        "webhook_url": f"https://{seus_dominio}/webhook/pix",
        "external_reference": transaction_id
    }

    try:
        logger.info(f"📤 Gerando PIX. Webhook: https://{seus_dominio}/webhook/pix")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]: return response.json()
        else:
            logger.error(f"Erro PushinPay: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exceção PushinPay: {e}")
        return None

# --- MODELOS ---
class IntegrationUpdate(BaseModel): token: str
class BotCreate(BaseModel): nome: str; token: str; id_canal_vip: str
class BotResponse(BotCreate): id: int; status: str; class Config: from_attributes = True
class PlanoCreate(BaseModel): bot_id: int; nome_exibicao: str; preco: float; dias_duracao: int
class FlowUpdate(BaseModel): msg_boas_vindas: str; media_url: Optional[str] = None; btn_text_1: str; autodestruir_1: bool; msg_2_texto: Optional[str] = None; msg_2_media: Optional[str] = None; mostrar_planos_2: bool
class RemarketingRequest(BaseModel): bot_id: int; tipo_envio: str; mensagem: str; media_url: Optional[str] = None; incluir_oferta: bool = False; plano_oferta_id: Optional[str] = None; valor_oferta: Optional[float] = 0.0; expire_timestamp: Optional[int] = 0; is_periodic: bool = False; is_test: bool = False; specific_user_id: Optional[str] = None

# =========================================================
# 💰 ROTA WEBHOOK PIX (A CORREÇÃO DEFINITIVA)
# =========================================================
@app.post("/webhook/pix")
async def webhook_pix(request: Request, db: Session = Depends(get_db)):
    print("🔔 WEBHOOK PIX CHEGOU!") 
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        
        print(f"📦 BODY RAW: {body_str}") 

        if not body_str:
            return {"status": "ignored", "reason": "empty_body"}

        # --- LÓGICA HÍBRIDA (JSON ou FORM DATA) ---
        data = {}
        try:
            # Tenta JSON primeiro
            data = json.loads(body_str)
        except:
            # Se falhar, tenta Form Data (igual seu arquivo de referência)
            try:
                parsed = urllib.parse.parse_qs(body_str)
                data = {k: v[0] for k, v in parsed.items()}
                print("✅ Convertido de Form-Data para Dict")
            except Exception as e_parse:
                print(f"❌ Falha ao ler dados: {e_parse}")
                return {"status": "error", "reason": "invalid_format"}

        # Extrai dados com flexibilidade
        tx_id = data.get("external_reference") or data.get("id") or data.get("uuid")
        status_pix = str(data.get("status", "")).lower()
        
        print(f"🔎 Processando: ID={tx_id} | Status={status_pix}")

        if status_pix not in ["paid", "approved", "completed", "succeeded"]:
            print(f"⚠️ Status ignorado: {status_pix}")
            return {"status": "ignored"}

        # Busca Pedido com JOIN para garantir conexão
        pedido = db.query(Pedido).join(Bot).filter(Pedido.transaction_id == tx_id).first()

        if not pedido:
            print(f"❌ Pedido {tx_id} não encontrado no banco.")
            return {"status": "ok", "msg": "Order not found"}

        if pedido.status == "paid":
            print("ℹ️ Pedido já estava pago.")
            return {"status": "ok", "msg": "Already paid"}

        # ATUALIZA BANCO
        pedido.status = "paid"
        pedido.mensagem_enviada = True
        db.commit()
        print(f"✅ Pedido {tx_id} PAGO!")
        
        # ENVIA TELEGRAM
        bot_data = pedido.bot 
        try:
            tb = telebot.TeleBot(bot_data.token)
            
            try: canal_id = int(str(bot_data.id_canal_vip).strip())
            except: canal_id = bot_data.id_canal_vip

            convite = tb.create_chat_invite_link(
                chat_id=canal_id, 
                member_limit=1, 
                name=f"Venda {pedido.first_name}"
            )
            
            msg = f"✅ **Pagamento Confirmado!**\n\nSeu acesso exclusivo:\n👉 {convite.invite_link}"
            tb.send_message(int(pedido.telegram_id), msg, parse_mode="Markdown")
            print("🏆 LINK ENVIADO!")

        except Exception as e_tg:
            print(f"❌ Erro Telegram: {e_tg}")
            try: tb.send_message(int(pedido.telegram_id), "✅ Pagamento recebido! Erro ao gerar link. Contate o suporte.")
            except: pass

        return {"status": "received"}

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO CODE: {e}") # Agora vai imprimir o erro real se houver
        return {"status": "error"}

# ===========================
# 🚀 WEBHOOK GERAL DO BOT
# ===========================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    if bot_token == "pix": return {"status": "ignored_loop"} # Proteção extra

    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
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

        elif update.callback_query and update.callback_query.data == "passo_2":
            chat_id = update.callback_query.message.chat.id
            fluxo = bot_db.fluxo
            texto_2 = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Escolha seu plano:"
            markup = types.InlineKeyboardMarkup()
            
            if fluxo and fluxo.mostrar_planos_2:
                for p in bot_db.planos:
                    markup.add(types.InlineKeyboardButton(text=f"{p.nome_exibicao} - R$ {p.preco_atual:.2f}", callback_data=f"checkout_{p.id}"))
            
            # Input de mídia secundária (vídeo de oferta)
            media_2 = fluxo.msg_2_media if (fluxo and fluxo.msg_2_media) else None
            
            if media_2:
                try:
                    if media_2.lower().endswith(('.mp4', '.mov')):
                        bot_temp.send_video(chat_id, media_2, caption=texto_2, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media_2, caption=texto_2, reply_markup=markup)
                except:
                    bot_temp.send_message(chat_id, texto_2, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto_2, reply_markup=markup)
            
            bot_temp.answer_callback_query(update.callback_query.id)

        elif update.callback_query and update.callback_query.data.startswith("checkout_"):
            chat_id = update.callback_query.message.chat.id
            plano_id = update.callback_query.data.split("_")[1]
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
            
            if plano:
                msg_wait = bot_temp.send_message(chat_id, "⏳ Gerando PIX...")
                tx_id = str(uuid.uuid4())
                pix_data = gerar_pix_pushinpay(plano.preco_atual, tx_id)
                
                if pix_data:
                    qr_code = pix_data.get("qr_code_text") or pix_data.get("qr_code")
                    novo_pedido = Pedido(
                        bot_id=bot_db.id, transaction_id=tx_id, telegram_id=str(chat_id),
                        first_name=update.callback_query.from_user.first_name,
                        username=update.callback_query.from_user.username,
                        plano_nome=plano.nome_exibicao, valor=plano.preco_atual,
                        status="pending", qr_code=qr_code
                    )
                    db.add(novo_pedido)
                    db.commit()
                    
                    try: bot_temp.delete_message(chat_id, msg_wait.message_id)
                    except: pass
                    
                    msg_pix = f"💰 **Pagamento Gerado**\nValor: R$ {plano.preco_atual:.2f}\n\nCopia e Cola:\n`{qr_code}`"
                    bot_temp.send_message(chat_id, msg_pix, parse_mode="Markdown")
                else:
                    bot_temp.send_message(chat_id, "❌ Erro ao gerar PIX.")
            
            bot_temp.answer_callback_query(update.callback_query.id)

        return {"status": "processed"}
    except Exception as e:
        logger.error(f"Erro webhook bot: {e}")
        return {"status": "error"}

# ROTAS API ADMIN
@app.get("/api/admin/bots", response_model=List[BotResponse])
def listar_bots(db: Session = Depends(get_db)): return db.query(Bot).all()

@app.post("/api/admin/bots", response_model=BotResponse)
def criar_bot(bot_data: BotCreate, db: Session = Depends(get_db)):
    if db.query(Bot).filter(Bot.token == bot_data.token).first(): raise HTTPException(400, "Token já cadastrado.")
    try:
        tb = telebot.TeleBot(bot_data.token)
        webhook_url = f"https://zenyx-gbs-production.up.railway.app/webhook/{bot_data.token}"
        tb.set_webhook(url=webhook_url)
        status = "conectado"
    except: status = "erro"
    novo_bot = Bot(nome=bot_data.nome, token=bot_data.token, id_canal_vip=bot_data.id_canal_vip, status=status)
    db.add(novo_bot); db.commit(); db.refresh(novo_bot)
    return novo_bot

@app.get("/api/admin/integrations/pushinpay")
def get_pushin_status(db: Session = Depends(get_db)):
    config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
    token = config.value if config else os.getenv("PUSHIN_PAY_TOKEN")
    return {"status": "conectado" if token else "desconectado", "token_mask": f"{token[:4]}...{token[-4:]}" if token else ""}

@app.post("/api/admin/integrations/pushinpay")
def save_pushin_token(data: IntegrationUpdate, db: Session = Depends(get_db)):
    config = db.query(SystemConfig).filter(SystemConfig.key == "pushin_pay_token").first()
    if not config: config = SystemConfig(key="pushin_pay_token"); db.add(config)
    config.value = data.token; db.commit()
    return {"status": "conectado"}

@app.post("/api/admin/plans")
def criar_plano(plano: PlanoCreate, db: Session = Depends(get_db)):
    novo_plano = PlanoConfig(bot_id=plano.bot_id, key_id=f"p_{plano.bot_id}_{int(time.time())}", nome_exibicao=plano.nome_exibicao, preco_cheio=plano.preco*2, preco_atual=plano.preco, dias_duracao=plano.dias_duracao)
    db.add(novo_plano); db.commit()
    return {"status": "ok"}

@app.get("/api/admin/plans/{bot_id}")
def listar_planos(bot_id: int, db: Session = Depends(get_db)): return db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()

@app.delete("/api/admin/plans/{plan_id}")
def deletar_plano(plan_id: int, db: Session = Depends(get_db)): db.query(PlanoConfig).filter(PlanoConfig.id == plan_id).delete(); db.commit(); return {"status": "deleted"}

@app.get("/api/admin/bots/{bot_id}/flow")
def obter_fluxo(bot_id: int, db: Session = Depends(get_db)):
    fluxo = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not fluxo: return {"msg_boas_vindas": "Olá!", "media_url": "", "btn_text_1": "Começar", "autodestruir_1": False, "msg_2_texto": "", "msg_2_media": "", "mostrar_planos_2": True}
    return fluxo

@app.post("/api/admin/bots/{bot_id}/flow")
def salvar_fluxo(bot_id: int, flow: FlowUpdate, db: Session = Depends(get_db)):
    fluxo_db = db.query(BotFlow).filter(BotFlow.bot_id == bot_id).first()
    if not fluxo_db: fluxo_db = BotFlow(bot_id=bot_id); db.add(fluxo_db)
    fluxo_db.msg_boas_vindas = flow.msg_boas_vindas; fluxo_db.media_url = flow.media_url; fluxo_db.btn_text_1 = flow.btn_text_1
    fluxo_db.autodestruir_1 = flow.autodestruir_1; fluxo_db.msg_2_texto = flow.msg_2_texto; fluxo_db.msg_2_media = flow.msg_2_media
    fluxo_db.mostrar_planos_2 = flow.mostrar_planos_2; db.commit()
    return {"status": "saved"}

# REMARKETING
CAMPAIGN_STATUS = {"running": False, "sent": 0, "total": 0, "blocked": 0}
def processar_envio_remarketing(bot_id: int, payload: RemarketingRequest, db: Session):
    global CAMPAIGN_STATUS
    CAMPAIGN_STATUS = {"running": True, "sent": 0, "total": 0, "blocked": 0}
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: CAMPAIGN_STATUS["running"] = False; return
    
    query = db.query(Pedido).filter(Pedido.bot_id == bot_id)
    usuarios = query.all()
    bot_sender = telebot.TeleBot(bot_db.token)
    CAMPAIGN_STATUS["total"] = len(usuarios)
    
    for u in usuarios:
        try: bot_sender.send_message(u.telegram_id, payload.mensagem); CAMPAIGN_STATUS["sent"] += 1
        except: CAMPAIGN_STATUS["blocked"] += 1
        time.sleep(0.05)
    
    CAMPAIGN_STATUS["running"] = False
    db.add(RemarketingCampaign(bot_id=bot_id, campaign_id=str(uuid.uuid4()), config=payload.mensagem, status="concluido", total_leads=len(usuarios), sent_success=CAMPAIGN_STATUS["sent"], blocked_count=CAMPAIGN_STATUS["blocked"]))
    db.commit()

@app.post("/api/admin/remarketing/send")
def enviar_remarketing(payload: RemarketingRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    background_tasks.add_task(processar_envio_remarketing, payload.bot_id, payload, db)
    return {"status": "enviando"}

@app.get("/api/admin/remarketing/status")
def status_remarketing(): return CAMPAIGN_STATUS

@app.get("/api/admin/remarketing/history/{bot_id}")
def historico_remarketing(bot_id: int, db: Session = Depends(get_db)):
    history = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id).order_by(RemarketingCampaign.data_envio.desc()).all()
    return [{"id": h.id, "data": h.data_envio.strftime("%d/%m/%Y %H:%M"), "total": h.total_leads, "sent": h.sent_success, "blocked": h.blocked_count, "config": {"content_data": h.config}} for h in history]

@app.get("/api/admin/dashboard/stats")
def dashboard_stats(bot_id: Optional[int] = None, db: Session = Depends(get_db)):
    q_revenue = db.query(func.sum(Pedido.valor)).filter(Pedido.status == "paid")
    q_users = db.query(Pedido.telegram_id).filter(Pedido.status == "paid")
    today = datetime.utcnow().date()
    q_sales_today = db.query(func.sum(Pedido.valor)).filter(Pedido.status == "paid", Pedido.created_at >= datetime.combine(today, datetime.min.time()))
    if bot_id: q_revenue = q_revenue.filter(Pedido.bot_id == bot_id); q_users = q_users.filter(Pedido.bot_id == bot_id); q_sales_today = q_sales_today.filter(Pedido.bot_id == bot_id)
    return {"total_revenue": q_revenue.scalar() or 0.0, "active_users": q_users.distinct().count(), "sales_today": q_sales_today.scalar() or 0.0}

@app.get("/api/admin/contacts")
def listar_contatos(status: str = "todos", db: Session = Depends(get_db)):
    query = db.query(Pedido)
    if status == "pagantes": query = query.filter(Pedido.status == "paid")
    elif status == "pendentes": query = query.filter(Pedido.status == "pending")
    return query.order_by(Pedido.created_at.desc()).all()

@app.get("/")
def home(): return {"status": "Zenyx SaaS Online"}
