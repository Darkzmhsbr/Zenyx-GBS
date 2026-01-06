import os
import logging
import telebot
import requests
import uuid
from fastapi import BackgroundTasks # <--- IMPORTANTE
import time
from sqlalchemy import text  # Importante para o SQL
from telebot import types
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta

# Importa banco de dados
from database import SessionLocal, init_db, Bot, PlanoConfig, BotFlow, Pedido, engine

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
# 🛠️ AUTO-REPARO DO BANCO DE DADOS (Executa ao ligar)
# =========================================================
@app.on_event("startup")
def on_startup():
    # 1. Cria tabelas que não existem
    init_db()
    
    # 2. FORÇA A CRIAÇÃO DAS COLUNAS NOVAS (Correção do Erro)
    try:
        with engine.connect() as conn:
            logger.info("🔧 Verificando integridade do banco de dados...")
            
            # Lista de comandos para garantir que as colunas existam
            comandos = [
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;"
            ]
            
            for cmd in comandos:
                try:
                    conn.execute(text(cmd))
                except Exception as e:
                    # Ignora erros se a coluna já existir, mas loga o aviso
                    logger.warning(f"Aviso SQL: {e}")
            
            conn.commit()
            logger.info("✅ BANCO DE DADOS ATUALIZADO E PRONTO!")
            
    except Exception as e:
        logger.error(f"❌ Erro crítico ao atualizar banco: {e}")

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
    
    payload = {
        "value": int(valor_float * 100),
        "webhook_url": f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN')}/webhook/pix",
        "external_reference": transaction_id
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            return response.json()
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
    autodestruir_1: bool
    msg_2_texto: Optional[str] = None
    msg_2_media: Optional[str] = None
    mostrar_planos_2: bool

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
# 🚀 WEBHOOK INTELIGENTE (FLUXO AVANÇADO)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
        # --- 1. COMANDO /START (MENSAGEM 1) ---
        if update.message and update.message.text == "/start":
            chat_id = update.message.chat.id
            fluxo = bot_db.fluxo
            
            # Dados da Msg 1
            texto = fluxo.msg_boas_vindas if fluxo else f"Olá! Eu sou o {bot_db.nome}."
            btn_txt = fluxo.btn_text_1 if (fluxo and fluxo.btn_text_1) else "🔓 DESBLOQUEAR ACESSO"
            
            # Botão que leva para o Passo 2
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text=btn_txt, callback_data="passo_2"))

            # Envio (Foto/Vídeo/Texto)
            media = fluxo.media_url if (fluxo and fluxo.media_url) else None
            if media:
                try:
                    if media.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_temp.send_video(chat_id, media, caption=texto, reply_markup=markup)
                    else:
                        bot_temp.send_photo(chat_id, media, caption=texto, reply_markup=markup)
                except Exception as e:
                    logger.error(f"Erro mídia 1: {e}")
                    bot_temp.send_message(chat_id, texto, reply_markup=markup)
            else:
                bot_temp.send_message(chat_id, texto, reply_markup=markup)

        # --- 2. CLIQUE NO BOTÃO DA MSG 1 (IR PARA MSG 2) ---
        elif update.callback_query and update.callback_query.data == "passo_2":
            chat_id = update.callback_query.message.chat.id
            msg_id = update.callback_query.message.message_id
            fluxo = bot_db.fluxo
            
            # A) Autodestruição (Se ativado)
            if fluxo and fluxo.autodestruir_1:
                try:
                    bot_temp.delete_message(chat_id, msg_id)
                except Exception as e:
                    logger.warning(f"Falha na autodestruição: {e}")

            # B) Prepara a Mensagem 2
            texto_2 = fluxo.msg_2_texto if (fluxo and fluxo.msg_2_texto) else "Escolha seu plano:"
            media_2 = fluxo.msg_2_media if (fluxo and fluxo.msg_2_media) else None
            
            # C) Botões de Planos (Se ativado)
            markup = types.InlineKeyboardMarkup()
            if fluxo and fluxo.mostrar_planos_2:
                planos = bot_db.planos
                for p in planos:
                    label = f"{p.nome_exibicao} - R$ {p.preco_atual:.2f}"
                    # Usa "checkout_" para manter padrão com sua lógica de pagamento
                    markup.add(types.InlineKeyboardButton(text=label, callback_data=f"checkout_{p.id}"))
            
            # D) Envio da Mensagem 2
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

# Memória Global para Status de Envio (Para a barra de progresso)
# Formato: { "running": bool, "sent": int, "total": int, "blocked": int }
CAMPAIGN_STATUS = {
    "running": False,
    "sent": 0,
    "total": 0,
    "blocked": 0
}

class RemarketingRequest(BaseModel):
    bot_id: int
    tipo_envio: str # 'todos', 'leads' (pendentes), 'ex_assinantes' (expirados), 'individual'
    mensagem: str
    media_url: Optional[str] = None
    incluir_oferta: bool = False
    
    # Campos Extras do Wizard
    plano_oferta_id: Optional[str] = None
    valor_oferta: Optional[float] = 0.0
    expire_timestamp: Optional[int] = 0
    is_periodic: bool = False
    
    # Controle de Teste
    is_test: bool = False
    specific_user_id: Optional[str] = None # Telegram ID para teste

def processar_envio_remarketing(bot_id: int, payload: RemarketingRequest, db: Session):
    """Processa o envio e atualiza o status global"""
    global CAMPAIGN_STATUS
    
    # Reseta status
    CAMPAIGN_STATUS = {"running": True, "sent": 0, "total": 0, "blocked": 0}
    
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: 
        CAMPAIGN_STATUS["running"] = False
        return

    # 1. Seleciona o Público
    usuarios_alvo = {} # {telegram_id: dados}

    if payload.is_test and payload.specific_user_id:
        # Se for teste, pega só o ID específico (ou o admin)
        usuarios_alvo[payload.specific_user_id] = {"first_name": "Admin Teste"}
        logger.info(f"🧪 Teste de Remarketing para: {payload.specific_user_id}")
    else:
        # Busca usuários reais
        query = db.query(Pedido).filter(Pedido.bot_id == bot_id)
        todos_pedidos = query.all()
        
        for p in todos_pedidos:
            # Lógica de Filtros Avançados
            if payload.tipo_envio == 'leads' and p.status != 'pending': continue
            if payload.tipo_envio == 'ex_assinantes' and p.status != 'expired': continue
            # 'todos' pega tudo
            
            usuarios_alvo[p.telegram_id] = p

    total_users = len(usuarios_alvo)
    CAMPAIGN_STATUS["total"] = total_users
    logger.info(f"📢 Iniciando Remarketing para {total_users} usuários (Filtro: {payload.tipo_envio})")

    # 2. Configura Bot e Teclado
    bot_sender = telebot.TeleBot(bot_db.token)
    
    markup = None
    if payload.incluir_oferta and payload.plano_oferta_id:
        markup = types.InlineKeyboardMarkup()
        # Busca infos do plano para o botão
        # Obs: Estamos usando o ID do plano padrão para garantir que o checkout funcione
        # Se quiser preço customizado, precisaria de uma lógica de checkout dinâmica.
        # Por enquanto, usaremos o ID do plano selecionado.
        
        # Tenta achar o plano pelo Key ID (string) ou ID numérico
        plano = db.query(PlanoConfig).filter(
            (PlanoConfig.key_id == payload.plano_oferta_id) | 
            (PlanoConfig.id == int(payload.plano_oferta_id) if payload.plano_oferta_id.isdigit() else False)
        ).first()
        
        if plano:
            # Texto do botão
            label_botao = f"🔥 {plano.nome_exibicao} - R$ {payload.valor_oferta if payload.valor_oferta > 0 else plano.preco_atual:.2f}"
            btn = types.InlineKeyboardButton(label_botao, callback_data=f"checkout_{plano.id}")
            markup.add(btn)

    # 3. Loop de Envio
    for chat_id in usuarios_alvo.keys():
        try:
            # Envia Mídia ou Texto
            if payload.media_url and len(payload.media_url) > 5:
                try:
                    if payload.media_url.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_sender.send_video(chat_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                    else:
                        bot_sender.send_photo(chat_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                except Exception as e:
                    logger.warning(f"Erro mídia, enviando texto: {e}")
                    bot_sender.send_message(chat_id, payload.mensagem, reply_markup=markup)
            else:
                bot_sender.send_message(chat_id, payload.mensagem, reply_markup=markup)
            
            CAMPAIGN_STATUS["sent"] += 1
            time.sleep(0.05) # Delay anti-flood
            
        except Exception as e:
            if "blocked" in str(e) or "kicked" in str(e):
                CAMPAIGN_STATUS["blocked"] += 1
            logger.error(f"Falha envio {chat_id}: {e}")

    # 4. Finaliza e Salva Histórico (Só salva se não for teste)
    CAMPAIGN_STATUS["running"] = False
    
    if not payload.is_test:
        campanha = RemarketingCampaign(
            bot_id=bot_id,
            campaign_id=str(uuid.uuid4()),
            config=payload.mensagem, # Salva a msg como config simples por enquanto
            status="concluido",
            total_leads=total_users,
            sent_success=CAMPAIGN_STATUS["sent"],
            blocked_count=CAMPAIGN_STATUS["blocked"]
        )
        db.add(campanha)
        db.commit()

@app.post("/api/admin/remarketing/send")
def enviar_remarketing(payload: RemarketingRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # Se for teste, precisamos de um ID alvo. Vamos pegar o último pedido como cobaia se não vier específico
    if payload.is_test and not payload.specific_user_id:
        ultimo = db.query(Pedido).filter(Pedido.bot_id == payload.bot_id).order_by(Pedido.id.desc()).first()
        if ultimo:
            payload.specific_user_id = ultimo.telegram_id
        else:
            raise HTTPException(400, "Nenhum usuário encontrado para teste. Interaja com o bot primeiro.")

    background_tasks.add_task(processar_envio_remarketing, payload.bot_id, payload, db)
    return {"status": "enviando", "msg": "Campanha iniciada."}

@app.get("/api/admin/remarketing/status")
def status_remarketing():
    """Rota para o Frontend atualizar a barra de progresso"""
    return CAMPAIGN_STATUS

@app.get("/api/admin/remarketing/history/{bot_id}")
def historico_remarketing(bot_id: int, db: Session = Depends(get_db)):
    history = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id).order_by(RemarketingCampaign.data_envio.desc()).all()
    
    # Formata para o frontend
    return [{
        "id": h.id,
        "data": h.data_envio.strftime("%d/%m/%Y %H:%M"),
        "total": h.total_leads,
        "sent": h.sent_success,
        "blocked": h.blocked_count,
        "config": { "content_data": h.config } # Adaptação simples para reuso
    } for h in history]
    
@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online - Banco Atualizado"}
