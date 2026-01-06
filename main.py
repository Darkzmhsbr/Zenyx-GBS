import os
import logging
import telebot
from telebot import types # Importante para os botões
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow

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

@app.on_event("startup")
def on_startup():
    init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- MODELOS ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str

class BotResponse(BotCreate):
    id: int
    status: str
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
    msg_oferta: str

# ===========================
# ⚙️ GESTÃO DE BOTS
# ===========================

@app.post("/api/admin/bots", response_model=BotResponse)
def criar_bot(bot_data: BotCreate, db: Session = Depends(get_db)):
    if db.query(Bot).filter(Bot.token == bot_data.token).first():
        raise HTTPException(status_code=400, detail="Token já cadastrado.")

    try:
        tb = telebot.TeleBot(bot_data.token)
        # Define Webhook
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

@app.get("/api/admin/bots", response_model=List[BotResponse])
def listar_bots(db: Session = Depends(get_db)):
    return db.query(Bot).all()

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
            "msg_boas_vindas": "Olá! Seja bem-vindo(a). Clique abaixo para entrar.",
            "media_url": "",
            "btn_text_1": "🔥 Liberar Acesso",
            "msg_oferta": "Escolha um dos planos abaixo:"
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
    fluxo_db.msg_oferta = flow.msg_oferta
    
    db.commit()
    return {"status": "saved"}

# =========================================================
# 🚀 WEBHOOK INTELIGENTE (AGORA COM FOTO E BOTÕES)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- 1. COMANDO /START (BOAS VINDAS) ---
        if update.message and update.message.text == "/start":
            chat_id = update.message.chat.id
            fluxo = bot_db.fluxo
            
            # Texto
            texto = fluxo.msg_boas_vindas if (fluxo and fluxo.msg_boas_vindas) else f"Olá! Eu sou o {bot_db.nome}."
            
            # Botão (Se configurado)
            markup = None
            if fluxo and fluxo.btn_text_1:
                markup = types.InlineKeyboardMarkup()
                # O callback_data="oferta" vai acionar o passo 2
                btn = types.InlineKeyboardButton(text=fluxo.btn_text_1, callback_data="ver_oferta")
                markup.add(btn)

            # Envia Foto ou Texto
            if fluxo and fluxo.media_url:
                try:
                    bot_temp.send_photo(chat_id, fluxo.media_url, caption=texto, reply_markup=markup)
                except Exception as e:
                    # Fallback se a foto falhar (link quebrado)
                    logger.error(f"Erro ao enviar foto: {e}")
                    bot_temp.send_message(chat_id, texto, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto, reply_markup=markup)

        # --- 2. CLIQUE NO BOTÃO (OFERTA + PLANOS) ---
        elif update.callback_query and update.callback_query.data == "ver_oferta":
            chat_id = update.callback_query.message.chat.id
            fluxo = bot_db.fluxo
            
            # Texto da Oferta
            texto_oferta = fluxo.msg_oferta if (fluxo and fluxo.msg_oferta) else "Escolha seu plano:"
            
            # Busca os Planos do Bot
            planos = bot_db.planos
            markup = types.InlineKeyboardMarkup()
            
            for p in planos:
                # Botão do Plano (Ex: "Semanal - R$ 9.90")
                label = f"{p.nome_exibicao} - R$ {p.preco_atual:.2f}"
                # Callback ex: "checkout_12" (onde 12 é o ID do plano)
                btn = types.InlineKeyboardButton(text=label, callback_data=f"checkout_{p.id}")
                markup.add(btn)
            
            bot_temp.send_message(chat_id, texto_oferta, reply_markup=markup)
            
            # Confirma o clique para parar o "reloginho" do botão
            bot_temp.answer_callback_query(update.callback_query.id)

        # --- 3. CLIQUE NO PLANO (CHECKOUT) ---
        elif update.callback_query and update.callback_query.data.startswith("checkout_"):
            # AQUI VAI ENTRAR A FASE #04 (Integração Pushin Pay)
            bot_temp.send_message(update.callback_query.message.chat.id, "🚧 Gerando Pix... (Em breve na Fase #04)")
            bot_temp.answer_callback_query(update.callback_query.id)

        return {"status": "processed"}
        
    except Exception as e:
        logger.error(f"Erro webhook: {e}")
        return {"status": "error"}

@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online"}
