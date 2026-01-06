import os
from sqlalchemy import create_engine, text

# Cole sua URL do Railway aqui (Aquela que começa com postgresql://...)
DATABASE_URL = "https://zenyx-gbs-production.up.railway.app/" 

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def adicionar_colunas():
    with engine.connect() as conn:
        try:
            # Comandos SQL para adicionar as colunas que faltam
            conn.execute(text("ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;"))
            conn.execute(text("ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;"))
            conn.execute(text("ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;"))
            conn.commit()
            print("✅ Banco de dados atualizado com sucesso!")
        except Exception as e:
            print(f"❌ Erro: {e}")

if __name__ == "__main__":
    adicionar_colunas()