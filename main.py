import os
import logging
import telebot
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig

# Configuração de Log (Essencial para debugar sem gastar RAM com print)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Zenyx Gbot SaaS")

# CORS (Para o React conversar com a API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inicializa DB
@app.on_event("startup")
def on_startup():
    init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- MODELOS Pydantic ---
class BotCreate(BaseModel):
    nome: str
    token: str
    id_canal_vip: str

class BotResponse(BotCreate):
    id: int
    status: str
    class Config:
        orm_mode = True

# =========================================================
# ⚙️ GESTÃO DE BOTS (CRUD)
# =========================================================

@app.post("/api/admin/bots", response_model=BotResponse)
def criar_bot(bot_data: BotCreate, db: Session = Depends(get_db)):
    # 1. Verifica duplicidade
    if db.query(Bot).filter(Bot.token == bot_data.token).first():
        raise HTTPException(status_code=400, detail="Token já cadastrado.")

    # 2. Verifica se o token é real no Telegram
    try:
        tb = telebot.TeleBot(bot_data.token)
        me = tb.get_me()
        status = "conectado"
        
        # 3. AUTOMAGIA: Define o Webhook automaticamente
        # Pega a URL pública do Railway (Você vai configurar isso nas variáveis depois)
        public_url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        if public_url:
            webhook_url = f"https://{public_url}/webhook/{bot_data.token}"
            tb.set_webhook(url=webhook_url)
            logger.info(f"Webhook definido para: {webhook_url}")
            
    except Exception as e:
        logger.error(f"Erro ao validar token: {e}")
        raise HTTPException(status_code=400, detail="Token inválido no Telegram.")

    # 4. Salva no Banco
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

# =========================================================
# 🚀 O SEGREDO DA ECONOMIA: ROTA WEBHOOK UNIVERSAL
# Esta única rota processa mensagens de 10, 100 ou 1000 bots
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    """
    Recebe updates do Telegram.
    Não carrega nada na memória se não tiver mensagem.
    """
    # 1. Valida se o bot existe no nosso banco (Segurança)
    # Usamos o DB para garantir que não estamos processando lixo
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    
    if not bot_db:
        # Se o bot não existe no nosso banco, ignoramos para economizar processamento
        return {"status": "ignored", "reason": "unknown_bot"}

    # 2. Processa a mensagem
    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        
        # Instancia o bot TEMPORARIAMENTE apenas para responder
        # Isso libera a memória assim que a função termina
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- AQUI ENTRA A LÓGICA DO SEU BOT ---
        # Por enquanto, vamos colocar apenas um "Eco" para testar
        # Futuramente, conectaremos com a lógica de Pagamento/Venda
        
        if update.message:
            chat_id = update.message.chat.id
            
            if update.message.text == "/start":
                # Lógica de Boas Vindas
                bot_temp.send_message(chat_id, f"Olá! Eu sou o {bot_db.nome}. Sistema SaaS Ativo 🚀")
            
            elif update.message.text == "/id":
                bot_temp.send_message(chat_id, f"Seu ID é: {chat_id}\nID do Canal VIP Configurado: {bot_db.id_canal_vip}")

        return {"status": "processed"}
        
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        # Retorna 200 para o Telegram parar de tentar reenviar a mensagem errada
        return {"status": "error"}

@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online", "mode": "Webhook Optimized"}

# =========================================================
# 💎 ROTAS DE PLANOS (Fase #02)
# Conforme PDF: Planos de pagamento [cite: 32, 33]
# =========================================================

class PlanoCreate(BaseModel):
    bot_id: int
    nome_exibicao: str  # Ex: "Acesso Semanal"
    preco: float        # Ex: 9.90
    dias_duracao: int   # Ex: 7

@app.post("/api/admin/plans")
def criar_plano(plano: PlanoCreate, db: Session = Depends(get_db)):
    # Verifica se o bot existe
    bot = db.query(Bot).filter(Bot.id == plano.bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")

    novo_plano = PlanoConfig(
        bot_id=plano.bot_id,
        key_id=f"plan_{plano.bot_id}_{plano.dias_duracao}d", # ID interno automático
        nome_exibicao=plano.nome_exibicao,
        descricao=f"Acesso de {plano.dias_duracao} dias",
        preco_cheio=plano.preco * 2, # Simulando um dobro para mostrar desconto
        preco_atual=plano.preco,
        dias_duracao=plano.dias_duracao
    )
    db.add(novo_plano)
    db.commit()
    return {"status": "ok", "msg": "Plano criado com sucesso"}

@app.get("/api/admin/plans/{bot_id}")
def listar_planos(bot_id: int, db: Session = Depends(get_db)):
    return db.query(PlanoConfig).filter(PlanoConfig.bot_id == bot_id).all()
