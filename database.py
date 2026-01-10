import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

# Ajuste para compatibilidade com Railway (postgres -> postgresql)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800
    )
else:
    engine = create_engine("sqlite:///./sql_app.db")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(bind=engine)

# =========================================================
# ⚙️ TABELA DE CONFIGURAÇÕES GERAIS
# =========================================================
class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String, primary_key=True, index=True) 
    value = Column(String)                             
    updated_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🤖 TABELA DE BOTS (CENTRAL)
# =========================================================
class Bot(Base):
    __tablename__ = "bots"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    token = Column(String, unique=True, index=True)
    username = Column(String, nullable=True) # @username do bot
    id_canal_vip = Column(String)
    admin_principal_id = Column(String, nullable=True)
    status = Column(String, default="ativo") # ativo, pausado
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relacionamentos
    planos = relationship("PlanoConfig", back_populates="bot", cascade="all, delete-orphan")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False, cascade="all, delete-orphan")
    
    # [NOVO V2] Relacionamento com os passos dinâmicos
    steps = relationship("BotFlowStep", back_populates="bot", cascade="all, delete-orphan")
    
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")

# =========================================================
# 👥 ADMINS EXTRAS DO BOT
# =========================================================
class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    telegram_id = Column(String)
    nome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    bot = relationship("Bot", back_populates="admins")

# =========================================================
# 💎 TABELA DE PLANOS
# =========================================================
class PlanoConfig(Base):
    __tablename__ = "planos_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="planos")
    key_id = Column(String, index=True)
    nome_exibicao = Column(String)
    descricao = Column(String)
    preco_cheio = Column(Float)
    preco_atual = Column(Float)
    dias_duracao = Column(Integer)
    oculto = Column(Boolean, default=False)
    tag = Column(String, nullable=True) 

# =========================================================
# 📢 TABELA DE REMARKETING & CAMPANHAS
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    campaign_id = Column(String, unique=True) # UUID único para tracking
    
    # Configuração
    target = Column(String, default="todos") # todos, pendentes, pagantes, expirados
    type = Column(String, default="massivo") # massivo, individual
    config = Column(String) # JSON com msg, media, etc
    status = Column(String, default="agendado") # agendado, enviando, concluido, erro
    
    # Controle de execução (Recorrência)
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    
    # Oferta e Expiração
    plano_id = Column(Integer, nullable=True)       # Qual plano é a base
    promo_price = Column(Float, nullable=True)      # Valor com desconto
    expiration_at = Column(DateTime, nullable=True) # Data exata que expira
    
    # Métricas
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    data_envio = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 💬 TABELA DE FLUXO DE CHAT (MENSAGENS FIXAS)
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    # Passo 1 (Fixo): Boas Vindas
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo.")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="🔓 DESBLOQUEAR")
    autodestruir_1 = Column(Boolean, default=False)
    
    # Passo Final (Fixo): Oferta/Checkout
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=True)

# =========================================================
# 🧩 [NOVO] TABELA DE PASSOS DO FLUXO (DINÂMICO V2)
# =========================================================
class BotFlowStep(Base):
    __tablename__ = "bot_flow_steps"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    # Ordem de exibição (1, 2, 3...)
    step_order = Column(Integer, default=1)
    
    # Conteúdo
    msg_texto = Column(Text, nullable=True)
    msg_media = Column(String, nullable=True) # Foto ou Vídeo
    
    # Botão para ir ao próximo passo
    btn_texto = Column(String, default="Próximo ▶️")
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relacionamento
    bot = relationship("Bot", back_populates="steps")

# =========================================================
# 🛒 TABELA DE PEDIDOS / TRANSAÇÕES
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    # Dados do Cliente
    telegram_id = Column(String)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)

    # Expiração de Acesso
    custom_expiration = Column(DateTime, nullable=True) 
    
    # Dados da Compra
    # --- [CORREÇÃO CRÍTICA: Mantendo nomes antigos + novo] ---
    plano_nome = Column(String, nullable=True) # Mantido da V1
    plano_id = Column(Integer, nullable=True)  # Novo da V2 (Que estava faltando)
    
    valor = Column(Float)
    status = Column(String, default="pending") 
    
    # --- [ATENÇÃO AQUI: Mantivemos txid para compatibilidade] ---
    txid = Column(String, unique=True, index=True) 
    qr_code = Column(Text, nullable=True)
    transaction_id = Column(String, nullable=True) # Fallback antigo se necessário
    
    # Controle de Acesso
    data_aprovacao = Column(DateTime, nullable=True)
    data_expiracao = Column(DateTime, nullable=True)
    link_acesso = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
