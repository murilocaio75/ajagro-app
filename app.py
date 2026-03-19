import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
import plotly.express as px
from datetime import date

# Configuração da Página
st.set_page_config(page_title="Sistema de Gestão de Pecuária - AJAGRO", layout="wide")

# --- CONEXÃO COM SUPABASE (POSTGRESQL) ---
def get_connection():
    db_url = st.secrets["DB_CONN_STRING"]
    engine = create_engine(db_url)
    return engine.connect()

# --- LÓGICA DE PREÇOS DINÂMICOS ---
def carregar_precos():
    conn = get_connection()
    try:
        query = text("SELECT * FROM precos_gestao")
        df = pd.read_sql(query, conn)
        if df.empty:
            raise Exception("Tabela vazia")
        precos = dict(zip(df['categoria'], df['valor']))
    except:
        precos = {
            "Vacas Lactantes": 5000.00, "Vacas Secas": 5000.00,
            "Vacas a refugar": 1000.00, "Vacas refugadas": 1500.00,
            "Mamando - Machos": 1000.00, "Mamando - Fêmeas": 1000.00,
            "Novilhas até 1 ano": 2000.00, "Novilhas de 1 a 2 anos": 3000.00,
            "Novilhas Prenhas": 5000.00, "Machos": 4000.00
        }
    finally:
        conn.close()
    return precos

# --- FUNÇÃO DE LOGIN ---
def login():
    if "autenticado" not in st.session_state:
        st.session_state["autenticado"] = False

    if not st.session_state["autenticado"]:
        st.markdown("<h1 style='text-align: center;'>🔐 Acesso Restrito - AJAGRO</h1>", unsafe_allow_html=True)
        
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            with st.form("login_form"):
                usuario = st.text_input("Usuário")
                senha = st.text_input("Senha", type="password")
                entrar = st.form_submit_button("Entrar no Sistema")

                if entrar:
                    # Busca credenciais nos Secrets
                    if usuario == st.secrets["USER_ADMIN"] and senha == st.secrets["PASS_ADMIN"]:
                        st.session_state["autenticado"] = True
                        st.success("Acesso autorizado!")
                        st.rerun()
                    else:
                        st.error("Usuário ou senha incorretos.")
        return False
    return True

# --- INÍCIO DA EXECUÇÃO PROTEGIDA ---
if login():
    # Inicializa os preços apenas após o login
    CATEGORIAS_PRECOS = carregar_precos()

    # --- NAVEGAÇÃO LATERAL ---
    st.sidebar.title("Módulo MEG - AJAGRO")
    menu = st.sidebar.selectbox("Ir para:", ["Dashboard & Valorização", "Cadastros Base", "Lançamento de Estoque", "⚙️ Ajuste de Preços"])
    
    # Botão de Logout
    st.sidebar.divider()
    if st.sidebar.button("🚪 Sair do Sistema"):
        st.session_state["autenticado"] = False
        st.rerun()

    # --- PÁGINA: CADASTROS BASE ---
    if menu == "Cadastros Base":
        st.header("🏢 Cadastros de Base")
        aba1, aba2 = st.tabs(["Fazendas", "Lotes"])
        
        with aba1:
            with st.form("form_fazenda"):
                nome = st.text_input("Nome da Fazenda")
                doc = st.text_input("CNPJ/CPF")
                if st.form_submit_button("Cadastrar Fazenda"):
                    conn = get_connection()
                    query = text("INSERT INTO fazendas (nome_fazenda, cnpj_cpf) VALUES (:nome, :doc)")
                    conn.execute(query, {"nome": nome, "doc": doc})
                    conn.commit()
                    conn.close()
                    st.success(f"Fazenda {nome} cadastrada!")

        with aba2:
            conn = get_connection()
            fazendas_df = pd.read_sql(text("SELECT * FROM fazendas"), conn)
            if not fazendas_df.empty:
                with st.form("form_lote"):
                    fazenda_sel = st.selectbox("Selecione a Fazenda", fazendas_df['nome_fazenda'])
                    id_fazenda = int(fazendas_df[fazendas_df['nome_fazenda'] == fazenda_sel]['id_fazenda'].values[0])
                    desc_lote = st.text_input("Descrição do Lote (Ex: Piquete 01)")
                    if st.form_submit_button("Cadastrar Lote"):
                        query = text("INSERT INTO lotes (id_fazenda, descricao) VALUES (:id_faz, :desc)")
                        conn.execute(query, {"id_faz": id_fazenda, "desc": desc_lote})
                        conn.commit()
                        st.success("Lote cadastrado!")
            else:
                st.warning("Cadastre uma fazenda primeiro.")
            conn.close()

    # --- PÁGINA: LANÇAMENTO ---
    elif menu == "Lançamento de Estoque":
        st.header("📝 Movimentação de Gado")
        conn = get_connection()
        query_lotes = text("SELECT l.id_lote, l.descricao, f.nome_fazenda FROM lotes l JOIN fazendas f ON l.id_fazenda = f.id_fazenda")
        lotes_df = pd.read_sql(query_lotes, conn)

        if not lotes_df.empty:
            with st.form("form_movimentacao"):
                col1, col2 = st.columns(2)
                with col1:
                    dt = st.date_input("Data do Movimento", date.today())
                    lote_sel = st.selectbox("Lote/Fazenda", lotes_df['descricao'] + " - " + lotes_df['nome_fazenda'])
                    cat = st.selectbox("Classe/Categoria (Valorização)", list(CATEGORIAS_PRECOS.keys()))
                with col2:
                    tipo = st.selectbox("Operação", ["Entrada (Compra)", "Nascimento", "Morte", "Venda", "Transferência"])
                    qtd = st.number_input("Quantidade de Cabeças", min_value=1)
                
                obs = st.text_area("Observações")
                if st.form_submit_button("Confirmar Lançamento"):
                    id_lote = int(lotes_df[lotes_df['descricao'] + " - " + lotes_df['nome_fazenda'] == lote_sel]['id_lote'].values[0])
                    query_ins = text("""
                        INSERT INTO lanc_estoque (data_movimento, id_lote, quantidade, tipo_movimento, observacao, categoria) 
                        VALUES (:dt, :id_lote, :qtd, :tipo, :obs, :cat)
                    """)
                    conn.execute(query_ins, {"dt": dt, "id_lote": id_lote, "qtd": qtd, "tipo": tipo, "obs": obs, "cat": cat})
                    conn.commit()
                    st.success("Movimentação registrada!")
        else:
            st.error("Nenhum Lote ou Fazenda encontrado.")
        conn.close()

# --- PÁGINA: DASHBOARD --- 
    elif menu == "Dashboard & Valorização":
        st.header("📊 Painel Resumo & Valorização Patrimonial")
        conn = get_connection()

        # Configuração da Barra Lateral para o Dashboard
        st.sidebar.divider()
        st.sidebar.subheader("Período de Análise")
        hoje = date.today()
        primeiro_dia = date(hoje.year, hoje.month, 1)
        data_inicio = st.sidebar.date_input("Data Inicial", primeiro_dia)
        data_fim = st.sidebar.date_input("Data Final", hoje)

        # 1. Query do Estado Atual (Filtrada pelo período lateral)
        query_filtrada = text("""
            SELECT 
                categoria as "categoria",
                SUM(CASE WHEN tipo_movimento IN ('Entrada (Compra)', 'Nascimento', 'Transferência') THEN quantidade ELSE 0 END) -
                SUM(CASE WHEN tipo_movimento IN ('Morte', 'Venda') THEN quantidade ELSE 0 END) as "saldo_qtd"
            FROM lanc_estoque
            WHERE data_movimento BETWEEN :inicio AND :fim
            GROUP BY categoria
        """)
        
        df_estoque = pd.read_sql(query_filtrada, conn, params={"inicio": data_inicio, "fim": data_fim})
        
        # Bloco Principal: Se houver dados no período selecionado
        if not df_estoque.empty:
            df_estoque['saldo_qtd'] = pd.to_numeric(df_estoque['saldo_qtd'], errors='coerce').fillna(0)
            df_estoque['Preço Unit.'] = df_estoque['categoria'].map(CATEGORIAS_PRECOS).fillna(0)
            df_estoque['Total R$'] = df_estoque['saldo_qtd'] * df_estoque['Preço Unit.']
            
            total_patrimonial = df_estoque['Total R$'].sum()
            df_estoque['Part. %'] = (df_estoque['Total R$'] / total_patrimonial * 100) if total_patrimonial > 0 else 0
            
            df_grafico = df_estoque[df_estoque['Total R$'] > 0]
            total_cabecas = df_estoque['saldo_qtd'].sum()
        
            # Métricas em destaque
            c1, c2, c3 = st.columns(3)
            c1.metric("Estoque Total", f"{total_cabecas} cab.")
            c2.metric("Valorização Total", f"R$ {total_patrimonial:,.2f}")
            c3.metric("Média por Animal", f"R$ {(total_patrimonial/total_cabecas if total_cabecas > 0 else 0):,.2f}")

            st.divider()
            col_graf, col_tab = st.columns([1, 1.2])

            with col_graf:
                st.subheader("Distribuição do Patrimônio (R$)")
                fig = px.pie(df_grafico, values='Total R$', names='categoria', hole=0.4, color_discrete_sequence=px.colors.qualitative.Prism)
                fig.update_layout(legend=dict(orientation="h", yanchor="bottom", y=-0.2))
                st.plotly_chart(fig, use_container_width=True)

            with col_tab:
                st.subheader("Balanço e Participação")
                st.dataframe(df_estoque.style.format({'Preço Unit.': 'R$ {:.2f}', 'Total R$': 'R$ {:.2f}', 'Part. %': '{:.1f}%'}), use_container_width=True, hide_index=True)
                
                import io
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df_estoque.to_excel(writer, index=False, sheet_name='Balanco_AJAGRO')
                st.download_button(label="📥 Baixar Balanço (Excel)", data=buffer.getvalue(), file_name=f"balanco_ajagro_{data_inicio}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            
            # --- SEÇÃO: GRÁFICO DE HISTÓRICO (Ignora o filtro lateral para ver 2023) ---
            st.divider()
            st.subheader("📈 Evolução Patrimonial Mensal (Histórico Total)")

            query_historico = text("""
                SELECT 
                    DATE_TRUNC('month', data_movimento) as mes,
                    SUM(CASE WHEN tipo_movimento IN ('Entrada (Compra)', 'Nascimento', 'Transferência') THEN quantidade ELSE 0 END) -
                    SUM(CASE WHEN tipo_movimento IN ('Morte', 'Venda') THEN quantidade ELSE 0 END) as saldo_mensal
                FROM lanc_estoque
                GROUP BY mes
                ORDER BY mes
            """)
            
            df_hist_bruto = pd.read_sql(query_historico, conn)
            
            if not df_hist_bruto.empty:
                df_hist_bruto['saldo_acumulado'] = df_hist_bruto['saldo_mensal'].cumsum()
                # Usamos a média de preços atual para valorizar o passado
                preco_medio_geral = df_estoque['Preço Unit.'].mean() if not df_estoque.empty else 0
                df_hist_bruto['Valor Estimado (R$)'] = df_hist_bruto['saldo_acumulado'] * preco_medio_geral

                fig_linha = px.line(
                    df_hist_bruto, 
                    x='mes', 
                    y='Valor Estimado (R$)',
                    markers=True,
                    labels={'mes': 'Mês de Referência', 'Valor Estimado (R$)': 'Patrimônio Total'},
                    color_discrete_sequence=['#2E7D32']
                )
                fig_linha.update_layout(hovermode="x unified")
                st.plotly_chart(fig_linha, use_container_width=True)
            else:
                st.info("Aguardando mais dados mensais para gerar o gráfico de linha.")

        else:
            st.info("Nenhum movimento encontrado no período selecionado na barra lateral.")

        # --- SEÇÃO: LISTAGEM DE LANÇAMENTOS (Final da página) ---
        st.divider()
        st.subheader("📑 Últimos 20 Lançamentos")
        query_recentes = text("SELECT TO_CHAR(data_movimento, 'DD/MM/YYYY') as Data, tipo_movimento as Operação, categoria as Classe, quantidade as Qtd FROM lanc_estoque ORDER BY id_lancamento DESC LIMIT 20")
        df_hist_lista = pd.read_sql(query_recentes, conn)
        st.dataframe(df_hist_lista, use_container_width=True, hide_index=True)
        
        # Fecha a conexão após realizar todas as consultas da página
        conn.close()

    # --- PÁGINA: AJUSTE DE PREÇOS ---
    elif menu == "⚙️ Ajuste de Preços":
        st.header("⚙️ Ajuste de Preços de Mercado")
        PRECOS_ATUAIS = carregar_precos()
        
        with st.form("form_precos"):
            st.subheader("Tabela de Valores Unitários")
            novos_precos = {}
            for cat, valor_atual in PRECOS_ATUAIS.items():
                novos_precos[cat] = st.number_input(f"{cat} (R$)", value=float(valor_atual), step=50.0)
            
            if st.form_submit_button("💾 Salvar Novos Preços"):
                conn = get_connection()
                for cat, valor in novos_precos.items():
                    query_upd = text("""
                        INSERT INTO precos_gestao (categoria, valor) VALUES (:cat, :val)
                        ON CONFLICT (categoria) DO UPDATE SET valor = EXCLUDED.valor
                    """)
                    conn.execute(query_upd, {"cat": cat, "val": valor})
                conn.commit()
                conn.close()
                st.success("Preços atualizados com sucesso!")
                st.rerun()
                



