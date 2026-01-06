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
# 🤖 TABELA MESTRA: BOTS
# =========================================================
class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    token = Column(String, unique=True, index=True)
    id_canal_vip = Column(String)
    status = Column(String, default="desconectado")
    
    pedidos = relationship("Pedido", back_populates="bot")
    planos = relationship("PlanoConfig", back_populates="bot")
    campanhas = relationship("RemarketingCampaign", back_populates="bot")
    
    # Relacionamento One-to-One com o Fluxo
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🛒 TABELA DE PEDIDOS (CRM)
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"

    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="pedidos")

    transaction_id = Column(String, unique=True, index=True)
    telegram_id = Column(String, index=True)
    
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    
    role = Column(String, default="user") 
    custom_expiration = Column(DateTime, nullable=True) 
    
    plano_nome = Column(String)
    valor = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    status = Column(String, default="pending") 
    qr_code = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)

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
# 📢 TABELA DE REMARKETING
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    
    # Vinculo com o Bot (Importante para saber quem dispara)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="campanhas")

    campaign_id = Column(String, unique=True, index=True)
    config = Column(Text) # JSON com a mensagem
    status = Column(String, default="enviado") # enviado, agendado
    
    type = Column(String, default="imediato") 
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    
    data_envio = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 💬 TABELA DE FLUXO DE CHAT
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    # --- PASSO 1: BOAS VINDAS ---
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo.")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="🔓 DESBLOQUEAR ACESSO")
    
    # Configuração Avançada 1
    autodestruir_1 = Column(Boolean, default=False)
    
    # --- PASSO 2: OFERTA ---
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    
    # Configuração Avançada 2
    mostrar_planos_2 = Column(Boolean, default=True)
    
    # Legado
    msg_oferta = Column(Text, nullable=True)
