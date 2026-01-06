import os
import logging
import telebot
import requests  # 👈 Necessário para chamar a Pushin Pay
import uuid      # Para gerar IDs únicos de transação
from telebot import types
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, Pedido

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

# --- INTEGRAÇÃO PUSHIN PAY ---
PUSHIN_PAY_TOKEN = os.getenv("PUSHIN_PAY_TOKEN")

def gerar_pix_pushinpay(valor_float: float, transaction_id: str):
    """Gera o PIX na API da Pushin Pay"""
    if not PUSHIN_PAY_TOKEN:
        logger.error("PUSHIN_PAY_TOKEN não configurado no Railway!")
        return None
    
    url = "https://api.pushinpay.com.br/api/pix/cashIn"
    headers = {
        "Authorization": f"Bearer {PUSHIN_PAY_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # Pushin Pay geralmente trabalha com centavos (int), verifique sua doc. 
    # Aqui vou mandar em Reais conforme padrão comum, mas se der erro convertemos * 100
    payload = {
        "value": int(valor_float * 100), # Convertendo para centavos
        "webhook_url": f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}/webhook/pix", # Webhook de retorno
        "external_reference": transaction_id
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            return response.json() # Retorna o objeto com qr_code e copia_cola
        else:
            logger.error(f"Erro PushinPay: {response.text}")
            return None
    except Exception as e:
        logger.error(f"Exceção PushinPay: {e}")
        return None

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
# 🚀 WEBHOOK INTELIGENTE (FOTO/VÍDEO + PIX)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- 1. COMANDO /START ---
        if update.message and update.message.text == "/start":
            chat_id = update.message.chat.id
            username = update.message.from_user.username
            first_name = update.message.from_user.first_name
            
            fluxo = bot_db.fluxo
            texto = fluxo.msg_boas_vindas if (fluxo and fluxo.msg_boas_vindas) else f"Olá! Eu sou o {bot_db.nome}."
            
            # Botão
            markup = None
            if fluxo and fluxo.btn_text_1:
                markup = types.InlineKeyboardMarkup()
                btn = types.InlineKeyboardButton(text=fluxo.btn_text_1, callback_data="ver_oferta")
                markup.add(btn)

            # --- CORREÇÃO DO PROBLEMA DE MÍDIA ---
            if fluxo and fluxo.media_url:
                media_link = fluxo.media_url.strip()
                try:
                    # Verifica extensão para decidir se é vídeo ou foto
                    if media_link.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
                        bot_temp.send_video(chat_id, media_link, caption=texto, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media_link, caption=texto, reply_markup=markup)
                except Exception as e:
                    logger.error(f"Erro mídia: {e}")
                    bot_temp.send_message(chat_id, texto, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto, reply_markup=markup)

        # --- 2. CLIQUE NO BOTÃO "VER OFERTA" ---
        elif update.callback_query and update.callback_query.data == "ver_oferta":
            chat_id = update.callback_query.message.chat.id
            fluxo = bot_db.fluxo
            
            texto_oferta = fluxo.msg_oferta if (fluxo and fluxo.msg_oferta) else "Escolha seu plano:"
            planos = bot_db.planos
            
            markup = types.InlineKeyboardMarkup()
            for p in planos:
                label = f"{p.nome_exibicao} - R$ {p.preco_atual:.2f}"
                # Callback carrega o ID do plano
                btn = types.InlineKeyboardButton(text=label, callback_data=f"checkout_{p.id}")
                markup.add(btn)
            
            bot_temp.send_message(chat_id, texto_oferta, reply_markup=markup)
            bot_temp.answer_callback_query(update.callback_query.id)

        # --- 3. CHECKOUT (GERAR PIX) ---
        elif update.callback_query and update.callback_query.data.startswith("checkout_"):
            chat_id = update.callback_query.message.chat.id
            plano_id = update.callback_query.data.split("_")[1]
            
            # Busca plano no DB
            plano = db.query(PlanoConfig).filter(PlanoConfig.id == plano_id).first()
            if not plano:
                bot_temp.send_message(chat_id, "Plano não encontrado.")
                return {"status": "error"}

            # Avisa que está gerando
            msg_aguarde = bot_temp.send_message(chat_id, "⏳ Gerando seu PIX, aguarde...")
            
            # Gera ID único para transação
            tx_id = str(uuid.uuid4())
            
            # --- CHAMA PUSHIN PAY ---
            pix_data = gerar_pix_pushinpay(plano.preco_atual, tx_id)
            
            if pix_data:
                qr_code_text = pix_data.get("qr_code_text") # Ajuste conforme retorno da API
                if not qr_code_text: qr_code_text = pix_data.get("qr_code") # Tentativa secundária
                
                # Salva pedido no Banco
                novo_pedido = Pedido(
                    bot_id=bot_db.id,
                    transaction_id=tx_id,
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

                # Apaga mensagem de "Aguarde"
                try: bot_temp.delete_message(chat_id, msg_aguarde.message_id)
                except: pass

                # --- MENSAGEM FINAL IGUAL DO SEU EXEMPLO ---
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

@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online"}
