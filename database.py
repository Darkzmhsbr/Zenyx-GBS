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
# 🤖 TABELA MESTRA: BOTS
# =========================================================
class Bot(Base):
    __tablename__ = "bots"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    token = Column(String, unique=True, index=True)
    id_canal_vip = Column(String)
    status = Column(String, default="desconectado")
    
    # NOVO CAMPO: Admin Principal para notificações
    admin_principal_id = Column(String, nullable=True) 
    
    # Relacionamentos
    pedidos = relationship("Pedido", back_populates="bot")
    planos = relationship("PlanoConfig", back_populates="bot")
    campanhas = relationship("RemarketingCampaign", back_populates="bot")
    fluxo = relationship("BotFlow", back_populates="bot", uselist=False)
    admins = relationship("BotAdmin", back_populates="bot", cascade="all, delete-orphan")
    
    created_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🛡️ TABELA DE ADMINISTRADORES
# =========================================================
class BotAdmin(Base):
    __tablename__ = "bot_admins"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="admins")
    telegram_id = Column(String, index=True)
    nome = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🛒 TABELA DE PEDIDOS
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
# 📢 TABELA DE REMARKETING (FUSÃO COMPLETA)
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="campanhas")

    campaign_id = Column(String, unique=True, index=True)
    admin_id = Column(String, nullable=True) 
    
    # Configurações
    type = Column(String, default="massivo") 
    target = Column(String, default="todos") 
    config = Column(Text) 
    status = Column(String, default="concluido")
    
    # Controle de execução (Recorrência)
    dia_atual = Column(Integer, default=0)
    data_inicio = Column(DateTime, default=datetime.utcnow)
    proxima_execucao = Column(DateTime, nullable=True)
    
    # Oferta e Expiração
    plano_id = Column(Integer, nullable=True)       
    promo_price = Column(Float, nullable=True)      
    expiration_at = Column(DateTime, nullable=True) 
    
    # Métricas
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
    msg_boas_vindas = Column(Text, default="Olá! Bem-vindo.")
    media_url = Column(String, nullable=True)
    btn_text_1 = Column(String, default="🔓 DESBLOQUEAR ACESSO")
    autodestruir_1 = Column(Boolean, default=False)
    msg_2_texto = Column(Text, nullable=True)
    msg_2_media = Column(String, nullable=True)
    mostrar_planos_2 = Column(Boolean, default=True)
    msg_oferta = Column(Text, nullable=True)
