import sqlite3

def init_db():
    conn = sqlite3.connect('ajagro.db')
    cursor = conn.cursor()
    
    # Criando a tabela de Fazendas (baseado no seu DER)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fazendas (
            id_fazenda INTEGER PRIMARY KEY AUTOINCREMENT,
            nome_fazenda TEXT NOT NULL,
            cnpj_cpf TEXT
        )
    ''')
    
    # Criando a tabela de Lotes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lotes (
            id_lote INTEGER PRIMARY KEY AUTOINCREMENT,
            id_fazenda INTEGER,
            descricao TEXT NOT NULL,
            FOREIGN KEY (id_fazenda) REFERENCES fazendas (id_fazenda)
        )
    ''')

    # Criando a tabela de Lançamentos de Estoque (MEG)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lanc_estoque (
            id_lancamento INTEGER PRIMARY KEY AUTOINCREMENT,
            data_movimento DATE NOT NULL,
            id_lote INTEGER,
            quantidade INTEGER,
            tipo_movimento TEXT,
            FOREIGN KEY (id_lote) REFERENCES lotes (id_lote)
        )
    ''')
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    