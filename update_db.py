import os
from sqlalchemy import create_engine, text

# Pega a URL do ambiente
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sql_app.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def adicionar_colunas():
    print("🔄 [RECOVERY] Iniciando reparo do banco de dados...")
    with engine.connect() as conn:
        try:
            comandos = [
                # --- CORREÇÃO CRÍTICA (ERRO 500 CONTATOS) ---
                # Adiciona a coluna que estava faltando na tabela de pedidos
                "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                
                # --- FLUXO DE MENSAGENS (V2) ---
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS autodestruir_1 BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_texto TEXT;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS msg_2_media VARCHAR;",
                "ALTER TABLE bot_flows ADD COLUMN IF NOT EXISTS mostrar_planos_2 BOOLEAN DEFAULT TRUE;",
                
                # --- REMARKETING (V2) ---
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS target VARCHAR DEFAULT 'todos';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS type VARCHAR DEFAULT 'massivo';",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS plano_id INTEGER;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS promo_price FLOAT;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS expiration_at TIMESTAMP WITHOUT TIME ZONE;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS dia_atual INTEGER DEFAULT 0;",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS data_inicio TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
                "ALTER TABLE remarketing_campaigns ADD COLUMN IF NOT EXISTS proxima_execucao TIMESTAMP WITHOUT TIME ZONE;",
                
                # --- TABELA NOVA (V2) ---
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
                except Exception as e_cmd:
                    print(f"⚠️ Aviso SQL (Provavelmente já existe): {e_cmd}")

            conn.commit()
            print("✅ BANCO DE DADOS REPARADO COM SUCESSO!")
        except Exception as e:
            print(f"❌ Erro no Reparo: {e}")

if __name__ == "__main__":
    adicionar_colunas()
