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

def gerar_pix_pushinpay(valor_float: float, transaction_id: str):
    token = get_pushin_token()
    
    if not token:
        logger.error("❌ Token Pushin Pay não configurado!")
        return None
    
    # [cite_start]Endpoint correto conforme Documentação Oficial (Pág 1) [cite: 13]
    url = "https://api.pushinpay.com.br/api/pix/cashIn"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    # [cite_start]Valor em centavos conforme Documentação Oficial (Pág 1) [cite: 24]
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

# ✅ MODELO COMPLETO PARA O WIZARD DE REMARKETING
class RemarketingRequest(BaseModel):
    bot_id: int
    tipo_envio: str # 'todos', 'leads', 'ex_assinantes', 'individual'
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
# 🚀 WEBHOOK PRINCIPAL (HOT FLOW & CHECKOUT)
# =========================================================
@app.post("/webhook/{bot_token}")
async def receber_update_telegram(bot_token: str, request: Request, db: Session = Depends(get_db)):
    
    bot_db = db.query(Bot).filter(Bot.token == bot_token).first()
    if not bot_db: return {"status": "ignored"}

    try:
        json_str = await request.json()
        update = telebot.types.Update.de_json(json_str)
        bot_temp = telebot.TeleBot(bot_token)
        
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

# Variável Global para monitorar o envio em tempo real
CAMPAIGN_STATUS = {
    "running": False,
    "sent": 0,
    "total": 0,
    "blocked": 0
}

def processar_envio_remarketing(bot_id: int, payload: RemarketingRequest, db: Session):
    """Função executada em BackgroundTasks"""
    global CAMPAIGN_STATUS
    
    # Inicia/Reseta o status
    CAMPAIGN_STATUS = {"running": True, "sent": 0, "total": 0, "blocked": 0}
    
    bot_db = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot_db: 
        CAMPAIGN_STATUS["running"] = False
        return

    # 1. Filtra os usuários com base na escolha do Wizard
    usuarios_alvo = {} # Dict para evitar duplicatas: {telegram_id: dados}

    # Se for teste, pega apenas o ID específico (admin ou último usuário)
    if payload.is_test and payload.specific_user_id:
        usuarios_alvo[payload.specific_user_id] = {"first_name": "Admin Teste"}
        logger.info(f"🧪 Teste de Remarketing para: {payload.specific_user_id}")
    else:
        # Busca no banco de pedidos
        query = db.query(Pedido).filter(Pedido.bot_id == bot_id)
        todos_pedidos = query.all()
        
        for p in todos_pedidos:
            # Filtros Avançados
            if payload.tipo_envio == 'leads' and p.status != 'pending': continue
            if payload.tipo_envio == 'ex_assinantes' and p.status != 'expired': continue
            # 'todos' inclui todo mundo, então não precisa de if
            
            usuarios_alvo[p.telegram_id] = p

    total_users = len(usuarios_alvo)
    CAMPAIGN_STATUS["total"] = total_users
    logger.info(f"📢 Iniciando envio para {total_users} usuários...")

    # 2. Prepara o Bot
    bot_sender = telebot.TeleBot(bot_db.token)
    
    # 3. Monta o Botão de Oferta (Se houver)
    markup = None
    if payload.incluir_oferta and payload.plano_oferta_id:
        markup = types.InlineKeyboardMarkup()
        # Busca detalhes do plano para o botão
        plano = db.query(PlanoConfig).filter(
            (PlanoConfig.key_id == payload.plano_oferta_id) | 
            (PlanoConfig.id == int(payload.plano_oferta_id) if payload.plano_oferta_id.isdigit() else False)
        ).first()
        
        if plano:
            # Usa valor customizado ou o preço atual do plano
            valor_final = payload.valor_oferta if payload.valor_oferta > 0 else plano.preco_atual
            label_btn = f"🔥 {plano.nome_exibicao} - R$ {valor_final:.2f}"
            
            # Callback para o webhook gerar o pix
            btn = types.InlineKeyboardButton(label_btn, callback_data=f"checkout_{plano.id}")
            markup.add(btn)

    # 4. Loop de Envio
    for chat_id in usuarios_alvo.keys():
        try:
            # Verifica se tem mídia (Foto/Vídeo)
            if payload.media_url and len(payload.media_url) > 5:
                try:
                    if payload.media_url.lower().endswith(('.mp4', '.mov', '.avi')):
                        bot_sender.send_video(chat_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                    else:
                        bot_sender.send_photo(chat_id, payload.media_url, caption=payload.mensagem, reply_markup=markup)
                except Exception as e:
                    # Se falhar a mídia, manda só texto
                    logger.warning(f"Erro ao enviar mídia: {e}")
                    bot_sender.send_message(chat_id, payload.mensagem, reply_markup=markup)
            else:
                # Apenas texto
                bot_sender.send_message(chat_id, payload.mensagem, reply_markup=markup)
            
            CAMPAIGN_STATUS["sent"] += 1
            time.sleep(0.05) # Pequena pausa para respeitar limites do Telegram
            
        except Exception as e:
            if "blocked" in str(e) or "kicked" in str(e):
                CAMPAIGN_STATUS["blocked"] += 1
            logger.error(f"Falha no envio para {chat_id}: {e}")

    # 5. Finalização
    CAMPAIGN_STATUS["running"] = False
    
    # Salva no histórico apenas se NÃO for teste
    if not payload.is_test:
        campanha = RemarketingCampaign(
            bot_id=bot_id,
            campaign_id=str(uuid.uuid4()),
            config=payload.mensagem, # Salva o texto enviado
            status="concluido",
            total_leads=total_users,
            sent_success=CAMPAIGN_STATUS["sent"],
            blocked_count=CAMPAIGN_STATUS["blocked"]
        )
        db.add(campanha)
        db.commit()

@app.post("/api/admin/remarketing/send")
def enviar_remarketing(payload: RemarketingRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # Lógica para Teste: Se não tiver ID específico, tenta pegar o último pedido do banco
    if payload.is_test and not payload.specific_user_id:
        ultimo = db.query(Pedido).filter(Pedido.bot_id == payload.bot_id).order_by(Pedido.id.desc()).first()
        if ultimo:
            payload.specific_user_id = ultimo.telegram_id
        else:
            # Se não tiver ninguém, não dá pra testar
            raise HTTPException(400, "Nenhum usuário encontrado para teste. Interaja com o bot primeiro (/start).")

    # Inicia processo em segundo plano
    background_tasks.add_task(processar_envio_remarketing, payload.bot_id, payload, db)
    
    return {"status": "enviando", "msg": "Campanha iniciada com sucesso!"}

@app.get("/api/admin/remarketing/status")
def status_remarketing():
    """Retorna o progresso atual para a barra de carregamento do painel"""
    return CAMPAIGN_STATUS

@app.get("/api/admin/remarketing/history/{bot_id}")
def historico_remarketing(bot_id: int, db: Session = Depends(get_db)):
    """Retorna histórico de envios para aquele bot"""
    history = db.query(RemarketingCampaign).filter(RemarketingCampaign.bot_id == bot_id).order_by(RemarketingCampaign.data_envio.desc()).all()
    
    return [{
        "id": h.id,
        "data": h.data_envio.strftime("%d/%m/%Y %H:%M"),
        "total": h.total_leads,
        "sent": h.sent_success,
        "blocked": h.blocked_count,
        "config": { "content_data": h.config }
    } for h in history]

@app.get("/")
def home():
    return {"status": "Zenyx SaaS Online - Banco Atualizado"}
