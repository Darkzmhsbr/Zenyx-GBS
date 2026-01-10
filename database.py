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
    username = Column(String, nullable=True)
    id_canal_vip = Column(String)
    admin_principal_id = Column(String, nullable=True)
    status = Column(String, default="ativo")
    created_at = Column(DateTime, default=datetime.utcnow)
    
    planos = relationship("PlanoConfig", back_populates="bot", cascade="all, delete-orphan")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False, cascade="all, delete-orphan")
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
# 💲 TABELA DE PLANOS DE ACESSO
# =========================================================
class PlanoConfig(Base):
    __tablename__ = "planos_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    key_id = Column(String, nullable=True)
    nome_exibicao = Column(String)
    descricao = Column(String, nullable=True)
    preco_cheio = Column(Float, nullable=True)
    preco_atual = Column(Float)
    dias_duracao = Column(Integer)
    bot = relationship("Bot", back_populates="planos")

# =========================================================
# 📢 TABELA DE REMARKETING & CAMPANHAS
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    campaign_id = Column(String, unique=True)
    target = Column(String, default="todos")
    type = Column(String, default="massivo")
    config = Column(String)
    status = Column(String, default="agendado")
    
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    
    plano_id = Column(Integer, nullable=True)
    promo_price = Column(Float, nullable=True)
    expiration_at = Column(DateTime, nullable=True)
    
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    data_envio = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 💬 TABELA DE FLUXO DE CHAT (FIXO)
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo.")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="🔓 DESBLOQUEAR")
    autodestruir_1 = Column(Boolean, default=False)
    
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=True)

# =========================================================
# 🧩 TABELA DE PASSOS DO FLUXO (DINÂMICO V2)
# =========================================================
class BotFlowStep(Base):
    __tablename__ = "bot_flow_steps"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    step_order = Column(Integer, default=1)
    msg_texto = Column(Text, nullable=True)
    msg_media = Column(String, nullable=True)
    btn_texto = Column(String, default="Próximo ▶️")
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="steps")

# =========================================================
# 🛒 TABELA DE PEDIDOS (CORRIGIDA)
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    # Cliente
    telegram_id = Column(String)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    
    # Compra
    plano_nome = Column(String, nullable=True)
    plano_id = Column(Integer, nullable=True) # [CRÍTICO] Coluna que faltava
    valor = Column(Float)
    status = Column(String, default="pending")
    
    # [CRÍTICO] Mantendo 'txid' para compatibilidade com V1
    txid = Column(String, unique=True, index=True) 
    qr_code = Column(Text, nullable=True)
    
    # Acesso
    data_aprovacao = Column(DateTime, nullable=True)
    data_expiracao = Column(DateTime, nullable=True)
    link_acesso = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
