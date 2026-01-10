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
# ⚙️ CONFIGURAÇÕES GERAIS
# =========================================================
class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String, primary_key=True, index=True) 
    value = Column(String)                             
    updated_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🤖 BOTS
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
    # [NOVO] Relacionamento para o Flow V2 (Fica inativo até configurarmos)
    steps = relationship("BotFlowStep", back_populates="bot", cascade="all, delete-orphan")
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")

class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    telegram_id = Column(String)
    nome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    bot = relationship("Bot", back_populates="admins")

# =========================================================
# 💲 PLANOS (CORREÇÃO DE CAMPOS FALTANTES)
# =========================================================
class PlanoConfig(Base):
    __tablename__ = "planos_config"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    # Estes campos são essenciais para evitar erro na criação
    key_id = Column(String, nullable=True) 
    nome_exibicao = Column(String)
    descricao = Column(String, nullable=True)
    preco_cheio = Column(Float, nullable=True)
    preco_atual = Column(Float)
    dias_duracao = Column(Integer)
    
    bot = relationship("Bot", back_populates="planos")

# =========================================================
# 📢 REMARKETING
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
# 💬 FLUXO (ESTRUTURA HÍBRIDA V1 + V2)
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    # Passo 1 (Fixo)
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo.")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="🔓 DESBLOQUEAR")
    autodestruir_1 = Column(Boolean, default=False)
    
    # Passo Final (Fixo)
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=True)

# [NOVO] Tabela para os passos intermediários (V2)
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
# 🛒 PEDIDOS (CORREÇÃO DA DATA MANUAL)
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    
    telegram_id = Column(String)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    
    plano_nome = Column(String, nullable=True)
    plano_id = Column(Integer, nullable=True)
    valor = Column(Float)
    status = Column(String, default="pending") 
    
    txid = Column(String, unique=True, index=True) 
    qr_code = Column(Text, nullable=True)
    transaction_id = Column(String, nullable=True) # Legado
    
    # --- DATAS (AQUI ESTAVA O PROBLEMA DA DATA MANUAL) ---
    data_aprovacao = Column(DateTime, nullable=True)
    data_expiracao = Column(DateTime, nullable=True)   # Backend V2 lê aqui
    custom_expiration = Column(DateTime, nullable=True) # Frontend V1 lê aqui (ESSENCIAL)
    
    link_acesso = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
