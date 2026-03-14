# 🐄 Residência Brisa - Módulo MEG (AJAGRO)

Sistema de Gestão de Pecuária focado em **Movimentação e Estoque de Gado (MEG)**. Este software foi desenvolvido para substituir planilhas manuais por uma interface dinâmica, permitindo o controle patrimonial em tempo real da propridade rural.

## 🚀 Funcionalidades

- **Cadastros Base:** Gestão de Fazendas e Lotes.
- **Movimentação de Estoque:** Lançamentos de entradas, saídas, nascimentos e mortes com categorização automática.
- **Dashboard de Valorização:** Painel de BI com gráficos interativos (Plotly) e métricas de patrimônio total.
- **Fechamento Mensal:** Filtros por período (competência) para análise de desempenho.
- **Ajuste Dinâmico de Preços:** Gestão de valores de mercado por categoria sem alteração de código.
- **Exportação:** Geração de relatórios em Excel (.xlsx).

## 🛠️ Tecnologias Utilizadas

- **Python 3.10+**
- **Streamlit:** Interface do usuário (Front-end).
- **SQLite:** Banco de dados relacional local.
- **Pandas:** Manipulação e análise de dados.
- **Plotly:** Gráficos interativos.

## 📦 Como Instalar e Rodar

1. **Clone o repositório:**
   git clone [https://github.com/SEU_USUARIO/ajagro-app.git](https://github.com/SEU_USUARIO/ajagro-app.git)

   Crie um ambiente virtual (opcional, mas recomendado):
   python -m venv venv
   source venv/bin/activate # Linux/Mac
   venv\Scripts\activate # Windows

Instale as dependências:
pip install -r requirements.txt

Prepare o Banco de Dados:
Execute o script de setup (ou os comandos de criação de tabela fornecidos).

Inicie a aplicação:
streamlit run app.py

Próximos Passos
[ ] Migração do banco para PostgreSQL (Supabase).

[ ] Sistema de Login e Permissões.

Desenvolvido por Caio Murilo como parte da jornada de aprendizado em Python e Ciência de Dados.
