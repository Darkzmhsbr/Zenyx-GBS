import os
from sqlalchemy import create_engine, text

# Pega a URL do ambiente ou usa um fallback
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def adicionar_colunas():
    print("🔄 [RECOVERY] Verificando integridade do banco de dados...")
    with engine.connect() as conn:
        comandos = [
            # 1. CORREÇÃO DA DATA MANUAL E FRONTEND
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS custom_expiration TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_expiracao TIMESTAMP WITHOUT TIME ZONE;",
            
            # 2. CORREÇÃO DA CRIAÇÃO DE PLANOS
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS key_id VARCHAR;",
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS descricao TEXT;",
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS preco_cheio FLOAT;",
            
            # 3. CORREÇÃO DE CAMPOS FALTANTES EM PEDIDOS
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS txid VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS qr_code TEXT;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS transaction_id VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS link_acesso VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mensagem_enviada BOOLEAN DEFAULT FALSE;",
            
            # 4. PREPARAÇÃO PARA FLOW CHAT V2 (Sem quebrar o V1)
            """
            CREATE TABLE IF NOT EXISTS bot_flow_steps (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER REFERENCES bots(id),
                step_order INTEGER DEFAULT 1,
                msg_texto TEXT,
                msg_media VARCHAR,
                btn_texto VARCHAR DEFAULT 'Próximo ▶️',
                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
            );
            """
        ]

        for cmd in comandos:
            try:
                conn.execute(text(cmd))
                conn.commit()
            except Exception as e_cmd:
                # Apenas avisa se der erro (ex: coluna já existe), não trava o sistema
                print(f"⚠️ Aviso SQL: {e_cmd}")

        print("✅ Banco de dados REPARADO e pronto!")

if __name__ == "__main__":
    adicionar_colunas()
