import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from datetime import datetime

# Pega a URL das variáveis do Railway
DATABASE_URL = os.getenv("DATABASE_URL")

# Ajuste para compatibilidade com Railway (postgres -> postgresql)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configuração da conexão (Engine)
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
    # Fallback para teste local se não tiver URL
    engine = create_engine("sqlite:///./sql_app.db")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def init_db():
    """Cria as tabelas no banco de dados"""
    Base.metadata.create_all(bind=engine)

# =========================================================
# 🤖 TABELA MESTRA: BOTS (NOVO)
# Baseado no PDF: Criar novo bot 
# =========================================================
class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)  # Ex: "Zenyx Bot Principal"
    token = Column(String, unique=True, index=True)  # O Token do Telegram
    id_canal_vip = Column(String)  # ID do Canal onde ele é Adm [cite: 23]
    
    # Status da conexão [cite: 25]
    status = Column(String, default="desconectado") # conectado / desconectado
    
    # Relacionamentos (Um bot tem muitos pedidos, planos, etc.)
    pedidos = relationship("Pedido", back_populates="bot")
    planos = relationship("PlanoConfig", back_populates="bot")
    campanhas = relationship("RemarketingCampaign", back_populates="bot")
    
    created_at = Column(DateTime, default=datetime.utcnow)

# =========================================================
# 🛒 TABELA DE PEDIDOS (Atualizada para Multi-Bot)
# =========================================================
class Pedido(Base):
    __tablename__ = "pedidos"

    id = Column(Integer, primary_key=True, index=True)
    
    # VÍNCULO: De qual bot é essa venda?
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
    
    status = Column(String, default="pending") # paid, expired, pending
    qr_code = Column(String, nullable=True)
    mensagem_enviada = Column(Boolean, default=False)

# =========================================================
# 💎 TABELA DE PLANOS (Atualizada para Multi-Bot)
# Baseado no PDF: Planos de pagamento [cite: 32]
# =========================================================
class PlanoConfig(Base):
    __tablename__ = "planos_config"

    id = Column(Integer, primary_key=True, index=True)
    
    # VÍNCULO: Esse plano pertence a qual bot?
    bot_id = Column(Integer, ForeignKey("bots.id"))
    bot = relationship("Bot", back_populates="planos")

    key_id = Column(String, index=True) # ex: 'semanal', 'mensal' [cite: 35, 36]
    nome_exibicao = Column(String)      # ex: 'ACESSO SEMANAL'
    descricao = Column(String)
    preco_cheio = Column(Float)
    preco_atual = Column(Float)         # [cite: 33]
    dias_duracao = Column(Integer)      # [cite: 35]
    
    oculto = Column(Boolean, default=False)
    tag = Column(String, nullable=True) 

# =========================================================
# 📢 TABELA DE REMARKETING (Atualizada para Multi-Bot)
# Baseado no PDF: Remarketing [cite: 75]
# =========================================================
class RemarketingCampaign(Base):
    __tablename__ = "remarketing_campaigns"

    id = Column(Integer, primary_key=True, index=True)
    
    # VÍNCULO: Campanha de qual bot?
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