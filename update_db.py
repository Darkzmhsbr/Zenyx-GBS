import os
from sqlalchemy import create_engine, text

# Pega a URL do ambiente ou usa um fallback
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def adicionar_colunas():
    print("🔄 [UPDATE] Verificando colunas do banco de dados...")
    with engine.connect() as conn:
        comandos = [
            # --- 1. CORREÇÃO CRÍTICA PARA O FRONTEND (DATA DE EXPIRAÇÃO) ---
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS custom_expiration TIMESTAMP WITHOUT TIME ZONE;",
            
            # --- 2. CORREÇÃO CRÍTICA PARA O BACKEND (V2) ---
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_expiracao TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS data_aprovacao TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_nome VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS txid VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS qr_code TEXT;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS link_acesso VARCHAR;",
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mensagem_enviada BOOLEAN DEFAULT FALSE;",
            
            # --- 3. CORREÇÕES DE PLANOS E FLOW ---
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS key_id VARCHAR;",
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS descricao TEXT;",
            "ALTER TABLE planos_config ADD COLUMN IF NOT EXISTS preco_cheio FLOAT;",
            "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
            "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
            "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;",
            
            # --- 4. REMARKETING ---
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS target VARCHAR DEFAULT 'todos';",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'massivo';",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS promo_price FLOAT;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS expiration_at TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS dia_atual INTEGER DEFAULT 0;",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS data_inicio TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
            "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS proxima_execucao TIMESTAMP WITHOUT TIME ZONE;",
            
            # --- 5. TABELA DE PASSOS (V2) ---
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
                # Ignora avisos se a coluna já existir
                print(f"⚠️ Aviso SQL: {e_cmd}")

        print("✅ Banco de dados ATUALIZADO e pronto para V2!")

if __name__ == "__main__":
    adicionar_colunas()
