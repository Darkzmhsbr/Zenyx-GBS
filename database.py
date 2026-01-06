import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")

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
    
    # NOVO RELACIONAMENTO
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)

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

class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="campanhas")
    campaign_id = Column(String, unique=True, index=True)
    config = Column(Text) 
    status = Column(String, default="ativo")
    type = Column(String, default="periodico") 
    total_leads = Column(Integer, default=0)
    sent_success = Column(Integer, default=0)
    blocked_count = Column(Integer, default=0)
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime)

# =========================================================
# 💬 TABELA NOVA: FLUXO DE CHAT (Fase #03)
# =========================================================
class BotFlow(Base):
    __tablename__ = "bot_flows"
    
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), unique=True)
    bot = relationship("Bot", back_populates="fluxo")
    
    # Configuração da Primeira Mensagem
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo ao VIP.")
    media_url = Column(String, nullable=True) # Foto ou Vídeo
    btn_text_1 = Column(String, default="🔥 Liberar Acesso")
    
    # Configuração da Segunda Mensagem (Oferta)
    msg_oferta = Column(Text, default="Escolha seu plano abaixo:")
